from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettingsError
from saxo_bank_mcp.oauth import OAuthRequestError
from saxo_bank_mcp.session import (
    SESSION_CAPABILITIES_PATH,
    SessionCapabilityFields,
    SessionRequestError,
)

type ToolLeaf = str | int | bool | None
type ToolValue = ToolLeaf | list[str] | dict[str, ToolLeaf] | dict[str, int]
type ToolResult = dict[str, ToolValue]

PKCE_START_TOOL_DESCRIPTION: Final = (
    "Starts SIM PKCE login by creating an authorization URL and storing verifier/state locally. "
    "Does not complete Saxo login, prove account access, or prove trading readiness/order safety. "
    "Raw authorization URL reveal is sensitive and opt-in only."
)
PKCE_EXCHANGE_TOOL_DESCRIPTION: Final = (
    "Exchanges a SIM PKCE authorization code for cached tokens after state verification. "
    "Does not prove account access or trading readiness/order safety."
)
REFRESH_TOOL_DESCRIPTION: Final = (
    "Refreshes the cached SIM access token using the cached refresh token and PKCE verifier. "
    "Does not prove account access or trading readiness/order safety."
)
SESSION_CAPABILITIES_TOOL_DESCRIPTION: Final = (
    "Reads SIM /root/v1/sessions/capabilities with the cached bearer token. "
    "Proves current session capability fields only, not trading readiness/order safety."
)
AUTH_VERIFIES: Final[tuple[str, ...]] = (
    "SIM PKCE parameters are built without OAuth scope",
    "pending verifier/state is stored owner-only outside the repository",
)
AUTH_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "Saxo login completed",
    "account access",
    "active session validity",
    "session capabilities",
    "trading readiness/order safety",
    "live endpoint access",
)
EXCHANGED_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "account access",
    "active session validity",
    "session capabilities",
    "trading readiness/order safety",
    "live endpoint access",
)
SESSION_VERIFIES: Final[tuple[str, ...]] = (
    "cached SIM bearer token can read current session capability fields",
)
SESSION_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "order placement safety",
    "instrument/account suitability",
    "real-money approval",
    "live endpoint access",
)
_DEFAULT_AUTH_NEXT_ACTION: Final = (
    "inspect the auth_required reason, then call saxo_auth_status for local configuration state"
)
_AUTH_NEXT_ACTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "sim_credentials_missing": (
            "configure SIM PKCE credentials, then call saxo_start_pkce_login"
        ),
        "sim_redirect_uri_missing": (
            "set SAXO_MCP_SIM_REDIRECT_URI to the registered Saxo redirect URI, "
            "then call saxo_start_pkce_login; PKCE cannot be completed "
            "machine-only without this URI and a Saxo-returned authorization code"
        ),
        "sim_endpoint_untrusted": "use the official Saxo SIM authorization and token endpoints",
        "token_cache_path_refused": (
            "move SAXO_MCP_TOKEN_CACHE_PATH outside the repository and common synced folders"
        ),
        "token_cache_missing": (
            "call saxo_start_pkce_login, complete Saxo login, then call "
            "saxo_exchange_pkce_code, or provide a valid SIM token cache/portal token"
        ),
        "token_cache_unreadable": (
            "remove or replace the unreadable token cache, then restart the SIM PKCE login"
        ),
        "pending_pkce_state_missing": (
            "call saxo_start_pkce_login before exchanging an authorization code"
        ),
        "pending_pkce_unreadable": (
            "remove the unreadable pending PKCE file or restart with saxo_start_pkce_login"
        ),
        "pending_pkce_state_mismatch": (
            "use the state returned by the most recent Saxo login redirect, "
            "or restart with saxo_start_pkce_login"
        ),
        "pending_pkce_expired": (
            "restart with saxo_start_pkce_login; the pending PKCE verifier expired"
        ),
        "redirect_uri_changed_since_login_start": (
            "restore SAXO_MCP_SIM_REDIRECT_URI to the value used for login start, "
            "or restart with saxo_start_pkce_login"
        ),
        "network_error": "retry the token request after confirming SIM token endpoint connectivity",
        "http_error": (
            "restart PKCE login if the code was already used, expired, or redirect URI is wrong"
        ),
        "invalid_token_response": (
            "inspect the redacted token-endpoint response shape before retrying"
        ),
        "invalid_capabilities_response": (
            "treat session capabilities as unverified; inspect the redacted response shape "
            "before using capability fields"
        ),
    },
)
_SESSION_NEXT_ACTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "network_error": (
            "retry saxo_get_session_capabilities after confirming SIM connectivity "
            "and cached token freshness"
        ),
        "http_error": (
            "call saxo_auth_status, refresh the cached token if expired, then retry "
            "saxo_get_session_capabilities"
        ),
        "invalid_capabilities_response": (
            "treat session capabilities as unverified; inspect the redacted response shape "
            "before using capability fields"
        ),
    },
)


def settings_error(tool_name: str, error: SimAuthSettingsError) -> ToolResult:
    return {
        "status": "auth_required",
        "tool_name": tool_name,
        "environment": "SIM",
        "reason": error.code,
        "detail": error.detail,
        "scope_used": False,
        "next_action": auth_next_action(error.code),
        "verifies": [],
        "does_not_verify": list(AUTH_DOES_NOT_VERIFY),
    }


def auth_required(tool_name: str, reason: str) -> ToolResult:
    return {
        "status": "auth_required",
        "tool_name": tool_name,
        "environment": "SIM",
        "reason": reason,
        "scope_used": False,
        "next_action": auth_next_action(reason),
        "verifies": [],
        "does_not_verify": list(AUTH_DOES_NOT_VERIFY),
    }


def auth_next_action(reason: str) -> str:
    return _AUTH_NEXT_ACTIONS.get(reason, _DEFAULT_AUTH_NEXT_ACTION)


def oauth_error(tool_name: str, error: OAuthRequestError) -> ToolResult:
    return {
        "status": "auth_required",
        "tool_name": tool_name,
        "environment": "SIM",
        "reason": error.code,
        "http_status": error.http_status,
        "detail": error.detail,
        "scope_used": False,
        "next_action": auth_next_action(error.code),
        "verifies": [],
        "does_not_verify": list(AUTH_DOES_NOT_VERIFY),
    }


def session_error(tool_name: str, error: SessionRequestError) -> ToolResult:
    return {
        "status": "session_capabilities_failed",
        "tool_name": tool_name,
        "environment": "SIM",
        "endpoint_path": SESSION_CAPABILITIES_PATH,
        "reason": error.code,
        "http_status": error.http_status,
        "detail": error.detail,
        "scope_used": False,
        "next_action": session_next_action(error.code),
        "verifies": [],
        "does_not_verify": list(SESSION_DOES_NOT_VERIFY),
    }


def session_next_action(reason: str) -> str:
    return _SESSION_NEXT_ACTIONS.get(
        reason,
        "call saxo_auth_status before retrying saxo_get_session_capabilities",
    )


def token_status(token: SaxoTokenSet) -> dict[str, ToolLeaf]:
    status = token.redacted_status()
    return {
        "has_access_token": status["has_access_token"],
        "has_refresh_token": status["has_refresh_token"],
        "has_code_verifier": status["has_code_verifier"],
        "expires_at": status["expires_at"],
        "is_expired": status["is_expired"],
    }


def session_capabilities(capabilities: SessionCapabilityFields) -> dict[str, ToolLeaf]:
    return {
        "AuthenticationLevel": capabilities["AuthenticationLevel"],
        "DataLevel": capabilities["DataLevel"],
        "TradeLevel": capabilities["TradeLevel"],
    }


def redacted_authorization_url(url: str) -> str:
    parsed = urlparse(url)
    redacted_query = urlencode(
        {
            key: "<redacted>" if key in {"client_id", "code_challenge", "state"} else value
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        },
    )
    return urlunparse(parsed._replace(query=redacted_query))
