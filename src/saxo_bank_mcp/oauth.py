from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

import httpx2
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic_core import PydanticCustomError

from saxo_bank_mcp.auth import SaxoTokenSet, TokenEnvironment
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.http_client import create_async_client

type OAuthFailureCode = Literal[
    "http_error",
    "network_error",
    "invalid_token_response",
    "token_not_refreshable",
]
HTTP_SUCCESS_MIN = 200
HTTP_SUCCESS_MAX = 300


@dataclass(frozen=True, slots=True)
class OAuthRequestError(Exception):
    code: OAuthFailureCode
    detail: str
    http_status: int | None = None


class OAuthTokenResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str = Field(
        validation_alias=AliasChoices("access_token", "accessToken", "AccessToken"),
    )
    refresh_token: str = Field(
        validation_alias=AliasChoices("refresh_token", "refreshToken", "RefreshToken"),
    )
    expires_in: int = Field(validation_alias=AliasChoices("expires_in", "expiresIn", "ExpiresIn"))

    @field_validator("access_token", "refresh_token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError("empty_oauth_token", "OAuth token value must not be empty")
        return value

    @field_validator("expires_in")
    @classmethod
    def validate_expires_in(cls, value: int) -> int:
        if value <= 0:
            raise PydanticCustomError("oauth_expiry", "OAuth token expiry must be positive")
        return value

    def to_token_set(
        self,
        *,
        code_verifier: str,
        environment: TokenEnvironment | None = None,
        received_at: datetime | None = None,
    ) -> SaxoTokenSet:
        issued_at = datetime.now(UTC) if received_at is None else received_at
        return SaxoTokenSet(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            code_verifier=code_verifier,
            environment=environment,
            expires_at=issued_at + timedelta(seconds=self.expires_in),
        )


_TOKEN_RESPONSE_ADAPTER = TypeAdapter(OAuthTokenResponse)


def authorization_code_form(
    settings: SimAuthSettings,
    *,
    code: str,
    code_verifier: str,
) -> dict[str, str]:
    return {
        "grant_type": "authorization_code",
        "client_id": settings.app_key,
        "code": code,
        "redirect_uri": settings.redirect_uri,
        "code_verifier": code_verifier,
    }


def refresh_token_form(token: SaxoTokenSet) -> dict[str, str]:
    refresh = token.refresh_material()
    if refresh is None:
        raise OAuthRequestError(
            "token_not_refreshable",
            "Cached token has no refresh token or PKCE verifier",
        )
    return {
        "grant_type": "refresh_token",
        "refresh_token": refresh.refresh_token,
        "code_verifier": refresh.code_verifier,
    }


async def exchange_authorization_code(
    settings: SimAuthSettings,
    *,
    code: str,
    code_verifier: str,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> SaxoTokenSet:
    form = authorization_code_form(settings, code=code, code_verifier=code_verifier)
    response = await _post_token_form(settings.token_url, form, transport=transport)
    parsed = _parse_token_response(response)
    return parsed.to_token_set(code_verifier=code_verifier, environment="SIM")


async def refresh_access_token(
    settings: SimAuthSettings,
    token: SaxoTokenSet,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> SaxoTokenSet:
    refresh = token.refresh_material()
    if refresh is None:
        raise OAuthRequestError(
            "token_not_refreshable",
            "Cached token has no refresh token or PKCE verifier",
        )
    response = await _post_token_form(
        settings.token_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh.refresh_token,
            "code_verifier": refresh.code_verifier,
        },
        transport=transport,
    )
    parsed = _parse_token_response(response)
    return parsed.to_token_set(code_verifier=refresh.code_verifier, environment=token.environment)


async def _post_token_form(
    token_url: str,
    form: dict[str, str],
    *,
    transport: httpx2.AsyncBaseTransport | None,
) -> httpx2.Response:
    try:
        async with create_async_client(transport=transport) as client:
            response = await client.post(
                token_url,
                data=form,
                headers={"Accept": "application/json"},
            )
    except httpx2.HTTPError as error:
        raise OAuthRequestError("network_error", type(error).__name__) from error
    if response.status_code < HTTP_SUCCESS_MIN or response.status_code >= HTTP_SUCCESS_MAX:
        raise OAuthRequestError(
            "http_error",
            "Saxo token endpoint rejected the PKCE request",
            response.status_code,
        )
    return response


def _parse_token_response(response: httpx2.Response) -> OAuthTokenResponse:
    try:
        return _TOKEN_RESPONSE_ADAPTER.validate_json(response.text)
    except ValidationError as error:
        raise OAuthRequestError(
            "invalid_token_response",
            "Saxo token endpoint returned an unexpected token shape",
            response.status_code,
        ) from error
