from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue, now_utc, write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.hard_task_manifest import DEFAULT_INCOMPLETE_TOOL_IDS
from saxo_bank_mcp.loop_manifest import GitState, current_git_state

JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


class HardTaskSummaryRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(min_length=1)
    receipt: str = Field(min_length=1)
    status: str = Field(min_length=1)
    fastmcp_called: bool
    git_sha: str = Field(min_length=1)
    git_dirty: bool
    secret_scan_clean: bool
    live_write: bool
    completion_claim_allowed: bool | None = None
    error: str = ""


class HardTaskExecutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_at: str
    command: Literal["hard_task_summary"]
    driver: Literal["loop_harness"]
    status: Literal["passed", "failed"]
    tool_count: int
    status_counts: dict[str, int]
    all_fastmcp_called: bool
    all_git_clean: bool
    all_secret_scans_clean: bool
    any_live_write: bool
    failed_tools: tuple[str, ...]
    rows: tuple[HardTaskSummaryRow, ...]
    git: GitState

    def to_json_value(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


def build_hard_task_execution_summary(
    receipts_dir: Path,
    *,
    expected_tool_ids: Iterable[str] = DEFAULT_INCOMPLETE_TOOL_IDS,
    git: GitState | None = None,
) -> HardTaskExecutionSummary:
    current_git = current_git_state() if git is None else git
    rows = tuple(
        _summary_row(receipts_dir / f"{tool_id}.json", tool_id, current_git)
        for tool_id in sorted(expected_tool_ids)
    )
    failed_tools = tuple(sorted(row.tool_id for row in rows if row.error))
    return HardTaskExecutionSummary(
        checked_at=now_utc(),
        command="hard_task_summary",
        driver="loop_harness",
        status="passed" if not failed_tools else "failed",
        tool_count=len(rows),
        status_counts=dict(sorted(Counter(row.status for row in rows).items())),
        all_fastmcp_called=all(row.fastmcp_called for row in rows),
        all_git_clean=all(
            not row.git_dirty and row.git_sha == current_git.sha for row in rows
        ),
        all_secret_scans_clean=all(row.secret_scan_clean for row in rows),
        any_live_write=any(row.live_write for row in rows),
        failed_tools=failed_tools,
        rows=rows,
        git=current_git,
    )


def handle_hard_task_summary(
    out: Path,
    receipts_dir: Path,
    *,
    expected_tool_ids: Iterable[str] = DEFAULT_INCOMPLETE_TOOL_IDS,
    git: GitState | None = None,
) -> int:
    summary = build_hard_task_execution_summary(
        receipts_dir,
        expected_tool_ids=expected_tool_ids,
        git=git,
    )
    payload = redact_json(summary.to_json_value())
    if not isinstance(payload, dict):
        raise TypeError("hard task summary redaction returned non-object")
    write_json(out, payload)
    findings, scan_errors = scan_secret_paths([str(out), str(receipts_dir)])
    payload["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, payload)
    clean = not findings and not scan_errors
    return 0 if summary.status == "passed" and clean else 1


def _summary_row(path: Path, tool_id: str, current_git: GitState) -> HardTaskSummaryRow:
    payload, load_error = _load_receipt(path)
    if load_error is not None or payload is None:
        return HardTaskSummaryRow(
            tool_id=tool_id,
            receipt=str(path),
            status="missing",
            fastmcp_called=False,
            git_sha="missing",
            git_dirty=True,
            secret_scan_clean=False,
            live_write=False,
            error=str(load_error),
        )

    git_sha, git_dirty = _receipt_git(payload)
    row = HardTaskSummaryRow(
        tool_id=tool_id,
        receipt=str(path),
        status=_string_field(payload, "status", default="unknown"),
        fastmcp_called=payload.get("fastmcp_called") is True,
        git_sha=git_sha,
        git_dirty=git_dirty,
        secret_scan_clean=_secret_scan_clean(payload),
        live_write=payload.get("live_write") is True,
        completion_claim_allowed=_optional_bool(payload.get("completion_claim_allowed")),
        error="",
    )
    return row.model_copy(update={"error": _row_error(row, current_git)})


def _load_receipt(path: Path) -> tuple[dict[str, JsonValue] | None, str | None]:
    if not path.exists():
        return None, "missing receipt"
    try:
        return JSON_OBJECT_ADAPTER.validate_json(path.read_text(encoding="utf-8")), None
    except (OSError, ValidationError) as exc:
        return None, f"invalid receipt: {type(exc).__name__}"


def _receipt_git(payload: Mapping[str, JsonValue]) -> tuple[str, bool]:
    raw_git = payload.get("git")
    if not isinstance(raw_git, Mapping):
        return "missing", True
    raw_sha = raw_git.get("sha")
    raw_dirty = raw_git.get("dirty")
    sha = raw_sha if isinstance(raw_sha, str) and raw_sha else "missing"
    dirty = raw_dirty if isinstance(raw_dirty, bool) else True
    return sha, dirty


def _secret_scan_clean(payload: Mapping[str, JsonValue]) -> bool:
    raw_scan = payload.get("secret_scan")
    if not isinstance(raw_scan, Mapping):
        return False
    return raw_scan.get("findings") == [] and raw_scan.get("scan_errors") == []


def _string_field(
    payload: Mapping[str, JsonValue],
    key: str,
    *,
    default: str,
) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) and value else default


def _optional_bool(value: JsonValue | None) -> bool | None:
    return value if isinstance(value, bool) else None


def _row_error(row: HardTaskSummaryRow, current_git: GitState) -> str:
    errors: list[str] = []
    if row.status == "failed":
        errors.append("receipt status is failed")
    if not row.fastmcp_called:
        errors.append("receipt did not prove FastMCP call")
    if row.git_dirty:
        errors.append("receipt generated from dirty git state")
    if row.git_sha != current_git.sha:
        errors.append("receipt git SHA does not match current HEAD")
    if not row.secret_scan_clean:
        errors.append("receipt secret scan is not clean")
    if row.live_write:
        errors.append("receipt attempted live write")
    return errors[0] if errors else ""
