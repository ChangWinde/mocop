"""Query and body schema types plus the validators the handlers run.

``api_manifest`` declares which routes exist and what each accepts; this
module owns the generic machinery those declarations are built from: the
parameter and field types with their JSON descriptions, and the two
validators that turn a raw query string or a parsed JSON body into accepted
values, raising the stable machine-readable codes the reference documents.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qs

from .config import is_safe_alias


class QueryError(ValueError):
    """A rejected query string: the stable code and, when one parameter is at
    fault, its name."""

    def __init__(self, message: str, code: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class QueryParameter:
    """One accepted query parameter.

    ``alias`` values must be safe SSH aliases, ``identity`` values are bounded
    printable GPU identities, and ``text`` values are bounded printable free
    text; all three raise the route's shape code when malformed. ``integer``
    values raise their own ``invalid_code`` when they are not integers inside
    ``[minimum, maximum]``.
    """

    kind: str
    required: bool = False
    minimum: int | None = None
    maximum: int | None = None
    default: int | str | None = None
    invalid_code: str | None = None

    def describe(self) -> dict[str, object]:
        described: dict[str, object] = {"type": self.kind, "required": self.required}
        if self.minimum is not None:
            described["minimum"] = self.minimum
        if self.maximum is not None:
            described["maximum"] = self.maximum
        if self.default is not None:
            described["default"] = self.default
        return described


@dataclass(frozen=True, slots=True)
class QuerySchema:
    parameters: dict[str, QueryParameter]
    shape_code: str
    shape_message: str

    def describe(self) -> dict[str, object]:
        return {
            name: parameter.describe() for name, parameter in self.parameters.items()
        }


def _valid_text(value: str) -> bool:
    return 1 <= len(value) <= 128 and not any(
        ord(character) < 32 for character in value
    )


def parse_query(schema: QuerySchema, query: str) -> dict[str, object]:
    """Validate a raw query string against ``schema``.

    Unknown names, then every cardinality and string-shape problem (route
    shape code), then integer parsing and bounds (per-parameter code): the
    same precedence the routes have always documented. Absent optional
    integers take their default; absent optional strings are ``None``.
    """
    raw = parse_qs(query, keep_blank_values=True)
    unknown = sorted(set(raw) - set(schema.parameters))
    if unknown:
        raise QueryError(
            "unknown query parameter", "UNKNOWN_QUERY_PARAMETER", unknown[0]
        )
    supplied: dict[str, str] = {}
    for name, parameter in schema.parameters.items():
        shape_error = QueryError(schema.shape_message, schema.shape_code, name)
        given = raw.get(name, [])
        if len(given) > 1 or (parameter.required and not given):
            raise shape_error
        if not given:
            continue
        if parameter.kind == "alias" and not is_safe_alias(given[0]):
            raise shape_error
        if parameter.kind in {"identity", "text"} and not _valid_text(given[0]):
            raise shape_error
        supplied[name] = given[0]
    values: dict[str, object] = {}
    for name, parameter in schema.parameters.items():
        if name not in supplied:
            values[name] = parameter.default
            continue
        if parameter.kind != "integer":
            values[name] = supplied[name]
            continue
        assert parameter.invalid_code is not None
        assert parameter.minimum is not None and parameter.maximum is not None
        try:
            number = int(supplied[name])
        except ValueError:
            number = parameter.minimum - 1
        if not parameter.minimum <= number <= parameter.maximum:
            raise QueryError(
                f"{name} must be between {parameter.minimum} and {parameter.maximum}",
                parameter.invalid_code,
                name,
            )
        values[name] = number
    return values


class BodyError(ValueError):
    """A rejected write body: the stable code and, when one field is at fault,
    its name."""

    def __init__(self, message: str, code: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class BodyField:
    """One accepted JSON field on a write route.

    ``alias`` and ``enum`` are strings restricted to the safe alias grammar or
    ``values``; ``integer`` and ``number`` are non-boolean JSON numbers inside
    ``values`` or ``[minimum, maximum]``; ``text`` is a string whose stripped
    length may not exceed ``maximum`` (the route enforces its own character
    grammar); ``timestamp`` is a string, or ``null`` when ``nullable``.
    """

    kind: str
    required: bool = True
    nullable: bool = False
    values: tuple[object, ...] | None = None
    minimum: int | None = None
    maximum: int | None = None
    notes: str | None = None

    def describe(self) -> dict[str, object]:
        described: dict[str, object] = {"type": self.kind, "required": self.required}
        if self.nullable:
            described["nullable"] = True
        if self.values is not None:
            described["values"] = list(self.values)
        if self.minimum is not None:
            described["minimum"] = self.minimum
        if self.maximum is not None:
            described["maximum"] = self.maximum
        if self.notes is not None:
            described["notes"] = self.notes
        return described

    def well_typed(self, value: object) -> bool:
        if value is None:
            return self.nullable
        if self.kind in {"alias", "enum", "text", "timestamp"}:
            return isinstance(value, str)
        if isinstance(value, bool):
            return False
        if self.kind == "integer":
            return isinstance(value, int)
        return isinstance(value, int | float)

    def accepts(self, value: object) -> bool:
        """Value-level check for a well-typed value."""
        if value is None:
            return True
        if self.kind == "alias":
            return is_safe_alias(value)
        if self.kind == "text":
            return self.maximum is None or len(value.strip()) <= self.maximum
        if self.values is not None:
            return value in self.values
        if self.minimum is None:
            return True
        try:
            numeric = float(value)
        except OverflowError:
            # JSON integers have unbounded precision; huge ones are invalid.
            return False
        assert self.maximum is not None
        return math.isfinite(numeric) and self.minimum <= value <= self.maximum


@dataclass(frozen=True, slots=True)
class BodySchema:
    """The JSON object a write route accepts.

    ``empty`` is the exact ``{}`` body used by restart, update, and the
    notification test. ``exact_keys`` is False only for collector settings,
    which take a non-empty subset of the published fields.
    """

    fields: dict[str, BodyField]
    exact_keys: bool = True
    empty: bool = False

    def describe(self) -> dict[str, object]:
        if self.empty:
            return {"type": "object", "empty": True}
        return {
            "type": "object",
            "exactKeys": self.exact_keys,
            "fields": {name: field.describe() for name, field in self.fields.items()},
        }

    def accepts_keys(self, keys: Iterable[str]) -> bool:
        supplied = set(keys)
        if self.empty:
            return not supplied
        if self.exact_keys:
            return supplied == set(self.fields)
        return bool(supplied) and supplied <= set(self.fields)


def validate_body(schema: BodySchema, payload: object) -> dict[str, object]:
    """Check a parsed JSON body against ``schema`` and return it as a dict.

    Anything but an object, a key set the schema does not accept, or a field
    of the wrong JSON type is ``INVALID_SCHEMA``; a well-typed value outside
    the published alias grammar, ``values``, bounds, or text length is
    ``INVALID_SETTINGS``. Cross-field rules stay with the route handler.
    """
    if not isinstance(payload, dict) or not schema.accepts_keys(payload):
        raise BodyError(
            "request body does not match the route schema", "INVALID_SCHEMA"
        )
    for name, value in payload.items():
        field = schema.fields[name]
        if not field.well_typed(value):
            raise BodyError(f"{name} has the wrong type", "INVALID_SCHEMA", name)
        if not field.accepts(value):
            raise BodyError(
                f"{name} is not an accepted value", "INVALID_SETTINGS", name
            )
    return payload
