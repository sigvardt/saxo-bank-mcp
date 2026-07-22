from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.live_precheck_ledger_models import SafeLedgerReport
from saxo_bank_mcp.live_precheck_proof_audit import (
    SourceProvenance,
    exposed_ledger_allows_proof,
    ledger_allows_proof,
    request_ledgers_match,
    transport_boundary_allows_proof,
    transport_boundary_matches,
)
from saxo_bank_mcp.live_precheck_proof_models import (
    AbortReason,
    ExecutionAborted,
    ExecutionOutcome,
    PersistedPrecheck,
    SanitizedPrecheck,
)
from saxo_bank_mcp.live_precheck_state_artifact import (
    account_money_state_scope_payload,
    state_structure_payload,
)
from saxo_bank_mcp.live_precheck_state_compare import unchanged_state
from saxo_bank_mcp.live_precheck_transport_artifact import transport_capture_payload
from saxo_bank_mcp.request_ledger import RequestLedgerEvent, safe_request_events
from saxo_bank_mcp.transport_boundary import TransportBoundaryCapture

_JSON_VALUE_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class ArtifactContext:
    events: list[RequestLedgerEvent]
    provenance: SourceProvenance
    driver: str
    token_validity_lower_bound_seconds: int
    exposed_ledger: SafeLedgerReport | None
    transport_capture: TransportBoundaryCapture | None


def artifact_payload(
    outcome: ExecutionOutcome,
    context: ArtifactContext,
) -> dict[str, JsonValue]:
    ledger_parity = context.exposed_ledger is not None and request_ledgers_match(
        context.events, context.exposed_ledger
    )
    boundary_parity = (
        context.transport_capture is not None
        and context.transport_capture.collector_complete
        and context.exposed_ledger is not None
        and transport_boundary_matches(
            context.transport_capture.events,
            context.events,
            context.exposed_ledger,
        )
    )
    payload: dict[str, JsonValue] = {
        "status": "aborted",
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "driver": context.driver,
        "token_validity_lower_bound_seconds": context.token_validity_lower_bound_seconds,
        "request_ledger": _JSON_VALUE_ADAPTER.validate_python(
            safe_request_events(context.events),
        ),
        "mcp_request_ledger": (
            None
            if context.exposed_ledger is None
            else _JSON_VALUE_ADAPTER.validate_python(
                context.exposed_ledger.model_dump(mode="json"),
            )
        ),
        "request_ledger_parity": ledger_parity,
        "transport_boundary_capture": transport_capture_payload(
            context.transport_capture,
        ),
        "transport_boundary_parity": boundary_parity,
        "source": {
            "git_head": context.provenance.git_head,
            "dirty_source_sha256": context.provenance.dirty_source_sha256,
        },
        "trace_scope": {
            "factory_http_covered": True,
            "collector_process": "separate_process",
            "sensor_relationship": (
                "transport wrapper emits directly to a child collector and does not consume "
                "request-ledger events"
            ),
            "websockets": "out_of_scope",
        },
        "account_money_state_scope": _JSON_VALUE_ADAPTER.validate_python(
            account_money_state_scope_payload(),
        ),
        "trade_readiness": "precheck_only_not_order_ready",
        "does_not_verify": [
            "order placement",
            "order execution",
            "order cancellation",
            "real-money write readiness",
            "requests made outside the proof MCP session",
        ],
    }
    if isinstance(outcome, ExecutionAborted):
        payload["abort_stage"] = outcome.stage
        payload["abort_reason"] = outcome.reason
        if outcome.account_counts is not None:
            payload["account_counts"] = {
                "active": outcome.account_counts["active"],
                "total": outcome.account_counts["total"],
            }
        return payload
    unchanged = unchanged_state(outcome.before, outcome.after)
    payload.update(
        {
            "instrument": {
                "amount": outcome.instrument["amount"],
                "asset_type": outcome.instrument["asset_type"],
                "buy_sell": outcome.instrument["buy_sell"],
                "uic": outcome.instrument["uic"],
                "verified_tradable_before_precheck": (
                    outcome.instrument["verified_tradable_before_precheck"]
                ),
            },
            "account_binding": {
                "source": outcome.account_binding["source"],
                "account_id": outcome.account_binding["account_id"],
                "account_position": outcome.account_binding["account_position"],
                "selector_sha256": outcome.account_binding["selector_sha256"],
            },
            "account_counts": {
                "active": outcome.account_counts["active"],
                "total": outcome.account_counts["total"],
            },
            "before_counts": {
                "orders": outcome.before.counts["orders"],
                "positions": outcome.before.counts["positions"],
                "trade_messages": outcome.before.counts["trade_messages"],
            },
            "after_counts": {
                "orders": outcome.after.counts["orders"],
                "positions": outcome.after.counts["positions"],
                "trade_messages": outcome.after.counts["trade_messages"],
            },
            "unchanged": {
                "account_money_state_fields": unchanged["account_money_state_fields"],
                "orders": unchanged["orders"],
                "orders_count": unchanged["orders_count"],
                "positions": unchanged["positions"],
                "positions_count": unchanged["positions_count"],
                "trade_messages": unchanged["trade_messages"],
                "trade_messages_count": unchanged["trade_messages_count"],
            },
            "response_structure": {
                "account_declared_count_consistent": True,
                "before": state_structure_payload(outcome.before),
                "after": state_structure_payload(outcome.after),
                "state_read_scope": "all_accounts_me",
            },
            "precheck": _JSON_VALUE_ADAPTER.validate_python(
                _persisted_precheck_payload(outcome.precheck),
            ),
        },
    )
    abort_reason = _completion_abort_reason(
        context,
        unchanged=all(unchanged.values()),
        ledger_parity=ledger_parity,
        boundary_parity=boundary_parity,
    )
    if abort_reason is not None:
        payload["abort_stage"] = "proof_policy"
        payload["abort_reason"] = abort_reason
    else:
        payload["status"] = "completed"
    return payload


def _persisted_precheck_payload(precheck: SanitizedPrecheck) -> PersistedPrecheck:
    return {
        "status": precheck["status"],
        "http_status": precheck["http_status"],
        "precheck_result": precheck["precheck_result"],
        "estimated_cash_required_value_present": (
            precheck["estimated_cash_required"] is not None
        ),
        "estimated_cash_required_currency_present": (
            precheck["estimated_cash_required_currency"] is not None
        ),
        "estimated_total_cost_in_account_currency_value_present": (
            precheck["estimated_total_cost_in_account_currency"] is not None
        ),
        "disclaimer_count": precheck["disclaimer_count"],
        "requires_disclaimer_review": precheck["requires_disclaimer_review"],
        "root_result_explicitly_ok": precheck["root_result_explicitly_ok"],
        "child_result_count": precheck["child_result_count"],
        "all_returned_results_explicitly_ok": (
            precheck["all_returned_results_explicitly_ok"]
        ),
        "disclaimer_object_present": precheck["disclaimer_object_present"],
        "error_object_present": precheck["error_object_present"],
        "precheck_request_accepted": precheck["precheck_request_accepted"],
        "account_lookup_endpoint_called": precheck["account_lookup_endpoint_called"],
        "instrument_lookup_endpoint_called": (
            precheck["instrument_lookup_endpoint_called"]
        ),
        "instrument_tradable": precheck["instrument_tradable"],
        "precheck_endpoint_called": precheck["precheck_endpoint_called"],
        "order_placement_endpoint_called": (
            precheck["order_placement_endpoint_called"]
        ),
        "order_change_endpoint_called": precheck["order_change_endpoint_called"],
        "order_cancel_endpoint_called": precheck["order_cancel_endpoint_called"],
        "disclaimer_response_endpoint_called": (
            precheck["disclaimer_response_endpoint_called"]
        ),
        "order_identifier_present": precheck["order_identifier_present"],
        "requires_order_readback": precheck["requires_order_readback"],
        "live_write_called": precheck["live_write_called"],
        "order_or_subscription_created": (
            precheck["order_or_subscription_created"]
        ),
        "request_summary": precheck["request_summary"],
    }


def _completion_abort_reason(
    context: ArtifactContext,
    *,
    unchanged: bool,
    ledger_parity: bool,
    boundary_parity: bool,
) -> AbortReason | None:
    exposed = context.exposed_ledger
    capture = context.transport_capture
    checks: tuple[tuple[bool, AbortReason], ...] = (
        (not unchanged, "state_changed"),
        (exposed is None, "session_request_ledger_unavailable"),
        (not ledger_parity, "request_ledger_parity_failed"),
        (
            capture is None or not capture.collector_complete,
            "transport_boundary_capture_unavailable",
        ),
        (not boundary_parity, "transport_boundary_parity_failed"),
    )
    for failed, reason in checks:
        if failed:
            return reason
    if capture is None or exposed is None:
        return "transport_boundary_capture_unavailable"
    if not (
        transport_boundary_allows_proof(capture.events)
        and ledger_allows_proof(context.events)
        and exposed_ledger_allows_proof(exposed)
    ):
        return "request_ledger_policy_failed"
    return None if context.provenance.complete else "source_provenance_incomplete"
