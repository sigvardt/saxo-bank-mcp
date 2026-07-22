from __future__ import annotations

from pathlib import Path
from typing import Final, cast

import anyio
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.auth_status import SaxoAuthStatus
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import TokenCachePathError, token_cache_path

AUTH_STATUS_ADAPTER: Final = TypeAdapter(SaxoAuthStatus)
SAXO_AUTH_REFERENCE_URLS: Final = (
    "https://www.developer.saxo/openapi/learn/oauth-authorization-code-grant-pkce",
    "https://www.developer.saxo/openapi/learn/security",
)


class AuthProbePayloadError(TypeError):
    pass


async def call_saxo_auth_status() -> SaxoAuthStatus:
    async with Client(mcp) as client:
        result = await client.call_tool("saxo_auth_status", {})
    return AUTH_STATUS_ADAPTER.validate_python(result.structured_content)


async def call_tool_payload(name: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    async with Client(mcp) as client:
        result = await client.call_tool(name, arguments)
    payload = result.structured_content
    if not isinstance(payload, dict):
        raise AuthProbePayloadError(f"{name} returned non-object structured content")
    return cast("dict[str, JsonValue]", payload)


def handle_auth_status(out: Path) -> int:
    payload = anyio.run(call_saxo_auth_status)
    auth: dict[str, JsonValue] = {
        "requested_environment": payload["requested_environment"],
        "effective_read_environment": payload["effective_read_environment"],
        "live_reads": payload["live_reads"],
        "live_writes": payload["live_writes"],
        "sim_credentials_present": payload["sim_credentials_present"],
        "sim_credential_source": payload["sim_credential_source"],
        "live_credentials_present": payload["live_credentials_present"],
        "sim_redirect_uri_present": payload["sim_redirect_uri_present"],
        "pending_pkce_authorization_present": payload["pending_pkce_authorization_present"],
        "token_cache_present": payload["token_cache_present"],
        "token_cache_readable": payload["token_cache_readable"],
        "token_cache_expired": payload["token_cache_expired"],
        "token_cache_refresh_supported": payload["token_cache_refresh_supported"],
        "token_cache_environment": payload["token_cache_environment"],
        "scope_used": payload["scope_used"],
        "verifies": payload["verifies"],
        "does_not_verify": payload["does_not_verify"],
        "blocking_reasons": payload["blocking_reasons"],
        "next_action": payload["next_action"],
    }
    event: dict[str, JsonValue] = {
        **base_event("auth-status", "passed", "FastMCP in-process saxo_auth_status returned"),
        "tool_name": "saxo_auth_status",
        "auth": auth,
    }
    redacted = redact_json(event)
    if not isinstance(redacted, dict):
        raise AuthProbePayloadError("auth status event redaction returned non-object")
    return 0 if write_scanned_json(out, redacted) else 1


def handle_sim_auth(out: Path) -> int:
    payload = anyio.run(call_sim_auth_flow)
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise AuthProbePayloadError("sim auth event redaction returned non-object")
    published = write_scanned_json(out, redacted)
    return 0 if redacted["status"] == "passed" and published else 1


async def call_sim_auth_flow() -> dict[str, JsonValue]:
    auth = await call_saxo_auth_status()
    capabilities = await call_tool_payload("saxo_get_session_capabilities", {})
    status = capabilities.get("status")
    if status == "passed":
        return {
            **base_event("sim-auth", "passed", "SIM session capabilities read through FastMCP"),
            "auth": _safe_auth_status(auth),
            "capabilities": capabilities,
        }

    start_result = await call_tool_payload(
        "saxo_start_pkce_login",
        {"reveal_authorization_url": False},
    )
    return {
        **base_event(
            "sim-auth",
            "blocked_external_auth_material",
            (
                "SIM auth cannot be completed headlessly with the available local "
                "credential material"
            ),
        ),
        "auth": _safe_auth_status(auth),
        "capabilities_attempt": capabilities,
        "pkce_start_attempt": start_result,
        "machine_completion_blocker": _machine_completion_blocker(capabilities, start_result),
        "machine_completion_possible": False,
        "prompted_user": False,
        "network_call_made": False,
        "missing_auth_material": _missing_auth_material(auth, capabilities, start_result),
        "official_saxo_auth_constraints": [
            (
                "PKCE requires a registered redirect URI and an authorization code "
                "returned after Saxo login"
            ),
            "The 24-hour SIM OpenAPI token shortcut is issued from Saxo's developer portal",
        ],
        "official_saxo_auth_references": list(SAXO_AUTH_REFERENCE_URLS),
        "next_action": (
            "provide a registered SIM redirect URI plus a Saxo authorization code, "
            "or provide a valid SIM token cache/portal token; do not claim session, "
            "account, or trading verification until saxo_get_session_capabilities passes"
        ),
    }


def handle_token_cache(out: Path) -> int:
    repo_root = Path.cwd()
    checks = {
        "default_path_ok": token_cache_path(repo_root=repo_root).is_absolute(),
        "repo_path_refused": _path_refused(repo_root / ".saxo-token.json", repo_root),
        "sync_path_refused": _path_refused(Path.home() / "Desktop" / "saxo-token.json", repo_root),
    }
    passed = all(checks.values())
    published = write_scanned_json(
        out,
        {
            **base_event(
                "token-cache",
                "passed" if passed else "failed",
                "token cache path default and refusal rules checked",
            ),
            **checks,
        },
    )
    return 0 if passed and published else 1


def _safe_auth_status(payload: SaxoAuthStatus) -> dict[str, JsonValue]:
    return {
        "requested_environment": payload["requested_environment"],
        "effective_read_environment": payload["effective_read_environment"],
        "live_reads": payload["live_reads"],
        "live_writes": payload["live_writes"],
        "sim_credentials_present": payload["sim_credentials_present"],
        "sim_credential_source": payload["sim_credential_source"],
        "sim_redirect_uri_present": payload["sim_redirect_uri_present"],
        "pending_pkce_authorization_present": payload["pending_pkce_authorization_present"],
        "token_cache_present": payload["token_cache_present"],
        "token_cache_readable": payload["token_cache_readable"],
        "token_cache_expired": payload["token_cache_expired"],
        "token_cache_refresh_supported": payload["token_cache_refresh_supported"],
        "token_cache_environment": payload["token_cache_environment"],
        "scope_used": payload["scope_used"],
        "verifies": payload["verifies"],
        "does_not_verify": payload["does_not_verify"],
        "blocking_reasons": payload["blocking_reasons"],
        "next_action": payload["next_action"],
    }


def _machine_completion_blocker(
    capabilities: dict[str, JsonValue],
    start_result: dict[str, JsonValue],
) -> str:
    start_reason = start_result.get("reason")
    if isinstance(start_reason, str):
        return start_reason
    capabilities_reason = capabilities.get("reason")
    if isinstance(capabilities_reason, str):
        return capabilities_reason
    return "authorization_code_required"


def _missing_auth_material(
    auth: SaxoAuthStatus,
    capabilities: dict[str, JsonValue],
    start_result: dict[str, JsonValue],
) -> list[str]:
    missing: list[str] = []
    if not auth["sim_redirect_uri_present"]:
        missing.append("registered_redirect_uri")
    if not auth["token_cache_present"]:
        missing.append("valid_sim_token_cache_or_portal_token")
    elif not auth["token_cache_readable"]:
        missing.append("readable_sim_token_cache")

    start_status = start_result.get("status")
    capabilities_status = capabilities.get("status")
    if capabilities_status != "passed" and start_status != "authorization_url_ready":
        missing.append("authorization_code_from_saxo_redirect")
    elif capabilities_status != "passed":
        missing.append("completed_saxo_pkce_login")
    return missing


def _path_refused(path: Path, repo_root: Path) -> bool:
    try:
        token_cache_path(path, repo_root=repo_root)
    except TokenCachePathError:
        return True
    return False
