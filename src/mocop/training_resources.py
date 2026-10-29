"""GPU totals from explicit job specifications and complete resource requests."""

from __future__ import annotations


def _count(value, maximum, minimum=0):
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and minimum <= value <= maximum
        else None
    )


def gpu_resources(value):
    count = _count(value.get("gpu_count"), 16384)
    source = "platform" if count is not None else None
    requests = value.get("resource_requests")
    rows = requests if isinstance(requests, list) and 0 < len(requests) <= 64 else []
    total, complete, models = 0, bool(rows), []
    models_complete = bool(rows)
    for row in rows:
        if not isinstance(row, dict):
            complete = False
            models_complete = False
            continue
        cards = _count(row.get("gpu_count"), 256)
        replicas = _count(row.get("replicas"), 256, 1)
        if cards is None or replicas is None:
            complete = False
        else:
            total += cards * replicas
        model = row.get("gpu_type")
        if cards != 0 and (not isinstance(model, str) or not model.strip()):
            models_complete = False
        if isinstance(model, str) and model.strip() and model[:120] not in models:
            models.append(model[:120])
    if count is None and complete and total <= 16384:
        count, source = total, "resource_requests"
    model = value.get("gpu_model")
    if not isinstance(model, str) or not model.strip():
        model = (" / ".join(models) or None) if models_complete else None
    return count, model, source
