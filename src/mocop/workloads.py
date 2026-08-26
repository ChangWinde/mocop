"""Workload identity records emitted by the fixed collector script.

Translates tab-separated ``WORKLOAD`` rows from ``remote_script.py`` into
:class:`~mocop.models.WorkloadMetadata` overlays keyed by PID. Every field is
validated strictly; a malformed record rejects the overlay so an attacker on
a monitored host cannot smuggle markup or unbounded text into the dashboard.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .models import WorkloadMetadata

_MAX_WORKLOAD_RECORDS = 4_096
_MAX_WORKLOAD_START_EPOCH = 4_102_444_800  # 2100-01-01T00:00:00Z
# Cumulative CPU seconds are bounded by centuries of many-core runtime and
# resident memory by 16 TiB, far above real hosts but finite for arithmetic.
_MAX_CPU_SECONDS = 10_000_000_000
_MAX_RSS_MIB = 16_777_216
_WORKLOAD_KINDS = frozenset({"process", "slurm", "kubernetes", "docker", "podman"})


def _workload_start_iso(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if not text.isdigit() or not 0 < int(text) <= _MAX_WORKLOAD_START_EPOCH:
        raise ValueError("resource payload has an invalid workload start time")
    return (
        datetime.fromtimestamp(int(text), tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _sanitized_workload_command(value: str) -> str | None:
    """Bound the display-only command line without discarding the record."""
    cleaned = "".join(
        " " if ord(character) < 32 or 127 <= ord(character) <= 159 else character
        for character in value.replace("\u2028", " ").replace("\u2029", " ")
    ).strip()
    return cleaned[:255] or None


def _optional_footprint(value: str, label: str, maximum: int) -> float | None:
    text = value.strip()
    if not text:
        return None
    if not text.isdigit() or int(text) > maximum:
        raise ValueError(f"resource payload has an invalid workload {label}")
    return float(text)


def parse_workload_records(payload: str) -> dict[int, WorkloadMetadata]:
    workloads: dict[int, WorkloadMetadata] = {}
    # ASCII newlines only: a Unicode line boundary inside a command line or
    # environment-derived field must stay within its record instead of
    # splitting it and discarding the whole workload overlay.
    for row_number, line in enumerate(payload.split("\n"), start=1):
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 12 or fields[0] != "WORKLOAD":
            raise ValueError(
                f"resource payload has an invalid workload record on row {row_number}"
            )
        pid_text = fields[1].strip()
        if not pid_text.isdigit() or not 1 <= int(pid_text) <= 2_147_483_647:
            raise ValueError("resource payload has an invalid workload PID")
        pid = int(pid_text)
        if pid in workloads:
            raise ValueError("resource payload has duplicate workload PIDs")
        kind = fields[2].strip()
        if kind not in _WORKLOAD_KINDS:
            raise ValueError("resource payload has an invalid workload kind")

        def optional_text(value: str, label: str) -> str | None:
            text = value.strip()
            if len(text) > 255 or any(ord(character) < 32 for character in text):
                raise ValueError(f"resource payload has invalid workload {label}")
            return text or None

        workloads[pid] = WorkloadMetadata(
            kind=kind,
            workload_id=optional_text(fields[3], "identifier"),
            name=optional_text(fields[4], "name"),
            owner=optional_text(fields[5], "owner"),
            queue=optional_text(fields[6], "queue"),
            namespace=optional_text(fields[7], "namespace"),
            started_at=_workload_start_iso(fields[8]),
            command=_sanitized_workload_command(fields[9]),
            cpu_seconds=_optional_footprint(fields[10], "cpu time", _MAX_CPU_SECONDS),
            rss_mib=_optional_footprint(fields[11], "memory", _MAX_RSS_MIB),
        )
    if len(workloads) > _MAX_WORKLOAD_RECORDS:
        raise ValueError("resource payload has too many workload records")
    return workloads
