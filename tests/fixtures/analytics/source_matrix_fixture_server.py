from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Final, NamedTuple, cast

SOURCE_MATRIX_CHILD_TOOLS: Final[tuple[str, ...]] = (
    "saxo_auth_status",
    "saxo_list_registered_endpoints",
    "saxo_get_session_capabilities",
    "saxo_get_entitlements",
    "saxo_get_safe_request_ledger",
    "saxo_call_registered_endpoint",
)

FIXED_CALL_SEQUENCE: Final[tuple[str, ...]] = (
    "saxo_auth_status",
    "saxo_list_registered_endpoints",
    "saxo_get_session_capabilities",
    "saxo_get_entitlements",
    "saxo_get_safe_request_ledger:clear",
    "saxo_call_registered_endpoint:before:orders",
    "saxo_call_registered_endpoint:before:positions",
    "saxo_call_registered_endpoint:before:balances",
    "saxo_call_registered_endpoint:sources",
    "saxo_call_registered_endpoint:after:orders",
    "saxo_call_registered_endpoint:after:positions",
    "saxo_call_registered_endpoint:after:balances",
    "saxo_get_safe_request_ledger:readback",
)

_REGISTRY_GROUPS: Final[tuple[str, ...]] = (
    "Account History",
    "Chart",
    "Client Services",
    "Corporate Actions",
    "Portfolio",
    "Reference Data",
    "Trading",
)
_STATE_PATHS: Final[tuple[str, ...]] = (
    "/port/v1/orders/me",
    "/port/v1/positions/me",
    "/port/v1/balances/me",
)
_STATE_CALL_COUNT: Final = len(_STATE_PATHS)


class _Operation(NamedTuple):
    contract_id: str | None
    operation_id: str
    service_group: str
    path_template: str
    contract_sha256: str | None


_OPERATIONS: Final[tuple[_Operation, ...]] = (
    _Operation(
        "chart_v3",
        "get.chart.v3.charts",
        "Chart",
        "/chart/v3/charts",
        "29f480ba97c45faac440876272afd3c865afcb558c5a9ee9c089d26d1ed46d09",
    ),
    _Operation(
        "reference_instruments_v1",
        "get.ref.v1.instruments",
        "Reference Data",
        "/ref/v1/instruments",
        "b1365658ef4c2a940bfeead25df274985dd3ab6ea022e0b03ab088ae8bcf96dc",
    ),
    _Operation(
        "reference_instrument_details_v1",
        "get.ref.v1.instruments.details",
        "Reference Data",
        "/ref/v1/instruments/details",
        "de4ed595d8a425af7dacd4ef3ed385d7133f079bc4ab45f5deb15190dda24583",
    ),
    _Operation(
        "options_chain_reference_v1",
        "get.ref.v1.instruments.contractoptionspaces.optionrootid",
        "Reference Data",
        "/ref/v1/instruments/contractoptionspaces/{OptionRootId}",
        "f6b783ceffc019792df2e18037ec08566b589679583b5d62e684fba52cfd2a1e",
    ),
    _Operation(
        "info_price_v1",
        "get.trade.v1.infoprices",
        "Trading",
        "/trade/v1/infoprices",
        "ff5581f8fedb7f25b50f955243820f7e28d2e31af0c4ea90b509041ca83b0328",
    ),
    _Operation(
        "info_prices_list_v1",
        "get.trade.v1.infoprices.list",
        "Trading",
        "/trade/v1/infoprices/list",
        "11361483cdf653fb640bc0263ae21adc6e4116d5f5e815bc64686b719740b8e2",
    ),
    _Operation(
        "performance_summary_v4",
        "get.hist.v4.performance.summary",
        "Account History",
        "/hist/v4/performance/summary",
        "0a7a19e0062d53d56efc9eb5fb2458a16061dfe2d0afb6ebdc2c0da70c0b9124",
    ),
    _Operation(
        "performance_timeseries_v4",
        "get.hist.v4.performance.timeseries",
        "Account History",
        "/hist/v4/performance/timeseries",
        "6b54f1bc6a0297f34346b28f944247bb7ae0b607451867e60c24142b1787b2da",
    ),
    _Operation(
        "balances_v1",
        "get.port.v1.balances",
        "Portfolio",
        "/port/v1/balances",
        "45d48bcbc3a9db82e8c51a08e4097d88c26fd19e1059da68b9d55dc4907919b0",
    ),
    _Operation(
        "positions_v1",
        "get.port.v1.positions",
        "Portfolio",
        "/port/v1/positions",
        "933eba148e0b94c9c47036d2f9f2f8308b840ca363f2f630ec068be9c09dd3b7",
    ),
    _Operation(
        "orders_v1",
        "get.port.v1.orders",
        "Portfolio",
        "/port/v1/orders",
        "b9c0d424635b7b3cbb341883663fb2ac69da17cbdd793111c0470bd7ceb097af",
    ),
    _Operation(
        "transactions_v1",
        "get.hist.v1.transactions",
        "Account History",
        "/hist/v1/transactions",
        "987ead400d61152c94987a81cad56ea48412252de9b5a33e35970410b4e11f20",
    ),
    _Operation(
        "bookings_v1",
        "get.cs.v1.reports.bookings.clientkey",
        "Client Services",
        "/cs/v1/reports/bookings/{ClientKey}",
        "997a8cfe470211298a38c5feaeec1ed4cd378025982f2ce5136336bfc0b73003",
    ),
    _Operation(
        "closed_positions_history_v1",
        "get.cs.v1.reports.closedpositions.clientkey.fromdate.todate",
        "Client Services",
        "/cs/v1/reports/closedPositions/{ClientKey}/{FromDate}/{ToDate}",
        "776b13724942b99786e1120cbf6c98ff1e1f283456c1688a040ab43bba52d70c",
    ),
    _Operation(
        "exposure_instruments_v1",
        "get.port.v1.exposure.instruments",
        "Portfolio",
        "/port/v1/exposure/instruments",
        "274a4bd77850f69364ed1651cf3dfa519d18abd65d8c6899fb0308fb1e5b6d03",
    ),
    _Operation(
        "costs_v1",
        "get.cs.v1.tradingconditions.cost.accountkey.uic.assettype",
        "Client Services",
        "/cs/v1/tradingconditions/cost/{AccountKey}/{Uic}/{AssetType}",
        "4e9c6f727f3507b5820e517a2e3c14117dd54dd845255ba844e3933fee1a8f5b",
    ),
    _Operation(
        "corporate_action_events_v2",
        "get.ca.v2.events",
        "Corporate Actions",
        "/ca/v2/events",
        "605e5e1c0fd324bf1022bfd4d51a84c5c5fbe7302a7c034309367a4e4b3f1e46",
    ),
    _Operation(
        "corporate_action_holdings_v2",
        "get.ca.v2.holdings",
        "Corporate Actions",
        "/ca/v2/holdings",
        "a1a493c1d9c930243542ee8d81855fe33fb00e0403ebbf77320426949f1f2de2",
    ),
    _Operation(
        None,
        "get.port.v1.orders.me",
        "Portfolio",
        "/port/v1/orders/me",
        None,
    ),
    _Operation(
        None,
        "get.port.v1.positions.me",
        "Portfolio",
        "/port/v1/positions/me",
        None,
    ),
    _Operation(
        None,
        "get.port.v1.balances.me",
        "Portfolio",
        "/port/v1/balances/me",
        None,
    ),
)


class _SourceCall(NamedTuple):
    contract_id: str
    resolved_path: str
    params: dict[str, str]


_SOURCE_CALLS: Final[tuple[_SourceCall, ...]] = (
    _SourceCall(
        "chart_v3",
        "/chart/v3/charts",
        {"AssetType": "Stock", "Count": "2", "Uic": "211"},
    ),
    _SourceCall(
        "reference_instruments_v1",
        "/ref/v1/instruments",
        {"$top": "10", "AssetTypes": "Stock", "Uics": "211"},
    ),
    _SourceCall(
        "reference_instrument_details_v1",
        "/ref/v1/instruments/details",
        {"$top": "10", "AssetTypes": "Stock", "Uics": "211"},
    ),
    _SourceCall(
        "options_chain_reference_v1",
        "/ref/v1/instruments/contractoptionspaces/120",
        {},
    ),
    _SourceCall(
        "info_price_v1",
        "/trade/v1/infoprices",
        {"Amount": "1", "AssetType": "Stock", "Uic": "211"},
    ),
    _SourceCall(
        "info_prices_list_v1",
        "/trade/v1/infoprices/list",
        {"Amount": "1", "AssetType": "Stock", "Uics": "211"},
    ),
    _SourceCall(
        "performance_summary_v4",
        "/hist/v4/performance/summary",
        {"StandardPeriod": "Year"},
    ),
    _SourceCall(
        "performance_timeseries_v4",
        "/hist/v4/performance/timeseries",
        {"StandardPeriod": "Year"},
    ),
    _SourceCall("balances_v1", "/port/v1/balances", {}),
    _SourceCall("positions_v1", "/port/v1/positions", {"$top": "100"}),
    _SourceCall("orders_v1", "/port/v1/orders", {"$top": "100"}),
    _SourceCall(
        "transactions_v1",
        "/hist/v1/transactions",
        {"$top": "100"},
    ),
    _SourceCall(
        "exposure_instruments_v1",
        "/port/v1/exposure/instruments",
        {"AssetType": "Stock", "Uic": "211"},
    ),
    _SourceCall(
        "corporate_action_events_v2",
        "/ca/v2/events",
        {"$top": "100"},
    ),
    _SourceCall(
        "corporate_action_holdings_v2",
        "/ca/v2/holdings",
        {"$top": "100"},
    ),
)

_OPERATION_BY_CONTRACT: Final = {
    operation.contract_id: operation
    for operation in _OPERATIONS
    if operation.contract_id is not None
}
_OPERATION_BY_PATH: Final = {operation.path_template: operation for operation in _OPERATIONS}


def resolve_eval_tool_filter(env: Mapping[str, str]) -> frozenset[str]:
    if (
        env.get("SAXO_MCP_EVAL_TOOL_FILTER") != "1"
        or env.get("SAXO_MCP_ENVIRONMENT") != "SIM"
        or env.get("SAXO_MCP_ENABLE_LIVE_READS") != "0"
        or env.get("SAXO_MCP_ENABLE_LIVE_WRITES") != ""
    ):
        raise ValueError("fixture filter invalid")
    raw = env.get("SAXO_MCP_EVAL_ALLOWED_TOOLS", "")
    tools = tuple(raw.split(","))
    if not tools or any(not tool for tool in tools) or len(set(tools)) != len(tools):
        raise ValueError("fixture filter invalid")
    return frozenset(tools)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _auth_payload() -> dict[str, object]:
    return {
        "status": "passed",
        "tool_name": "saxo_auth_status",
        "call_class": "local_status_succeeded",
        "requested_environment": "SIM",
        "effective_read_environment": "SIM",
        "live_reads": False,
        "live_writes": False,
        "sim_credentials_present": True,
        "sim_credential_source": "env",
        "live_credentials_present": False,
        "sim_redirect_uri_present": False,
        "pending_pkce_authorization_present": False,
        "token_cache_present": True,
        "token_cache_readable": True,
        "token_cache_expired": False,
        "token_cache_refresh_supported": False,
        "token_cache_environment": "SIM",
        "scope_used": False,
        "verifies": [],
        "does_not_verify": [],
        "blocking_reasons": [],
        "next_action": "test readiness action",
        "network_call_made": False,
        "live_write_called": False,
        "order_or_subscription_created": False,
    }


def _session_payload() -> dict[str, object]:
    return {
        "status": "passed",
        "tool_name": "saxo_get_session_capabilities",
        "environment": "SIM",
        "call_class": "sim_read_succeeded",
        "endpoint_path": "/root/v1/sessions/capabilities",
        "token_refreshed": False,
        "token": {
            "has_access_token": True,
            "has_refresh_token": False,
            "has_code_verifier": False,
            "environment": "SIM",
            "expires_at": "2099-01-01T00:00:00+00:00",
            "is_expired": False,
        },
        "token_refresh_supported": False,
        "scope_used": False,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "capabilities": {
            "AuthenticationLevel": "Strong",
            "DataLevel": "Full",
            "TradeLevel": "None",
        },
        "next_action": "use current capability fields only",
        "verifies": [
            "cached SIM bearer token can read current session capability fields",
        ],
        "does_not_verify": [
            "order placement safety",
            "instrument/account suitability",
            "real-money approval",
            "live endpoint access",
        ],
    }


def _entitlements_payload() -> dict[str, object]:
    return {
        "status": "passed",
        "tool_name": "saxo_get_entitlements",
        "environment": "SIM",
        "call_class": "sim_read_succeeded",
        "endpoint_path": "/port/v1/users/me/entitlements",
        "entitlement_field_set": "Default",
        "token_refreshed": False,
        "network_call_made": True,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "entitlement_summary": {
            "exchange_count": 0,
            "max_rows": 1000,
            "response_count": 0,
            "has_next_page": False,
            "possibly_truncated": False,
        },
        "exchange_ids": [],
        "entitlement_bucket_counts": {
            "DelayedFullBook": 0,
            "DelayedGreeks": 0,
            "Greeks": 0,
            "RealTimeFullBook": 0,
            "RealTimeTopOfBook": 0,
        },
        "verifies": [
            "cached SIM bearer token can read current market-data entitlement summary",
        ],
        "does_not_verify": [
            "price availability for a specific instrument",
            "quote recency or real-time price delivery for any instrument",
            "order placement safety",
            "instrument/account suitability",
            "real-money approval",
            "live endpoint access",
        ],
    }


def _registry_payload(group: str) -> dict[str, object]:
    operations = [
        {
            "operation_id": operation.operation_id,
            "service_group": operation.service_group,
            "method": "GET",
            "path_template": operation.path_template,
            "read_write_class": "read",
            "mcp_support_policy": "read_only_definition_registered",
            "refusal_reason": "",
        }
        for operation in _OPERATIONS
        if operation.service_group == group
    ]
    return {
        "status": "metadata_only_not_ready_for_trading",
        "network_call_made": False,
        "operations": operations,
        "next_offset": None,
        "returned_count": len(operations),
    }


def _ledger_clear_payload() -> dict[str, object]:
    return {
        "status": "cleared",
        "tool_name": "saxo_get_safe_request_ledger",
        "scope": "current_mcp_session",
        "safe_fields_only": True,
        "ledger_complete": True,
        "events_evicted": 0,
        "negative_proof_available": True,
        "request_count": 0,
        "non_get_request_count": 0,
        "unsafe_gateway_request_detected": False,
        "order_placement_endpoint_called": False,
        "events": [],
    }


def _ledger_readback_payload() -> dict[str, object]:
    event: dict[str, object] = {
        "timestamp": "2026-07-31T12:00:00+00:00",
        "phase": "attempted",
        "host_role": "gateway",
        "environment": "SIM",
        "method": "GET",
        "path": "/sim/openapi/{redacted}",
        "query_names": [],
        "query_present": False,
        "status": None,
    }
    events: list[dict[str, object]] = [event.copy() for _index in range(21)]
    return {
        "status": "passed",
        "tool_name": "saxo_get_safe_request_ledger",
        "scope": "current_mcp_session",
        "safe_fields_only": True,
        "ledger_complete": True,
        "events_evicted": 0,
        "negative_proof_available": True,
        "request_count": len(events),
        "non_get_request_count": 0,
        "unsafe_gateway_request_detected": False,
        "order_placement_endpoint_called": False,
        "events": events,
    }


def _state_payload(path: str) -> dict[str, object]:
    operation = _OPERATION_BY_PATH[path]
    return {
        "status": "passed",
        "operation_id": operation.operation_id,
        "method": "GET",
        "path": operation.path_template,
        "environment": "SIM",
        "network_call_made": True,
        "response_visibility": "fingerprint_only",
        "response": None,
        "response_fingerprint": "a" * 64,
        "response_fingerprint_scope": (
            "account_money_state_fields"
            if path == "/port/v1/balances/me"
            else "raw_response_body"
        ),
        "http_status": 200,
    }


def _validated_source_params(source: _SourceCall, arguments: dict[str, object]) -> dict[str, str]:
    raw_params = arguments.get("params", {})
    if not isinstance(raw_params, dict):
        raise ValueError("fixture source arguments invalid")  # noqa: TRY004
    params: dict[str, str] = {}
    for key, value in cast("dict[object, object]", raw_params).items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("fixture source arguments invalid")  # noqa: TRY004
        params[key] = value
    if source.contract_id != "transactions_v1":
        if params != source.params:
            raise ValueError("fixture source arguments invalid")
        return params
    if set(params) != {"$top", "FromDate", "ToDate"} or params["$top"] != "100":
        raise ValueError("fixture source arguments invalid")
    try:
        from_date = date.fromisoformat(params["FromDate"])
        to_date = date.fromisoformat(params["ToDate"])
    except ValueError:
        raise ValueError("fixture source arguments invalid") from None
    if to_date != datetime.now(tz=UTC).date() or to_date - from_date != timedelta(days=365):
        raise ValueError("fixture source arguments invalid")
    return params


def _object_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in cast("dict[object, object]", value).items():
        if not isinstance(key, str):
            return None
        result[key] = item
    return result


def _source_payload(source: _SourceCall, arguments: dict[str, object]) -> dict[str, object]:
    params = _validated_source_params(source, arguments)
    operation = _OPERATION_BY_CONTRACT[source.contract_id]
    request: dict[str, str] = dict(params)
    if source.contract_id == "options_chain_reference_v1":
        request["OptionRootId"] = "120"
    page_count = 2 if source.contract_id == "chart_v3" else 1
    pages = [
        {
            "page_number": page_number,
            "row_count": 1 if page_number == 1 else 0,
            "page_fingerprint_sha256": f"{page_number:x}" * 64,
            "source_revision_sha256": "e" * 64,
            "schema_fingerprint_sha256": "f" * 64,
            "timestamp_value_count": 1 if page_number == 1 else 0,
            "timestamp_fingerprint_sha256": "9" * 64,
        }
        for page_number in range(1, page_count + 1)
    ]
    request_fingerprint = _canonical_digest(
        {"contract_id": source.contract_id, "request": request},
    )
    return {
        "status": "passed",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "sim_read_succeeded",
        "operation_id": operation.operation_id,
        "service_group": operation.service_group,
        "method": "GET",
        "path": operation.path_template,
        "environment": "SIM",
        "network_call_made": True,
        "network_call_count": page_count,
        "attempt_count": page_count,
        "initial_attempt_count": 1,
        "continuation_attempt_count": page_count - 1,
        "retry_count": 0,
        "distinct_target_count": page_count,
        "successful_page_count": page_count,
        "live_write_called": False,
        "order_or_subscription_created": False,
        "arbitrary_url_allowed": False,
        "live_write": False,
        "live_access": False,
        "auth_exercised": True,
        "trading_ready": False,
        "response_visibility": "analytics_contract_receipt",
        "response": None,
        "response_fingerprint": _canonical_digest(pages),
        "response_fingerprint_scope": "analytics_contract_receipt",
        "analytics_contract_id": source.contract_id,
        "analytics_contract_sha256": operation.contract_sha256,
        "page_count": page_count,
        "row_count": 1,
        "continuation_call_count": page_count - 1,
        "page_receipts": pages,
        "source_revision_fingerprint_sha256": _canonical_digest(["e" * 64] * page_count),
        "timestamp_value_count": 1,
        "timestamp_fingerprint_sha256": _canonical_digest(["9" * 64] * page_count),
        "request_fingerprint_sha256": request_fingerprint,
        "http_status": 200,
    }


def _require_arguments(actual: dict[str, object], expected: dict[str, object]) -> None:
    if actual != expected:
        raise ValueError("fixture arguments invalid")


def _dispatch_auth(
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    if name != "saxo_auth_status":
        raise ValueError("fixture sequence invalid")
    _require_arguments(arguments, {})
    return _auth_payload()


def _dispatch_prelude(
    index: int,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    registry_index = index - 1
    if 0 <= registry_index < len(_REGISTRY_GROUPS):
        group = _REGISTRY_GROUPS[registry_index]
        if name != "saxo_list_registered_endpoints":
            raise ValueError("fixture sequence invalid")
        _require_arguments(
            arguments,
            {"service_group": group, "limit": 100, "offset": 0},
        )
        return _registry_payload(group)
    capabilities_index = 1 + len(_REGISTRY_GROUPS)
    if index == capabilities_index:
        if name != "saxo_get_session_capabilities":
            raise ValueError("fixture sequence invalid")
        _require_arguments(arguments, {})
        return _session_payload()
    if index == capabilities_index + 1:
        if name != "saxo_get_entitlements":
            raise ValueError("fixture sequence invalid")
        _require_arguments(arguments, {})
        return _entitlements_payload()
    if index == capabilities_index + 2:
        if name != "saxo_get_safe_request_ledger":
            raise ValueError("fixture sequence invalid")
        _require_arguments(arguments, {"clear": True})
        return _ledger_clear_payload()
    raise ValueError("fixture sequence invalid")


def _dispatch_tool_call(
    index: int,
    name: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    prelude_count = 1 + len(_REGISTRY_GROUPS) + 3
    if index == 0:
        return _dispatch_auth(name, arguments)
    if index < prelude_count:
        return _dispatch_prelude(index, name, arguments)
    registered_index = index - prelude_count
    if registered_index < _STATE_CALL_COUNT:
        return _dispatch_state(name, arguments, registered_index)
    source_index = registered_index - _STATE_CALL_COUNT
    if source_index < len(_SOURCE_CALLS):
        return _dispatch_source(name, arguments, _SOURCE_CALLS[source_index])
    after_index = source_index - len(_SOURCE_CALLS)
    if after_index < _STATE_CALL_COUNT:
        return _dispatch_state(name, arguments, after_index)
    if after_index == _STATE_CALL_COUNT:
        if name != "saxo_get_safe_request_ledger":
            raise ValueError("fixture sequence invalid")
        _require_arguments(arguments, {})
        return _ledger_readback_payload()
    raise ValueError("fixture sequence invalid")


def _dispatch_state(
    name: str,
    arguments: dict[str, object],
    state_index: int,
) -> dict[str, object]:
    if name != "saxo_call_registered_endpoint":
        raise ValueError("fixture sequence invalid")
    path = _STATE_PATHS[state_index]
    _require_arguments(
        arguments,
        {"method": "GET", "path": path, "response_mode": "fingerprint_only"},
    )
    return _state_payload(path)


def _dispatch_source(
    name: str,
    arguments: dict[str, object],
    source: _SourceCall,
) -> dict[str, object]:
    if name != "saxo_call_registered_endpoint":
        raise ValueError("fixture sequence invalid")
    expected_keys = {"method", "path", "response_mode", "analytics_contract_id"}
    if source.params or source.contract_id == "transactions_v1":
        expected_keys.add("params")
    if set(arguments) != expected_keys:
        raise ValueError("fixture source arguments invalid")
    if (
        arguments.get("method") != "GET"
        or arguments.get("path") != source.resolved_path
        or arguments.get("response_mode") != "analytics_contract_receipt"
        or arguments.get("analytics_contract_id") != source.contract_id
    ):
        raise ValueError("fixture source arguments invalid")
    return _source_payload(source, arguments)


class _FixtureServer:
    def __init__(self) -> None:
        self._initialized = False
        self._initialized_notification = False
        self._listed = False
        self._call_index = 0

    @property
    def complete(self) -> bool:
        expected_calls = (
            1
            + len(_REGISTRY_GROUPS)
            + 2
            + 1
            + _STATE_CALL_COUNT
            + len(_SOURCE_CALLS)
            + _STATE_CALL_COUNT
            + 1
        )
        return self._listed and self._call_index == expected_calls

    def handle(self, message: dict[str, object]) -> dict[str, object] | None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params")
        if method == "initialize":
            return self._initialize(request_id, params)
        if method == "notifications/initialized":
            self._initialized_notify(request_id)
            return None
        if method == "tools/list":
            return self._list_tools(request_id)
        if method != "tools/call" or not self._listed or request_id is None:
            raise ValueError("fixture protocol invalid")
        return self._call_tool(request_id, params)

    def _initialize(
        self,
        request_id: object,
        params: object,
    ) -> dict[str, object]:
        typed_params = _object_mapping(params)
        if self._initialized or typed_params is None or request_id is None:
            raise ValueError("fixture protocol invalid")
        protocol_version = typed_params.get("protocolVersion")
        if not isinstance(protocol_version, str):
            raise TypeError("fixture protocol invalid")
        self._initialized = True
        result: dict[str, object] = {
            "protocolVersion": protocol_version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sealed-source-matrix-fixture", "version": "1"},
        }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _initialized_notify(self, request_id: object) -> None:
        if not self._initialized or self._initialized_notification or request_id is not None:
            raise ValueError("fixture protocol invalid")
        self._initialized_notification = True

    def _list_tools(self, request_id: object) -> dict[str, object]:
        if not self._initialized_notification or self._listed or request_id is None:
            raise ValueError("fixture protocol invalid")
        self._listed = True
        result = {
            "tools": [
                {
                    "name": name,
                    "description": "sealed deterministic fixture",
                    "inputSchema": {"type": "object", "additionalProperties": True},
                }
                for name in SOURCE_MATRIX_CHILD_TOOLS
            ],
        }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call_tool(
        self,
        request_id: object,
        params: object,
    ) -> dict[str, object]:
        typed_params = _object_mapping(params)
        if typed_params is None or set(typed_params) != {"name", "arguments"}:
            raise ValueError("fixture protocol invalid")
        name = typed_params.get("name")
        arguments = _object_mapping(typed_params.get("arguments"))
        if not isinstance(name, str) or arguments is None:
            raise TypeError("fixture protocol invalid")
        payload = _dispatch_tool_call(self._call_index, name, arguments)
        self._call_index += 1
        result: dict[str, object] = {
            "content": [],
            "structuredContent": payload,
            "isError": False,
        }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _write_json(value: object) -> None:
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        if resolve_eval_tool_filter(os.environ) != frozenset(SOURCE_MATRIX_CHILD_TOOLS):
            return 64
    except ValueError:
        return 64
    server = _FixtureServer()
    try:
        for line in sys.stdin.buffer:
            message = _object_mapping(json.loads(line.decode("utf-8", errors="strict")))
            if message is None:
                return 65
            response = server.handle(message)
            if response is not None:
                _write_json(response)
    except (KeyError, TypeError, ValueError):
        return 65
    return 0 if server.complete else 65


if __name__ == "__main__":
    raise SystemExit(main())
