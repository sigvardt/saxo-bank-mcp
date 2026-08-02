from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

_NO_ORDER_AUTHORITY: Final = " Never places, modifies, cancels, or approves a Saxo order."

ANALYTICS_TOOL_DESCRIPTIONS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "saxo_analytics_capabilities": (
            "saxo_analytics_capabilities reports installed analytics, fixed limits, formats, "
            "source scope, and proof-profile quarantine state without calling Saxo."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_resolve_research_universe": (
            "saxo_resolve_research_universe resolves a bounded natural-language query through "
            "the frozen Saxo instrument source into safe handles and explicit ambiguities."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_manage_research_universe": (
            "saxo_manage_research_universe creates, lists, revision-updates, or deletes one "
            "owner-only local universe using safe instrument handles." + _NO_ORDER_AUTHORITY
        ),
        "saxo_sync_research_data": (
            "saxo_sync_research_data performs one bounded on-demand Saxo read and stores only "
            "source-bound normalized research datasets; it is not a collector."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_get_research_dataset": (
            "saxo_get_research_dataset returns one bounded page of safe normalized rows plus "
            "dataset lineage metadata from owner-only local storage." + _NO_ORDER_AUTHORITY
        ),
        "saxo_analyze_market": (
            "saxo_analyze_market runs a typed bounded-universe, depth, wrapper, saved-condition, "
            "or session analysis using existing verified domain services." + _NO_ORDER_AUTHORITY
        ),
        "saxo_analyze_instruments": (
            "saxo_analyze_instruments runs typed price, quote, or dossier research over safe "
            "instrument handles and source-bound datasets." + _NO_ORDER_AUTHORITY
        ),
        "saxo_analyze_portfolio": (
            "saxo_analyze_portfolio dispatches typed accounting, attribution, exposure, income, "
            "cost, liquidity, trade-review, and approved-query domain services."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_size_position": (
            "saxo_size_position calculates a proposal only from a caller-supplied confirmed "
            "risk budget and explicit source-bound constraints." + _NO_ORDER_AUTHORITY
        ),
        "saxo_run_scenario": (
            "saxo_run_scenario evaluates only explicit accepted numeric shock maps; narrative "
            "numbers must be echoed and confirmed before calculation." + _NO_ORDER_AUTHORITY
        ),
        "saxo_optimize_portfolio": (
            "saxo_optimize_portfolio runs the bounded minimum-variance or risk-parity service "
            "and returns mathematical target deltas and diagnostics only." + _NO_ORDER_AUTHORITY
        ),
        "saxo_model_derivatives": (
            "saxo_model_derivatives dispatches only the bounded supported option, surface, "
            "lifecycle, futures, and FX-forward models with explicit limitations."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_backtest_strategy": (
            "saxo_backtest_strategy runs the bounded declarative backtest engine with costs, "
            "holdout controls, source limitations, and no executable strategy input."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_propose_trade_from_analysis": (
            "saxo_propose_trade_from_analysis validates a current analysis-bound proposal and "
            "returns typed preview input and a pre-trade impact card only; it never places an "
            "order or grants approval or execution authority." + _NO_ORDER_AUTHORITY
        ),
        "saxo_render_analysis": (
            "saxo_render_analysis proof-replays a stored analysis, issues a server-owned opaque "
            "binding, and renders one approved template with bounded private delivery."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_export_analysis": (
            "saxo_export_analysis proof-replays a stored analysis and exports exact bound values "
            "through approved table or report formats with owner-only fallback."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_explain_analysis": (
            "saxo_explain_analysis proof-replays one stored analysis and returns its metric "
            "definitions, lineage, assumptions, warnings, and active proof binding."
            + _NO_ORDER_AUTHORITY
        ),
        "saxo_manage_analysis_job": (
            "saxo_manage_analysis_job starts, checks, or cancels one allowlisted bounded "
            "in-process analytics job and never exposes a partial conclusion." + _NO_ORDER_AUTHORITY
        ),
        "saxo_list_analytics_storage": (
            "saxo_list_analytics_storage lists safe owner-only local storage metadata without "
            "returning private values or local filesystem locations." + _NO_ORDER_AUTHORITY
        ),
        "saxo_preview_analytics_deletion": (
            "saxo_preview_analytics_deletion computes an exact revision-bound local dependency "
            "closure and issues one expiring single-use deletion token." + _NO_ORDER_AUTHORITY
        ),
        "saxo_delete_analytics_data": (
            "saxo_delete_analytics_data consumes one valid preview token to delete only its "
            "exact owner-local closure and return a value-free receipt." + _NO_ORDER_AUTHORITY
        ),
    },
)


def analytics_tool_description(tool_id: str) -> str:
    """Return the reviewed description for one exact analytics tool ID."""
    return ANALYTICS_TOOL_DESCRIPTIONS[tool_id]
