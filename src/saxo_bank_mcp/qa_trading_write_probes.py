from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

import anyio
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.qa_account import resolve_sim_account_key
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.trading_write_registry import TradingWriteSpec, trading_write_specs

type JsonObject = dict[str, JsonValue]

STOCK_UIC: Final = 211
OPTION_UICS: Final = (30004846, 30004926)
OPTION_ROOT_ID: Final = 120
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])
_SUBSCRIPTION_CLEANUP: Final[dict[str, str]] = {
    "post.trade.v1.infoprices.subscriptions": (
        "delete.trade.v1.infoprices.subscriptions.contextid.referenceid"
    ),
    "post.trade.v1.messages.subscriptions": (
        "delete.trade.v1.messages.subscriptions.contextid.referenceid"
    ),
    "post.trade.v1.optionschain.subscriptions": (
        "delete.trade.v1.optionschain.subscriptions.contextid.referenceid"
    ),
    "post.trade.v1.prices.multileg.subscriptions": (
        "delete.trade.v1.prices.multileg.subscriptions.contextid.referenceid"
    ),
    "post.trade.v1.prices.subscriptions": (
        "delete.trade.v1.prices.subscriptions.contextid.referenceid"
    ),
}


def handle_trading_write_matrix(out: Path) -> int:
    payload = anyio.run(_trading_write_matrix)
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        return 1
    redacted["secret_scan"] = {"findings": [], "scan_errors": []}
    return 0 if write_scanned_json(out, redacted) and payload["status"] == "passed" else 1


async def _trading_write_matrix() -> JsonObject:
    account = await resolve_sim_account_key(
        default_account_key="SIM-ACCOUNT-1",
        tool_name="saxo_trading_write_matrix_qa",
    )
    rows: list[JsonValue] = []
    with (
        tempfile.TemporaryDirectory(prefix="saxo-mcp-write-qa-") as audit_dir,
        _safety_environment(account.account_key, Path(audit_dir)),
    ):
        async with Client(mcp) as client:
            rows.extend(
                [
                    await _exercise_spec(client, spec, account.account_key)
                    for spec in trading_write_specs()
                ],
            )

    generic_rows = [row for row in rows if isinstance(row, dict) and not row["delegated"]]
    network_rows = [row for row in generic_rows if row.get("network_call_made") is True]
    prepared_rows = [row for row in generic_rows if row.get("preview_status") == "preview_created"]
    delegated_rows = [row for row in rows if isinstance(row, dict) and row["delegated"]]
    complete_transport = len(network_rows) == len(generic_rows) == len(prepared_rows)
    semantic_success_count = sum(
        1 for row in generic_rows if row.get("execution_status") == "completed"
    )
    complete_semantics = semantic_success_count == len(generic_rows)
    return {
        **base_event(
            "trading-write-matrix",
            "passed" if complete_transport else "failed",
            "Every registered Saxo Trading write was delegated or called through FastMCP in SIM",
        ),
        "environment": "SIM",
        "registered_operation_count": len(rows),
        "delegated_order_operation_count": len(delegated_rows),
        "generic_operation_count": len(generic_rows),
        "prepared_operation_count": len(prepared_rows),
        "network_exercised_operation_count": len(network_rows),
        "semantic_success_count": semantic_success_count,
        "all_generic_routes_reached_saxo": complete_transport,
        "transport_coverage_complete": complete_transport,
        "semantic_completion_claim_allowed": complete_semantics,
        "completion_claim_allowed": complete_semantics,
        "live_write": False,
        "rows": rows,
        "account_resolution": account.to_safe_json(),
    }


async def _exercise_spec(
    client: Client[FastMCPTransport],
    spec: TradingWriteSpec,
    account_key: str,
) -> JsonObject:
    if spec.specialized_tool is not None:
        return {
            "operation_id": spec.operation_id,
            "delegated": True,
            "specialized_tool": spec.specialized_tool,
            "network_call_made": False,
        }
    context_id = f"qa-{uuid.uuid4().hex[:16]}"
    reference_id = f"ref-{uuid.uuid4().hex[:16]}"
    arguments = _arguments(spec, account_key, context_id, reference_id)
    prepared_result = await client.call_tool(
        "saxo_prepare_trading_write",
        arguments,
        raise_on_error=False,
    )
    prepared = _payload(prepared_result.structured_content)
    token = prepared.get("preview_token")
    if not isinstance(token, str):
        return _row(spec, prepared, {})
    executed_result = await client.call_tool(
        "saxo_execute_trading_write",
        {"preview_token": token},
        raise_on_error=False,
    )
    executed = _payload(executed_result.structured_content)
    cleanup = await _cleanup_subscription(
        client,
        spec,
        account_key,
        context_id,
        reference_id,
        executed,
    )
    return _row(spec, prepared, executed, cleanup)


async def _cleanup_subscription(  # noqa: PLR0913
    client: Client[FastMCPTransport],
    spec: TradingWriteSpec,
    account_key: str,
    context_id: str,
    reference_id: str,
    executed: JsonObject,
) -> JsonObject:
    cleanup_operation = _SUBSCRIPTION_CLEANUP.get(spec.operation_id)
    if cleanup_operation is None or executed.get("status") != "completed":
        return {"cleanup_required": False}
    cleanup_spec = next(
        row for row in trading_write_specs() if row.operation_id == cleanup_operation
    )
    prepared_result = await client.call_tool(
        "saxo_prepare_trading_write",
        _arguments(cleanup_spec, account_key, context_id, reference_id),
        raise_on_error=False,
    )
    prepared = _payload(prepared_result.structured_content)
    token = prepared.get("preview_token")
    if not isinstance(token, str):
        return {"cleanup_required": True, "cleanup_status": "preview_failed"}
    result = await client.call_tool(
        "saxo_execute_trading_write",
        {"preview_token": token},
        raise_on_error=False,
    )
    payload = _payload(result.structured_content)
    return {
        "cleanup_required": True,
        "cleanup_status": str(payload.get("status", "failed")),
        "cleanup_network_call_made": payload.get("network_call_made") is True,
    }


def _arguments(
    spec: TradingWriteSpec,
    account_key: str,
    context_id: str,
    reference_id: str,
) -> JsonObject:
    path_values = {
        "AllocationKeyId": "999999999",
        "ContextId": context_id,
        "MessageId": "qa-message-not-found",
        "PositionId": "999999999",
        "PositionIds": "999999999",
        "ReferenceId": reference_id,
    }
    query_values: JsonObject = {
        "AccountKey": account_key,
        "AssetType": "Stock",
        "MessageIds": "qa-message-not-found",
        "Tag": "qa",
        "Uic": STOCK_UIC,
    }
    uic = OPTION_UICS[0] if spec.service == "Positions" else STOCK_UIC
    payload: JsonObject = {
        "operation_id": spec.operation_id,
        "path_parameters": {
            name: path_values.get(name, "999999999") for name in spec.path_parameter_names
        },
        "query_parameters": {
            name: query_values[name]
            for name in spec.query_parameter_names
            if name in query_values
        },
        "request_body": _request_body(spec.operation_id, account_key, context_id, reference_id),
    }
    if spec.risk == "money_moving":
        payload.update(
            {
                "account_key": account_key,
                "instrument_uic": uic,
                "quantity": 1,
                "estimated_notional": 100,
            },
        )
    return payload


def _request_body(  # noqa: C901, PLR0911
    operation_id: str,
    account_key: str,
    context_id: str,
    reference_id: str,
) -> JsonObject:
    subscription = {"ContextId": context_id, "ReferenceId": reference_id}
    if operation_id == "post.trade.v1.allocationkeys":
        return {
            "AllocationKeyName": f"qa-{uuid.uuid4().hex[:12]}",
            "AllocationUnitType": "Percentage",
            "MarginHandling": "Reduce",
            "OneTime": True,
            "OwnerAccountKey": account_key,
            "ParticipatingAccountsInfo": [
                {
                    "AcceptRemainderAmount": True,
                    "AccountKey": account_key,
                    "Priority": 1,
                    "UnitValue": 100,
                },
            ],
        }
    if operation_id == "post.trade.v1.messages.subscriptions":
        return {**subscription, "Arguments": {"MessageTypes": ["TradeConfirmation"]}}
    if "optionschain" in operation_id:
        return {
            **subscription,
            "Arguments": {
                "AccountKey": account_key,
                "AssetType": "StockIndexOption",
                "Identifier": OPTION_ROOT_ID,
                "MaxStrikesPerExpiry": 3,
            },
        }
    if operation_id == "post.trade.v1.infoprices.subscriptions":
        return {
            **subscription,
            "Arguments": {
                "AccountKey": account_key,
                "AssetType": "Stock",
                "FieldGroups": ["PriceInfo"],
                "Uics": str(STOCK_UIC),
            },
        }
    if operation_id == "post.trade.v1.prices.subscriptions":
        return {
            **subscription,
            "Arguments": {
                "AccountKey": account_key,
                "AssetType": "Stock",
                "Uic": STOCK_UIC,
            },
        }
    if operation_id == "post.trade.v1.prices.multileg":
        return {
            "AccountKey": account_key,
            "FieldGroups": ["Quote"],
            "Legs": _option_legs(),
        }
    if operation_id == "post.trade.v1.prices.multileg.subscriptions":
        return {
            **subscription,
            "Arguments": {
                "AccountKey": account_key,
                "FieldGroups": ["Quote"],
                "Legs": _option_legs(),
            },
        }
    if operation_id in {
        "post.trade.v2.orders.precheck",
        "post.trade.v2.orders.multileg.precheck",
    }:
        return _precheck_body(operation_id, account_key)
    if operation_id.startswith("put.trade.v1.positions"):
        return {
            "AccountKey": account_key,
            "Amount": 1,
            "AssetType": "StockIndexOption",
            "Uic": OPTION_UICS[0],
        }
    if operation_id == "patch.trade.v1.positions.positionid":
        return {"AccountKey": account_key, "ExerciseMethod": "Cash"}
    if operation_id.startswith("post.trade.v") and operation_id.endswith("trades"):
        return {"AccountKey": account_key, "Amount": 1, "Uic": STOCK_UIC}
    return {}


def _precheck_body(operation_id: str, account_key: str) -> JsonObject:
    if ".multileg." in operation_id:
        return {
            "AccountKey": account_key,
            "Legs": _option_legs(),
            "ManualOrder": False,
            "OrderDuration": {"DurationType": "DayOrder"},
            "OrderPrice": 1,
            "OrderType": "Limit",
        }
    return {
        "AccountKey": account_key,
        "Amount": 1,
        "AssetType": "Stock",
        "BuySell": "Buy",
        "ManualOrder": False,
        "OrderDuration": {"DurationType": "DayOrder"},
        "OrderPrice": 1,
        "OrderType": "Limit",
        "Uic": STOCK_UIC,
    }


def _option_legs() -> list[JsonValue]:
    return [
        {
            "Amount": 1,
            "AssetType": "StockIndexOption",
            "BuySell": side,
            "ToOpenClose": "ToOpen",
            "Uic": uic,
        }
        for side, uic in zip(("Buy", "Sell"), OPTION_UICS, strict=True)
    ]


def _row(
    spec: TradingWriteSpec,
    prepared: JsonObject,
    executed: JsonObject,
    cleanup: JsonObject | None = None,
) -> JsonObject:
    return {
        "operation_id": spec.operation_id,
        "delegated": False,
        "preview_status": str(prepared.get("status", "missing")),
        "preview_denial_reason": prepared.get("denial_reason"),
        "preview_denial_reasons": prepared.get("denial_reasons", []),
        "preview_validation_errors": prepared.get("validation_errors", []),
        "execution_status": str(executed.get("status", "not_called")),
        "execution_denial_reason": executed.get("denial_reason"),
        "http_status": executed.get("http_status"),
        "response_error_code": _response_error_code(executed.get("response")),
        "network_call_made": executed.get("network_call_made") is True,
        "mutation_may_have_occurred": executed.get("mutation_may_have_occurred") is True,
        "retry_unsafe": executed.get("retry_unsafe") is True,
        "cleanup": {"cleanup_required": False} if cleanup is None else cleanup,
    }


def _response_error_code(value: JsonValue | None) -> str | None:
    if not isinstance(value, dict):
        return None
    code = value.get("ErrorCode")
    if isinstance(code, str):
        return code
    error_info = value.get("ErrorInfo")
    if not isinstance(error_info, dict):
        return None
    nested_code = error_info.get("ErrorCode")
    return nested_code if isinstance(nested_code, str) else None


def _payload(value: JsonValue | None) -> JsonObject:
    return JSON_OBJECT_ADAPTER.validate_python(value)


@contextmanager
def _safety_environment(account_key: str, audit_dir: Path) -> Generator[None, None, None]:
    names = (
        "SAXO_MCP_ENVIRONMENT",
        "SAXO_MCP_ACCOUNT_ALLOWLIST",
        "SAXO_MCP_INSTRUMENT_ALLOWLIST",
        "SAXO_MCP_MAX_QUANTITY",
        "SAXO_MCP_MAX_NOTIONAL",
        "SAXO_MCP_AUDIT_DIR",
    )
    previous = {name: os.environ.get(name) for name in names}
    os.environ.update(
        {
            "SAXO_MCP_ENVIRONMENT": "SIM",
            "SAXO_MCP_ACCOUNT_ALLOWLIST": account_key,
            "SAXO_MCP_INSTRUMENT_ALLOWLIST": ",".join(
                str(value) for value in (STOCK_UIC, *OPTION_UICS)
            ),
            "SAXO_MCP_MAX_QUANTITY": "10",
            "SAXO_MCP_MAX_NOTIONAL": "10000",
            "SAXO_MCP_AUDIT_DIR": str(audit_dir),
        },
    )
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
