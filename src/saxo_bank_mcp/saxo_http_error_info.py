from __future__ import annotations

from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value

type SaxoErrorCode = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9]*$"),
]


class SaxoHttpErrorInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    error_code: SaxoErrorCode = Field(alias="ErrorCode")
    message: str = Field(alias="Message")
    model_state: dict[str, list[str]] | None = Field(default=None, alias="ModelState")


_ERROR_INFO_ADAPTER: Final = TypeAdapter(SaxoHttpErrorInfo)


def validated_saxo_error_code(content: bytes) -> str | None:
    try:
        error_info = _ERROR_INFO_ADAPTER.validate_python(
            parse_json_value(content),
            strict=True,
        )
    except (StrictJsonError, ValidationError):
        return None
    return error_info.error_code
