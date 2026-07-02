from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.loop_schema import TribunalCompletion, validate_completion_artifact


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
        "completed_at": datetime.now(UTC).isoformat(),
    }


def test_complete_rejects_remaining_feedback() -> None:
    data = valid_completion()
    data["remaining_actionable_feedback"] = ["approval factor is unclear"]
    with pytest.raises(ValidationError, match="remaining_actionable_feedback"):
        TribunalCompletion.model_validate(data)


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
    (tmp_path / "tribunal").mkdir(exist_ok=True)
    (tmp_path / "tribunal" / "judge-output.json").write_text("{}", encoding="utf-8")

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
    audit_text = "Substantive safety controls described over fifty characters."
    (tmp_path / "audit.md").write_text(audit_text, encoding="utf-8")
    (tmp_path / "tribunal").mkdir()
    (tmp_path / "tribunal" / "judge-output.json").write_text("{}", encoding="utf-8")
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
