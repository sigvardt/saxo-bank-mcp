from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from saxo_bank_mcp._evidence import JsonValue

LIVE_READ_SCENARIO_STATUSES: Final = {
    "saxo_get_session_capabilities": "passed",
    "saxo_get_entitlements": "passed",
    "saxo_list_registered_endpoints": "metadata_only_not_ready_for_trading",
    "saxo_call_registered_endpoint_public_diagnostics": "passed",
    "saxo_call_registered_endpoint_authenticated_account": "passed",
    "saxo_call_registered_endpoint_balances": "passed",
    "saxo_call_registered_endpoint_positions": "passed",
    "saxo_call_registered_endpoint_orders": "passed",
    "saxo_call_registered_endpoint_prices": "passed",
}
EXPECTED_NETWORK_CALL_COUNT: Final = 8
EXPECTED_LOCAL_REGISTRY_CALL_COUNT: Final = 1
HTTP_OK: Final = 200
_NETWORK_EXPECTATIONS: Final = {
    "saxo_get_session_capabilities": True,
    "saxo_get_entitlements": True,
    "saxo_list_registered_endpoints": False,
    "saxo_call_registered_endpoint_public_diagnostics": True,
    "saxo_call_registered_endpoint_authenticated_account": True,
    "saxo_call_registered_endpoint_balances": True,
    "saxo_call_registered_endpoint_positions": True,
    "saxo_call_registered_endpoint_orders": True,
    "saxo_call_registered_endpoint_prices": True,
}
_AUTH_EXPECTATIONS: Final = {
    "saxo_call_registered_endpoint_public_diagnostics": False,
    "saxo_call_registered_endpoint_authenticated_account": True,
    "saxo_call_registered_endpoint_balances": True,
    "saxo_call_registered_endpoint_positions": True,
    "saxo_call_registered_endpoint_orders": True,
    "saxo_call_registered_endpoint_prices": True,
}
_HTTP_STATUS_EXPECTATIONS: Final = frozenset(
    {
        "saxo_call_registered_endpoint_public_diagnostics",
        "saxo_call_registered_endpoint_authenticated_account",
        "saxo_call_registered_endpoint_balances",
        "saxo_call_registered_endpoint_positions",
        "saxo_call_registered_endpoint_orders",
        "saxo_call_registered_endpoint_prices",
    },
)
_SCENARIO_IDENTITIES: Final[dict[str, tuple[tuple[str, JsonValue], ...]]] = {
    "saxo_get_session_capabilities": (("tool_name", "saxo_get_session_capabilities"),),
    "saxo_get_entitlements": (("tool_name", "saxo_get_entitlements"),),
    "saxo_list_registered_endpoints": (("tool_name", "saxo_list_registered_endpoints"),),
    "saxo_call_registered_endpoint_public_diagnostics": (
        ("tool_name", "saxo_call_registered_endpoint"),
        ("operation_id", "get.root.v1.diagnostics.get"),
        ("method", "GET"),
        ("path", "/root/v1/diagnostics/get"),
        ("response_fingerprint_scope", "raw_response_body"),
    ),
    "saxo_call_registered_endpoint_authenticated_account": (
        ("tool_name", "saxo_call_registered_endpoint"),
        ("operation_id", "get.port.v1.accounts.me"),
        ("method", "GET"),
        ("path", "/port/v1/accounts/me"),
        ("response_fingerprint_scope", "raw_response_body"),
    ),
    "saxo_call_registered_endpoint_balances": (
        ("tool_name", "saxo_call_registered_endpoint"),
        ("operation_id", "get.port.v1.balances.me"),
        ("method", "GET"),
        ("path", "/port/v1/balances/me"),
        ("response_fingerprint_scope", "account_money_state_fields"),
    ),
    "saxo_call_registered_endpoint_positions": (
        ("tool_name", "saxo_call_registered_endpoint"),
        ("operation_id", "get.port.v1.positions.me"),
        ("method", "GET"),
        ("path", "/port/v1/positions/me"),
        ("response_fingerprint_scope", "raw_response_body"),
    ),
    "saxo_call_registered_endpoint_orders": (
        ("tool_name", "saxo_call_registered_endpoint"),
        ("operation_id", "get.port.v1.orders.me"),
        ("method", "GET"),
        ("path", "/port/v1/orders/me"),
        ("response_fingerprint_scope", "raw_response_body"),
    ),
    "saxo_call_registered_endpoint_prices": (
        ("tool_name", "saxo_call_registered_endpoint"),
        ("operation_id", "get.trade.v1.infoprices"),
        ("method", "GET"),
        ("path", "/trade/v1/infoprices"),
        ("response_fingerprint_scope", "raw_response_body"),
    ),
}


def live_read_transport_passed(
    payloads: Mapping[str, Mapping[str, JsonValue]],
) -> bool:
    if payloads.keys() != LIVE_READ_SCENARIO_STATUSES.keys():
        return False
    network_evidence = tuple(
        payload.get("network_call_made") for payload in payloads.values()
    )
    if (
        sum(value is True for value in network_evidence) != EXPECTED_NETWORK_CALL_COUNT
        or sum(value is False for value in network_evidence)
        != EXPECTED_LOCAL_REGISTRY_CALL_COUNT
    ):
        return False
    return _scenario_identities_passed(payloads) and all(
        payloads[name].get("status") == status
        for name, status in LIVE_READ_SCENARIO_STATUSES.items()
    ) and all(
        payloads[name].get("http_status") == HTTP_OK
        for name in _HTTP_STATUS_EXPECTATIONS
    ) and all(
        payloads.get(name, {}).get("network_call_made") is expected
        for name, expected in _NETWORK_EXPECTATIONS.items()
    ) and all(
        payloads.get(name, {}).get("auth_exercised") is expected
        for name, expected in _AUTH_EXPECTATIONS.items()
    )


def _scenario_identities_passed(
    payloads: Mapping[str, Mapping[str, JsonValue]],
) -> bool:
    return all(
        all(payloads[name].get(field) == expected for field, expected in identity)
        for name, identity in _SCENARIO_IDENTITIES.items()
    )
