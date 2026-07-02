from __future__ import annotations

from saxo_bank_mcp.live_mode import (
    LiveReadSettingsError,
    live_cached_token_for_tool,
    live_read_auth_required,
    live_session_error,
    resolve_live_read_settings,
)
from saxo_bank_mcp.mcp_tool_results import ToolResult, session_capabilities
from saxo_bank_mcp.session import (
    SESSION_CAPABILITIES_PATH,
    SessionRequestError,
    read_session_capabilities,
)


async def read_live_session_capabilities() -> ToolResult:
    try:
        settings = resolve_live_read_settings()
    except LiveReadSettingsError as error:
        return live_read_auth_required("saxo_get_session_capabilities", error.code)
    token_or_result = live_cached_token_for_tool(
        "saxo_get_session_capabilities",
        settings.cache_path,
    )
    if isinstance(token_or_result, dict):
        return token_or_result
    try:
        capabilities = await read_session_capabilities(settings, token_or_result)
    except SessionRequestError as error:
        return live_session_error("saxo_get_session_capabilities", error)
    return {
        "status": "passed",
        "tool_name": "saxo_get_session_capabilities",
        "requested_environment": "LIVE",
        "environment": "LIVE",
        "endpoint_path": SESSION_CAPABILITIES_PATH,
        "token_refreshed": False,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "account_identifiers_redacted": True,
        "capabilities": session_capabilities(capabilities),
        "verifies": ["cached LIVE bearer token can read current session capability fields"],
        "does_not_verify": [
            "order placement safety",
            "instrument/account suitability",
            "real-money approval",
            "live-write permission",
        ],
    }
