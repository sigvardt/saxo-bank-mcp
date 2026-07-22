from __future__ import annotations

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.live_precheck_proof_models import (
    AccountMoneyStateScopeJson,
    StateSnapshot,
)


def account_money_state_scope_payload() -> AccountMoneyStateScopeJson:
    return {
        "fingerprint_scope": "modeled_account_money_state_fields",
        "no_change_conclusion_requires": [
            "modeled_account_money_state_fields_unchanged",
            "complete_no_write_transport_evidence",
        ],
        "limitation": (
            "not_proof_that_every_possible_saxo_balance_field_was_observed"
        ),
    }


def state_structure_payload(snapshot: StateSnapshot) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {}
    for name in ("orders", "positions", "trade_messages"):
        structure = snapshot.collection_structure[name]
        payload[name] = {
            "shape": structure["shape"],
            "declared_count_present": structure["declared_count_present"],
            "declared_count_consistent": structure["declared_count_consistent"],
        }
    return payload
