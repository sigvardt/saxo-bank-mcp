from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import scan_secret_paths
from saxo_bank_mcp.evidence_publication import write_scanned_text
from saxo_bank_mcp.final_verify_common import (
    JSON_MAPPING_ADAPTER,
    GitStateProvider,
    evidence_status_check,
    render_report,
)

HARD_TASK_RECEIPTS_DIR = Path(".omo/evidence/saxo-bank-mcp/strict-g003-hard-tasks")
MCP_HARD_TASK_SUMMARY = ".omo/evidence/saxo-bank-mcp/strict-g003-hard-task-execution-summary.json"
MCP_REQUIRED_EVIDENCE = (
    ".omo/evidence/saxo-bank-mcp/task-2-sim-auth.json",
    ".omo/evidence/saxo-bank-mcp/task-4-read-smoke.json",
    ".omo/evidence/saxo-bank-mcp/task-6-precheck.json",
    ".omo/evidence/saxo-bank-mcp/task-7-sim-order.json",
    ".omo/evidence/saxo-bank-mcp/task-8-stream.json",
    ".omo/evidence/saxo-bank-mcp/task-10-live-write-refusal.json",
    ".omo/evidence/saxo-bank-mcp/strict-g003-hard-task-manifest.json",
)
MCP_ALLOWED_STATUSES: dict[str, frozenset[str]] = {
    ".omo/evidence/saxo-bank-mcp/task-2-sim-auth.json": frozenset({"passed", "complete"}),
    ".omo/evidence/saxo-bank-mcp/task-4-read-smoke.json": frozenset({"passed", "exercised"}),
    ".omo/evidence/saxo-bank-mcp/task-6-precheck.json": frozenset({"passed", "exercised"}),
    ".omo/evidence/saxo-bank-mcp/task-7-sim-order.json": frozenset({"passed", "exercised"}),
    ".omo/evidence/saxo-bank-mcp/task-8-stream.json": frozenset({"passed", "exercised"}),
    ".omo/evidence/saxo-bank-mcp/task-10-live-write-refusal.json": frozenset({"refused"}),
    ".omo/evidence/saxo-bank-mcp/strict-g003-hard-task-manifest.json": frozenset(
        {"passed"},
    ),
}
HARD_TASK_ROWS_ADAPTER = TypeAdapter(list[dict[str, JsonValue]])


def _load_summary_payload(evidence_path: Path) -> tuple[dict[str, JsonValue] | None, str | None]:
    if not evidence_path.exists():
        return None, "missing"
    try:
        return (
            JSON_MAPPING_ADAPTER.validate_json(evidence_path.read_text(encoding="utf-8")),
            None,
        )
    except (OSError, ValidationError) as exc:
        return None, f"invalid JSON evidence: {type(exc).__name__}"


def _summary_flag_error(payload: dict[str, JsonValue]) -> str | None:
    required_flags = (
        ("all_fastmcp_called", True),
        ("all_git_clean", True),
        ("all_secret_scans_clean", True),
        ("any_live_write", False),
    )
    for key, expected in required_flags:
        if payload.get(key) is not expected:
            return f"{key}={payload.get(key)!r}"
    if payload.get("failed_tools") != []:
        return f"failed_tools={payload.get('failed_tools')!r}"
    return None


def _summary_rows(payload: dict[str, JsonValue]) -> tuple[list[dict[str, JsonValue]], str | None]:
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        return [], "missing hard-task rows"
    try:
        rows = HARD_TASK_ROWS_ADAPTER.validate_python(raw_rows)
    except ValidationError as exc:
        return [], f"invalid hard-task rows: {type(exc).__name__}"
    tool_count = payload.get("tool_count")
    if isinstance(tool_count, int) and tool_count != len(rows):
        return [], f"tool_count={tool_count} rows={len(rows)}"
    return rows, None


def _summary_receipts_error(rows: list[dict[str, JsonValue]], current_sha: str) -> str | None:
    seen_tool_ids: set[str] = set()
    for index, row in enumerate(rows):
        tool_id = row.get("tool_id")
        if not isinstance(tool_id, str) or not tool_id:
            return f"missing hard-task tool_id at row {index}"
        if tool_id in seen_tool_ids:
            return f"duplicate hard-task receipt: {tool_id}"
        seen_tool_ids.add(tool_id)
        if row.get("git_dirty") is not False:
            return f"{tool_id}: hard-task summary was dirty"
        row_sha = row.get("git_sha")
        if row_sha != current_sha:
            return (
                f"{tool_id}: hard-task summary SHA {row_sha!r} "
                f"does not match current HEAD {current_sha!r}"
            )
        receipt = HARD_TASK_RECEIPTS_DIR / f"{tool_id}.json"
        if not receipt.exists():
            return f"missing hard-task receipt: {receipt}"
    return None


def hard_task_summary_check(
    path: str,
    git_state_provider: GitStateProvider,
) -> tuple[str, bool, str]:
    payload, load_error = _load_summary_payload(Path(path))
    if load_error is not None or payload is None:
        return path, False, str(load_error)

    flag_error = _summary_flag_error(payload)
    if flag_error is not None:
        return path, False, flag_error

    rows, rows_error = _summary_rows(payload)
    if rows_error is not None:
        return path, False, rows_error

    receipts_error = _summary_receipts_error(rows, git_state_provider().sha)
    if receipts_error is not None:
        return path, False, receipts_error

    return path, True, f"{len(rows)} hard-task receipts"


def mcp_evidence_secret_scan_check() -> tuple[str, bool, str]:
    paths = [
        *MCP_REQUIRED_EVIDENCE,
        MCP_HARD_TASK_SUMMARY,
        str(HARD_TASK_RECEIPTS_DIR),
    ]
    findings, scan_errors = scan_secret_paths(paths)
    if findings or scan_errors:
        return (
            "MCP evidence secret scan",
            False,
            f"findings={len(findings)} scan_errors={len(scan_errors)}",
        )
    return "MCP evidence secret scan", True, f"{len(paths)} paths clean"


def verify_mcp(out: Path, git_state_provider: GitStateProvider) -> int:
    checks = [
        evidence_status_check(path, MCP_ALLOWED_STATUSES[path], git_state_provider)
        for path in MCP_REQUIRED_EVIDENCE
    ]
    checks.append(hard_task_summary_check(MCP_HARD_TASK_SUMMARY, git_state_provider))
    checks.append(mcp_evidence_secret_scan_check())
    passed = all(ok for _, ok, _ in checks)
    published = write_scanned_text(
        out,
        render_report(
            "MCP Manual QA Gate",
            passed=passed,
            checks=checks,
            git_state_provider=git_state_provider,
        ),
    )
    return 0 if passed and published else 1
