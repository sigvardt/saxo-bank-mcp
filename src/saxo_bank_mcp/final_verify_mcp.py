from __future__ import annotations

from pathlib import Path

from saxo_bank_mcp._evidence import write_text
from saxo_bank_mcp.final_verify_common import (
    GitStateProvider,
    evidence_status_check,
    render_report,
)

MCP_REQUIRED_EVIDENCE = (
    ".omo/evidence/saxo-bank-mcp/task-2-sim-auth.json",
    ".omo/evidence/saxo-bank-mcp/task-4-read-smoke.json",
    ".omo/evidence/saxo-bank-mcp/task-6-precheck.json",
    ".omo/evidence/saxo-bank-mcp/task-7-sim-order.json",
    ".omo/evidence/saxo-bank-mcp/task-8-stream.json",
    ".omo/evidence/saxo-bank-mcp/task-10-live-write-refusal.json",
)
MCP_ALLOWED_STATUSES: dict[str, frozenset[str]] = {
    ".omo/evidence/saxo-bank-mcp/task-2-sim-auth.json": frozenset({"passed", "complete"}),
    ".omo/evidence/saxo-bank-mcp/task-4-read-smoke.json": frozenset({"passed", "exercised"}),
    ".omo/evidence/saxo-bank-mcp/task-6-precheck.json": frozenset({"passed", "exercised"}),
    ".omo/evidence/saxo-bank-mcp/task-7-sim-order.json": frozenset({"passed", "exercised"}),
    ".omo/evidence/saxo-bank-mcp/task-8-stream.json": frozenset({"passed", "exercised"}),
    ".omo/evidence/saxo-bank-mcp/task-10-live-write-refusal.json": frozenset({"refused"}),
}


def verify_mcp(out: Path, git_state_provider: GitStateProvider) -> int:
    checks = [
        evidence_status_check(path, MCP_ALLOWED_STATUSES[path], git_state_provider)
        for path in MCP_REQUIRED_EVIDENCE
    ]
    passed = all(ok for _, ok, _ in checks)
    write_text(
        out,
        render_report(
            "MCP Manual QA Gate",
            passed=passed,
            checks=checks,
            git_state_provider=git_state_provider,
        ),
    )
    return 0 if passed else 1
