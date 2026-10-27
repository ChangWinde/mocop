"""Deadline-bounded collector process I/O and cancellation ownership."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import threading
import time
import weakref
from contextlib import suppress
from dataclasses import dataclass

_PROCESS_IO_CHUNK_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class _BoundedProcessResult:
    returncode: int
    stdout: str
    stderr: str


class _ProcessOutputLimitExceeded(RuntimeError):
    pass


class _ProcessCancelled(RuntimeError):
    pass


class _ActiveProcessRegistry:
    """Own active collectors so shutdown can interrupt them without polling."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[bytes]] = set()
        # Raw descriptors: the registry lives for the whole probe lifetime and
        # selectors accept integer descriptors directly. A finalizer reclaims
        # both ends so recreating probes never leaks the wake-up pipe.
        self.cancel_wakeup, self._cancel_write_fd = os.pipe()
        os.set_blocking(self._cancel_write_fd, False)
        self._finalizer = weakref.finalize(
            self,
            _ActiveProcessRegistry._close_fds,
            self.cancel_wakeup,
            self._cancel_write_fd,
        )

    @staticmethod
    def _close_fds(read_fd: int, write_fd: int) -> None:
        for fd in (read_fd, write_fd):
            with suppress(OSError):
                os.close(fd)

    def close(self) -> None:
        """Idempotently reclaim the wake-up pipe once the probe is done."""
        self._finalizer()

    def register(self, process: subprocess.Popen[bytes]) -> bool:
        with self._lock:
            if self.cancelled.is_set():
                return False
            self._processes.add(process)
            return True

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.discard(process)

    def cancel(self) -> None:
        self.cancelled.set()
        # Level-triggered wake-up: the byte is intentionally never consumed,
        # so every selector that registered the read end stays readable.
        with suppress(OSError):
            os.write(self._cancel_write_fd, b"x")
        with self._lock:
            processes = tuple(self._processes)
        for process in processes:
            _kill_process_group(process)


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        with suppress(OSError):
            process.kill()


def _run_bounded_process(
    command: list[str],
    *,
    input_text: str,
    timeout_seconds: float,
    max_output_bytes: int,
    environment: dict[str, str],
    cancel_event: threading.Event | None = None,
    process_registry: _ActiveProcessRegistry | None = None,
) -> _BoundedProcessResult:
    """Bound script delivery, output collection and exit by one deadline."""
    if cancel_event is not None and cancel_event.is_set():
        raise _ProcessCancelled
    deadline = time.monotonic() + timeout_seconds
    pending_input = memoryview(input_text.encode("utf-8"))
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
        bufsize=0,
    )
    stdin = process.stdin
    stdout = process.stdout
    stderr = process.stderr
    registered = process_registry.register(process) if process_registry else False

    try:
        if process_registry is not None and not registered:
            raise _ProcessCancelled
        if stdin is None or stdout is None or stderr is None:
            raise RuntimeError("SSH process pipes were not created")
        output = {"stdout": bytearray(), "stderr": bytearray()}
        total_bytes = 0
        selector = selectors.DefaultSelector()
        try:
            selector.register(stdout, selectors.EVENT_READ, "stdout")
            selector.register(stderr, selectors.EVENT_READ, "stderr")
            stream_keys = 2
            if pending_input:
                os.set_blocking(stdin.fileno(), False)
                selector.register(stdin, selectors.EVENT_WRITE, "stdin")
                stream_keys += 1
            else:
                stdin.close()
            if process_registry is not None:
                # Leave the wake-up byte unread for concurrent probes.
                selector.register(
                    process_registry.cancel_wakeup, selectors.EVENT_READ, "cancel"
                )
            while stream_keys:
                if cancel_event is not None and cancel_event.is_set():
                    raise _ProcessCancelled
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        command,
                        timeout_seconds,
                        output=bytes(output["stdout"]),
                        stderr=bytes(output["stderr"]),
                    )
                wait_seconds = (
                    remaining if process_registry is not None else min(remaining, 0.25)
                )
                events = selector.select(wait_seconds)
                for key, _mask in events:
                    if key.data == "cancel":
                        # The wake-up byte stays unread so concurrent probes
                        # sharing the registry observe the same level trigger.
                        raise _ProcessCancelled
                    if key.data == "stdin":
                        try:
                            written = os.write(
                                stdin.fileno(), pending_input[:_PROCESS_IO_CHUNK_BYTES]
                            )
                            pending_input = pending_input[written:]
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            # Preserve the child's exit status and stderr.
                            pending_input = pending_input[:0]
                        if not pending_input:
                            selector.unregister(stdin)
                            stdin.close()
                            stream_keys -= 1
                        continue
                    chunk = os.read(key.fileobj.fileno(), _PROCESS_IO_CHUNK_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        stream_keys -= 1
                        continue
                    total_bytes += len(chunk)
                    if total_bytes > max_output_bytes:
                        raise _ProcessOutputLimitExceeded
                    output[key.data].extend(chunk)
        except (
            subprocess.TimeoutExpired,
            _ProcessOutputLimitExceeded,
            _ProcessCancelled,
        ):
            _kill_process_group(process)
            raise
        finally:
            selector.close()

        if cancel_event is not None and cancel_event.is_set():
            raise _ProcessCancelled
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            returncode = process.poll()
            if returncode is None:
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    output=bytes(output["stdout"]),
                    stderr=bytes(output["stderr"]),
                )
        else:
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(
                    command,
                    timeout_seconds,
                    output=bytes(output["stdout"]),
                    stderr=bytes(output["stderr"]),
                ) from None
        return _BoundedProcessResult(
            returncode=returncode,
            stdout=output["stdout"].decode("utf-8", errors="replace"),
            stderr=output["stderr"].decode("utf-8", errors="replace"),
        )
    finally:
        if process_registry is not None and registered:
            process_registry.unregister(process)
        if process.poll() is None:
            _kill_process_group(process)
        with suppress(OSError, subprocess.TimeoutExpired):
            process.wait(timeout=1)
        for stream in (stdin, stdout, stderr):
            if stream is not None:
                with suppress(OSError):
                    stream.close()
