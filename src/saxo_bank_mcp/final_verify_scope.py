from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from saxo_bank_mcp import tribunal_index
from saxo_bank_mcp._evidence import write_text
from saxo_bank_mcp.final_verify_common import (
    JSON_MAPPING_ADAPTER,
    GitStateProvider,
    render_report,
)

SCOPE_REQUIRED_PATHS = (
    "data/saxo_endpoint_registry.json",
    ".omo/evidence/saxo-bank-mcp/task-9-tribunal-index.json",
)


def registry_tool_ids(path: Path) -> tuple[frozenset[str], str | None]:
    if not path.exists():
        return frozenset(), "missing"
    try:
        payload = JSON_MAPPING_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        return frozenset(), type(exc).__name__
    candidates = payload.get("tools") or payload.get("operations") or payload.get("tool_ids")
    if not isinstance(candidates, list):
        return frozenset(), "missing tools/operations/tool_ids list"
    tool_ids: set[str] = set()
    for item in candidates:
        match item:
            case str():
                tool_ids.add(item)
            case dict():
                value = (
                    item.get("tool_id")
                    or item.get("mcp_tool_id")
                    or item.get("id")
                    or item.get("name")
                )
                if isinstance(value, str):
                    tool_ids.add(value)
            case _:
                continue
    if not tool_ids:
        return frozenset(), "empty expected tool list"
    return frozenset(tool_ids), None


def run_scope_tribunal_index(expected_tools: frozenset[str]) -> tuple[str, bool, str]:
    if not expected_tools:
        return "tribunal_index run", False, "empty expected tool list"
    tools_file = Path(".omo/evidence/saxo-bank-mcp/final-scope-tools.json")
    write_text(tools_file, json.dumps(sorted(expected_tools), indent=2) + "\n")
    result = tribunal_index.main(
        [
            "--tools-file",
            str(tools_file),
            "--out",
            ".omo/evidence/saxo-bank-mcp/final-scope-tribunal-index.json",
        ],
    )
    detail = "exit 0" if result == 0 else f"exit {result}"
    return "tribunal_index run", result == 0, detail


def verify_scope(out: Path, git_state_provider: GitStateProvider) -> int:
    registry_path = Path("data/saxo_endpoint_registry.json")
    expected_tools, registry_error = registry_tool_ids(registry_path)
    checks = [
        (path, Path(path).exists(), "present" if Path(path).exists() else "missing")
        for path in SCOPE_REQUIRED_PATHS
    ]
    checks.append(
        (
            "endpoint registry expected tools",
            registry_error is None,
            f"{len(expected_tools)} tools" if registry_error is None else registry_error,
        ),
    )
    checks.append(run_scope_tribunal_index(expected_tools))
    passed = all(ok for _, ok, _ in checks)
    write_text(
        out,
        render_report(
            "Scope Fidelity Gate",
            passed=passed,
            checks=checks,
            git_state_provider=git_state_provider,
        ),
    )
    return 0 if passed else 1
