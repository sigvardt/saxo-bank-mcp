from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value

INSTRUMENT_DETAILS_PATH: Final = "/ref/v1/instruments/details/{uic}/{asset_type}"
INSTRUMENT_IDENTITY_SOURCE: Final = "saxo_live_reference_data"
_MAX_DESCRIPTION_LEN: Final = 200
_MAX_SYMBOL_LEN: Final = 64
_MAX_CURRENCY_LEN: Final = 16
_MAX_ASSET_TYPE_LEN: Final = 64
_ASCII_CONTROL_MAX: Final = 31
_ASCII_DELETE: Final = 127


class LiveInstrumentDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    uic: int = Field(alias="Uic", gt=0)
    asset_type: str = Field(alias="AssetType", min_length=1)
    is_tradable: bool = Field(alias="IsTradable")


@dataclass(frozen=True, slots=True)
class LiveInstrumentIdentity:
    """Trusted LIVE instrument labels from Saxo reference data only."""

    uic: int
    asset_type: str
    description: str
    symbol: str
    price_currency: str
    source: str = INSTRUMENT_IDENTITY_SOURCE


_INSTRUMENT_ADAPTER: Final = TypeAdapter(LiveInstrumentDetails)


def instrument_details_path(uic: int, asset_type: str) -> str:
    return INSTRUMENT_DETAILS_PATH.format(uic=uic, asset_type=asset_type)


def parse_live_instrument(content: bytes) -> LiveInstrumentDetails:
    return _INSTRUMENT_ADAPTER.validate_python(parse_json_value(content), strict=True)


def parse_live_instrument_identity(
    content: bytes,
    *,
    expected_uic: int,
    expected_asset_type: str,
) -> LiveInstrumentIdentity | None:
    """Parse Saxo instrument details into approval identity, or None if untrusted."""
    try:
        payload = parse_json_value(content)
    except StrictJsonError:
        return None
    if not isinstance(payload, dict):
        return None

    uic = _as_positive_int(payload.get("Uic"))
    asset_type = normalize_instrument_text(
        payload.get("AssetType"),
        max_length=_MAX_ASSET_TYPE_LEN,
    )
    description = normalize_instrument_text(
        payload.get("Description"),
        max_length=_MAX_DESCRIPTION_LEN,
    )
    symbol = normalize_instrument_text(payload.get("Symbol"), max_length=_MAX_SYMBOL_LEN)
    price_currency = normalize_instrument_text(
        payload.get("PriceCurrency"),
        max_length=_MAX_CURRENCY_LEN,
    )
    expected_asset = normalize_instrument_text(
        expected_asset_type,
        max_length=_MAX_ASSET_TYPE_LEN,
    )
    if (
        uic is None
        or asset_type is None
        or description is None
        or symbol is None
        or price_currency is None
        or expected_asset is None
        or uic != expected_uic
        or asset_type != expected_asset
    ):
        return None
    return LiveInstrumentIdentity(
        uic=uic,
        asset_type=asset_type,
        description=description,
        symbol=symbol,
        price_currency=price_currency,
    )


def normalize_instrument_text(value: object, *, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or len(stripped) > max_length:
        return None
    if any(_is_disallowed_identity_char(char) for char in stripped):
        return None
    return stripped


def _as_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _is_disallowed_identity_char(char: str) -> bool:
    code = ord(char)
    return code <= _ASCII_CONTROL_MAX or code == _ASCII_DELETE
