"""OpenSSH client failure policy: sanitized classification and retry rules.

The probe never shows an operator or an agent what the SSH client printed —
stderr can name users, addresses, and key paths — so failures are reduced to
the stable vocabulary in ``models.SERVER_MESSAGES``. The same stderr decides
whether a failed attempt was a stale multiplexed transport worth one retry
over a fresh connection.
"""

from __future__ import annotations


def classify_ssh_failure(stderr: str) -> str:
    """Classify SSH failures without exposing remote addresses, users or paths."""
    normalized = stderr.lower()
    # Ordered by root cause: a jump host that cannot open the forward also
    # makes the target's key exchange fail, so the forwarding text wins.
    categories = (
        (("remote host identification has changed",), "SSH host key changed"),
        (("host key verification failed",), "SSH host key is not trusted"),
        (("permission denied", "authentication failed"), "SSH authentication failed"),
        (
            ("could not resolve hostname", "name or service not known"),
            "SSH name resolution failed",
        ),
        (
            (
                "stdio forwarding request failed",
                "channel 0: open failed",
                "open failed: administratively prohibited",
                "open failed: connect failed",
            ),
            "SSH jump host could not reach the target",
        ),
        (("connection refused",), "SSH connection was refused"),
        (("connection timed out", "operation timed out"), "SSH connection timed out"),
        (("no route to host", "network is unreachable"), "SSH network is unreachable"),
        (
            (
                "kex_exchange_identification",
                "banner exchange",
                "connection reset by peer",
                "connection closed by remote host",
            ),
            "SSH connection closed during key exchange",
        ),
        (
            ("timeout, server", "server not responding"),
            "SSH transport stopped responding",
        ),
    )
    for needles, message in categories:
        if any(needle in normalized for needle in needles):
            return message
    return "SSH connection failed"


def is_retryable_ssh_transport_failure(stderr: str) -> bool:
    """Recognize stale multiplexed sessions without retrying hard failures.

    A healthy master can still emit ``mux_client_request_session`` or
    ``control socket connect`` while refusing a session or denying access to
    its socket; those are not dead transports, so an authentication, host-key
    or refusal signal vetoes the retry even when a mux marker is present.
    """
    normalized = stderr.lower()
    hard_failures = (
        "permission denied",
        "authentication failed",
        "session open refused",
        "administratively prohibited",
        "host key verification failed",
        "remote host identification has changed",
        "open failed",
    )
    if any(marker in normalized for marker in hard_failures):
        return False
    stale_markers = (
        "mux_client_request_session",
        "control socket connect",
        "master is dead",
        "broken pipe",
        "read from master failed",
    )
    return any(marker in normalized for marker in stale_markers)


def force_fresh_transport(command: list[str]) -> list[str]:
    """Return the command with any shared ControlMaster bypassed.

    The reused mux socket may point at a dead master whose keepalive this
    invocation cannot influence, so the recovery attempt opens its own
    connection instead of re-binding to the same stale control path.
    """
    if "--" not in command:
        return list(command)
    separator = command.index("--")
    return [
        *command[:separator],
        "-o",
        "ControlMaster=no",
        "-o",
        "ControlPath=none",
        *command[separator:],
    ]
