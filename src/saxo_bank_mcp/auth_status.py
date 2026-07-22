from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, TypedDict

type EffectiveReadEnvironment = Literal["SIM", "LIVE", "LIVE_READ_DISABLED"]
type SimCredentialSource = Literal["file", "env", "missing"]
type EnvironmentName = Literal["SIM", "LIVE"]

AUTH_STATUS_VERIFIES: Final[tuple[str, ...]] = (
    "local Saxo environment selection",
    "local credential-source presence without exposing credentials",
    "local token-cache presence, readability, and expiry metadata",
)
AUTH_STATUS_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "Saxo login/server-side authentication",
    "account access",
    "active session validity",
    "session capabilities",
    "trading/order readiness",
    "live-write permission",
)


class SaxoAuthStatus(TypedDict):
    requested_environment: EnvironmentName
    effective_read_environment: EffectiveReadEnvironment
    live_reads: bool
    live_writes: Literal[False]
    sim_credentials_present: bool
    sim_credential_source: SimCredentialSource
    live_credentials_present: bool
    sim_redirect_uri_present: bool
    pending_pkce_authorization_present: bool
    token_cache_present: bool
    token_cache_readable: bool
    token_cache_expired: bool | None
    token_cache_refresh_supported: bool | None
    token_cache_environment: EnvironmentName | None
    scope_used: Literal[False]
    verifies: list[str]
    does_not_verify: list[str]
    blocking_reasons: list[str]
    next_action: str


@dataclass(frozen=True, slots=True)
class AuthStatusInputs:
    requested_environment: EnvironmentName
    effective_read_environment: EffectiveReadEnvironment
    live_reads_enabled: bool
    sim_credentials_present: bool
    sim_credential_source: SimCredentialSource
    live_credentials_present: bool
    sim_redirect_uri_present: bool
    pending_pkce_authorization_present: bool
    token_cache_path_refused: bool
    token_cache_present: bool
    token_cache_readable: bool
    token_cache_expired: bool | None
    token_cache_refresh_supported: bool | None
    token_cache_environment: EnvironmentName | None


def build_auth_status(inputs: AuthStatusInputs) -> SaxoAuthStatus:
    blocking_reasons = _blocking_reasons(inputs)
    return {
        "requested_environment": inputs.requested_environment,
        "effective_read_environment": inputs.effective_read_environment,
        "live_reads": inputs.effective_read_environment == "LIVE",
        "live_writes": False,
        "sim_credentials_present": inputs.sim_credentials_present,
        "sim_credential_source": inputs.sim_credential_source,
        "live_credentials_present": inputs.live_credentials_present,
        "sim_redirect_uri_present": inputs.sim_redirect_uri_present,
        "pending_pkce_authorization_present": inputs.pending_pkce_authorization_present,
        "token_cache_present": inputs.token_cache_present,
        "token_cache_readable": inputs.token_cache_readable,
        "token_cache_expired": inputs.token_cache_expired,
        "token_cache_refresh_supported": inputs.token_cache_refresh_supported,
        "token_cache_environment": inputs.token_cache_environment,
        "scope_used": False,
        "verifies": list(AUTH_STATUS_VERIFIES),
        "does_not_verify": list(AUTH_STATUS_DOES_NOT_VERIFY),
        "blocking_reasons": blocking_reasons,
        "next_action": _next_action(inputs, blocking_reasons),
    }


def _blocking_reasons(inputs: AuthStatusInputs) -> list[str]:
    return [*_environment_blocking_reasons(inputs), *_token_blocking_reasons(inputs)]


def _environment_blocking_reasons(inputs: AuthStatusInputs) -> list[str]:
    reasons: list[str] = []
    if inputs.requested_environment == "SIM":
        if not inputs.sim_credentials_present:
            reasons.append("sim_credentials_missing")
        if not inputs.sim_redirect_uri_present and not _has_usable_token_cache(inputs):
            reasons.append("sim_redirect_uri_missing")
    elif inputs.effective_read_environment != "LIVE":
        if not inputs.live_reads_enabled:
            reasons.append("live_reads_disabled")
        if not inputs.live_credentials_present:
            reasons.append("live_credentials_missing")
    return reasons


def _has_usable_token_cache(inputs: AuthStatusInputs) -> bool:
    return (
        not inputs.token_cache_path_refused
        and inputs.token_cache_present
        and inputs.token_cache_readable
        and inputs.token_cache_expired is False
    )


def _token_blocking_reasons(inputs: AuthStatusInputs) -> list[str]:
    reasons: list[str] = []
    if inputs.token_cache_path_refused:
        reasons.append("token_cache_path_refused")
    elif not inputs.token_cache_present:
        if inputs.pending_pkce_authorization_present:
            reasons.append("pending_pkce_authorization_present")
        reasons.append("token_cache_missing")
    elif not inputs.token_cache_readable:
        reasons.append("token_cache_unreadable")
    elif inputs.token_cache_expired is True:
        reasons.append("token_cache_expired")
    elif (
        inputs.effective_read_environment == "LIVE"
        and inputs.token_cache_environment != inputs.requested_environment
    ):
        reasons.append("token_environment_mismatch")
    return reasons


def _next_action(inputs: AuthStatusInputs, blocking_reasons: list[str]) -> str:
    if _needs_fresh_portal_token(inputs, blocking_reasons):
        return (
            "call saxo_cache_sim_access_token with a fresh Saxo developer portal SIM "
            "token, then call saxo_get_session_capabilities"
        )
    if inputs.requested_environment == "LIVE":
        live_action = _live_next_action(blocking_reasons)
        if live_action is not None:
            return live_action
    actions: tuple[tuple[str, str], ...] = (
        (
            "sim_credentials_missing",
            "configure SIM PKCE credentials, then call saxo_auth_status again",
        ),
        (
            "sim_redirect_uri_missing",
            "set SAXO_MCP_SIM_REDIRECT_URI to the registered Saxo redirect URI, "
            "then call saxo_start_pkce_login; PKCE cannot be completed "
            "machine-only without this URI and a Saxo-returned authorization code; "
            "or call saxo_cache_sim_access_token with a fresh Saxo developer portal SIM token",
        ),
        (
            "token_cache_path_refused",
            "move SAXO_MCP_TOKEN_CACHE_PATH outside the repository and common synced folders",
        ),
        (
            "token_cache_unreadable",
            "remove or replace the unreadable token cache, then restart the SIM PKCE login",
        ),
        (
            "pending_pkce_authorization_present",
            "complete the Saxo login already started, then call saxo_exchange_pkce_code; "
            "do not claim session verification until saxo_get_session_capabilities passes",
        ),
        (
            "token_cache_missing",
            "call saxo_start_pkce_login, complete Saxo login, then call saxo_exchange_pkce_code, "
            "or provide a valid SIM token cache/portal token",
        ),
        (
            "token_cache_expired",
            "call saxo_refresh_token, then call saxo_get_session_capabilities",
        ),
        (
            "live_reads_disabled",
            "use SIM mode, or configure LIVE read credentials and SAXO_MCP_ENABLE_LIVE_READS=1 "
            "in a later approved live-read phase",
        ),
        (
            "live_credentials_missing",
            "use SIM mode, or configure LIVE read credentials and SAXO_MCP_ENABLE_LIVE_READS=1 "
            "in a later approved live-read phase",
        ),
    )
    for reason, action in actions:
        if reason in blocking_reasons:
            return action
    if inputs.requested_environment == "LIVE":
        return (
            "call saxo_get_session_capabilities to verify the current LIVE "
            "session capability fields"
        )
    return "call saxo_get_session_capabilities to verify the current SIM session capability fields"


def _live_next_action(blocking_reasons: list[str]) -> str | None:
    actions: tuple[tuple[str, str], ...] = (
        (
            "token_cache_path_refused",
            "move SAXO_MCP_LIVE_TOKEN_CACHE_PATH outside the repository and common synced folders",
        ),
        (
            "token_cache_unreadable",
            "remove or replace the unreadable LIVE token cache, then restart LIVE PKCE login",
        ),
        (
            "pending_pkce_authorization_present",
            "complete the Saxo LIVE login already started, then exchange the returned code",
        ),
        (
            "token_cache_missing",
            "complete LIVE PKCE login and save a LIVE token cache before retrying",
        ),
        (
            "token_cache_expired",
            "refresh or recreate the LIVE token cache before retrying LIVE reads",
        ),
        (
            "token_environment_mismatch",
            "replace the token cache with a LIVE-issued token before retrying LIVE reads",
        ),
    )
    for reason, action in actions:
        if reason in blocking_reasons:
            return action
    return None


def _needs_fresh_portal_token(
    inputs: AuthStatusInputs,
    blocking_reasons: list[str],
) -> bool:
    return (
        inputs.requested_environment == "SIM"
        and inputs.token_cache_present
        and inputs.token_cache_readable
        and inputs.token_cache_refresh_supported is False
        and (
            "token_cache_expired" in blocking_reasons
            or "sim_redirect_uri_missing" in blocking_reasons
        )
    )
