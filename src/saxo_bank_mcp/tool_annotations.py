from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from mcp.types import ToolAnnotations

READ_LOCAL: Final = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
READ_SAXO: Final = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
PRECHECK_SAXO: Final = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
MUTATE_LOCAL: Final = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
MUTATE_SAXO: Final = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)
PREVIEW_LOCAL: Final = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
PREVIEW_SAXO: Final = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

TOOL_ANNOTATIONS: Final[Mapping[str, ToolAnnotations]] = MappingProxyType(
    {
        "saxo_health": READ_LOCAL,
        "saxo_auth_status": READ_LOCAL,
        "saxo_start_pkce_login": MUTATE_LOCAL,
        "saxo_exchange_pkce_code": MUTATE_SAXO,
        "saxo_cache_sim_access_token": MUTATE_LOCAL,
        "saxo_refresh_token": MUTATE_SAXO,
        "saxo_get_session_capabilities": READ_SAXO,
        "saxo_get_entitlements": READ_SAXO,
        "saxo_list_live_accounts": READ_SAXO,
        "saxo_precheck_live_order": PRECHECK_SAXO,
        "saxo_get_safe_request_ledger": MUTATE_LOCAL,
        "saxo_list_registered_endpoints": READ_LOCAL,
        "saxo_call_registered_endpoint": READ_SAXO,
        "saxo_safety_status": READ_LOCAL,
        "saxo_create_write_preview": PREVIEW_LOCAL,
        "saxo_commit_write_preview": MUTATE_LOCAL,
        "saxo_create_order_preview": PREVIEW_SAXO,
        "saxo_get_multileg_order_defaults": READ_SAXO,
        "saxo_get_required_disclaimers": READ_SAXO,
        "saxo_register_disclaimer_response": MUTATE_SAXO,
        "saxo_list_trading_write_operations": READ_LOCAL,
        "saxo_prepare_trading_write": PREVIEW_LOCAL,
        "saxo_execute_trading_write": MUTATE_SAXO,
        "saxo_place_order": MUTATE_SAXO,
        "saxo_modify_order": MUTATE_SAXO,
        "saxo_cancel_order": MUTATE_SAXO,
        "saxo_cancel_orders_by_instrument": MUTATE_SAXO,
        "saxo_place_multileg_order": MUTATE_SAXO,
        "saxo_modify_multileg_order": MUTATE_SAXO,
        "saxo_cancel_multileg_order": MUTATE_SAXO,
        "saxo_place_sim_order": MUTATE_SAXO,
        "saxo_modify_sim_order": MUTATE_SAXO,
        "saxo_cancel_sim_order": MUTATE_SAXO,
        "saxo_cancel_sim_orders_by_instrument": MUTATE_SAXO,
        "saxo_place_multileg_sim_order": MUTATE_SAXO,
        "saxo_modify_multileg_sim_order": MUTATE_SAXO,
        "saxo_cancel_multileg_sim_order": MUTATE_SAXO,
        "saxo_create_streaming_price_subscription": MUTATE_SAXO,
        "saxo_cleanup_streaming_subscriptions": MUTATE_SAXO,
        "saxo_analytics_capabilities": READ_LOCAL,
        "saxo_resolve_research_universe": PREVIEW_SAXO,
        "saxo_manage_research_universe": MUTATE_LOCAL,
        "saxo_sync_research_data": PREVIEW_SAXO,
        "saxo_get_research_dataset": READ_LOCAL,
        "saxo_analyze_market": PREVIEW_LOCAL,
        "saxo_analyze_instruments": READ_LOCAL,
        "saxo_analyze_portfolio": READ_LOCAL,
        "saxo_size_position": READ_LOCAL,
        "saxo_run_scenario": READ_LOCAL,
        "saxo_optimize_portfolio": READ_LOCAL,
        "saxo_model_derivatives": READ_LOCAL,
        "saxo_backtest_strategy": READ_LOCAL,
        "saxo_propose_trade_from_analysis": READ_LOCAL,
        "saxo_render_analysis": PREVIEW_LOCAL,
        "saxo_export_analysis": PREVIEW_LOCAL,
        "saxo_explain_analysis": READ_LOCAL,
        "saxo_manage_analysis_job": MUTATE_LOCAL,
        "saxo_list_analytics_storage": READ_LOCAL,
        "saxo_preview_analytics_deletion": PREVIEW_LOCAL,
        "saxo_delete_analytics_data": MUTATE_LOCAL,
    },
)


@dataclass(frozen=True, slots=True)
class ToolAnnotationDriftError(Exception):
    missing_tool_ids: tuple[str, ...]
    unknown_tool_ids: tuple[str, ...]

    def __str__(self) -> str:
        """Return a stable drift message for test and QA evidence."""
        missing = _ids_for_message(self.missing_tool_ids)
        unknown = _ids_for_message(self.unknown_tool_ids)
        return f"tool annotation map drift: missing={missing}; unknown={unknown}"


def annotation_for_tool(tool_id: str) -> ToolAnnotations:
    return TOOL_ANNOTATIONS[tool_id]


def assert_tool_annotations_cover(
    runtime_tool_ids: Iterable[str],
    annotations: Mapping[str, ToolAnnotations] = TOOL_ANNOTATIONS,
) -> None:
    runtime = frozenset(runtime_tool_ids)
    annotation_ids = frozenset(annotations)
    missing = tuple(sorted(runtime - annotation_ids))
    unknown = tuple(sorted(annotation_ids - runtime))
    if missing or unknown:
        raise ToolAnnotationDriftError(missing, unknown)


def _ids_for_message(tool_ids: tuple[str, ...]) -> str:
    if not tool_ids:
        return "none"
    return ",".join(tool_ids)
