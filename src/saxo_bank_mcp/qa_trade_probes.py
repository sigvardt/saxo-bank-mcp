from __future__ import annotations

import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

import anyio
import httpx2
from fastmcp import Client
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.config import SIM_ENDPOINTS, SimAuthSettingsError, resolve_sim_auth_settings
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.mcp_token_state import (
    CachedTokenBlocked,
    CachedTokenReady,
    cached_token_for_tool,
)
from saxo_bank_mcp.qa_account import resolve_sim_account_key
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.safety import TEST_APPROVAL_FACTOR, reset_safety_state
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.trade_preview import DISCLAIMER_RESPONSE_ENDPOINT_PATH

FIXTURE_ACCOUNT: Final = "SIM-ACCOUNT-1"
FIXTURE_INSTRUMENT: Final = 21
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])
JSON_VALUE_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
SYNTHETIC_DISCLAIMER_CONTEXT: Final = "fixture-context"
SYNTHETIC_DISCLAIMER_HANDLE: Final = "fixture-disclaimer-handle"
KNOWN_LOOKUP_FIXTURE_ERRORS: Final = frozenset(
    {"InValidDisclaimerToken", "InvalidDisclaimerToken", "DisclaimerTokenNotFound"},
)
KNOWN_RESPONSE_FIXTURE_ERRORS: Final = frozenset(
    {"DisclaimerFetchError", "DisclaimerTokenNotFound", "InvalidRequest"},
)
type DisclaimerInputSource = Literal[
    "real_precheck",
    "synthetic_invalid_token",
    "auth_unavailable",
]


@dataclass(frozen=True, slots=True)
class DisclaimerProbeInput:
    disclaimer_context: str
    disclaimer_token: str
    source: DisclaimerInputSource
    real_disclaimer_input_found: bool
    discovery_status: str
    discovery_network_call_made: bool
    precheck_http_status: int | None
    precheck_error_code: str
    candidate_count: int

    def to_safe_json(self) -> dict[str, JsonValue]:
        return {
            "source": self.source,
            "real_disclaimer_input_found": self.real_disclaimer_input_found,
            "discovery_status": self.discovery_status,
            "discovery_network_call_made": self.discovery_network_call_made,
            "precheck_http_status": self.precheck_http_status,
            "precheck_error_code": self.precheck_error_code,
            "candidate_count": self.candidate_count,
            "disclaimer_context_redacted": True,
            "disclaimer_token_redacted": True,
        }


@dataclass(frozen=True, slots=True)
class DisclaimerDiscoveryFallback:
    network_call_made: bool = False
    http_status: int | None = None
    error_code: str = ""
    candidate_count: int = 0


def handle_trade_precheck(out: Path) -> int:
    payload = anyio.run(_trade_precheck)
    return _write_redacted_with_secret_scan(out, payload, "passed")


def handle_trade_disclaimer_blocked(out: Path) -> int:
    payload = anyio.run(_trade_disclaimer_blocked)
    return _write_redacted_with_secret_scan(out, payload, "denied")


def handle_trade_multileg_defaults(out: Path) -> int:
    payload = anyio.run(_trade_multileg_defaults)
    return _write_redacted_with_secret_scan(out, payload, "incomplete_auth_required", "passed")


def handle_trade_disclaimer_lookup(out: Path) -> int:
    payload = anyio.run(_trade_disclaimer_lookup)
    return _write_redacted_with_secret_scan(
        out,
        payload,
        "exercised",
        "incomplete_auth_required",
        "passed",
    )


def handle_trade_disclaimer_response(out: Path) -> int:
    payload = anyio.run(_trade_disclaimer_response)
    return _write_redacted_with_secret_scan(
        out,
        payload,
        "exercised",
        "incomplete_auth_required",
        "passed",
    )


async def _trade_precheck() -> dict[str, JsonValue]:
    reset_safety_state()
    with _safety_env():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_create_order_preview",
                {
                    "order_body": _order_body(),
                    "precheck_response": _precheck_response(),
                    "disclaimer_response_state": "none",
                },
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    passed = payload.get("status") == "preview_created" and result.is_error is False
    return {
        **base_event(
            "trade-precheck",
            "passed" if passed else "failed",
            "FastMCP trade preview path exercised with no-secret pre-check fixture",
        ),
        "tool_name": "saxo_create_order_preview",
        "fastmcp_called": True,
        "mcp_is_error": result.is_error,
        "environment": "SIM",
        "precheck_endpoint": "/trade/v2/orders/precheck",
        "preview_created": payload.get("preview_created") is True,
        "preview_status": str(payload.get("status", "")),
        "account_key_redacted": True,
        "network_call_made": payload.get("network_call_made") is True,
        "fixture_precheck_used": True,
        "order_placed": payload.get("order_placed") is True,
        "order_modified": payload.get("order_modified") is True,
        "order_cancelled": payload.get("order_cancelled") is True,
        "live_write": payload.get("live_write") is True,
        "preview": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _trade_multileg_defaults() -> dict[str, JsonValue]:
    reset_safety_state()
    account = await resolve_sim_account_key(
        default_account_key=FIXTURE_ACCOUNT,
        tool_name="saxo_trade_multileg_defaults_qa",
    )
    with _safety_env(account.account_key):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_get_multileg_order_defaults",
                {
                    "account_key": account.account_key,
                    "option_root_id": 1,
                    "options_strategy_type": "Straddle",
                },
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    status = _auth_aware_status(payload)
    return {
        **base_event(
            "trade-multileg-defaults",
            status,
            "FastMCP multileg defaults lookup exercised with SIM fixture inputs",
        ),
        "tool_name": "saxo_get_multileg_order_defaults",
        "fastmcp_called": True,
        "mcp_is_error": result.is_error,
        "environment": "SIM",
        "endpoint_path": "/trade/v2/orders/multileg/defaults",
        "account_key_redacted": True,
        "account_resolution": account.to_safe_json(),
        "network_call_made": payload.get("network_call_made") is True,
        "defaults_returned": status == "passed",
        "order_placed": payload.get("order_placed") is True,
        "live_write": payload.get("live_write") is True,
        "result": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _trade_disclaimer_lookup() -> dict[str, JsonValue]:
    reset_safety_state()
    probe_input = await _discover_pretrade_disclaimer_input()
    with _safety_env():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_get_required_disclaimers",
                {"disclaimer_tokens": [probe_input.disclaimer_token]},
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    status = disclaimer_lookup_status(payload, probe_input)
    return {
        **base_event(
            "trade-disclaimer-lookup",
            status,
            "FastMCP required-disclaimer lookup exercised with discovered or synthetic token",
        ),
        "tool_name": "saxo_get_required_disclaimers",
        "fastmcp_called": True,
        "mcp_is_error": result.is_error,
        "environment": "SIM",
        "endpoint_path": "/dm/v2/disclaimers",
        "input_discovery": probe_input.to_safe_json(),
        "disclaimer_tokens_redacted": True,
        "network_call_made": payload.get("network_call_made") is True,
        "disclaimers_returned": status == "passed",
        "happy_path_verified": status == "passed",
        "safe_fixture_exercised": status == "exercised",
        "completion_claim_allowed": status in {"passed", "exercised"},
        "coverage_limitation": _disclaimer_coverage_limitation(status, probe_input),
        "order_placed": payload.get("order_placed") is True,
        "live_write": payload.get("live_write") is True,
        "result": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _trade_disclaimer_response() -> dict[str, JsonValue]:
    reset_safety_state()
    probe_input = await _discover_pretrade_disclaimer_input()
    with _safety_env():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_register_disclaimer_response",
                {
                    "disclaimer_context": probe_input.disclaimer_context,
                    "disclaimer_token": probe_input.disclaimer_token,
                    "response_type": "Accepted",
                    "approval_factor": TEST_APPROVAL_FACTOR,
                },
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    status = disclaimer_response_status(payload, probe_input)
    return {
        **base_event(
            "trade-disclaimer-response",
            status,
            "FastMCP disclaimer response exercised with test-only SIM approval factor",
        ),
        "tool_name": "saxo_register_disclaimer_response",
        "fastmcp_called": True,
        "mcp_is_error": result.is_error,
        "environment": "SIM",
        "endpoint_path": DISCLAIMER_RESPONSE_ENDPOINT_PATH,
        "input_discovery": probe_input.to_safe_json(),
        "approval_factor_mode": "test_only_sim",
        "approval_factor_redacted": True,
        "disclaimer_token_redacted": True,
        "network_call_made": payload.get("network_call_made") is True,
        "disclaimer_response_submitted": payload.get("disclaimer_response_submitted") is True,
        "happy_path_verified": status == "passed",
        "safe_fixture_exercised": status == "exercised",
        "completion_claim_allowed": disclaimer_response_completion_claim_allowed(status),
        "coverage_limitation": _disclaimer_coverage_limitation(status, probe_input),
        "order_placed": payload.get("order_placed") is True,
        "live_write": payload.get("live_write") is True,
        "result": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


async def _trade_disclaimer_blocked() -> dict[str, JsonValue]:
    reset_safety_state()
    with _safety_env():
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_create_order_preview",
                {
                    "order_body": _order_body(),
                    "precheck_response": {
                        **_precheck_response(),
                        "PreTradeDisclaimers": {
                            "DisclaimerContext": "fixture-context",
                            "DisclaimerTokens": ["fixture-token"],
                        },
                    },
                    "disclaimer_details": _blocking_disclaimer_details(),
                    "disclaimer_response_state": "missing",
                },
                raise_on_error=False,
            )
    payload = _payload(result.structured_content)
    raw_reasons = payload.get("denial_reasons", [])
    reasons = [str(value) for value in raw_reasons] if isinstance(raw_reasons, list) else []
    denied = payload.get("status") == "denied" and "blocking_disclaimer" in reasons
    return {
        **base_event(
            "trade-disclaimer-blocked",
            "denied" if denied else "failed",
            "FastMCP trade preview refused required or blocking disclaimer",
        ),
        "tool_name": "saxo_create_order_preview",
        "fastmcp_called": True,
        "mcp_is_error": result.is_error,
        "denial_reasons": reasons,
        "disclaimer_context_present": payload.get("disclaimer_context_present") is True,
        "disclaimer_tokens_count": payload.get("disclaimer_tokens_count", 0),
        "disclaimer_details_sanitized": payload.get("disclaimer_details_sanitized") is True,
        "exact_disclaimer_content_present": (
            payload.get("exact_disclaimer_content_present") is True
        ),
        "response_endpoint_path": payload.get("response_endpoint_path", ""),
        "network_call_made": payload.get("network_call_made") is True,
        "disclaimer_response_submitted": payload.get("disclaimer_response_submitted") is True,
        "preview_created": payload.get("preview_created") is True,
        "order_placed": payload.get("order_placed") is True,
        "order_modified": payload.get("order_modified") is True,
        "order_cancelled": payload.get("order_cancelled") is True,
        "live_write": payload.get("live_write") is True,
        "preview": payload,
        "git": current_git_state().model_dump(mode="json"),
    }


def _order_body() -> dict[str, JsonValue]:
    return {
        "AccountKey": FIXTURE_ACCOUNT,
        "Uic": FIXTURE_INSTRUMENT,
        "AssetType": "Stock",
        "Amount": 10,
        "BuySell": "Buy",
        "OrderType": "Market",
        "OrderDuration": {"DurationType": "DayOrder"},
        "ContractMultiplier": 1,
    }


def _precheck_response() -> dict[str, JsonValue]:
    return {
        "PreCheckResult": "Ok",
        "EstimatedCashRequired": 500,
        "EstimatedCashRequiredCurrency": "USD",
        "EstimatedTotalCostInAccountCurrency": 500,
        "InstrumentToAccountConversionRate": 1,
        "CostInAccountCurrency": {"Amount": 500},
        "MarginImpactBuySell": {"MarginImpact": 20},
    }


def _blocking_disclaimer_details() -> dict[str, JsonValue]:
    return {
        "Data": [
            {
                "Body": "Trading this instrument requires exchange rules acceptance.",
                "Conditions": [{"Type": "Checkbox", "Label": "I understand"}],
                "DisclaimerToken": "fixture-token",
                "IsBlocking": True,
                "ResponseOptions": [{"ResponseType": "Accepted", "Label": "I accept"}],
            },
        ],
    }


def _payload(value: object) -> dict[str, JsonValue]:
    return JSON_OBJECT_ADAPTER.validate_python(value)


async def _discover_pretrade_disclaimer_input() -> DisclaimerProbeInput:
    try:
        settings = resolve_sim_auth_settings(require_redirect=False)
    except SimAuthSettingsError as error:
        return _synthetic_disclaimer_input("auth_unavailable", error.code)
    cache_check = cached_token_for_tool("saxo_create_order_preview", settings.cache_path)
    match cache_check:
        case CachedTokenReady(token=token):
            return await _try_discover_pretrade_disclaimer_input(token.access_token)
        case CachedTokenBlocked(result=result):
            return _synthetic_disclaimer_input(
                "auth_unavailable",
                str(result.get("reason", "token_missing")),
            )


async def _try_discover_pretrade_disclaimer_input(
    access_token: str,
) -> DisclaimerProbeInput:
    account = await resolve_sim_account_key(
        default_account_key=FIXTURE_ACCOUNT,
        tool_name="saxo_create_order_preview",
    )
    last_http_status: int | None = None
    last_error_code = ""
    network_call_made = False
    headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as client:
            for candidate in _disclaimer_discovery_candidates(account.account_key):
                response = await client.post(
                    "trade/v2/orders/precheck",
                    json=candidate,
                    headers=headers,
                )
                network_call_made = True
                last_http_status = response.status_code
                payload = _response_json(response)
                found = _first_disclaimer_input(payload)
                if found is not None:
                    return DisclaimerProbeInput(
                        disclaimer_context=found[0],
                        disclaimer_token=found[1],
                        source="real_precheck",
                        real_disclaimer_input_found=True,
                        discovery_status="real_disclaimer_input_found",
                        discovery_network_call_made=True,
                        precheck_http_status=response.status_code,
                        precheck_error_code="",
                        candidate_count=len(_DISCOVERY_CANDIDATES),
                    )
                last_error_code = _error_code(payload)
    except httpx2.HTTPError as error:
        return _synthetic_disclaimer_input(
            "synthetic_invalid_token",
            type(error).__name__,
            fallback=DisclaimerDiscoveryFallback(
                network_call_made=True,
                http_status=last_http_status,
                candidate_count=len(_DISCOVERY_CANDIDATES),
            ),
        )
    return _synthetic_disclaimer_input(
        "synthetic_invalid_token",
        "real_precheck_disclaimer_not_available",
        fallback=DisclaimerDiscoveryFallback(
            network_call_made=network_call_made,
            http_status=last_http_status,
            error_code=last_error_code,
            candidate_count=len(_DISCOVERY_CANDIDATES),
        ),
    )


def _synthetic_disclaimer_input(
    source: DisclaimerInputSource,
    status: str,
    *,
    fallback: DisclaimerDiscoveryFallback | None = None,
) -> DisclaimerProbeInput:
    effective_fallback = DisclaimerDiscoveryFallback() if fallback is None else fallback
    return DisclaimerProbeInput(
        disclaimer_context=SYNTHETIC_DISCLAIMER_CONTEXT,
        disclaimer_token=SYNTHETIC_DISCLAIMER_HANDLE,
        source=source,
        real_disclaimer_input_found=False,
        discovery_status=status,
        discovery_network_call_made=effective_fallback.network_call_made,
        precheck_http_status=effective_fallback.http_status,
        precheck_error_code=effective_fallback.error_code,
        candidate_count=effective_fallback.candidate_count,
    )


def _disclaimer_discovery_candidates(account_key: str) -> tuple[dict[str, JsonValue], ...]:
    return tuple(
        {
            "AccountKey": account_key,
            "Uic": uic,
            "AssetType": asset_type,
            "Amount": 1,
            "BuySell": "Buy",
            "OrderType": "Market",
            "OrderDuration": {"DurationType": "DayOrder"},
            "FieldGroups": ["PreTradeDisclaimers"],
        }
        for uic, asset_type in _DISCOVERY_CANDIDATES
    )


def _response_json(response: httpx2.Response) -> JsonValue:
    if not response.content:
        return None
    try:
        return JSON_VALUE_ADAPTER.validate_python(response.json())
    except ValueError:
        return None


def _first_disclaimer_input(value: JsonValue) -> tuple[str, str] | None:
    if isinstance(value, Mapping):
        found = _disclaimer_input_from_container(value.get("PreTradeDisclaimers"))
        if found is not None:
            return found
        for child in value.values():
            found = _first_disclaimer_input(child)
            if found is not None:
                return found
        return None
    if isinstance(value, str) or not isinstance(value, Sequence):
        return None
    for child in value:
        found = _first_disclaimer_input(child)
        if found is not None:
            return found
    return None


def _disclaimer_input_from_container(value: object) -> tuple[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    container = cast("Mapping[str, object]", value)
    context: object = container.get("DisclaimerContext")
    tokens: object = container.get("DisclaimerTokens")
    if not isinstance(context, str) or not context.strip():
        return None
    if isinstance(tokens, str) or not isinstance(tokens, Sequence):
        return None
    tokens_sequence = cast("Sequence[object]", tokens)
    for token in tokens_sequence:
        if isinstance(token, str) and token.strip():
            return context.strip(), token.strip()
    return None


def disclaimer_lookup_status(
    payload: dict[str, JsonValue],
    probe_input: DisclaimerProbeInput,
) -> str:
    if payload.get("status") == "auth_required":
        return "incomplete_auth_required"
    if payload.get("status") == "passed":
        return "passed"
    if (
        probe_input.source == "synthetic_invalid_token"
        and payload.get("status") == "http_error"
        and _error_code(payload) in KNOWN_LOOKUP_FIXTURE_ERRORS
    ):
        return "exercised"
    return str(payload.get("status", "failed"))


def disclaimer_response_status(
    payload: dict[str, JsonValue],
    probe_input: DisclaimerProbeInput,
) -> str:
    if payload.get("status") == "auth_required":
        return "incomplete_auth_required"
    if payload.get("status") == "passed":
        return "passed"
    if (
        probe_input.source == "synthetic_invalid_token"
        and payload.get("status") == "http_error"
        and _error_code(payload) in KNOWN_RESPONSE_FIXTURE_ERRORS
    ):
        return "exercised"
    return str(payload.get("status", "failed"))


def disclaimer_response_completion_claim_allowed(status: str) -> bool:
    return status == "passed"


def _disclaimer_coverage_limitation(
    status: str,
    probe_input: DisclaimerProbeInput,
) -> str:
    if status == "passed":
        return ""
    if status == "exercised":
        return (
            "No real outstanding SIM pre-trade disclaimer token/context was available; "
            "the target tool was exercised against Saxo with a synthetic invalid token and "
            "must not be represented as a successful user consent."
        )
    if probe_input.source == "auth_unavailable":
        return "SIM auth was unavailable, so the target tool stopped before network."
    return "Disclaimer tool did not reach a completion oracle."


def _error_code(value: JsonValue) -> str:
    if not isinstance(value, Mapping):
        return ""
    direct = value.get("ErrorCode")
    if isinstance(direct, str):
        return direct
    response = value.get("response")
    if isinstance(response, Mapping):
        nested = response.get("ErrorCode")
        if isinstance(nested, str):
            return nested
        error_info = response.get("ErrorInfo")
        if isinstance(error_info, Mapping):
            nested = error_info.get("ErrorCode")
            if isinstance(nested, str):
                return nested
    error_info = value.get("ErrorInfo")
    if isinstance(error_info, Mapping):
        nested = error_info.get("ErrorCode")
        if isinstance(nested, str):
            return nested
    return ""


def _auth_aware_status(payload: dict[str, JsonValue]) -> str:
    return (
        "incomplete_auth_required"
        if payload.get("status") == "auth_required"
        else str(
            payload.get("status", "failed"),
        )
    )


def _write_redacted_with_secret_scan(
    out: Path,
    payload: dict[str, JsonValue],
    *success_statuses: str,
) -> int:
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise TypeError("trade probe redaction returned non-object")
    write_json(out, redacted)
    findings, scan_errors = scan_secret_paths([str(out)])
    redacted["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, redacted)
    clean = not findings and not scan_errors
    return 0 if redacted.get("status") in success_statuses and clean else 1


@contextmanager
def _safety_env(account_key: str = FIXTURE_ACCOUNT) -> Generator[None]:
    previous = {key: os.environ.get(key) for key in _SAFETY_ENV_DEFAULTS}
    try:
        for key, value in _SAFETY_ENV_DEFAULTS.items():
            os.environ[key] = value
        os.environ["SAXO_MCP_ACCOUNT_ALLOWLIST"] = account_key
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


_SAFETY_ENV_DEFAULTS: Final = {
    "SAXO_MCP_ENVIRONMENT": "SIM",
    "SAXO_MCP_ACCOUNT_ALLOWLIST": FIXTURE_ACCOUNT,
    "SAXO_MCP_INSTRUMENT_ALLOWLIST": str(FIXTURE_INSTRUMENT),
}
_DISCOVERY_CANDIDATES: Final[tuple[tuple[int, str], ...]] = (
    (211, "CfdOnStock"),
    (211, "Stock"),
    (21, "FxSpot"),
)
