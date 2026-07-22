from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp._evidence import JsonValue


class CollectionStructureJson(TypedDict):
    shape: Literal["data_envelope", "top_level_array"]
    declared_count_present: bool
    declared_count_consistent: bool | None


class StateCollectionStructureJson(TypedDict):
    orders: CollectionStructureJson
    positions: CollectionStructureJson
    trade_messages: CollectionStructureJson


class CollectionEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    data: list[JsonValue] = Field(alias="Data")
    count: int = Field(alias="__count", ge=0)
    max_rows: int | None = Field(default=None, alias="MaxRows", ge=0)
    next_link: str | None = Field(default=None, alias="__next")

    @field_validator("next_link")
    @classmethod
    def reject_incomplete_pagination(cls, value: str | None) -> str | None:
        if value:
            raise PydanticCustomError(
                "pagination_present",
                "collection pagination must be completed before proof",
            )
        return value

    @model_validator(mode="after")
    def reject_declared_count_mismatch(self) -> CollectionEnvelope:
        if self.count != len(self.data):
            raise PydanticCustomError(
                "declared_count_mismatch",
                "declared collection count must match returned rows",
            )
        return self

    def __len__(self) -> int:
        """Return the number of collection rows."""
        return len(self.data)


class CollectionPayload(RootModel[CollectionEnvelope]):
    model_config = ConfigDict(frozen=True, strict=True)

    @property
    def count(self) -> int:
        return len(self.root)

    def structure(self) -> CollectionStructureJson:
        return {
            "shape": "data_envelope",
            "declared_count_present": True,
            "declared_count_consistent": True,
        }


class MessagesPayload(RootModel[list[JsonValue]]):
    model_config = ConfigDict(frozen=True, strict=True)

    @property
    def count(self) -> int:
        return len(self.root)

    def structure(self) -> CollectionStructureJson:
        return {
            "shape": "top_level_array",
            "declared_count_present": False,
            "declared_count_consistent": None,
        }
