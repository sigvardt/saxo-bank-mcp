from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest
from analytics_source_matrix_support import REPOSITORY_COPY_IGNORE
from fastmcp import Client
from mcp.types import Tool as McpTool
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_catalog_statuses import emitted_statuses, status_discovery
from saxo_bank_mcp.endpoint_registry import load_inventory
from saxo_bank_mcp.server import mcp

ROOT: Final = Path(__file__).resolve().parents[1]
SCRIPT: Final = ROOT / "scripts/generate_agent_skill_catalogs.py"
SOURCE_PATHS: Final = (
    ROOT / "data/saxo/agent_tool_routes.json",
    ROOT / "data/saxo/agent_status_routes.json",
    ROOT / "data/saxo/agent_tool_scenarios.json",
)
GENERATED_PATHS: Final = (
    ROOT / "skills/saxo-bank/references/tool-catalog.md",
    ROOT / "skills/saxo-openapi/references/operation-catalog.md",
    ROOT / "skills/saxo-safety-recovery/references/status-recovery.md",
    ROOT / "skills/saxo-qa-operations/references/scenario-coverage.md",
)
JSON_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
OBSERVED_FIX_STATUS_VALUES: Final = frozenset(
    {
        "account_lookup_unavailable",
        "authentication_required",
        "cache_replace_blocked",
        "forbidden",
        "instrument_lookup_unavailable",
        "precheck_unavailable",
    },
)
OBSERVED_FIX_STATUS_SOURCES: Final = {
    "account_lookup_unavailable": "live_precheck_results.http_failure_result",
    "authentication_required": "live_precheck_results._HTTP_FAILURE_STATUSES",
    "cache_replace_blocked": "mcp_portal_token_tools.saxo_cache_sim_access_token",
    "forbidden": "live_precheck_results._HTTP_FAILURE_STATUSES",
    "instrument_lookup_unavailable": "live_precheck_results.http_failure_result",
    "precheck_unavailable": "live_precheck_results.http_failure_result",
}
SECOND_FIX_STATUS_VALUES: Final = frozenset(
    {
        "account_id_invalid",
        "account_ref_invalid",
        "account_selection_required",
        "instrument_not_eligible",
        "invalid_account_response",
        "refused",
    },
)
SECOND_FIX_STATUS_SOURCES: Final = {
    "account_id_invalid": "mcp_live_trade_tools._select_account",
    "account_lookup_failed": "mcp_live_account_tools.saxo_list_live_accounts",
    "account_ref_invalid": "mcp_live_trade_tools._select_account",
    "account_selection_required": "mcp_live_trade_tools._select_account",
    "accounts_listed": "mcp_live_account_tools.saxo_list_live_accounts",
    "instrument_not_eligible": "mcp_live_trade_tools._validate_instrument",
    "invalid_account_response": "mcp_live_account_tools/mcp_live_trade_tools",
    "invalid_arguments": "fastmcp_logging_safety._ValidationSafeFunctionTool",
    "refused": "live_mode/mcp_live_account_tools/mcp_live_trade_tools",
    "tool_error": "fastmcp_logging_safety.generic_tool_error_result",
    "unknown_tool": "fastmcp_logging_safety.SafeFastMCP.call_tool",
}
TASK_9_STATUS_MODULES: Final = frozenset(
    {
        "src/saxo_bank_mcp/analytics_account_data.py",
        "src/saxo_bank_mcp/analytics_portfolio_snapshots.py",
    },
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_catalog_check_passes_for_runtime_and_inventory_contract() -> None:
    # Given: Todo 4 requires a checked deterministic generator and reviewed sources.
    runtime_tools = await _registered_tools()

    # When: the generator check runs against the real worktree.
    result = _run_generator("--check")

    # Then: the binary verdict is green and the checked counts match runtime truth.
    assert result.returncode == 0, result.stderr
    assert f"tool_count={len(runtime_tools)}" in result.stdout
    assert "operation_count=294" in result.stdout
    assert "implemented_count=182" in result.stdout
    assert "refused_count=112" in result.stdout
    assert "service_group_count=17" in result.stdout
    assert "scenarios=60" in result.stdout
    assert "only_completed_unqualified_success=true" in result.stdout


@pytest.mark.anyio
async def test_reviewed_sources_cover_runtime_tools_without_stale_operation_ids() -> None:
    # Given: runtime tools and the official Saxo inventory are the authoritative facts.
    runtime_tool_names = frozenset(tool.name for tool in await _registered_tools())
    operation_ids = frozenset(operation.operation_id for operation in load_inventory().operations)

    # When: the reviewed route and scenario sources are parsed.
    route_tools = frozenset(_source_tool_names(SOURCE_PATHS[0], "tool_routes"))
    scenario_tools = frozenset(_source_tool_names(SOURCE_PATHS[2], "scenarios"))
    explicit_operations = _explicit_operation_ids(SOURCE_PATHS[0])

    # Then: source rows cover all runtime tools and never name unknown Saxo operations.
    assert route_tools == runtime_tool_names
    assert scenario_tools == runtime_tool_names
    assert explicit_operations <= operation_ids


def test_generation_is_byte_stable_after_second_run() -> None:
    # Given: generated Markdown already matches the source tree.
    before = {path: path.read_bytes() for path in GENERATED_PATHS}

    # When: generation runs twice.
    first = _run_generator()
    second = _run_generator()

    # Then: both invocations succeed and bytes stay unchanged.
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert {path: path.read_bytes() for path in GENERATED_PATHS} == before


def test_status_source_covers_observed_emitted_statuses() -> None:
    # Given: production result code emits additional non-success status values.
    emitted = emitted_statuses(ROOT)
    status_rows = _status_rows(SOURCE_PATHS[1])

    # When: the reviewed status source is checked against production emissions.
    missing = emitted - frozenset(status_rows)
    unqualified_successes = {
        status
        for status, row in status_rows.items()
        if row.get("unqualified_mutation_success") is True
    }

    # Then: every emitted status is routed and only completed is plain success.
    assert emitted >= OBSERVED_FIX_STATUS_VALUES
    assert missing == frozenset()
    assert unqualified_successes == {"completed"}
    for status, source in OBSERVED_FIX_STATUS_SOURCES.items():
        row = status_rows[status]
        wording = row.get("safe_user_wording")
        assert row.get("source") == source
        assert row.get("mutation_possible") is False
        assert row.get("unqualified_mutation_success") is False
        assert isinstance(wording, str)
        assert "success" not in wording.lower()


def test_status_source_covers_live_mode_and_live_trade_emissions() -> None:
    # Given: LIVE mode and LIVE trade tools emit agent-visible non-success statuses.
    emitted = emitted_statuses(ROOT)
    status_rows = _status_rows(SOURCE_PATHS[1])

    # When: catalog status coverage is compared with discovered production emissions.
    missing = SECOND_FIX_STATUS_VALUES - frozenset(status_rows)

    # Then: the discovery includes and routes those LIVE-mode values.
    assert emitted >= SECOND_FIX_STATUS_VALUES
    assert missing == frozenset()
    for status, source in SECOND_FIX_STATUS_SOURCES.items():
        row = status_rows[status]
        assert row.get("source") == source
        assert row.get("unqualified_mutation_success") is False


def test_status_discovery_classifies_every_candidate_module() -> None:
    # Given: production package scanning finds literal status emission candidates.
    discovery = status_discovery(ROOT)
    included = frozenset(item.module for item in discovery.included)
    excluded = frozenset(item.module for item in discovery.excluded)

    # When: classification is checked for unreviewed or stale modules.
    unclassified = tuple(item.module for item in discovery.unclassified)

    # Then: every candidate is reviewed and LIVE tool modules are included.
    assert unclassified == ()
    assert discovery.stale_classifications == ()
    assert "src/saxo_bank_mcp/live_mode.py" in included
    assert "src/saxo_bank_mcp/mcp_live_trade_tools.py" in included
    assert "src/saxo_bank_mcp/mcp_live_account_tools.py" in included
    assert excluded >= TASK_9_STATUS_MODULES


def test_generated_source_hashes_are_checkout_path_independent(tmp_path: Path) -> None:
    # Given: the same source tree exists at two different absolute roots.
    first_root = _copy_worktree(tmp_path / "checkout-a")
    second_root = _copy_worktree(tmp_path / "nested/checkout-b")

    # When: each checkout regenerates the catalogs.
    first = _run_generator_in(first_root)
    second = _run_generator_in(second_root)

    # Then: all generated files are byte-identical across absolute paths.
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    for path in GENERATED_PATHS:
        relative = path.relative_to(ROOT)
        assert (first_root / relative).read_bytes() == (second_root / relative).read_bytes()


def test_explicit_operation_ids_reads_plural_route_field(tmp_path: Path) -> None:
    # Given: route data uses the real plural operation_ids field.
    source = tmp_path / "routes.json"
    source.write_text(
        json.dumps({"tool_routes": [{"operation_ids": ["get.fixture.stale"]}]}),
        encoding="utf-8",
    )

    # When: the stale-operation guard collects explicit route IDs.
    found = _explicit_operation_ids(source)

    # Then: plural explicit IDs are visible to the source test.
    assert found == frozenset({"get.fixture.stale"})


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("unknown-tool", "unknown_tool"),
        ("stale-operation", "stale_operation"),
        ("missing-status", "missing_status"),
        ("unclassified-status-module", "unclassified_status_module"),
        ("uncovered-tool", "uncovered_tool"),
    ],
)
def test_failure_fixtures_return_distinct_sanitized_errors(
    fixture_name: str,
    expected: str,
) -> None:
    # Given: each drift fixture isolates one class of catalog failure.
    redaction_canary = "CANARY_DO_NOT_ECHO"

    # When: the generator validates that fixture.
    result = _run_generator("--self-test-fixture", fixture_name, redaction_canary)

    # Then: it fails with only the sanitized failure class.
    assert result.returncode != 0
    combined = f"{result.stdout}\n{result.stderr}"
    assert expected in combined
    assert redaction_canary not in combined


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    assert SCRIPT.exists(), f"missing generator: {SCRIPT.relative_to(ROOT)}"
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def _run_generator_in(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/generate_agent_skill_catalogs.py"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )


def _copy_worktree(target: Path) -> Path:
    shutil.copytree(ROOT, target, ignore=REPOSITORY_COPY_IGNORE)
    return target


async def _registered_tools() -> list[McpTool]:
    async with Client(mcp) as client:
        return list(await client.list_tools())


def _json(path: Path) -> JsonValue:
    assert path.exists(), f"missing source: {path.relative_to(ROOT)}"
    return JSON_ADAPTER.validate_python(json.loads(path.read_text(encoding="utf-8")))


def _source_tool_names(path: Path, key: str) -> tuple[str, ...]:
    data = _json(path)
    assert isinstance(data, dict)
    rows = data.get(key)
    assert isinstance(rows, list)
    names = [row.get("tool") for row in rows if isinstance(row, dict)]
    assert all(isinstance(name, str) for name in names)
    return tuple(str(name) for name in names)


def _status_rows(path: Path) -> dict[str, dict[str, JsonValue]]:
    data = _json(path)
    assert isinstance(data, dict)
    rows = data.get("status_routes")
    assert isinstance(rows, list)
    statuses: dict[str, dict[str, JsonValue]] = {}
    for row in rows:
        assert isinstance(row, dict)
        status = row.get("status")
        assert isinstance(status, str)
        statuses[status] = row
    return statuses


def _explicit_operation_ids(path: Path) -> frozenset[str]:
    data = _json(path)
    found: set[str] = set()
    _collect_operation_ids(data, found)
    return frozenset(found)


def _collect_operation_ids(value: JsonValue, found: set[str]) -> None:
    if isinstance(value, dict):
        operation_ids = value.get("operation_ids")
        if isinstance(operation_ids, list):
            found.update(item for item in operation_ids if isinstance(item, str))
        for child in value.values():
            _collect_operation_ids(child, found)
    elif isinstance(value, list):
        for child in value:
            _collect_operation_ids(child, found)
