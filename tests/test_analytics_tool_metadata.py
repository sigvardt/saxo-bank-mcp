from __future__ import annotations

from typing import Final

from saxo_bank_mcp.analytics_tool_descriptions import ANALYTICS_TOOL_DESCRIPTIONS
from saxo_bank_mcp.server_tool_ids import ANALYTICS_TOOL_IDS
from saxo_bank_mcp.server_tool_registration import FUNCTION_TOOL_REGISTRATIONS
from saxo_bank_mcp.tool_annotations import TOOL_ANNOTATIONS
from saxo_bank_mcp.tool_metadata import metadata_for_tool, tool_metadata

LOCAL_STATE_ANALYTICS_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "saxo_resolve_research_universe",
        "saxo_manage_research_universe",
        "saxo_sync_research_data",
        "saxo_render_analysis",
        "saxo_export_analysis",
        "saxo_manage_analysis_job",
        "saxo_preview_analytics_deletion",
        "saxo_delete_analytics_data",
    },
)
DESTRUCTIVE_LOCAL_ANALYTICS_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "saxo_manage_research_universe",
        "saxo_manage_analysis_job",
        "saxo_delete_analytics_data",
    },
)


def test_analytics_catalog_descriptions_registrations_annotations_and_metadata_sync() -> None:
    expected = frozenset(ANALYTICS_TOOL_IDS)
    registered = frozenset(
        registration.tool_id
        for registration in FUNCTION_TOOL_REGISTRATIONS
        if registration.tool_id in expected
    )
    metadata = tool_metadata()

    assert registered == expected
    assert frozenset(ANALYTICS_TOOL_DESCRIPTIONS) == expected
    assert expected <= frozenset(TOOL_ANNOTATIONS)
    assert expected <= frozenset(metadata)


def test_analytics_metadata_proves_no_broker_write_or_disclaimer_path() -> None:
    for tool_id in ANALYTICS_TOOL_IDS:
        metadata = metadata_for_tool(tool_id)
        assert metadata is not None
        assert metadata["write_effect"] in {"none", "local_state"}
        assert "LIVE_WRITE" not in metadata["environment_support"]
        assert metadata["safe_in_live_read_mode"] is True
        assert "broker write" in metadata["agent_hint"].casefold()
        assert "disclaimer" in metadata["agent_hint"].casefold()


def test_local_state_effects_and_annotations_match_actual_analytics_behavior() -> None:
    for tool_id in ANALYTICS_TOOL_IDS:
        metadata = metadata_for_tool(tool_id)
        annotation = TOOL_ANNOTATIONS[tool_id]
        assert metadata is not None
        if tool_id in LOCAL_STATE_ANALYTICS_TOOLS:
            assert metadata["write_effect"] == "local_state"
            assert metadata["state_changing"] is True
            assert annotation.readOnlyHint is False
        else:
            assert metadata["write_effect"] == "none"
            assert metadata["state_changing"] is False
            assert annotation.readOnlyHint is True
        assert annotation.destructiveHint is (tool_id in DESTRUCTIVE_LOCAL_ANALYTICS_TOOLS)


def test_trade_proposal_tool_is_precheck_only_and_never_executing() -> None:
    metadata = metadata_for_tool("saxo_propose_trade_from_analysis")
    description = ANALYTICS_TOOL_DESCRIPTIONS["saxo_propose_trade_from_analysis"]

    assert metadata is not None
    assert metadata["tool_class"] == "analytics_trade_precheck"
    assert metadata["write_effect"] == "none"
    assert metadata["state_changing"] is False
    assert "typed preview input only" in metadata["agent_hint"]
    assert "never approves or executes" in metadata["agent_hint"]
    assert "never places" in description.casefold()


def test_descriptions_are_useful_bounded_and_value_free() -> None:
    for tool_id, description in ANALYTICS_TOOL_DESCRIPTIONS.items():
        assert tool_id in description
        assert "raw account" not in description.casefold()
        assert "file path" not in description.casefold()
        assert "never places, modifies, cancels, or approves a saxo order" in (
            description.casefold()
        )
