from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from saxo_bank_mcp.strict_json import parse_json_value

INSTRUMENT_DETAILS_PATH: Final = "/ref/v1/instruments/details/{uic}/{asset_type}"


class LiveInstrumentDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    uic: int = Field(alias="Uic", gt=0)
    asset_type: str = Field(alias="AssetType", min_length=1)
    is_tradable: bool = Field(alias="IsTradable")


_INSTRUMENT_ADAPTER: Final = TypeAdapter(LiveInstrumentDetails)


def instrument_details_path(uic: int, asset_type: str) -> str:
    return INSTRUMENT_DETAILS_PATH.format(uic=uic, asset_type=asset_type)


def parse_live_instrument(content: bytes) -> LiveInstrumentDetails:
    return _INSTRUMENT_ADAPTER.validate_python(parse_json_value(content), strict=True)
