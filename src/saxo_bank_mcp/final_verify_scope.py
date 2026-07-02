from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from saxo_bank_mcp import tribunal_index
from saxo_bank_mcp._evidence import write_text
from saxo_bank_mcp.endpoint_registry import load_inventory, validate_inventory
from saxo_bank_mcp.final_verify_common import (
    JSON_MAPPING_ADAPTER,
    GitStateProvider,
    render_report,
)

INVENTORY_PATH = Path("data/saxo/openapi_inventory.json")
FINAL_SCOPE_TRIBUNAL_INDEX = Path(
    ".omo/evidence/saxo-bank-mcp/final-scope-tribunal-index.json",
)
SCOPE_REQUIRED_PATHS = (
    str(INVENTORY_PATH),
    ".omo/evidence/saxo-bank-mcp/task-9-tribunal-index.json",
)


def registry_tool_ids(path: Path) -> tuple[frozenset[str], str | None]:
    if not path.exists():
        return frozenset(), "missing inventory"
    return tribunal_index.list_registered_mcp_tool_ids(), None


def inventory_validation_check(path: Path) -> tuple[str, bool, str]:
    try:
        inventory = load_inventory(path)
        validation = validate_inventory(inventory)
    except (OSError, ValidationError) as exc:
        return "Saxo inventory validation", False, type(exc).__name__
    passed = validation.get("status") == "passed"
    detail = "passed" if passed else "failed"
    return "Saxo inventory validation", passed, detail


def run_scope_tribunal_index() -> tuple[str, bool, str]:
    result = tribunal_index.main(
        [
            "--out",
            str(FINAL_SCOPE_TRIBUNAL_INDEX),
        ],
    )
    detail = "exit 0" if result == 0 else f"exit {result}"
    return "tribunal_index run", result == 0, detail


def tribunal_index_state_check(
    path: Path = FINAL_SCOPE_TRIBUNAL_INDEX,
) -> tuple[str, bool, str]:
    if not path.exists():
        return "tribunal_index no deferred state", False, f"missing {path}"
    try:
        payload = JSON_MAPPING_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        return "tribunal_index no deferred state", False, type(exc).__name__

    status = payload.get("status")
    if status != "passed":
        return "tribunal_index no deferred state", False, f"status={status!r}"

    incomplete_tool_ids = payload.get("incomplete_tool_ids")
    if payload.get("no_deferred_state") is not True:
        return (
            "tribunal_index no deferred state",
            False,
            f"no_deferred_state=false incomplete_tool_ids={incomplete_tool_ids!r}",
        )

    remaining_feedback = payload.get("remaining_actionable_feedback_complete_tool_ids")
    if remaining_feedback != []:
        return (
            "tribunal_index no remaining feedback",
            False,
            f"remaining_actionable_feedback_complete_tool_ids={remaining_feedback!r}",
        )
    return "tribunal_index no deferred state", True, "passed"


def verify_scope(out: Path, git_state_provider: GitStateProvider) -> int:
    checks = [
        (path, Path(path).exists(), "present" if Path(path).exists() else "missing")
        for path in SCOPE_REQUIRED_PATHS
    ]
    checks.append(inventory_validation_check(INVENTORY_PATH))
    checks.append(run_scope_tribunal_index())
    checks.append(tribunal_index_state_check())
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
