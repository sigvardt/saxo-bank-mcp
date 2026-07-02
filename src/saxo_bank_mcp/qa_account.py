from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import httpx2
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SIM_ENDPOINTS, SimAuthSettingsError, resolve_sim_auth_settings
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)

type AccountKeySource = Literal[
    "env_override",
    "sim_accounts_me",
    "fixture_no_cached_token",
    "fixture_discovery_failed",
]

QA_ACCOUNT_ENV: Final = "SAXO_MCP_QA_ACCOUNT_KEY"
JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class SimAccountKeyResolution:
    account_key: str
    source: AccountKeySource
    discovered: bool
    network_call_made: bool
    http_status: int | None
    reason: str

    def to_safe_json(self) -> dict[str, JsonValue]:
        return {
            "account_key_redacted": True,
            "source": self.source,
            "discovered": self.discovered,
            "network_call_made": self.network_call_made,
            "http_status": self.http_status,
            "reason": self.reason,
        }


async def resolve_sim_account_key(
    *,
    default_account_key: str,
    tool_name: str,
) -> SimAccountKeyResolution:
    env_value = os.environ.get(QA_ACCOUNT_ENV, "").strip()
    if env_value:
        return SimAccountKeyResolution(
            account_key=env_value,
            source="env_override",
            discovered=True,
            network_call_made=False,
            http_status=None,
            reason="env_override_present",
        )
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return _fixture_resolution(default_account_key, str(error.code))
    cache_check = cached_token_for_tool(tool_name, settings.cache_path)
    match cache_check:
        case CachedTokenReady(token=token):
            return await _discover_account_key(default_account_key, token.access_token)
        case CachedTokenBlocked(result=result):
            return _fixture_resolution(
                default_account_key, str(result.get("reason", "token_missing"))
            )


async def _discover_account_key(
    default_account_key: str,
    access_token: str,
) -> SimAccountKeyResolution:
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            response = await client.get(
                "port/v1/accounts/me",
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            )
    except httpx2.HTTPError as error:
        return SimAccountKeyResolution(
            account_key=default_account_key,
            source="fixture_discovery_failed",
            discovered=False,
            network_call_made=True,
            http_status=None,
            reason=type(error).__name__,
        )
    account_key = _first_account_key(_response_json(response))
    if account_key is None:
        return SimAccountKeyResolution(
            account_key=default_account_key,
            source="fixture_discovery_failed",
            discovered=False,
            network_call_made=True,
            http_status=response.status_code,
            reason="account_key_not_found",
        )
    return SimAccountKeyResolution(
        account_key=account_key,
        source="sim_accounts_me",
        discovered=True,
        network_call_made=True,
        http_status=response.status_code,
        reason="account_key_discovered",
    )


def _response_json(response: httpx2.Response) -> JsonValue:
    try:
        return JSON_VALUE_ADAPTER.validate_python(response.json())
    except (ValueError, ValidationError):
        return None


def _first_account_key(value: JsonValue) -> str | None:
    if isinstance(value, Mapping):
        candidate = value.get("AccountKey")
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        for child in value.values():
            found = _first_account_key(child)
            if found is not None:
                return found
        return None
    if isinstance(value, str) or not isinstance(value, Sequence):
        return None
    for child in value:
        found = _first_account_key(child)
        if found is not None:
            return found
    return None


def _fixture_resolution(default_account_key: str, reason: str) -> SimAccountKeyResolution:
    return SimAccountKeyResolution(
        account_key=default_account_key,
        source="fixture_no_cached_token",
        discovered=False,
        network_call_made=False,
        http_status=None,
        reason=reason,
    )
