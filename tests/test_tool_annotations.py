from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from typing import Final

import pytest
from fastmcp import Client
from mcp.types import Tool as McpTool
from mcp.types import ToolAnnotations

from saxo_bank_mcp.tool_metadata import metadata_for_tool

EXPECTED_TOOL_COUNT: Final = 39
STANDARD_HINT_FIELDS: Final = (
    "readOnlyHint",
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
)
PURE_READ_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "saxo_health",
        "saxo_auth_status",
        "saxo_get_session_capabilities",
        "saxo_get_entitlements",
        "saxo_list_live_accounts",
        "saxo_list_registered_endpoints",
        "saxo_call_registered_endpoint",
        "saxo_safety_status",
        "saxo_get_multileg_order_defaults",
        "saxo_get_required_disclaimers",
        "saxo_list_trading_write_operations",
        "saxo_precheck_live_order",
    },
)
NON_DESTRUCTIVE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        *PURE_READ_TOOLS,
        "saxo_create_write_preview",
        "saxo_create_order_preview",
        "saxo_prepare_trading_write",
    },
)
PURE_IDEMPOTENT_TOOLS: Final[frozenset[str]] = PURE_READ_TOOLS - frozenset(
    {"saxo_precheck_live_order"},
)
LOCAL_CLOSED_WORLD_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "saxo_health",
        "saxo_auth_status",
        "saxo_start_pkce_login",
        "saxo_cache_sim_access_token",
        "saxo_get_safe_request_ledger",
        "saxo_list_registered_endpoints",
        "saxo_safety_status",
        "saxo_create_write_preview",
        "saxo_commit_write_preview",
        "saxo_list_trading_write_operations",
        "saxo_prepare_trading_write",
    },
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_registered_tools_publish_all_standard_annotations() -> None:
    # Given: the real FastMCP server registrations.
    tools = await _registered_tools()

    # When: the host asks for the runtime tool list.
    tool_count = len(tools)

    # Then: every listed tool exposes every standard MCP annotation hint.
    assert tool_count == EXPECTED_TOOL_COUNT
    missing = [
        tool.name
        for tool in tools
        if tool.annotations is None or _missing_standard_hints(tool.annotations)
    ]
    assert missing == []


@pytest.mark.anyio
async def test_annotation_map_matches_runtime_registration() -> None:
    # Given: the real runtime list and the reviewed annotation map.
    annotations_module = import_module("saxo_bank_mcp.tool_annotations")
    runtime_ids = frozenset(tool.name for tool in await _registered_tools())

    # When: the map is validated against the runtime list.
    annotations_module.assert_tool_annotations_cover(runtime_ids)

    # Then: the test uses the runtime list as truth, not a copied 39-name mirror.
    assert len(runtime_ids) == EXPECTED_TOOL_COUNT


def test_conservative_annotation_groups_match_reviewed_policy() -> None:
    # Given: the single reviewed annotation map.
    annotations_module = import_module("saxo_bank_mcp.tool_annotations")
    annotations = _annotation_map_from_module(annotations_module.TOOL_ANNOTATIONS)

    # When: policy-critical groups are inspected.
    read_only = frozenset(
        tool_id for tool_id, hint in annotations.items() if hint.readOnlyHint is True
    )
    non_destructive = frozenset(
        tool_id for tool_id, hint in annotations.items() if hint.destructiveHint is False
    )
    idempotent = frozenset(
        tool_id for tool_id, hint in annotations.items() if hint.idempotentHint is True
    )
    closed_world = frozenset(
        tool_id for tool_id, hint in annotations.items() if hint.openWorldHint is False
    )

    # Then: annotations stay advisory and conservative.
    assert read_only == PURE_READ_TOOLS
    assert non_destructive == NON_DESTRUCTIVE_TOOLS
    assert idempotent == PURE_IDEMPOTENT_TOOLS
    assert closed_world == LOCAL_CLOSED_WORLD_TOOLS


def test_preview_metadata_matches_configured_sim_and_live_preview_behavior() -> None:
    # Given: the metadata published through saxo_safety_status.
    metadata = metadata_for_tool("saxo_create_order_preview")

    # When: saxo_create_order_preview metadata is inspected.
    assert metadata is not None

    # Then: it describes configured SIM/LIVE preview behavior, not SIM-only execution.
    assert metadata["tool_class"] == "order_precheck_preview"
    assert metadata["environment_support"] == ["SIM", "LIVE_WRITE"]
    assert metadata["write_effect"] == "live_network"
    assert metadata["state_changing"] is True
    assert metadata["safe_in_live_read_mode"] is False
    hint = metadata["agent_hint"]
    assert "configured SIM or LIVE-write mode" in hint
    assert "preview/precheck does not place, modify, or cancel an order" in hint
    assert "SIM-only" not in hint
    assert "refuses before network when configured for LIVE" not in hint


@pytest.mark.anyio
async def test_annotation_validator_names_only_missing_runtime_tool() -> None:
    # Given: a copied map with one runtime tool removed.
    annotations_module = import_module("saxo_bank_mcp.tool_annotations")
    runtime_ids = frozenset(tool.name for tool in await _registered_tools())
    copied = dict(_annotation_map_from_module(annotations_module.TOOL_ANNOTATIONS))
    copied.pop("saxo_create_order_preview")

    # When: the copied map is validated.
    with pytest.raises(annotations_module.ToolAnnotationDriftError) as error:
        annotations_module.assert_tool_annotations_cover(runtime_ids, copied)

    # Then: the failure names only the omitted logical ID.
    assert error.value.missing_tool_ids == ("saxo_create_order_preview",)
    assert error.value.unknown_tool_ids == ()
    assert str(error.value) == (
        "tool annotation map drift: missing=saxo_create_order_preview; unknown=none"
    )


async def _registered_tools() -> list[McpTool]:
    module = import_module("saxo_bank_mcp.server")
    async with Client(module.mcp) as client:
        return list(await client.list_tools())


def _missing_standard_hints(annotations: ToolAnnotations) -> tuple[str, ...]:
    dumped = annotations.model_dump(exclude_none=False)
    return tuple(
        field
        for field in STANDARD_HINT_FIELDS
        if field not in dumped or not isinstance(dumped[field], bool)
    )


def _annotation_map_from_module(
    value: Mapping[str, ToolAnnotations],
) -> Mapping[str, ToolAnnotations]:
    return value
