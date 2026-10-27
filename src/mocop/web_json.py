"""Strict JSON decoding for HTTP request bodies, before schema validation."""

from __future__ import annotations

import json


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def decode_json_body(body: bytes) -> object:
    """Reject ambiguous keys and numbers that JSON does not define."""
    return json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )
