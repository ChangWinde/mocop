from __future__ import annotations

from http import HTTPStatus

from .training_projection import ReadError


def send_training_jobs(handler) -> None:
    source = handler.monitor_server.training_jobs
    handler._send_json(source.snapshot() if source is not None else {"enabled": False})


def send_training_detail(handler, query: str) -> None:
    values = handler._parse_query("/api/training-job-detail", query)
    if values is None:
        return
    source = handler.monitor_server.training_jobs
    if source is None:
        handler._send_error(
            "training monitoring is not configured",
            HTTPStatus.SERVICE_UNAVAILABLE,
            code="TRAINING_NOT_CONFIGURED",
        )
        return
    try:
        handler._send_json(source.detail(values["id"]))
    except ReadError as error:
        status, code = {
            400: (400, "INVALID_TRAINING_JOB"),
            401: (403, "TRAINING_ACCESS_DENIED"),
            403: (403, "TRAINING_ACCESS_DENIED"),
            404: (404, "TRAINING_JOB_NOT_FOUND"),
            429: (429, "TRAINING_RATE_LIMITED"),
        }.get(error.status, (503, "TRAINING_READ_FAILED"))
        extra = (
            (("Retry-After", str(max(1, int(error.retry_after)))),)
            if status == 429
            else ()
        )
        handler._send_error(
            "training observation unavailable", status, code=code, extra_headers=extra
        )
