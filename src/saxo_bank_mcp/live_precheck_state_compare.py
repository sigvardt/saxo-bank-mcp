from __future__ import annotations

from saxo_bank_mcp.live_precheck_proof_models import StateSnapshot, UnchangedJson


def unchanged_state(before: StateSnapshot, after: StateSnapshot) -> UnchangedJson:
    return {
        "account_money_state_fields": (
            before.balance_fingerprint == after.balance_fingerprint
        ),
        "orders": before.orders_fingerprint == after.orders_fingerprint,
        "orders_count": before.counts["orders"] == after.counts["orders"],
        "positions": before.positions_fingerprint == after.positions_fingerprint,
        "positions_count": before.counts["positions"] == after.counts["positions"],
        "trade_messages": before.trade_messages_fingerprint == after.trade_messages_fingerprint,
        "trade_messages_count": (before.counts["trade_messages"] == after.counts["trade_messages"]),
    }
