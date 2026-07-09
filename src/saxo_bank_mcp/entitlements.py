from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol, TypedDict

import httpx2
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.http_client import create_async_client

ENTITLEMENTS_PATH: Final = "/port/v1/users/me/entitlements"
ENTITLEMENT_FIELD_SET: Final = "Default"
HTTP_SUCCESS_MIN = 200
HTTP_SUCCESS_MAX = 300

type EntitlementsFailureCode = Literal[
    "http_error",
    "network_error",
    "invalid_entitlements_response",
]


class EntitlementBucketFields(TypedDict):
    DelayedFullBook: list[str]
    DelayedGreeks: list[str]
    Greeks: list[str]
    RealTimeFullBook: list[str]
    RealTimeTopOfBook: list[str]


class EntitlementExchangeFields(TypedDict):
    ExchangeId: str
    Entitlements: list[EntitlementBucketFields]


class UserEntitlementsFields(TypedDict):
    Data: list[EntitlementExchangeFields]
    MaxRows: int | None
    Count: int | None
    HasNextPage: bool


class EntitlementsSummary(TypedDict):
    exchange_count: int
    exchange_ids: list[str]
    max_rows: int | None
    response_count: int | None
    has_next_page: bool
    possibly_truncated: bool
    entitlement_bucket_counts: dict[str, int]


class EntitlementReadSettings(Protocol):
    rest_base_url: str


@dataclass(frozen=True, slots=True)
class EntitlementsRequestError(Exception):
    code: EntitlementsFailureCode
    detail: str
    http_status: int | None = None


class EntitlementBucketResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    DelayedFullBook: tuple[str, ...] = ()
    DelayedGreeks: tuple[str, ...] = ()
    Greeks: tuple[str, ...] = ()
    RealTimeFullBook: tuple[str, ...] = ()
    RealTimeTopOfBook: tuple[str, ...] = ()

    def to_fields(self) -> EntitlementBucketFields:
        return {
            "DelayedFullBook": list(self.DelayedFullBook),
            "DelayedGreeks": list(self.DelayedGreeks),
            "Greeks": list(self.Greeks),
            "RealTimeFullBook": list(self.RealTimeFullBook),
            "RealTimeTopOfBook": list(self.RealTimeTopOfBook),
        }


class EntitlementExchangeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    ExchangeId: str
    Entitlements: tuple[EntitlementBucketResponse, ...] = ()

    def to_fields(self) -> EntitlementExchangeFields:
        return {
            "ExchangeId": self.ExchangeId,
            "Entitlements": [entitlement.to_fields() for entitlement in self.Entitlements],
        }


class UserEntitlementsResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    Data: tuple[EntitlementExchangeResponse, ...] = ()
    MaxRows: int | None = None
    count: int | None = Field(default=None, alias="__count")
    next_url: str | None = Field(default=None, alias="__next")

    def to_fields(self) -> UserEntitlementsFields:
        return {
            "Data": [exchange.to_fields() for exchange in self.Data],
            "MaxRows": self.MaxRows,
            "Count": self.count,
            "HasNextPage": self.next_url is not None,
        }


_ENTITLEMENTS_ADAPTER = TypeAdapter(UserEntitlementsResponse)


async def read_user_entitlements(
    settings: EntitlementReadSettings,
    token: SaxoTokenSet,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> UserEntitlementsFields:
    try:
        async with create_async_client(
            base_url=settings.rest_base_url,
            transport=transport,
        ) as client:
            response = await client.get(
                ENTITLEMENTS_PATH.lstrip("/"),
                params={"EntitlementFieldSet": ENTITLEMENT_FIELD_SET},
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token.access_token}",
                },
            )
    except httpx2.HTTPError as error:
        raise EntitlementsRequestError("network_error", type(error).__name__) from error
    if response.status_code < HTTP_SUCCESS_MIN or response.status_code >= HTTP_SUCCESS_MAX:
        raise EntitlementsRequestError(
            "http_error",
            "Saxo entitlements request was rejected",
            response.status_code,
        )
    return _parse_entitlements_response(response).to_fields()


def summarize_user_entitlements(entitlements: UserEntitlementsFields) -> EntitlementsSummary:
    counts = {
        "DelayedFullBook": 0,
        "DelayedGreeks": 0,
        "Greeks": 0,
        "RealTimeFullBook": 0,
        "RealTimeTopOfBook": 0,
    }
    exchange_ids: list[str] = []
    for exchange in entitlements["Data"]:
        exchange_ids.append(exchange["ExchangeId"])
        for bucket in exchange["Entitlements"]:
            counts["DelayedFullBook"] += len(bucket["DelayedFullBook"])
            counts["DelayedGreeks"] += len(bucket["DelayedGreeks"])
            counts["Greeks"] += len(bucket["Greeks"])
            counts["RealTimeFullBook"] += len(bucket["RealTimeFullBook"])
            counts["RealTimeTopOfBook"] += len(bucket["RealTimeTopOfBook"])
    return {
        "exchange_count": len(entitlements["Data"]),
        "exchange_ids": exchange_ids,
        "max_rows": entitlements["MaxRows"],
        "response_count": entitlements["Count"],
        "has_next_page": entitlements["HasNextPage"],
        "possibly_truncated": entitlement_response_is_possibly_truncated(entitlements),
        "entitlement_bucket_counts": counts,
    }


def entitlement_response_is_possibly_truncated(entitlements: UserEntitlementsFields) -> bool:
    exchange_count = len(entitlements["Data"])
    response_count = entitlements["Count"]
    max_rows = entitlements["MaxRows"]
    return (
        entitlements["HasNextPage"]
        or (response_count is not None and exchange_count < response_count)
        or (max_rows is not None and exchange_count >= max_rows)
    )


def _parse_entitlements_response(response: httpx2.Response) -> UserEntitlementsResponse:
    try:
        return _ENTITLEMENTS_ADAPTER.validate_json(response.text)
    except ValidationError as error:
        raise EntitlementsRequestError(
            "invalid_entitlements_response",
            "Saxo entitlements response did not match documented fields",
            response.status_code,
        ) from error
