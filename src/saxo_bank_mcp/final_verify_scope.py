from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from saxo_bank_mcp import tribunal_index
from saxo_bank_mcp._evidence import write_text
from saxo_bank_mcp.endpoint_registry import load_inventory, validate_inventory
from saxo_bank_mcp.final_verify_common import (
    GitStateProvider,
    render_report,
)

INVENTORY_PATH = Path("data/saxo/openapi_inventory.json")
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
            ".omo/evidence/saxo-bank-mcp/final-scope-tribunal-index.json",
        ],
    )
    detail = "exit 0" if result == 0 else f"exit {result}"
    return "tribunal_index run", result == 0, detail


def verify_scope(out: Path, git_state_provider: GitStateProvider) -> int:
    checks = [
        (path, Path(path).exists(), "present" if Path(path).exists() else "missing")
        for path in SCOPE_REQUIRED_PATHS
    ]
    checks.append(inventory_validation_check(INVENTORY_PATH))
    checks.append(run_scope_tribunal_index())
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
