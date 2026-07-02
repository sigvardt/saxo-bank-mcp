from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

import httpx2
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.http_client import create_async_client

SESSION_CAPABILITIES_PATH = "/root/v1/sessions/capabilities"

type CapabilityValue = str | int | None
type SessionFailureCode = Literal["http_error", "network_error", "invalid_capabilities_response"]
HTTP_SUCCESS_MIN = 200
HTTP_SUCCESS_MAX = 300


class SessionCapabilityFields(TypedDict):
    AuthenticationLevel: CapabilityValue
    DataLevel: CapabilityValue
    TradeLevel: CapabilityValue


class SessionReadSettings(Protocol):
    rest_base_url: str


@dataclass(frozen=True, slots=True)
class SessionRequestError(Exception):
    code: SessionFailureCode
    detail: str
    http_status: int | None = None


class SessionCapabilitiesResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    AuthenticationLevel: CapabilityValue = None
    DataLevel: CapabilityValue = None
    TradeLevel: CapabilityValue = None

    def to_fields(self) -> SessionCapabilityFields:
        return {
            "AuthenticationLevel": self.AuthenticationLevel,
            "DataLevel": self.DataLevel,
            "TradeLevel": self.TradeLevel,
        }


_CAPABILITIES_ADAPTER = TypeAdapter(SessionCapabilitiesResponse)


async def read_session_capabilities(
    settings: SessionReadSettings,
    token: SaxoTokenSet,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> SessionCapabilityFields:
    try:
        async with create_async_client(
            base_url=settings.rest_base_url,
            transport=transport,
        ) as client:
            response = await client.get(
                SESSION_CAPABILITIES_PATH.lstrip("/"),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token.access_token}",
                },
            )
    except httpx2.HTTPError as error:
        raise SessionRequestError("network_error", type(error).__name__) from error
    if response.status_code < HTTP_SUCCESS_MIN or response.status_code >= HTTP_SUCCESS_MAX:
        raise SessionRequestError(
            "http_error",
            "Saxo session capabilities request was rejected",
            response.status_code,
        )
    return _parse_capabilities_response(response).to_fields()


def _parse_capabilities_response(response: httpx2.Response) -> SessionCapabilitiesResponse:
    try:
        return _CAPABILITIES_ADAPTER.validate_json(response.text)
    except ValidationError as error:
        raise SessionRequestError(
            "invalid_capabilities_response",
            "Saxo session capabilities response did not match documented fields",
            response.status_code,
        ) from error
