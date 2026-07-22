from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Literal

from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from pydantic import ValidationError

from saxo_bank_mcp.live_precheck_acceptance_guard import (
    contains_reserved_precheck_key,
)
from saxo_bank_mcp.live_precheck_accepted_models import AcceptedPrecheck
from saxo_bank_mcp.live_precheck_collection_models import (
    CollectionPayload,
    CollectionStructureJson,
    MessagesPayload,
)
from saxo_bank_mcp.live_precheck_proof_account import ProofAccount, proof_account
from saxo_bank_mcp.live_precheck_proof_models import (
    ExecutionAborted,
    ExecutionCompleted,
    ExecutionOutcome,
    ProofOrder,
    RegisteredRead,
    SanitizedPrecheck,
    StateCountJson,
    StateSnapshot,
)
from saxo_bank_mcp.read_tool_types import ReadResponseMode
from saxo_bank_mcp.strict_json import (
    StrictJsonError,
    parse_json_value,
    validate_json_value,
)

_READ_TOOL = "saxo_call_registered_endpoint"
_PRECHECK_TOOL = "saxo_precheck_live_order"
_ORDERS_PATH = "/port/v1/orders/me"
_POSITIONS_PATH = "/port/v1/positions/me"
_BALANCES_PATH = "/port/v1/balances/me"
_MESSAGES_PATH = "/trade/v1/messages"


@dataclass(frozen=True, slots=True)
class _StateReadFailure:
    reason: Literal["state_collection_shape_invalid", "state_read_failed"]


@dataclass(frozen=True, slots=True)
class _CollectionObservation:
    count: int
    structure: CollectionStructureJson


async def execute_proof(
    client: Client[FastMCPTransport],
    order: ProofOrder,
) -> ExecutionOutcome:
    account = await proof_account(client, order.account_position)
    if isinstance(account, ExecutionAborted):
        return account
    before = await _read_state(client)
    if isinstance(before, _StateReadFailure):
        return ExecutionAborted(account.counts, "state_before", before.reason)
    precheck = await _accepted_precheck(client, order, account)
    if isinstance(precheck, ExecutionAborted):
        return precheck
    after = await _read_state(client)
    if isinstance(after, _StateReadFailure):
        return ExecutionAborted(account.counts, "state_after", after.reason)
    return ExecutionCompleted(
        account_counts=account.counts,
        account_binding={
            "source": "visible_account_id_and_process_scoped_ref",
            "account_id": account.account_id,
            "account_position": account.position,
            "selector_sha256": hashlib.sha256(account.account_ref.encode()).hexdigest(),
        },
        instrument={
            "amount": order.amount,
            "asset_type": order.asset_type,
            "buy_sell": order.buy_sell,
            "uic": order.uic,
            "verified_tradable_before_precheck": True,
        },
        before=before,
        after=after,
        precheck=precheck,
    )


async def _accepted_precheck(
    client: Client[FastMCPTransport],
    order: ProofOrder,
    account: ProofAccount,
) -> SanitizedPrecheck | ExecutionAborted:
    precheck_result = await client.call_tool(
        _PRECHECK_TOOL,
        {
            "order": {
                "account_id": account.account_id,
                "uic": order.uic,
                "asset_type": order.asset_type,
                "amount": order.amount,
                "buy_sell": order.buy_sell,
            },
        },
        raise_on_error=False,
    )
    if precheck_result.is_error:
        return ExecutionAborted(
            account.counts,
            "precheck",
            "precheck_rejected_or_invalid",
        )
    try:
        structured = validate_json_value(precheck_result.structured_content)
    except StrictJsonError:
        return ExecutionAborted(
            account.counts,
            "precheck",
            "precheck_rejected_or_invalid",
        )
    if contains_reserved_precheck_key(structured):
        return ExecutionAborted(
            account.counts,
            "precheck",
            "precheck_rejected_or_invalid",
        )
    try:
        precheck = AcceptedPrecheck.model_validate(structured)
    except ValidationError:
        return ExecutionAborted(
            account.counts,
            "precheck",
            "precheck_rejected_or_invalid",
        )
    if not _precheck_matches_order(precheck, order, account):
        return ExecutionAborted(
            account.counts,
            "precheck",
            "precheck_binding_mismatch",
        )
    return precheck.sanitized()


def _precheck_matches_order(
    precheck: AcceptedPrecheck,
    order: ProofOrder,
    account: ProofAccount,
) -> bool:
    summary = precheck.request_summary
    return (
        hmac.compare_digest(precheck.account_ref, account.account_ref)
        and hmac.compare_digest(precheck.account_id, account.account_id)
        and summary.amount == order.amount
        and summary.asset_type == order.asset_type
        and summary.buy_sell == order.buy_sell
        and summary.duration_type == "DayOrder"
        and set(summary.field_groups) == {"Costs", "MarginImpactBuySell"}
        and summary.manual_order is False
        and summary.order_type == "Market"
        and summary.uic == order.uic
    )


async def _read_state(
    client: Client[FastMCPTransport],
) -> StateSnapshot | _StateReadFailure:
    orders = await _read_payload(client, _ORDERS_PATH)
    positions = await _read_payload(client, _POSITIONS_PATH)
    balances = await _read_payload(
        client,
        _BALANCES_PATH,
        response_mode="fingerprint_only",
    )
    messages = await _read_payload(client, _MESSAGES_PATH)
    if orders is None or positions is None or balances is None or messages is None:
        return _StateReadFailure("state_read_failed")
    if balances.response_fingerprint_scope != "account_money_state_fields":
        return _StateReadFailure("state_read_failed")
    try:
        order_collection = _collection_observation(orders, _ORDERS_PATH)
        position_collection = _collection_observation(positions, _POSITIONS_PATH)
        message_collection = _collection_observation(messages, _MESSAGES_PATH)
        counts: StateCountJson = {
            "orders": order_collection.count,
            "positions": position_collection.count,
            "trade_messages": message_collection.count,
        }
    except (StrictJsonError, ValidationError):
        return _StateReadFailure("state_collection_shape_invalid")
    return StateSnapshot(
        counts=counts,
        balance_fingerprint=balances.response_fingerprint,
        orders_fingerprint=orders.response_fingerprint,
        positions_fingerprint=positions.response_fingerprint,
        trade_messages_fingerprint=messages.response_fingerprint,
        collection_structure={
            "orders": order_collection.structure,
            "positions": position_collection.structure,
            "trade_messages": message_collection.structure,
        },
    )


def _collection_observation(
    payload: RegisteredRead,
    path: str,
) -> _CollectionObservation:
    raw = parse_json_value(payload.response or "null")
    if path == _MESSAGES_PATH:
        messages = MessagesPayload.model_validate(raw, strict=True)
        return _CollectionObservation(messages.count, messages.structure())
    collection = CollectionPayload.model_validate(raw, strict=True)
    return _CollectionObservation(collection.count, collection.structure())


async def _read_payload(
    client: Client[FastMCPTransport],
    path: str,
    *,
    params: dict[str, str] | None = None,
    response_mode: ReadResponseMode = "redacted_body",
) -> RegisteredRead | None:
    result = await client.call_tool(
        _READ_TOOL,
        {
            "method": "GET",
            "path": path,
            "params": params,
            "response_mode": response_mode,
        },
        raise_on_error=False,
    )
    if result.is_error:
        return None
    try:
        payload = RegisteredRead.model_validate(result.structured_content)
    except ValidationError:
        return None
    return payload if payload.path == path else None
