from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlparse

import anyio
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.qa_sim_tool_matrix_models import (
    FIXTURE_ASSET_TYPE,
    FIXTURE_INSTRUMENT,
    FIXTURE_LIMIT_PRICE,
    FIXTURE_MODIFIED_LIMIT_PRICE,
    FIXTURE_ORDER_AMOUNT,
    FIXTURE_STREAM_UIC,
    MULTILEG_FIXTURE_ASSET_TYPE,
    MULTILEG_FIXTURE_UICS,
    MatrixFixtures,
    MatrixScenarioReceipt,
)

JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])
type MatrixClient = Client[FastMCPTransport]


@dataclass(frozen=True, slots=True)
class MatrixToolObservation:
    payload: dict[str, JsonValue]
    result_parsed: bool
    result_state: str
    mcp_is_error: bool
    timed_out: bool = False


async def call_tool(
    client: MatrixClient,
    tool: str,
    arguments: dict[str, JsonValue],
    *,
    timeout_seconds: float | None = None,
) -> MatrixToolObservation:
    result = None
    if timeout_seconds is None:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
    else:
        with anyio.move_on_after(timeout_seconds) as scope:
            result = await client.call_tool(tool, arguments, raise_on_error=False)
        if scope.cancel_called:
            return MatrixToolObservation(
                payload={"status": "timed_out"},
                result_parsed=False,
                result_state="timed_out",
                mcp_is_error=True,
                timed_out=True,
            )
    if result is None:
        return MatrixToolObservation(
            payload={"status": "failed"},
            result_parsed=False,
            result_state="failed",
            mcp_is_error=True,
        )
    try:
        payload = JSON_OBJECT.validate_python(result.structured_content)
    except ValidationError:
        return MatrixToolObservation(
            payload={"status": "unparsed"},
            result_parsed=False,
            result_state="unparsed",
            mcp_is_error=True,
        )
    state = _safe_result_state(payload.get("status"))
    return MatrixToolObservation(
        payload=payload,
        result_parsed=True,
        result_state=state,
        mcp_is_error=result.is_error is True,
    )


def receipt_for(
    tool: str,
    result: MatrixToolObservation | Mapping[str, JsonValue],
    arguments: Mapping[str, JsonValue],
    *,
    status: Literal["completed", "expected_refusal", "reconciled", "failed"] = "completed",
) -> MatrixScenarioReceipt:
    observation = _observation(result)
    mapping = observation.payload
    return MatrixScenarioReceipt(
        tool=tool,
        status=status,
        result_parsed=observation.result_parsed,
        result_state=observation.result_state,
        mcp_is_error=observation.mcp_is_error,
        network_call_made=bool(mapping.get("network_call_made")),
        hosts=hosts_of(mapping),
        request_digest=digest(dict(arguments)),
        response_digest=digest(mapping),
    )


def _observation(
    result: MatrixToolObservation | Mapping[str, JsonValue],
) -> MatrixToolObservation:
    if isinstance(result, MatrixToolObservation):
        return result
    payload = dict(result)
    parsed = payload.get("result_parsed") is not False
    state = _safe_result_state(payload.get("status")) if parsed else "unparsed"
    failure_states = {
        "auth_required",
        "blocked",
        "denied",
        "failed",
        "invalid_arguments",
        "invalid_request",
        "network_error",
        "refused",
        "timed_out",
        "unparsed",
        "unsupported",
    }
    return MatrixToolObservation(
        payload=payload,
        result_parsed=parsed,
        result_state=state,
        mcp_is_error=payload.get("mcp_is_error") is True or state in failure_states,
        timed_out=state == "timed_out",
    )


def _safe_result_state(value: JsonValue | None) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().lower().replace("-", "_")
    if not normalized or not normalized.replace("_", "").isalnum():
        return "unknown"
    return normalized[:128]


def digest(value: JsonValue) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode(),
    ).hexdigest()


def hosts_of(value: JsonValue) -> tuple[str, ...]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            if key in {"host", "hostname"} and isinstance(item, str):
                found.append(item)
            else:
                found.extend(hosts_of(item))
        return tuple(found)
    if isinstance(value, list):
        found_list: list[str] = []
        for item in value:
            found_list.extend(hosts_of(item))
        return tuple(found_list)
    if isinstance(value, str) and "saxobank.com" in value:
        host = urlparse(value).hostname
        return (host,) if host else ()
    return ()


def live_transport_events(value: JsonValue) -> int:
    """Count only real LIVE transport, never SIM refusals labeled environment=LIVE."""
    if isinstance(value, list):
        return sum(live_transport_events(item) for item in value)
    if not isinstance(value, dict):
        return 0
    count = 0
    network = value.get("network_call_made") is True
    for host in hosts_of(value):
        lowered = host.lower()
        if lowered.startswith("live.") or lowered == "live.logonvalidation.net":
            count += 1
        if network and lowered == "gateway.saxobank.com" and value.get("environment") == "LIVE":
            count += 1
    if network and str(value.get("environment", "")).upper() == "LIVE":
        count += 1
    for item in value.values():
        count += live_transport_events(item)
    return count


def needs_recovery(auth_payload: Mapping[str, JsonValue]) -> bool:
    auth = auth_payload.get("auth")
    if not isinstance(auth, Mapping):
        return auth_payload.get("status") == "auth_required"
    return bool(auth.get("token_cache_expired")) or bool(auth.get("blocking_reasons"))


def fixture_values_match(fixtures: MatrixFixtures) -> bool:
    return (
        fixtures.stock_uic == FIXTURE_INSTRUMENT
        and fixtures.amount == FIXTURE_ORDER_AMOUNT
        and fixtures.limit_price == FIXTURE_LIMIT_PRICE
        and fixtures.modified_limit_price == FIXTURE_MODIFIED_LIMIT_PRICE
        and fixtures.option_uics == MULTILEG_FIXTURE_UICS
        and fixtures.stream_uic == FIXTURE_STREAM_UIC
    )


def account_discovery_call() -> tuple[str, dict[str, JsonValue]]:
    return (
        "saxo_call_registered_endpoint",
        {"method": "GET", "path": "/port/v1/accounts/me", "params": {}},
    )


def fixture_read_calls(fixtures: MatrixFixtures) -> tuple[tuple[str, dict[str, JsonValue]], ...]:
    return (
        (
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": "/ref/v1/instruments/details",
                "params": {
                    "Uics": str(fixtures.stock_uic),
                    "AssetTypes": FIXTURE_ASSET_TYPE,
                },
            },
        ),
        (
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": "/ref/v1/instruments/details",
                "params": {
                    "Uics": ",".join(str(uic) for uic in fixtures.option_uics),
                    "AssetTypes": MULTILEG_FIXTURE_ASSET_TYPE,
                },
            },
        ),
        (
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": "/ref/v1/instruments/details",
                "params": {"Uics": str(fixtures.stream_uic), "AssetTypes": "FxSpot"},
            },
        ),
        (
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": "/trade/v1/infoprices",
                "params": {
                    "Amount": format(fixtures.amount, "g"),
                    "AssetType": FIXTURE_ASSET_TYPE,
                    "Uic": str(fixtures.stock_uic),
                },
            },
        ),
    )


def state_read_calls() -> tuple[tuple[str, str, dict[str, JsonValue]], ...]:
    registered = "saxo_call_registered_endpoint"
    return (
        (
            "balances",
            registered,
            {
                "method": "GET",
                "path": "/port/v1/balances/me",
                "params": {},
                "response_mode": "fingerprint_only",
            },
        ),
        (
            "positions",
            registered,
            {"method": "GET", "path": "/port/v1/positions/me", "params": {}},
        ),
        (
            "orders",
            registered,
            {"method": "GET", "path": "/port/v1/orders/me", "params": {}},
        ),
        (
            "trade_messages",
            registered,
            {"method": "GET", "path": "/trade/v1/messages", "params": {}},
        ),
        ("subscriptions", "saxo_safety_status", {}),
        ("previews_write_state", "saxo_safety_status", {}),
        ("jobs", "saxo_list_analytics_storage", {"scope": {}}),
        ("caches", "saxo_list_analytics_storage", {"scope": {}}),
        ("temporary_files", "saxo_list_analytics_storage", {"scope": {}}),
    )


def safe_account_selectors(payload: Mapping[str, JsonValue]) -> tuple[str, ...]:
    response = payload.get("response")
    if isinstance(response, str):
        try:
            response = JSON_OBJECT.validate_python(json.loads(response))
        except (json.JSONDecodeError, ValidationError):
            return ()
    found: list[str] = []
    _collect_safe_account_selectors(response, found)
    return tuple(sorted(set(found)))


def _collect_safe_account_selectors(value: JsonValue | None, found: list[str]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_safe_account_selectors(item, found)
        return
    if not isinstance(value, Mapping):
        return
    selector = value.get("SafeAccountSelector")
    if isinstance(selector, str) and selector.startswith("proc-acct-"):
        found.append(selector)
    for item in value.values():
        _collect_safe_account_selectors(item, found)


def local_and_read_calls(
    _fixtures: MatrixFixtures,
) -> list[tuple[str, dict[str, JsonValue]]]:
    return [
        ("saxo_health", {}),
        ("saxo_safety_status", {}),
        ("saxo_list_registered_endpoints", {"limit": 5}),
        ("saxo_list_trading_write_operations", {}),
        ("saxo_get_safe_request_ledger", {"clear": True}),
        ("saxo_get_entitlements", {}),
    ]


def auth_lifecycle_calls() -> list[tuple[str, dict[str, JsonValue]]]:
    rejection: dict[str, JsonValue] = {"__qa_schema_extra_rejection__": True}
    return [
        ("saxo_start_pkce_login", dict(rejection)),
        ("saxo_exchange_pkce_code", dict(rejection)),
        ("saxo_cache_sim_access_token", dict(rejection)),
    ]


def refusal_arguments(tool: str) -> dict[str, JsonValue]:
    if tool == "saxo_precheck_live_order":
        return {
            "order": {
                "uic": FIXTURE_INSTRUMENT,
                "asset_type": "Stock",
                "amount": 1,
                "buy_sell": "Buy",
            }
        }
    return {}


def is_blocker(errors: list[str]) -> bool:
    prefixes = ("sim_session", "account_", "fixture_", "disclaimer_")
    return any(error.startswith(prefixes) for error in errors)
