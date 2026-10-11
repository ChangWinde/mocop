from __future__ import annotations

from .capacity import CapacityRequest, match_capacity


def send_capacity(handler, query: str) -> None:
    """Rank idle GPU groups against a demand; observations, never reservations."""
    values = handler._parse_query("/api/capacity", query)
    if values is None:
        return
    state = handler.monitor_server.state
    snapshot = state.snapshot_view()
    thresholds = snapshot["thresholds"]
    assert isinstance(thresholds, dict)
    result = match_capacity(
        snapshot["servers"],  # type: ignore[arg-type]
        state.incidents(1)["active"],  # type: ignore[arg-type]
        CapacityRequest(values["gpus"], values["min_vram_gib"], values["model"]),
        busy_pct=float(thresholds["gpu_busy_pct"]),
        temperature_c=float(thresholds["gpu_temperature_warning_c"]),
    )
    result["generatedAt"] = snapshot["generatedAt"]
    result["lastPollCompletedAt"] = snapshot["lastPollCompletedAt"]
    handler._send_json(result)
