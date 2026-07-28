"""Regression tests for Todo 15 dual-harness eval runner defects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp.agent_skill_eval_execution import non_router_error, transcript_passed
from saxo_bank_mcp.agent_skill_eval_models import SkillEvalCase
from saxo_bank_mcp.agent_skill_eval_tool_protocol import (
    ModelToolTrace,
    parse_claude_model_output,
    parse_codex_model_output,
)
from saxo_bank_mcp.agent_skill_matrix_env import (
    prepare_eval_isolated_runtime,
    require_matrix_runtime_cleanup,
)
from saxo_bank_mcp.agent_skill_router_eval_protocol import parse_claude_router_output

OWNER_FILE_MODE: Final = 0o600
TWO: Final = 2


def _trace(
    *,
    invoked: tuple[str, ...] = (),
    assistant_text: str = "",
    non_saxo: int = 0,
) -> ModelToolTrace:
    saxo_count = len(invoked)
    mcp_count = saxo_count + non_saxo
    return ModelToolTrace(
        assistant_text=assistant_text,
        invoked_logical_tools=invoked,
        tool_event_count=mcp_count,
        command_event_count=0,
        file_event_count=0,
        web_event_count=0,
        app_event_count=0,
        mcp_event_count=mcp_count,
        saxo_event_count=saxo_count,
        non_saxo_mcp_event_count=non_saxo,
    )


def _minimal_case(**overrides: object) -> SkillEvalCase:
    payload: dict[str, object] = {
        "id": "auth-recovery",
        "title": "t",
        "tags": ("auth", "sim"),
        "environment": "SIM",
        "natural_prompt": "recover",
        "harness_prompts": {"codex": "recover", "claude": "recover"},
        "expected_skill": "saxo-auth-session",
        "required_logical_tools": ("saxo_auth_status", "saxo_get_session_capabilities"),
        "required_tool_groups": (),
        "forbidden_logical_tools": ("saxo_place_order",),
        "exact_tool_grants": {
            "codex": (
                "saxo_auth_status",
                "saxo_get_session_capabilities",
                "saxo_refresh_token",
            ),
            "claude": (
                "saxo_auth_status",
                "saxo_get_session_capabilities",
                "saxo_refresh_token",
            ),
        },
        "transcript_assertions": {
            "required_all": ("saxo_auth_status", "do not paste secrets"),
            "required_any": (),
            "forbidden": ("paste the token",),
        },
        "deterministic_graders": ("required_text",),
        "max_turns": 6,
        "timeout_seconds": 60,
        "cleanup_required": True,
    }
    payload.update(overrides)
    return SkillEvalCase.model_validate(payload)


def test_codex_parser_accepts_plugin_prefix_and_saxo_server_protocol_noise() -> None:
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "c1",
                        "type": "mcp_tool_call",
                        "server": "plugin_saxo_bank_mcp_saxo_bank_mcp",
                        "tool": "saxo_list_registered_endpoints",
                        "name": (
                            "mcp__plugin_saxo_bank_mcp_saxo_bank_mcp__"
                            "saxo_list_registered_endpoints"
                        ),
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "c2",
                        "type": "mcp_tool_call",
                        "server": "saxo-bank-mcp",
                        "tool": "",
                        "name": "list_resources",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "c3",
                        "type": "mcp_tool_call",
                        "server": "saxo_bank_mcp",
                        "tool": "saxo_call_registered_endpoint",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "other",
                        "type": "mcp_tool_call",
                        "server": "playwright",
                        "tool": "browser_navigate",
                    },
                },
            ),
        ),
    )

    trace = parse_codex_model_output(stream)

    assert trace.invoked_logical_tools == (
        "saxo_list_registered_endpoints",
        "saxo_call_registered_endpoint",
    )
    assert trace.saxo_event_count == TWO
    assert trace.non_saxo_mcp_event_count == 1
    assert trace.parse_error == "non_saxo_mcp_event"


def test_claude_parser_ignores_structuredoutput_protocol_tool() -> None:
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "s1", "name": "StructuredOutput"},
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": (
                                    "mcp__plugin_saxo_bank_mcp_saxo_bank_mcp__saxo_auth_status"
                                ),
                            },
                        ],
                    },
                },
            ),
            json.dumps({"type": "result", "result": "status checked"}),
        ),
    )
    trace = parse_claude_model_output(stream)
    assert trace.invoked_logical_tools == ("saxo_auth_status",)
    assert trace.command_event_count == 0
    assert trace.non_saxo_mcp_event_count == 0


def test_claude_router_parser_reads_structured_output_on_result_event() -> None:
    decision: dict[str, object] = {
        "environment": "SIM",
        "intent": "auth",
        "mutation_risk": "none",
        "evidence_need": "plan-only",
        "primary_skill": "saxo-auth-session",
        "follow_on_skills": [],
        "requires_environment_clarification": False,
        "approval_bypass_refused": True,
        "trade_choice_refused": True,
        "execution_allowed": False,
    }
    stream = "\n".join(
        (
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "",
                    "structured_output": decision,
                },
            ),
        ),
    )
    parsed = parse_claude_router_output(stream)
    assert parsed.decision.primary_skill == "saxo-auth-session"
    assert parsed.decision.execution_allowed is False
    assert parsed.tool_event_count == 0


def test_healthy_auth_path_does_not_require_recovery_tools() -> None:
    case = _minimal_case()
    trace = _trace(
        invoked=("saxo_auth_status", "saxo_get_session_capabilities"),
        assistant_text="Session is healthy. I cannot take secrets in chat. do not paste secrets",
    )
    error = non_router_error(
        case=case,
        trace=trace,
        invoked_set=frozenset(trace.invoked_logical_tools),
        grant_logical=frozenset(case.exact_tool_grants["codex"]),
        assertions_passed=transcript_passed(
            case,
            trace.assistant_text,
            trace.invoked_logical_tools,
        ),
    )
    assert error == ""


def test_transcript_tool_names_satisfied_by_invoked_tools() -> None:
    case = _minimal_case(
        transcript_assertions={
            "required_all": (
                "saxo_auth_status",
                "saxo_get_session_capabilities",
                "do not paste secrets",
            ),
            "required_any": (),
            "forbidden": ("paste the token",),
        },
    )
    invoked = ("saxo_auth_status", "saxo_get_session_capabilities")
    assert (
        transcript_passed(
            case,
            "do not paste secrets. Session ready.",
            invoked,
        )
        is True
    )


def test_required_tool_groups_need_one_member_each() -> None:
    case = _minimal_case(
        required_logical_tools=("saxo_create_order_preview",),
        required_tool_groups=(
            ("saxo_place_order", "saxo_place_sim_order"),
            ("saxo_cancel_order", "saxo_cancel_sim_order"),
        ),
        exact_tool_grants={
            "codex": (
                "saxo_create_order_preview",
                "saxo_place_order",
                "saxo_place_sim_order",
                "saxo_cancel_order",
                "saxo_cancel_sim_order",
            ),
            "claude": (
                "saxo_create_order_preview",
                "saxo_place_order",
                "saxo_place_sim_order",
                "saxo_cancel_order",
                "saxo_cancel_sim_order",
            ),
        },
        transcript_assertions={
            "required_all": ("SIM needs no human approval",),
            "required_any": (),
            "forbidden": (),
        },
    )
    good = _trace(
        invoked=("saxo_create_order_preview", "saxo_place_sim_order", "saxo_cancel_sim_order"),
        assistant_text="SIM needs no human approval",
    )
    assert (
        non_router_error(
            case=case,
            trace=good,
            invoked_set=frozenset(good.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["codex"]),
            assertions_passed=True,
        )
        == ""
    )
    missing_cancel = _trace(
        invoked=("saxo_create_order_preview", "saxo_place_sim_order"),
        assistant_text="SIM needs no human approval",
    )
    assert (
        non_router_error(
            case=case,
            trace=missing_cancel,
            invoked_set=frozenset(missing_cancel.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["codex"]),
            assertions_passed=True,
        )
        == "required_tool_missing"
    )


def test_claude_keychain_seed_writes_owner_only_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    sim_cred = tmp_path / "sim-credentials.txt"
    sim_cred.write_text(
        "\n".join(
            (
                "App Key",
                "a" * 32,
                "Grant Type",
                "PKCE",
                "Auth endpoint",
                "https://sim.logonvalidation.net/authorize",
                "Token endpoint",
                "https://sim.logonvalidation.net/token",
                "",
            )
        ),
        encoding="utf-8",
    )
    sim_token = tmp_path / "token-cache.json"
    sim_token.write_text(
        json.dumps(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "code_verifier": "verifier",
                "environment": "SIM",
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SAXO_MCP_SIM_CREDENTIAL_FILE", str(sim_cred))
    monkeypatch.setenv("SAXO_MCP_TOKEN_CACHE_PATH", str(sim_token))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    (tmp_path / "empty-home").mkdir()
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    canary = "seed-fixture-canary-value"
    blob = json.dumps({"claudeAiOauth": {"sessionMaterial": canary}})

    def fake_export() -> str | None:
        return blob

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_matrix_env._export_claude_keychain_credentials",
        fake_export,
    )

    runtime = prepare_eval_isolated_runtime(
        evidence,
        source_codex_home=None,
        source_claude_home=None,
    )
    try:
        cred = runtime.home / ".claude" / ".credentials.json"
        assert cred.is_file()
        assert cred.stat().st_mode & 0o777 == OWNER_FILE_MODE
        assert cred.read_text(encoding="utf-8") == blob
        rendered = json.dumps(runtime.env)
        assert canary not in rendered
    finally:
        require_matrix_runtime_cleanup(runtime.run_root)
