from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from saxo_bank_mcp import final_verify, tribunal_index
from saxo_bank_mcp.loop_manifest import GitState

EXPECTED_PLAN_MARKERS = (
    "`tribunal-completion.json` schema",
    "Final verification requires four independent checks",
    "python -m saxo_bank_mcp.tribunal_index",
    "python -m saxo_bank_mcp.final_verify plan",
    "python -m saxo_bank_mcp.final_verify code",
    "python -m saxo_bank_mcp.final_verify mcp",
    "python -m saxo_bank_mcp.final_verify scope",
)
EXPECTED_MCP_REQUIRED_EVIDENCE = (
    ".omo/evidence/saxo-bank-mcp/task-2-sim-auth.json",
    ".omo/evidence/saxo-bank-mcp/task-4-read-smoke.json",
    ".omo/evidence/saxo-bank-mcp/task-6-precheck.json",
    ".omo/evidence/saxo-bank-mcp/task-7-sim-order.json",
    ".omo/evidence/saxo-bank-mcp/task-8-stream.json",
    ".omo/evidence/saxo-bank-mcp/task-10-live-write-refusal.json",
)


def test_plan_marker_contract_is_literal() -> None:
    assert final_verify.PLAN_MARKERS == EXPECTED_PLAN_MARKERS


def test_mcp_required_evidence_contract_is_literal() -> None:
    assert final_verify.MCP_REQUIRED_EVIDENCE == EXPECTED_MCP_REQUIRED_EVIDENCE


def test_plan_gate_passes_when_markers_exist(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("\n".join(EXPECTED_PLAN_MARKERS), encoding="utf-8")
    out = tmp_path / "report.md"

    result = final_verify.main(["plan", "--plan", str(plan), "--out", str(out)])

    assert result == 0
    assert "status: `passed`" in out.read_text(encoding="utf-8")


def test_mcp_gate_fails_without_real_evidence(tmp_path: Path) -> None:
    out = tmp_path / "report.md"

    result = final_verify.main(["mcp", "--out", str(out)])

    assert result == 1
    assert "status: `failed`" in out.read_text(encoding="utf-8")


def test_mcp_gate_rejects_placeholder_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    for raw_path in EXPECTED_MCP_REQUIRED_EVIDENCE:
        path = tmp_path / raw_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"status":"incomplete","driver":"loop_harness","detail":"not implemented yet"}\n',
            encoding="utf-8",
        )
    out = tmp_path / "report.md"

    result = final_verify.main(["mcp", "--out", str(out)])

    assert result == 1
    assert "placeholder" in out.read_text(encoding="utf-8")


def test_mcp_gate_rejects_bare_spoofed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    for raw_path in EXPECTED_MCP_REQUIRED_EVIDENCE:
        path = tmp_path / raw_path
        path.parent.mkdir(parents=True, exist_ok=True)
        status = "refused" if "live-write-refusal" in raw_path else "passed"
        path.write_text(f'{{"status":"{status}"}}\n', encoding="utf-8")
    out = tmp_path / "report.md"

    result = final_verify.main(["mcp", "--out", str(out)])

    assert result == 1
    assert "loop_harness" in out.read_text(encoding="utf-8")


def test_mcp_gate_passes_with_perfect_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    # Mock current git state to have stable sha/dirty
    monkeypatch.setattr(
        final_verify,
        "current_git_state",
        lambda: GitState(sha="test_sha_123", dirty=False),
    )

    for raw_path in EXPECTED_MCP_REQUIRED_EVIDENCE:
        path = tmp_path / raw_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if "live-write-refusal" in raw_path:
            status = "refused"
        elif "sim-auth" in raw_path:
            status = "passed"
        else:
            status = "exercised"
        payload = {
            "status": status,
            "driver": "loop_harness",
            "command": "python -m saxo_bank_mcp.qa some-scenario",
            "checked_at": "2026-06-29T20:00:00Z",
            "git": {"sha": "test_sha_123", "dirty": False},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "report.md"
    result = final_verify.main(["mcp", "--out", str(out)])
    assert result == 0
    assert "status: `passed`" in out.read_text(encoding="utf-8")


def test_plan_gate_fails_when_checked_task_missing_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.md"
    plan_text = "\n".join(EXPECTED_PLAN_MARKERS) + "\n- [x] 2. Implement Saxo..."
    plan.write_text(plan_text, encoding="utf-8")
    out = tmp_path / "report.md"

    result = final_verify.main(["plan", "--plan", str(plan), "--out", str(out)])

    assert result == 1
    assert "missing despite task marked as completed" in out.read_text(encoding="utf-8")


def test_plan_gate_detects_uppercase_checked_task_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.md"
    plan_text = "\n".join(EXPECTED_PLAN_MARKERS) + "\n- [X] 2. Implement Saxo..."
    plan.write_text(plan_text, encoding="utf-8")
    out = tmp_path / "report.md"

    result = final_verify.main(["plan", "--plan", str(plan), "--out", str(out)])

    assert result == 1
    assert "missing despite task marked as completed" in out.read_text(encoding="utf-8")


def test_plan_gate_rejects_empty_json_for_non_mcp_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    plan = tmp_path / "plan.md"
    plan_text = "\n".join(EXPECTED_PLAN_MARKERS) + "\n- [x] 1. Bootstrap project"
    plan.write_text(plan_text, encoding="utf-8")
    evidence_path = tmp_path / ".omo/evidence/saxo-bank-mcp/task-1-gitignore.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text("{}", encoding="utf-8")
    out = tmp_path / "report.md"

    result = final_verify.main(["plan", "--plan", str(plan), "--out", str(out)])

    assert result == 1
    assert "loop_harness" in out.read_text(encoding="utf-8")


def test_plan_gate_passes_when_checked_task_has_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        final_verify,
        "current_git_state",
        lambda: GitState(sha="test_sha_123", dirty=False),
    )
    plan = tmp_path / "plan.md"
    plan_text = "\n".join(EXPECTED_PLAN_MARKERS) + "\n- [x] 2. Implement Saxo..."
    plan.write_text(plan_text, encoding="utf-8")

    evidence_path = tmp_path / ".omo/evidence/saxo-bank-mcp/task-2-sim-auth.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked_at": "2026-06-29T20:00:00Z",
        "command": "sim-auth",
        "driver": "loop_harness",
        "git": {"dirty": False, "sha": "test_sha_123"},
        "status": "passed",
    }
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")

    out = tmp_path / "report.md"

    result = final_verify.main(["plan", "--plan", str(plan), "--out", str(out)])

    assert result == 0
    assert "status: `passed`" in out.read_text(encoding="utf-8")


def test_plan_gate_fails_task_three_without_evidence(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan_text = "\n".join(EXPECTED_PLAN_MARKERS) + "\n- [x] 3. Safety kernel"
    plan.write_text(plan_text, encoding="utf-8")
    out = tmp_path / "report.md"

    result = final_verify.main(["plan", "--plan", str(plan), "--out", str(out)])

    assert result == 1
    assert "Task 3 evidence" in out.read_text(encoding="utf-8")


def test_scope_gate_fails_when_registered_tools_lack_tribunal_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_inventory = (
        Path(__file__).resolve().parents[1] / "data/saxo/openapi_inventory.json"
    )
    monkeypatch.chdir(tmp_path)
    inventory = tmp_path / "data/saxo/openapi_inventory.json"
    inventory.parent.mkdir(parents=True)
    shutil.copyfile(source_inventory, inventory)
    existing_report = tmp_path / ".omo/evidence/saxo-bank-mcp/task-9-tribunal-index.json"
    existing_report.parent.mkdir(parents=True)
    existing_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    monkeypatch.setattr(
        tribunal_index,
        "list_registered_mcp_tool_ids",
        lambda: frozenset({"registered_tool_a", "registered_tool_b"}),
    )
    out = tmp_path / "scope.md"

    result = final_verify.main(["scope", "--out", str(out)])

    index = json.loads(
        (tmp_path / ".omo/evidence/saxo-bank-mcp/final-scope-tribunal-index.json")
        .read_text(encoding="utf-8"),
    )
    assert result == 1
    report = out.read_text(encoding="utf-8")
    assert "`PASS` data/saxo/openapi_inventory.json: present" in report
    assert "`PASS` Saxo inventory validation: passed" in report
    assert "tribunal_index run" in report
    assert index["source"] == "fastmcp_tool_list"
    assert index["missing_tool_ids"] == ["registered_tool_a", "registered_tool_b"]
