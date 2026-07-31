from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import (
    SourceContract,
    source_contract_fingerprint,
)
from saxo_bank_mcp.analytics_source_process import (
    SOURCE_MATRIX_CHILD_TOOLS,
    MatrixSession,
    ProcessSessionError,
)
from saxo_bank_mcp.endpoint_registry import find_registered_operation, load_inventory

if TYPE_CHECKING:
    from saxo_bank_mcp.analytics_source_process import RegisteredCallProfile
    from saxo_bank_mcp.qa_analytics_source_matrix import PreparedMatrix


_SOURCE_CONTRACT_ORDER: Final[tuple[str, ...]] = (
    "chart_v3",
    "reference_instruments_v1",
    "reference_instrument_details_v1",
    "options_chain_reference_v1",
    "info_price_v1",
    "info_prices_list_v1",
    "performance_summary_v4",
    "performance_timeseries_v4",
    "balances_v1",
    "positions_v1",
    "orders_v1",
    "transactions_v1",
    "bookings_v1",
    "closed_positions_history_v1",
    "exposure_instruments_v1",
    "costs_v1",
    "corporate_action_events_v2",
    "corporate_action_holdings_v2",
)
_STATE_PATHS: Final[tuple[str, ...]] = (
    "/port/v1/orders/me",
    "/port/v1/positions/me",
    "/port/v1/balances/me",
)


@dataclass(slots=True)
class ScriptedMatrixSession(MatrixSession):
    payloads: dict[str, deque[dict[str, JsonValue]]]
    tool_names: tuple[str, ...] = SOURCE_MATRIX_CHILD_TOOLS
    events: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=list)
    list_count: int = 0

    async def list_tools_once(self) -> tuple[str, ...]:
        self.list_count += 1
        if self.list_count != 1:
            raise ProcessSessionError("invalid_transition")
        self.events.append(("tools/list", {}))
        return self.tool_names

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        self.events.append((name, arguments))
        queue = self.payloads[name]
        if not queue:
            raise AssertionError(f"unexpected repeated tool call: {name}")
        return queue.popleft()


def canonical_digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8"),
    ).hexdigest()


def matrix_payloads(
    prepared: PreparedMatrix,
) -> dict[str, deque[dict[str, JsonValue]]]:
    contracts = {contract.contract_id: contract for contract in prepared.contracts}
    source_profiles = _source_profiles(prepared)
    registered_payloads = [_state_payload(path) for path in _STATE_PATHS]
    registered_payloads.extend(
        _source_payload(
            contracts[profile.analytics_contract_id],
            profile,
        )
        for profile in source_profiles
        if profile.analytics_contract_id is not None
    )
    registered_payloads.extend(_state_payload(path) for path in _STATE_PATHS)
    ledger_events = [_ledger_event() for _payload in registered_payloads]
    return {
        "saxo_auth_status": deque([_auth_payload()]),
        "saxo_list_registered_endpoints": deque(_registry_payloads(prepared)),
        "saxo_get_session_capabilities": deque([_session_payload()]),
        "saxo_get_entitlements": deque([_entitlements_payload()]),
        "saxo_get_safe_request_ledger": deque(
            [_ledger_clear_payload(), _ledger_readback_payload(ledger_events)],
        ),
        "saxo_call_registered_endpoint": deque(registered_payloads),
    }


def expected_matrix_events(
    prepared: PreparedMatrix,
) -> list[tuple[str, dict[str, JsonValue]]]:
    events: list[tuple[str, dict[str, JsonValue]]] = [
        ("tools/list", {}),
        ("saxo_auth_status", {}),
    ]
    events.extend(
        ("saxo_list_registered_endpoints", arguments)
        for arguments in _registry_arguments(prepared)
    )
    events.extend(
        (
            ("saxo_get_session_capabilities", {}),
            ("saxo_get_entitlements", {}),
            ("saxo_get_safe_request_ledger", {"clear": True}),
            ("claim", {}),
        ),
    )
    state_arguments = [_registered_arguments(path=path) for path in _STATE_PATHS]
    events.extend(("saxo_call_registered_endpoint", arguments) for arguments in state_arguments)
    contracts = {contract.contract_id: contract for contract in prepared.contracts}
    for contract_id in _SOURCE_CONTRACT_ORDER:
        request = prepared.requests[contract_id]
        if request is None:
            continue
        contract = contracts[contract_id]
        path = _resolved_path(contract, request)
        params = {
            key: _render_query_value(value)
            for key, value in request.items()
            if key in contract.query_parameters
        }
        events.append(
            (
                "saxo_call_registered_endpoint",
                _registered_arguments(
                    path=path,
                    params=params,
                    analytics_contract_id=contract_id,
                ),
            ),
        )
    events.extend(("saxo_call_registered_endpoint", arguments) for arguments in state_arguments)
    events.append(("saxo_get_safe_request_ledger", {}))
    return events


def _source_profiles(prepared: PreparedMatrix) -> tuple[RegisteredCallProfile, ...]:
    return tuple(
        profile
        for profile in prepared.call_policy.registered_calls
        if profile.analytics_contract_id is not None
    )


def _registry_arguments(prepared: PreparedMatrix) -> list[dict[str, JsonValue]]:
    inventory = load_inventory()
    groups = sorted(
        {
            operation.service_group
            for contract in prepared.contracts
            if (operation := find_registered_operation("GET", contract.path_template)) is not None
        }
        | {
            operation.service_group
            for path in _STATE_PATHS
            if (operation := find_registered_operation("GET", path)) is not None
        },
    )
    return [
        {"service_group": group, "limit": 100, "offset": offset}
        for group in groups
        for offset in tuple(range(0, inventory.service_group_counts[group], 100)) or (0,)
    ]


def _registry_payloads(prepared: PreparedMatrix) -> list[dict[str, JsonValue]]:
    inventory = load_inventory()
    payloads: list[dict[str, JsonValue]] = []
    for arguments in _registry_arguments(prepared):
        group = cast("str", arguments["service_group"])
        offset = cast("int", arguments["offset"])
        selected = [
            operation
            for operation in inventory.operations
            if operation.service_group == group
        ]
        page = selected[offset : offset + 100]
        next_offset = offset + 100 if offset + 100 < len(selected) else None
        payloads.append(
            {
                "status": "metadata_only_not_ready_for_trading",
                "network_call_made": False,
                "operations": [
                    {
                        "operation_id": operation.operation_id,
                        "service_group": operation.service_group,
                        "method": operation.method,
                        "path_template": operation.path_template,
                        "read_write_class": operation.read_write_class,
                        "mcp_support_policy": (
                            "read_only_definition_registered"
                            if operation.status == "implemented" and operation.method == "GET"
                            else "refused"
                        ),
                        "refusal_reason": operation.refusal_reason,
                    }
                    for operation in page
                ],
                "next_offset": next_offset,
                "returned_count": len(page),
            },
        )
    return payloads


def _auth_payload() -> dict[str, JsonValue]:
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


def _session_payload() -> dict[str, JsonValue]:
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


def _entitlements_payload() -> dict[str, JsonValue]:
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


def _state_payload(path: str) -> dict[str, JsonValue]:
    operation = find_registered_operation("GET", path)
    assert operation is not None
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


def _source_payload(
    contract: SourceContract,
    profile: RegisteredCallProfile,
) -> dict[str, JsonValue]:
    operation = find_registered_operation("GET", profile.path)
    assert operation is not None
    page_count = 2 if contract.contract_id == "chart_v3" else 1
    pages: list[dict[str, JsonValue]] = [
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
    request_fingerprint = _request_fingerprint(contract, profile.path, dict(profile.params))
    return {
        "status": "passed",
        "tool_name": "saxo_call_registered_endpoint",
        "call_class": "sim_read_succeeded",
        "operation_id": operation.operation_id,
        "service_group": operation.service_group,
        "method": "GET",
        "path": contract.path_template,
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
        "auth_exercised": operation.auth_requirement != "none",
        "trading_ready": False,
        "response_visibility": "analytics_contract_receipt",
        "response": None,
        "response_fingerprint": canonical_digest(pages),
        "response_fingerprint_scope": "analytics_contract_receipt",
        "analytics_contract_id": contract.contract_id,
        "analytics_contract_sha256": source_contract_fingerprint(contract),
        "page_count": page_count,
        "row_count": 1,
        "continuation_call_count": page_count - 1,
        "page_receipts": cast("list[JsonValue]", pages),
        "source_revision_fingerprint_sha256": canonical_digest(["e" * 64] * page_count),
        "timestamp_value_count": 1,
        "timestamp_fingerprint_sha256": canonical_digest(["9" * 64] * page_count),
        "request_fingerprint_sha256": request_fingerprint,
        "http_status": 200,
    }


def _request_fingerprint(
    contract: SourceContract,
    resolved_path: str,
    params: dict[str, str],
) -> str:
    request: dict[str, object] = dict(params)
    for template, resolved in zip(
        contract.path_template.strip("/").split("/"),
        resolved_path.strip("/").split("/"),
        strict=True,
    ):
        if template.startswith("{") and template.endswith("}"):
            request[template[1:-1]] = resolved
    return canonical_digest({"contract_id": contract.contract_id, "request": request})


def _ledger_clear_payload() -> dict[str, JsonValue]:
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


def _ledger_event() -> dict[str, JsonValue]:
    return {
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


def _ledger_readback_payload(
    events: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
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
        "events": cast("list[JsonValue]", events),
    }


def _registered_arguments(
    *,
    path: str,
    params: Mapping[str, str] | None = None,
    analytics_contract_id: str | None = None,
) -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {
        "method": "GET",
        "path": path,
        "response_mode": (
            "analytics_contract_receipt"
            if analytics_contract_id is not None
            else "fingerprint_only"
        ),
    }
    if params:
        arguments["params"] = dict(params)
    if analytics_contract_id is not None:
        arguments["analytics_contract_id"] = analytics_contract_id
    return arguments


def _resolved_path(contract: SourceContract, request: Mapping[str, object]) -> str:
    path = contract.path_template
    for parameter in contract.path_parameters:
        path = path.replace(f"{{{parameter}}}", str(request[parameter]))
    return path


def _render_query_value(value: object) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        items = cast("list[object] | tuple[object, ...]", value)
        return ",".join(str(item) for item in items)
    raise TypeError("unsupported scripted query value")
