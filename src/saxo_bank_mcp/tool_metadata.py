from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, cast

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.tool_metadata_live import LIVE_TOOL_METADATA
from saxo_bank_mcp.tool_metadata_types import ToolEnvironment, ToolMetadata, WriteEffect

# SIZE_OK: declarative reviewed metadata table; splitting hides one-to-one auditability.

__all__ = (
    "ToolEnvironment",
    "ToolMetadata",
    "WriteEffect",
    "metadata_for_tool",
    "tool_metadata",
)

_PRODUCTION_ORDER_TOOL_NAMES = (
    "saxo_place_order",
    "saxo_modify_order",
    "saxo_cancel_order",
    "saxo_cancel_orders_by_instrument",
    "saxo_place_multileg_order",
    "saxo_modify_multileg_order",
    "saxo_cancel_multileg_order",
)
_PRODUCTION_ORDER_TOOL_METADATA: dict[str, ToolMetadata] = {
    name: {
        "tool_class": "order_mutation",
        "environment_support": ["SIM", "LIVE_WRITE"],
        "write_effect": "live_network",
        "state_changing": True,
        "safe_in_live_read_mode": False,
        "agent_hint": (
            "Use only after exact precheck/current-order preview. SIM is autonomous; LIVE needs "
            "one exact-action approval statement sent by the human in agent chat."
        ),
    }
    for name in _PRODUCTION_ORDER_TOOL_NAMES
}

_ANALYTICS_SAFETY_HINT: Final = (
    " No broker write or disclaimer response is available from this analytics tool."
)


def _analytics_metadata(
    tool_class: str,
    *,
    environments: list[ToolEnvironment],
    local_state: bool,
    hint: str,
) -> ToolMetadata:
    return {
        "tool_class": tool_class,
        "environment_support": environments,
        "write_effect": "local_state" if local_state else "none",
        "state_changing": local_state,
        "safe_in_live_read_mode": True,
        "agent_hint": hint + _ANALYTICS_SAFETY_HINT,
    }


_ANALYTICS_TOOL_METADATA: dict[str, ToolMetadata] = {
    "saxo_analytics_capabilities": _analytics_metadata(
        "analytics_local_read",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Use first to inspect fixed limits, formats, and proof quarantine state.",
    ),
    "saxo_resolve_research_universe": _analytics_metadata(
        "analytics_saxo_read_local_index",
        environments=["SIM", "LIVE_READ"],
        local_state=True,
        hint="Resolve ambiguity explicitly before saving or synchronizing a universe.",
    ),
    "saxo_manage_research_universe": _analytics_metadata(
        "analytics_local_universe_state",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=True,
        hint="Revision-bound local universe operation using only safe instrument handles.",
    ),
    "saxo_sync_research_data": _analytics_metadata(
        "analytics_saxo_read_local_ingestion",
        environments=["SIM", "LIVE_READ"],
        local_state=True,
        hint="One bounded on-demand source read and owner-only dataset write.",
    ),
    "saxo_get_research_dataset": _analytics_metadata(
        "analytics_local_read",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Read one bounded normalized dataset page by opaque handle.",
    ),
    "saxo_analyze_market": _analytics_metadata(
        "analytics_local_compute",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Run only a typed bounded-market domain calculation.",
    ),
    "saxo_analyze_instruments": _analytics_metadata(
        "analytics_local_compute",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Run only typed source-bound instrument research.",
    ),
    "saxo_analyze_portfolio": _analytics_metadata(
        "analytics_private_local_compute",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Use safe aliases and select an explicit owner-result visibility.",
    ),
    "saxo_size_position": _analytics_metadata(
        "analytics_private_local_compute",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Requires a caller-supplied confirmed risk budget and returns a proposal only.",
    ),
    "saxo_run_scenario": _analytics_metadata(
        "analytics_private_local_compute",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Run only explicit accepted numeric shocks; narrative proposals are not executed.",
    ),
    "saxo_optimize_portfolio": _analytics_metadata(
        "analytics_private_local_compute",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Returns mathematical deltas only and does not choose objectives or risk tolerance.",
    ),
    "saxo_model_derivatives": _analytics_metadata(
        "analytics_private_local_compute",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Use only supported bounded derivative models and retain stated refusals.",
    ),
    "saxo_backtest_strategy": _analytics_metadata(
        "analytics_private_local_compute",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Declarative research only; no arbitrary code, execution, forecast, or advice.",
    ),
    "saxo_propose_trade_from_analysis": _analytics_metadata(
        "analytics_trade_precheck",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint=(
            "Returns typed preview input only and never approves or executes a trade; use the "
            "separate existing order precheck for any later explicit request."
        ),
    ),
    "saxo_render_analysis": _analytics_metadata(
        "analytics_local_artifact_state",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=True,
        hint="Proof-replays stored analysis and may register one owner-only artifact.",
    ),
    "saxo_export_analysis": _analytics_metadata(
        "analytics_local_artifact_state",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=True,
        hint="Exports only proof-bound stored values and may register one owner-only artifact.",
    ),
    "saxo_explain_analysis": _analytics_metadata(
        "analytics_local_read",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Explain only a current proof-replayed stored analysis.",
    ),
    "saxo_manage_analysis_job": _analytics_metadata(
        "analytics_local_job_state",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=True,
        hint="Starts, checks, or cancels one allowlisted in-process bounded job.",
    ),
    "saxo_list_analytics_storage": _analytics_metadata(
        "analytics_local_read",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=False,
        hint="Lists value-free owner storage metadata without filesystem locations.",
    ),
    "saxo_preview_analytics_deletion": _analytics_metadata(
        "analytics_local_deletion_preview",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=True,
        hint="Issues a revision-bound expiring token but deletes no retained data.",
    ),
    "saxo_delete_analytics_data": _analytics_metadata(
        "analytics_local_deletion",
        environments=["LOCAL", "SIM", "LIVE_READ"],
        local_state=True,
        hint="Consumes one exact preview token and deletes only its local dependency closure.",
    ),
}


_TOOLS: Mapping[str, ToolMetadata] = MappingProxyType(
    {
        "saxo_health": {
            "tool_class": "local_metadata_read",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": "Use for MCP process liveness only, then call saxo_auth_status.",
        },
        "saxo_auth_status": {
            "tool_class": "local_metadata_read",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": "Use before network reads to check local config and token-cache state.",
        },
        "saxo_start_pkce_login": {
            "tool_class": "sim_auth_state",
            "environment_support": ["SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM PKCE login helper. Do not use for LIVE read validation.",
        },
        "saxo_exchange_pkce_code": {
            "tool_class": "sim_auth_state",
            "environment_support": ["SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM PKCE token-cache helper. Do not use for LIVE read validation.",
        },
        "saxo_cache_sim_access_token": {
            "tool_class": "sim_auth_state",
            "environment_support": ["SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "Caches a SIM portal token only.",
        },
        "saxo_refresh_token": {
            "tool_class": "sim_auth_state",
            "environment_support": ["SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "Refreshes a SIM token cache only.",
        },
        "saxo_get_session_capabilities": {
            "tool_class": "network_read",
            "environment_support": ["SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Safe live-read probe when LIVE reads and a LIVE token cache are configured."
            ),
        },
        "saxo_get_entitlements": {
            "tool_class": "network_read",
            "environment_support": ["SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": "Reads entitlement summary without placing orders.",
        },
        "saxo_list_registered_endpoints": {
            "tool_class": "local_metadata_read",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": "Registry metadata only. It does not prove Saxo connectivity.",
        },
        "saxo_call_registered_endpoint": {
            "tool_class": "network_read",
            "environment_support": ["SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Use only for registered GET/read operations. It denies writes before network."
            ),
        },
        "saxo_safety_status": {
            "tool_class": "local_metadata_read",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Reports local write-safety config and tool metadata. It does not call Saxo."
            ),
        },
        "saxo_create_write_preview": {
            "tool_class": "local_write_preview",
            "environment_support": ["LOCAL", "SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "Creates only a local simulation preview token. It does not call Saxo.",
        },
        "saxo_commit_write_preview": {
            "tool_class": "local_write_preview",
            "environment_support": ["LOCAL", "SIM"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "Approves only local simulation. It does not call Saxo.",
        },
        "saxo_create_order_preview": {
            "tool_class": "order_precheck_preview",
            "environment_support": ["SIM", "LIVE_WRITE"],
            "write_effect": "live_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "Runs Saxo order precheck in configured SIM or LIVE-write mode and creates "
                "only a local preview/approval token. The preview/precheck does not place, "
                "modify, or cancel an order."
            ),
        },
        "saxo_get_multileg_order_defaults": {
            "tool_class": "sim_only_network_read",
            "environment_support": ["SIM"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "SIM-only GET helper. Use saxo_call_registered_endpoint for LIVE read checks."
            ),
        },
        "saxo_get_required_disclaimers": {
            "tool_class": "sim_only_network_read",
            "environment_support": ["SIM"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "SIM-only GET helper. Use saxo_call_registered_endpoint for LIVE read checks."
            ),
        },
        "saxo_register_disclaimer_response": {
            "tool_class": "disclaimer_write_preview_or_execution",
            "environment_support": ["SIM", "LIVE_WRITE"],
            "write_effect": "live_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "SIM submits autonomously. LIVE creates an exact-request preview; one human "
                "chat approval is required before saxo_execute_trading_write."
            ),
        },
        "saxo_list_trading_write_operations": {
            "tool_class": "local_write_metadata",
            "environment_support": ["LOCAL", "SIM", "LIVE_READ", "LIVE_WRITE"],
            "write_effect": "none",
            "state_changing": False,
            "safe_in_live_read_mode": True,
            "agent_hint": (
                "Inspect this before preparing a write. Order mutations are routed to their "
                "specialized precheck tools."
            ),
        },
        "saxo_prepare_trading_write": {
            "tool_class": "registered_write_preview",
            "environment_support": ["SIM", "LIVE_WRITE"],
            "write_effect": "local_state",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "SIM needs no human approval. LIVE returns one exact approval statement that "
                "must be sent by the human in agent chat."
            ),
        },
        "saxo_execute_trading_write": {
            "tool_class": "registered_write_execution",
            "environment_support": ["SIM", "LIVE_WRITE"],
            "write_effect": "live_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "Consumes one prepared request. Never retry an unknown-state result; inspect "
                "account state and the audit record first."
            ),
        },
        **_PRODUCTION_ORDER_TOOL_METADATA,
        "saxo_place_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_modify_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_cancel_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_cancel_sim_orders_by_instrument": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_place_multileg_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_modify_multileg_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_cancel_multileg_sim_order": {
            "tool_class": "sim_order_mutation",
            "environment_support": ["SIM"],
            "write_effect": "sim_network",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM order mutation tool. LIVE writes remain blocked.",
        },
        "saxo_create_streaming_price_subscription": {
            "tool_class": "sim_streaming_state",
            "environment_support": ["SIM"],
            "write_effect": "sim_streaming",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": "SIM streaming subscription tool. Do not use for LIVE read validation.",
        },
        "saxo_cleanup_streaming_subscriptions": {
            "tool_class": "sim_streaming_cleanup",
            "environment_support": ["SIM"],
            "write_effect": "sim_streaming",
            "state_changing": True,
            "safe_in_live_read_mode": False,
            "agent_hint": (
                "Cleans SIM streaming subscriptions. Do not use for LIVE read validation."
            ),
        },
        **LIVE_TOOL_METADATA,
        **_ANALYTICS_TOOL_METADATA,
    },
)


def tool_metadata() -> dict[str, dict[str, JsonValue]]:
    return {name: cast("dict[str, JsonValue]", dict(metadata)) for name, metadata in _TOOLS.items()}


def metadata_for_tool(tool_name: str) -> ToolMetadata | None:
    return _TOOLS.get(tool_name)
