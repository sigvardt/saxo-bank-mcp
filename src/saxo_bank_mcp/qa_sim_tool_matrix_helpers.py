from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Final, Literal
from urllib.parse import urlparse

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SIM_ENDPOINTS, SimAuthSettingsError, resolve_sim_auth_settings
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.mcp_token_state import CachedTokenReady, cached_token_for_tool
from saxo_bank_mcp.qa_order_probes import (
    FIXTURE_ASSET_TYPE,
    FIXTURE_INSTRUMENT,
    FIXTURE_LIMIT_PRICE,
    FIXTURE_MODIFIED_LIMIT_PRICE,
    FIXTURE_ORDER_AMOUNT,
    MULTILEG_FIXTURE_ASSET_TYPE,
    MULTILEG_FIXTURE_UICS,
    raw_open_orders_for_matrix,
)
from saxo_bank_mcp.qa_sim_tool_matrix_models import (
    DISCLAIMER_RETRY_DELAYS_SECONDS,
    FIXTURE_STREAM_UIC,
    HTTP_CLIENT_ERROR_MIN,
    RATE_LIMIT_CODES,
    RATE_LIMIT_STATUSES,
    MatrixFixtures,
    MatrixScenarioReceipt,
)
from saxo_bank_mcp.qa_trade_probes import (
    DisclaimerProbeInput,
    discover_pretrade_disclaimer_input,
)

JSON_OBJECT: Final = TypeAdapter(dict[str, JsonValue])
type MatrixClient = Client[FastMCPTransport]


async def call_tool(
    client: MatrixClient,
    tool: str,
    arguments: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    result = await client.call_tool(tool, arguments, raise_on_error=False)
    try:
        return JSON_OBJECT.validate_python(result.structured_content)
    except ValidationError:
        return {
            "status": "failed",
            "tool_name": tool,
            "raw_type": type(result.structured_content).__name__,
            "result_parsed": False,
        }


def receipt_for(
    tool: str,
    payload: Mapping[str, JsonValue],
    arguments: Mapping[str, JsonValue],
    *,
    status: Literal["completed", "expected_refusal"] = "completed",
) -> MatrixScenarioReceipt:
    mapping = dict(payload)
    return MatrixScenarioReceipt(
        tool=tool,
        status=status,
        network_call_made=bool(mapping.get("network_call_made")),
        hosts=hosts_of(mapping),
        request_digest=digest(dict(arguments)),
        response_digest=digest(mapping),
    )


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


async def discover_disclaimer_with_retry() -> DisclaimerProbeInput:
    last = await discover_pretrade_disclaimer_input()
    if last.real_disclaimer_input_found:
        return last
    for delay in DISCLAIMER_RETRY_DELAYS_SECONDS:
        if (
            last.precheck_http_status not in RATE_LIMIT_STATUSES
            and last.precheck_error_code not in RATE_LIMIT_CODES
        ):
            break
        await asyncio.sleep(delay)
        last = await discover_pretrade_disclaimer_input()
        if last.real_disclaimer_input_found:
            return last
    return last


async def validate_fixtures(fixtures: MatrixFixtures) -> bool:
    if not _fixture_values_match(fixtures):
        return False
    try:
        cache_path = resolve_sim_auth_settings(require_redirect=False).cache_path
    except SimAuthSettingsError:
        return False
    cache = cached_token_for_tool("saxo_sim_tool_matrix", cache_path)
    if not isinstance(cache, CachedTokenReady):
        return False
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {cache.token.access_token}",
    }
    try:
        async with create_async_client(base_url=SIM_ENDPOINTS.rest_base_url) as http:
            stock = await http.get(
                "ref/v1/instruments/details",
                params={"Uics": str(fixtures.stock_uic), "AssetTypes": FIXTURE_ASSET_TYPE},
                headers=headers,
            )
            options = await http.get(
                "ref/v1/instruments/details",
                params={
                    "Uics": ",".join(str(uic) for uic in fixtures.option_uics),
                    "AssetTypes": MULTILEG_FIXTURE_ASSET_TYPE,
                },
                headers=headers,
            )
            stream = await http.get(
                "ref/v1/instruments/details",
                params={"Uics": str(fixtures.stream_uic), "AssetTypes": "FxSpot"},
                headers=headers,
            )
    except OSError:
        return False
    return (
        stock.status_code < HTTP_CLIENT_ERROR_MIN
        and options.status_code < HTTP_CLIENT_ERROR_MIN
        and stream.status_code < HTTP_CLIENT_ERROR_MIN
    )


def _fixture_values_match(fixtures: MatrixFixtures) -> bool:
    return (
        fixtures.stock_uic == FIXTURE_INSTRUMENT
        and fixtures.amount == FIXTURE_ORDER_AMOUNT
        and fixtures.limit_price == FIXTURE_LIMIT_PRICE
        and fixtures.modified_limit_price == FIXTURE_MODIFIED_LIMIT_PRICE
        and fixtures.option_uics == MULTILEG_FIXTURE_UICS
        and fixtures.stream_uic == FIXTURE_STREAM_UIC
    )


async def state_fingerprint() -> dict[str, JsonValue]:
    empty: dict[str, JsonValue] = {
        "open_orders": {"count": 0, "ids_digest": digest([])},
        "positions_money": {"fingerprint": "account_bound"},
        "subscriptions": {"local": "empty"},
        "preview_write_state": {"fingerprint": "reset"},
    }
    try:
        cache_path = resolve_sim_auth_settings(require_redirect=False).cache_path
    except SimAuthSettingsError:
        unavailable: dict[str, JsonValue] = {
            **empty,
            "positions_money": {"fingerprint": "unavailable"},
        }
        return unavailable
    cache = cached_token_for_tool("saxo_sim_tool_matrix", cache_path)
    if not isinstance(cache, CachedTokenReady):
        return empty
    rows, _report = await raw_open_orders_for_matrix(tool_name="saxo_sim_tool_matrix")
    ids = sorted(
        {
            str(row.get("OrderId") or row.get("MultiLegOrderId") or "")
            for row in rows
            if row.get("OrderId") or row.get("MultiLegOrderId")
        }
    )
    result: dict[str, JsonValue] = {
        "open_orders": {"count": len(ids), "ids_digest": digest(ids)},
        "positions_money": {"fingerprint": "account_bound"},
        "subscriptions": {"local": "empty"},
        "preview_write_state": {"fingerprint": "reset"},
    }
    return result


def local_and_read_calls(
    fixtures: MatrixFixtures,
) -> list[tuple[str, dict[str, JsonValue]]]:
    return [
        ("saxo_health", {}),
        ("saxo_safety_status", {}),
        ("saxo_list_registered_endpoints", {"limit": 5}),
        ("saxo_list_trading_write_operations", {}),
        ("saxo_get_safe_request_ledger", {"clear": True}),
        ("saxo_get_entitlements", {}),
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
    ]


def write_preview_args(
    account_key: str,
    fixtures: MatrixFixtures,
) -> dict[str, JsonValue]:
    notional = float(fixtures.limit_price) * float(fixtures.amount)
    body: dict[str, JsonValue] = {
        "Uic": fixtures.stock_uic,
        "AssetType": FIXTURE_ASSET_TYPE,
        "Amount": fixtures.amount,
        "BuySell": "Buy",
        "OrderType": "Limit",
        "OrderPrice": fixtures.limit_price,
        "AccountKey": account_key,
        "ManualOrder": False,
        "OrderDuration": {"DurationType": "DayOrder"},
    }
    return {
        "operation_id": "post.trade.v2.orders",
        "account_key": account_key,
        "instrument_uic": fixtures.stock_uic,
        "quantity": fixtures.amount,
        "estimated_notional": notional,
        "account_currency": "USD",
        "risk": {
            "cost": notional,
            "cash_required": notional,
            "margin_impact": 0.0,
            "contract_multiplier": 1.0,
            "conversion_known": True,
        },
        "request_body": body,
    }


def order_preview_args(
    account_key: str,
    fixtures: MatrixFixtures,
) -> dict[str, JsonValue]:
    return {
        "order_body": {
            "Uic": fixtures.stock_uic,
            "AssetType": FIXTURE_ASSET_TYPE,
            "Amount": fixtures.amount,
            "BuySell": "Buy",
            "OrderType": "Limit",
            "OrderPrice": fixtures.limit_price,
            "AccountKey": account_key,
            "OrderDuration": {"DurationType": "DayOrder"},
            "ManualOrder": False,
        }
    }


def auth_lifecycle_calls() -> list[tuple[str, dict[str, JsonValue]]]:
    return [
        ("saxo_start_pkce_login", {"reveal_authorization_url": False}),
        (
            "saxo_exchange_pkce_code",
            {"code": "invalid-todo15-code", "state": "invalid-todo15-state"},
        ),
        (
            "saxo_cache_sim_access_token",
            {
                "access_token": "x" * 64,
                "expires_in_seconds": 60,
                "replace_existing_cache": False,
            },
        ),
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
    prefixes = ("sim_session", "account_", "fixture_", "disclaimer_context")
    return any(error.startswith(prefixes) for error in errors)
