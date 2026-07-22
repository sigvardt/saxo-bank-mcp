from __future__ import annotations

from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
SKILL_ROOT: Final = ROOT / "skills/saxo-trading"
SKILL_TEXT: Final = SKILL_ROOT / "SKILL.md"
REFERENCE_FILES: Final = (
    SKILL_ROOT / "references/order-lifecycle.md",
    SKILL_ROOT / "references/generic-trading-writes.md",
    SKILL_ROOT / "references/approval-dialogue.md",
)
OPENAI_METADATA: Final = SKILL_ROOT / "agents/openai.yaml"
REQUIRED_EXACT_PHRASES: Final = (
    "SIM requires no human approval.",
    "LIVE requires one exact new chat statement AND the server authorization token.",
    "Before risky action communicate plain-language environment, account alias, instrument, "
    "side, quantity, type, price, duration, impact, expiry, uncertainty.",
    "Approval copyback text must say what will happen.",
    "`precheck_accepted` and `preview_created` are not execution success; only `completed` "
    "is unqualified success.",
    "Do not answer LIVE disclaimers, choose investments, expose secrets/submitted validation "
    "values, or blind retry.",
    "Unknown/partial/duplicate/post-boundary outcomes prohibit retry until concrete "
    "reconciliation.",
    "Validation errors name field/rule but not submitted value.",
    "Account numbers may be internal selectors, but user-facing text prefers alias.",
)
REQUIRED_TOOL_IDS: Final = (
    "saxo_list_registered_endpoints",
    "saxo_call_registered_endpoint",
    "saxo_get_multileg_order_defaults",
    "saxo_precheck_live_order",
    "saxo_create_order_preview",
    "saxo_get_required_disclaimers",
    "saxo_register_disclaimer_response",
    "saxo_create_write_preview",
    "saxo_place_order",
    "saxo_modify_order",
    "saxo_cancel_order",
    "saxo_cancel_orders_by_instrument",
    "saxo_place_multileg_order",
    "saxo_modify_multileg_order",
    "saxo_cancel_multileg_order",
    "saxo_place_sim_order",
    "saxo_modify_sim_order",
    "saxo_cancel_sim_order",
    "saxo_cancel_sim_orders_by_instrument",
    "saxo_place_multileg_sim_order",
    "saxo_modify_multileg_sim_order",
    "saxo_cancel_multileg_sim_order",
    "saxo_list_trading_write_operations",
    "saxo_prepare_trading_write",
    "saxo_execute_trading_write",
    "saxo_get_safe_request_ledger",
)
ADVERSARIAL_TERMS: Final = (
    "Opaque approval",
    "Expired approval",
    "Replayed approval",
    "Mismatched approval",
    "TradeNotCompleted",
    "partial multileg",
    "duplicate conflict",
    "Timeout after commit",
    "skip readback",
)


def test_trading_skill_package_has_required_files_and_metadata() -> None:
    for path in (SKILL_TEXT, *REFERENCE_FILES, OPENAI_METADATA):
        assert path.is_file(), path

    metadata = OPENAI_METADATA.read_text(encoding="utf-8")
    assert "Use $saxo-bank-mcp:saxo-trading" in metadata
    assert "Saxo trading" in metadata


def test_trading_skill_contains_exact_safety_contract_and_tool_coverage() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in (SKILL_TEXT, *REFERENCE_FILES)
    )

    for phrase in REQUIRED_EXACT_PHRASES:
        assert phrase in combined
    for tool_id in REQUIRED_TOOL_IDS:
        assert tool_id in combined
    for term in ADVERSARIAL_TERMS:
        assert term.lower() in combined.lower()

    assert "$saxo-bank-mcp:saxo-trading" in combined
    assert "/saxo-bank-mcp:saxo-trading" in combined
    assert "created_at + 5h" in combined
    assert "one order per second" in combined
