from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import (
    LIVE_ENDPOINTS,
    SIM_ENDPOINTS,
    SaxoRuntimeConfig,
    SimAuthSettings,
    SimAuthSettingsError,
    resolve_sim_auth_settings,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation
from saxo_bank_mcp.live_mode import LiveReadSettingsError
from saxo_bank_mcp.live_oauth_settings import resolve_live_oauth_settings
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    cached_token_for_tool,
)
from saxo_bank_mcp.read_tool_results import auth_required, live_auth_required
from saxo_bank_mcp.read_tool_types import (
    READ_DOES_NOT_VERIFY,
    ReadExecutionContext,
    ReadExecutionResult,
    ReadToolResult,
    ReadToolValue,
)

type LiveTokenLoader = Callable[
    [str, SimAuthSettings], Awaitable[SaxoTokenSet | Mapping[str, ReadToolValue]]
]


async def execution_context(
    operation: EndpointOperation,
    *,
    live_token_loader: LiveTokenLoader,
) -> ReadExecutionResult:
    runtime = SaxoRuntimeConfig.from_env()
    environment = runtime.effective_read_environment()
    if environment == "SIM":
        return _sim_execution_context(operation)
    if environment == "LIVE_READ_DISABLED":
        return _live_refusal(operation, runtime)
    return await _live_execution_context(operation, live_token_loader=live_token_loader)


def _sim_execution_context(operation: EndpointOperation) -> ReadExecutionResult:
    if operation.auth_requirement == "none":
        return ReadExecutionContext(
            environment="SIM",
            rest_base_url=SIM_ENDPOINTS.rest_base_url,
            token=None,
        )
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return auth_required(error.code)
    cache_check = cached_token_for_tool("saxo_call_registered_endpoint", settings.cache_path)
    if isinstance(cache_check, CachedTokenBlocked):
        return auth_required(str(cache_check.result.get("reason", "token_missing")))
    return ReadExecutionContext(
        environment="SIM",
        rest_base_url=SIM_ENDPOINTS.rest_base_url,
        token=cache_check.token,
    )


async def _live_execution_context(
    operation: EndpointOperation,
    *,
    live_token_loader: LiveTokenLoader,
) -> ReadExecutionResult:
    try:
        settings = resolve_live_oauth_settings()
    except LiveReadSettingsError as error:
        return live_auth_required(operation, error.code)
    if operation.auth_requirement == "none":
        return ReadExecutionContext(
            environment="LIVE",
            rest_base_url=LIVE_ENDPOINTS.rest_base_url,
            token=None,
        )
    token_or_result = await live_token_loader("saxo_call_registered_endpoint", settings)
    if not isinstance(token_or_result, SaxoTokenSet):
        return live_auth_required(
            operation,
            str(token_or_result.get("reason", "token_cache_missing")),
            network_call_made=bool(token_or_result.get("network_call_made", False)),
            missing_requirements=_string_list(token_or_result.get("missing_requirements")),
            next_action=_string_value(token_or_result.get("next_action")),
        )
    return ReadExecutionContext(
        environment="LIVE",
        rest_base_url=settings.rest_base_url,
        token=token_or_result,
    )


def _live_refusal(
    operation: EndpointOperation,
    runtime: SaxoRuntimeConfig,
) -> ReadToolResult:
    return {
        "status": "live_not_called",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "live_read_disabled",
        "operation_id": operation.operation_id,
        "method": operation.method,
        "path": operation.path_template,
        "requested_environment": runtime.requested_environment.value,
        "effective_read_environment": runtime.effective_read_environment(),
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "reason": "missing_live_read_enablement",
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": False,
        "trading_ready": False,
        "does_not_verify": list(READ_DOES_NOT_VERIFY),
    }


def read_headers(token: SaxoTokenSet | None) -> dict[str, str]:
    if token is None:
        return {"Accept": "application/json"}
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.access_token}",
    }


def _string_list(value: ReadToolValue | None) -> list[str] | None:
    if not isinstance(value, list):
        return None
    strings = [item for item in value if isinstance(item, str)]
    return strings if len(strings) == len(value) else None


def _string_value(value: ReadToolValue | None) -> str | None:
    return value if isinstance(value, str) else None
