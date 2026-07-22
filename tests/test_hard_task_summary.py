from __future__ import annotations

import json
from pathlib import Path

import pytest

from saxo_bank_mcp import hard_task_summary as summary_module
from saxo_bank_mcp import qa
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.hard_task_summary import build_hard_task_execution_summary
from saxo_bank_mcp.loop_manifest import GitState


def write_receipt(
    path: Path,
    *,
    sha: str,
    dirty: bool = False,
    status: str = "passed",
    extra: dict[str, JsonValue] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "status": status,
                "driver": "loop_harness",
                "command": "sim-order-mutation",
                "fastmcp_called": True,
                "live_write": False,
                "git": {"sha": sha, "dirty": dirty, "unavailable_reason": None},
                "secret_scan": {"findings": [], "scan_errors": []},
                **({} if extra is None else extra),
            },
        ),
        encoding="utf-8",
    )


def test_hard_task_summary_derives_receipt_rows(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    write_receipt(receipts / "saxo_place_sim_order.json", sha="abc123")

    summary = build_hard_task_execution_summary(
        receipts,
        expected_tool_ids=("saxo_place_sim_order",),
        git=GitState(sha="abc123", dirty=False),
    )

    assert summary.status == "passed"
    assert summary.tool_count == 1
    assert summary.all_fastmcp_called is True
    assert summary.all_git_clean is True
    assert summary.failed_tools == ()
    assert summary.rows[0].tool_id == "saxo_place_sim_order"
    assert summary.rows[0].receipt == str(receipts / "saxo_place_sim_order.json")
    assert summary.rows[0].git_sha == "abc123"
    assert summary.rows[0].git_dirty is False


def test_hard_task_summary_rejects_receipts_that_cannot_claim_completion(
    tmp_path: Path,
) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    write_receipt(
        receipts / "saxo_cancel_sim_order.json",
        sha="abc123",
        status="exercised",
        extra={"completion_claim_allowed": False},
    )
    write_receipt(
        receipts / "saxo_create_streaming_price_subscription.json",
        sha="abc123",
        status="incomplete_no_frame",
    )

    summary = build_hard_task_execution_summary(
        receipts,
        expected_tool_ids=(
            "saxo_cancel_sim_order",
            "saxo_create_streaming_price_subscription",
        ),
        git=GitState(sha="abc123", dirty=False),
    )

    assert summary.status == "failed"
    assert summary.failed_tools == (
        "saxo_cancel_sim_order",
        "saxo_create_streaming_price_subscription",
    )
    errors_by_tool = {row.tool_id: row.error for row in summary.rows}
    assert (
        errors_by_tool["saxo_cancel_sim_order"]
        == "receipt explicitly disallows completion claim"
    )
    assert (
        errors_by_tool["saxo_create_streaming_price_subscription"]
        == "receipt status incomplete_no_frame does not allow completion"
    )


def test_hard_task_summary_allows_safe_fixture_coverage_without_completion_claim(
    tmp_path: Path,
) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    write_receipt(
        receipts / "saxo_register_disclaimer_response.json",
        sha="abc123",
        status="exercised",
        extra={
            "completion_claim_allowed": False,
            "safe_fixture_coverage_claim_allowed": True,
        },
    )

    summary = build_hard_task_execution_summary(
        receipts,
        expected_tool_ids=("saxo_register_disclaimer_response",),
        git=GitState(sha="abc123", dirty=False),
    )

    assert summary.status == "passed"
    assert summary.failed_tools == ()
    row = summary.rows[0]
    assert row.completion_claim_allowed is False
    assert row.safe_fixture_coverage_claim_allowed is True


def test_hard_task_summary_rejects_stale_or_dirty_receipts(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    write_receipt(receipts / "saxo_place_sim_order.json", sha="old")
    write_receipt(
        receipts / "saxo_modify_sim_order.json",
        sha="abc123",
        dirty=True,
    )

    summary = build_hard_task_execution_summary(
        receipts,
        expected_tool_ids=("saxo_place_sim_order", "saxo_modify_sim_order"),
        git=GitState(sha="abc123", dirty=False),
    )

    assert summary.status == "failed"
    assert summary.all_git_clean is False
    assert summary.failed_tools == ("saxo_modify_sim_order", "saxo_place_sim_order")
    errors_by_tool = {row.tool_id: row.error for row in summary.rows}
    assert errors_by_tool["saxo_place_sim_order"] == "receipt git SHA does not match current HEAD"
    assert errors_by_tool["saxo_modify_sim_order"] == "receipt generated from dirty git state"


def test_hard_task_summary_qa_command_writes_report(tmp_path: Path) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    write_receipt(
        receipts / "saxo_create_order_preview.json",
        sha="abc123",
    )
    out = tmp_path / "summary.json"

    result = qa.main(
        [
            "hard-task-summary",
            "--receipts-dir",
            str(receipts),
            "--expected-tool",
            "saxo_create_order_preview",
            "--expected-sha",
            "abc123",
            "--out",
            str(out),
        ],
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["rows"][0]["tool_id"] == "saxo_create_order_preview"
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_hard_task_summary_scans_before_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    write_receipt(receipts / "saxo_create_order_preview.json", sha="abc123")
    out = tmp_path / "summary.json"
    marker = "rejected-summary-marker"

    def finding_scan(
        _paths: list[str],
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return ([{"path": marker, "pattern": "synthetic"}], [])

    monkeypatch.setattr(summary_module, "scan_secret_paths", finding_scan)

    result = summary_module.handle_hard_task_summary(
        out,
        receipts,
        expected_tool_ids=("saxo_create_order_preview",),
        git=GitState(sha="abc123", dirty=False),
    )

    raw = out.read_text(encoding="utf-8")
    assert result == 1
    assert marker not in raw
    assert json.loads(raw) == {
        "reason": "evidence_secret_scan_failed",
        "status": "failed",
    }
