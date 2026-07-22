from __future__ import annotations

import json
from typing import Annotated, Final, Never

from pydantic import Field, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue

type FiniteJsonFloat = Annotated[float, Field(allow_inf_nan=False)]
type StrictJsonValue = (
    str | int | FiniteJsonFloat | bool | None | list[StrictJsonValue] | dict[str, StrictJsonValue]
)

_JSON_ADAPTER: Final[TypeAdapter[StrictJsonValue]] = TypeAdapter(StrictJsonValue)


class StrictJsonError(ValueError):
    pass


def parse_json_value(content: bytes | str) -> JsonValue:
    try:
        raw: StrictJsonValue = json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite_constant,
        )
    except StrictJsonError:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError, ValueError) as error:
        raise StrictJsonError("invalid_json") from error
    try:
        return _JSON_ADAPTER.validate_python(raw, strict=True)
    except ValidationError as error:
        raise StrictJsonError("invalid_json_value") from error


def validate_json_value(value: JsonValue) -> JsonValue:
    try:
        return _JSON_ADAPTER.validate_python(value, strict=True)
    except ValidationError as error:
        raise StrictJsonError("invalid_json_value") from error


def _unique_object(
    pairs: list[tuple[str, StrictJsonValue]],
) -> dict[str, StrictJsonValue]:
    result: dict[str, StrictJsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError("duplicate_json_member")
        result[key] = value
    return result


def _reject_nonfinite_constant(_value: str) -> Never:
    raise StrictJsonError("nonfinite_json_number")
