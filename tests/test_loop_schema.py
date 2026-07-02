from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.loop_schema import TribunalCompletion, validate_completion_artifact


def write_tribunal_run(directory: Path) -> None:
    (directory / "rounds" / "round-01").mkdir(parents=True, exist_ok=True)
    (directory / "normalized.json").write_text("{}", encoding="utf-8")
    (directory / "rounds" / "round-01" / "judge-output.json").write_text(
        '{"verdict": {"status": "confirmed"}}',
        encoding="utf-8",
    )
    (directory / "judge-input.md").write_text(
        "Hard task for saxo_get_session_capabilities through the real MCP path.",
        encoding="utf-8",
    )


def valid_mcp_execution() -> dict[str, JsonValue]:
    return {
        "surface": "fastmcp_tool_call",
        "tool_id": "saxo_get_session_capabilities",
        "evidence": "fastmcp-output.json",
        "mock_only": False,
        "unit_tests_only": False,
        "internal_client_only": False,
        "pre_implementation_only": False,
        "notes": "FastMCP tool call captured from the real server path.",
    }


def valid_completion() -> dict[str, JsonValue]:
    return {
        "tool_id": "saxo_get_session_capabilities",
        "status": "complete",
        "mcp_tool_schema": "schema.json",
        "task": "task.md",
        "input": "input.json",
        "output": "output.json",
        "error": None,
        "audit": "audit.md",
        "tribunal_run": "tribunal",
        "fixed_feedback": [],
        "remaining_actionable_feedback": [],
        "refusal_reason": None,
        "exemption_reason": None,
        "mcp_execution": valid_mcp_execution(),
        "completed_at": datetime.now(UTC).isoformat(),
    }


def write_valid_evidence_files(directory: Path) -> None:
    for name in ("schema.json", "task.md", "input.json"):
        (directory / name).write_text("{}", encoding="utf-8")
    (directory / "output.json").write_text('{"status":"passed"}', encoding="utf-8")
    (directory / "fastmcp-output.json").write_text(
        '{"status":"passed","surface":"fastmcp_tool_call"}',
        encoding="utf-8",
    )
    (directory / "audit.md").write_text(
        "Substantive safety controls described over fifty characters.",
        encoding="utf-8",
    )
    write_tribunal_run(directory / "tribunal")


def test_complete_accepts_fastmcp_execution_evidence(tmp_path: Path) -> None:
    payload = valid_completion()
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]
    path = tmp_path / "tribunal-completion.json"
    path.write_text(TribunalCompletion.model_validate(payload).model_dump_json(), encoding="utf-8")
    write_valid_evidence_files(tmp_path)

    result = validate_completion_artifact(path)

    assert not result.errors


def test_complete_rejects_missing_mcp_execution(tmp_path: Path) -> None:
    payload = valid_completion()
    payload["mcp_execution"] = None
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]
    path = tmp_path / "tribunal-completion.json"
    path.write_text(TribunalCompletion.model_validate(payload).model_dump_json(), encoding="utf-8")
    write_valid_evidence_files(tmp_path)

    result = validate_completion_artifact(path)

    assert any("complete tools require mcp_execution" in error for error in result.errors)


@pytest.mark.parametrize(
    "flag",
    ["mock_only", "unit_tests_only", "internal_client_only", "pre_implementation_only"],
)
def test_complete_rejects_disallowed_mcp_execution_flags(
    flag: str,
    tmp_path: Path,
) -> None:
    payload = valid_completion()
    execution = valid_mcp_execution()
    execution[flag] = True
    payload["mcp_execution"] = execution
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]
    path = tmp_path / "tribunal-completion.json"
    path.write_text(TribunalCompletion.model_validate(payload).model_dump_json(), encoding="utf-8")
    write_valid_evidence_files(tmp_path)

    result = validate_completion_artifact(path)

    assert any(
        "complete tool cannot be marked "
        "mock_only/unit_tests_only/internal_client_only/pre_implementation_only" in error
        for error in result.errors
    )


def test_complete_rejects_mcp_execution_tool_id_mismatch(tmp_path: Path) -> None:
    payload = valid_completion()
    execution = valid_mcp_execution()
    execution["tool_id"] = "saxo_refresh_token"
    payload["mcp_execution"] = execution
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]
    path = tmp_path / "tribunal-completion.json"
    path.write_text(TribunalCompletion.model_validate(payload).model_dump_json(), encoding="utf-8")
    write_valid_evidence_files(tmp_path)

    result = validate_completion_artifact(path)

    assert any("mcp_execution.tool_id must match tool_id" in error for error in result.errors)


def test_complete_rejects_missing_mcp_execution_evidence_path(tmp_path: Path) -> None:
    payload = valid_completion()
    execution = valid_mcp_execution()
    execution["evidence"] = "missing-fastmcp-output.json"
    payload["mcp_execution"] = execution
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]
    path = tmp_path / "tribunal-completion.json"
    path.write_text(TribunalCompletion.model_validate(payload).model_dump_json(), encoding="utf-8")
    write_valid_evidence_files(tmp_path)

    result = validate_completion_artifact(path)

    assert any("missing referenced path" in error for error in result.errors)


def test_mcp_execution_rejects_unknown_surface(tmp_path: Path) -> None:
    payload = valid_completion()
    execution = valid_mcp_execution()
    execution["surface"] = "unit_test"
    payload["mcp_execution"] = execution
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]
    path = tmp_path / "tribunal-completion.json"
    path.write_text(TribunalCompletion.model_validate(payload).model_dump_json(), encoding="utf-8")
    write_valid_evidence_files(tmp_path)

    result = validate_completion_artifact(path)

    assert any(
        "mcp_execution.surface must be fastmcp_tool_call or fastmcp_evidence_bundle"
        in error
        for error in result.errors
    )


def test_complete_rejects_remaining_feedback(tmp_path: Path) -> None:
    data = valid_completion()
    data["remaining_actionable_feedback"] = ["approval factor is unclear"]
    path = tmp_path / "tribunal-completion.json"
    tc = TribunalCompletion.model_validate(data)
    path.write_text(tc.model_dump_json(), encoding="utf-8")
    for name in ("schema.json", "task.md", "input.json", "output.json", "audit.md"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    write_tribunal_run(tmp_path / "tribunal")

    result = validate_completion_artifact(path)
    assert any("remaining_actionable_feedback" in e for e in result.errors)


def test_refused_requires_reason() -> None:
    data = valid_completion()
    data["status"] = "refused"
    with pytest.raises(ValidationError, match="refusal_reason"):
        TribunalCompletion.model_validate(data)


def test_complete_requires_output() -> None:
    data = valid_completion()
    data["output"] = None
    with pytest.raises(ValidationError, match="output"):
        TribunalCompletion.model_validate(data)


def test_missing_referenced_path_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "tribunal-completion.json"
    payload = TribunalCompletion.model_validate(valid_completion()).model_dump_json()
    path.write_text(payload, encoding="utf-8")

    result = validate_completion_artifact(path)

    assert result.errors
    assert "missing referenced path" in result.errors[0]


def test_write_and_money_moving_validation_rules(tmp_path: Path) -> None:
    # 1. Reject missing fixed_feedback
    payload = valid_completion()
    payload["risk_class"] = "money_moving"
    payload["fixed_feedback"] = []

    path = tmp_path / "tribunal-completion.json"
    tc = TribunalCompletion.model_validate(payload)
    path.write_text(tc.model_dump_json(), encoding="utf-8")

    # Write empty files for referenced paths to satisfy other checks
    for name in ("schema.json", "task.md", "input.json", "output.json", "audit.md"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    (tmp_path / "fastmcp-output.json").write_text(
        '{"status":"passed","surface":"fastmcp_tool_call"}',
        encoding="utf-8",
    )
    write_tribunal_run(tmp_path / "tribunal")

    result = validate_completion_artifact(path)
    assert any("must have non-empty fixed_feedback" in e for e in result.errors)

    payload = valid_completion()
    payload["risk_class"] = "money_moving"
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]

    tc = TribunalCompletion.model_validate(payload)
    path.write_text(tc.model_dump_json(), encoding="utf-8")
    (tmp_path / "output.json").write_text("{}", encoding="utf-8")
    audit_desc = "Substantive safety controls described in detail over fifty characters."
    (tmp_path / "audit.md").write_text(audit_desc, encoding="utf-8")

    result = validate_completion_artifact(path)
    assert any("has trivial output content" in e for e in result.errors)

    (tmp_path / "output.json").write_text('{"order_id": "123"}', encoding="utf-8")
    (tmp_path / "audit.md").write_text("Too short.", encoding="utf-8")

    result = validate_completion_artifact(path)
    assert any("at least 50 characters of safety/control descriptions" in e for e in result.errors)

    (tmp_path / "audit.md").write_text(audit_desc, encoding="utf-8")
    result = validate_completion_artifact(path)
    assert not result.errors


def test_write_and_money_moving_rejects_more_trivial_outputs(tmp_path: Path) -> None:
    payload = valid_completion()
    payload["risk_class"] = "write"
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]
    path = tmp_path / "tribunal-completion.json"
    path.write_text(TribunalCompletion.model_validate(payload).model_dump_json(), encoding="utf-8")
    for name in ("schema.json", "task.md", "input.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    (tmp_path / "fastmcp-output.json").write_text(
        '{"status":"passed","surface":"fastmcp_tool_call"}',
        encoding="utf-8",
    )
    audit_text = "Substantive safety controls described over fifty characters."
    (tmp_path / "audit.md").write_text(audit_text, encoding="utf-8")
    write_tribunal_run(tmp_path / "tribunal")
    for value in ("null", "0", "{ }"):
        (tmp_path / "output.json").write_text(value, encoding="utf-8")

        result = validate_completion_artifact(path)

        assert any("trivial output" in error for error in result.errors)


def test_validate_completion_artifact_malformed_json_returns_schema_error(tmp_path: Path) -> None:
    path = tmp_path / "malformed.json"
    path.write_text("{invalid json", encoding="utf-8")

    result = validate_completion_artifact(path)

    assert result.tool_id is None
    assert result.status is None
    assert result.errors
    assert any(e.startswith("schema:") for e in result.errors)


def test_validate_completion_artifact_rejects_placeholder_tribunal_run(
    tmp_path: Path,
) -> None:
    payload = valid_completion()
    payload["fixed_feedback"] = [{"finding": "X", "fix": "Y", "evidence": "audit.md"}]
    path = tmp_path / "tribunal-completion.json"
    path.write_text(TribunalCompletion.model_validate(payload).model_dump_json(), encoding="utf-8")
    for name in ("schema.json", "task.md", "input.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    (tmp_path / "fastmcp-output.json").write_text(
        '{"status":"passed","surface":"fastmcp_tool_call"}',
        encoding="utf-8",
    )
    (tmp_path / "output.json").write_text('{"status":"passed"}', encoding="utf-8")
    (tmp_path / "audit.md").write_text(
        "Substantive safety controls described over fifty characters.",
        encoding="utf-8",
    )
    (tmp_path / "tribunal").mkdir()

    result = validate_completion_artifact(path)

    assert any("tribunal_run missing normalized.json" in e for e in result.errors)
