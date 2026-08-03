"""Regression tests for Todo 15 dual-harness eval runner defects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp.agent_skill_eval_commands import (
    claude_non_router_command,
    enrich_eval_cli_env,
    path_with_cli_dirs,
    resolve_cli_executable,
    write_claude_sim_mcp_config,
)
from saxo_bank_mcp.agent_skill_eval_execution import (
    HarnessRoots,
    execute_model_case,
    non_router_error,
    transcript_passed,
)
from saxo_bank_mcp.agent_skill_eval_models import SkillEvalCase
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager, ManagedProcessResult
from saxo_bank_mcp.agent_skill_eval_runner import resolve_tool_grants
from saxo_bank_mcp.agent_skill_eval_tool_protocol import (
    ModelToolTrace,
    logical_tools_from_grants,
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


def test_auth_recovery_case_requires_secret_sentence_even_when_tools_ran() -> None:
    """Fresh dual-eval failure: Claude called both auth tools but omitted secret close-out."""
    from saxo_bank_mcp.agent_skill_eval_models import load_eval_cases  # noqa: PLC0415

    case = next(c for c in load_eval_cases() if c.id == "auth-recovery")
    tools_only = _trace(
        invoked=("saxo_auth_status", "saxo_get_session_capabilities"),
        assistant_text="Session capabilities look ready.",
    )
    assert (
        non_router_error(
            case=case,
            trace=tools_only,
            invoked_set=frozenset(tools_only.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["claude"]),
            assertions_passed=transcript_passed(
                case,
                tools_only.assistant_text,
                tools_only.invoked_logical_tools,
            ),
        )
        == "transcript_assertion_failed"
    )
    with_closeout = _trace(
        invoked=("saxo_auth_status", "saxo_get_session_capabilities"),
        assistant_text=(
            "Session is ready. I cannot take secrets in chat. Use the local browser "
            "login or configured owner-only cache flow, then I can check redacted status."
        ),
    )
    assert (
        non_router_error(
            case=case,
            trace=with_closeout,
            invoked_set=frozenset(with_closeout.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["claude"]),
            assertions_passed=transcript_passed(
                case,
                with_closeout.assistant_text,
                with_closeout.invoked_logical_tools,
            ),
        )
        == ""
    )


def test_lifecycle_case_requires_cancel_group_not_just_preview_and_ledger() -> None:
    """Claude dual-eval called preview, place, and ledger but skipped cancel cleanup."""
    from saxo_bank_mcp.agent_skill_eval_models import load_eval_cases  # noqa: PLC0415

    case = next(c for c in load_eval_cases() if c.id == "sim-order-lifecycle")
    missing_cancel = _trace(
        invoked=(
            "saxo_create_order_preview",
            "saxo_place_sim_order",
            "saxo_get_safe_request_ledger",
        ),
        assistant_text="SIM needs no human approval. cleanup incomplete.",
    )
    assert (
        non_router_error(
            case=case,
            trace=missing_cancel,
            invoked_set=frozenset(missing_cancel.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["claude"]),
            assertions_passed=True,
        )
        == "required_tool_missing"
    )
    complete = _trace(
        invoked=(
            "saxo_create_order_preview",
            "saxo_place_sim_order",
            "saxo_create_write_preview",
            "saxo_cancel_sim_orders_by_instrument",
            "saxo_get_safe_request_ledger",
        ),
        assistant_text="SIM needs no human approval. cleanup done after cancel.",
    )
    assert (
        non_router_error(
            case=case,
            trace=complete,
            invoked_set=frozenset(complete.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["claude"]),
            assertions_passed=transcript_passed(
                case,
                complete.assistant_text,
                complete.invoked_logical_tools,
            ),
        )
        == ""
    )
    missing_preview = _trace(
        invoked=(
            "saxo_call_registered_endpoint",
            "saxo_get_safe_request_ledger",
            "saxo_auth_status",
        ),
        assistant_text="SIM needs no human approval. ledger only.",
    )
    assert (
        non_router_error(
            case=case,
            trace=missing_preview,
            invoked_set=frozenset(missing_preview.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["codex"]),
            assertions_passed=True,
        )
        == "required_tool_missing"
    )
    # final-3aacee2 Claude path: place + repeated write previews + ledger, no cancel tool.
    write_preview_only = _trace(
        invoked=(
            "saxo_list_registered_endpoints",
            "saxo_call_registered_endpoint",
            "saxo_create_order_preview",
            "saxo_place_sim_order",
            "saxo_create_write_preview",
            "saxo_create_write_preview",
            "saxo_create_write_preview",
            "saxo_get_safe_request_ledger",
        ),
        assistant_text="SIM needs no human approval. cleanup attempted via write preview.",
    )
    assert (
        non_router_error(
            case=case,
            trace=write_preview_only,
            invoked_set=frozenset(write_preview_only.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["claude"]),
            assertions_passed=True,
        )
        == "required_tool_missing"
    )
    for text in (
        case.natural_prompt,
        case.harness_prompts["claude"],
        Path("skills/saxo-trading/SKILL.md").read_text(encoding="utf-8"),
    ):
        lowered = text.lower().replace("**", "")
        assert "is not cancel" in lowered or "does not cancel" in lowered
        assert (
            "saxo_cancel_orders_by_instrument" in text
            or "saxo_cancel_sim_orders_by_instrument" in text
        )


def test_streaming_cleanup_grants_auth_status_preflight() -> None:
    """Codex final-3aacee2 failed out_of_grant_tool on legitimate saxo_auth_status preflight."""
    from saxo_bank_mcp.agent_skill_eval_models import load_eval_cases  # noqa: PLC0415

    case = next(c for c in load_eval_cases() if c.id == "streaming-cleanup")
    for harness in ("codex", "claude"):
        grants = frozenset(case.exact_tool_grants[harness])
        assert "saxo_auth_status" in grants
        assert "saxo_create_streaming_price_subscription" in grants
        assert "saxo_cleanup_streaming_subscriptions" in grants
    with_auth = _trace(
        invoked=(
            "saxo_auth_status",
            "saxo_create_streaming_price_subscription",
            "saxo_cleanup_streaming_subscriptions",
        ),
        assistant_text="SIM bounded stream cleanup complete.",
    )
    assert (
        non_router_error(
            case=case,
            trace=with_auth,
            invoked_set=frozenset(with_auth.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["codex"]),
            assertions_passed=transcript_passed(
                case,
                with_auth.assistant_text,
                with_auth.invoked_logical_tools,
            ),
        )
        == ""
    )
    # Auth remains optional: create+cleanup alone still pass.
    no_auth = _trace(
        invoked=(
            "saxo_create_streaming_price_subscription",
            "saxo_cleanup_streaming_subscriptions",
        ),
        assistant_text="SIM bounded stream cleanup complete.",
    )
    assert (
        non_router_error(
            case=case,
            trace=no_auth,
            invoked_set=frozenset(no_auth.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["claude"]),
            assertions_passed=True,
        )
        == ""
    )
    # Without the grant, the same auth preflight must still fail closed.
    narrow = frozenset(
        (
            "saxo_create_streaming_price_subscription",
            "saxo_cleanup_streaming_subscriptions",
        ),
    )
    assert (
        non_router_error(
            case=case,
            trace=with_auth,
            invoked_set=frozenset(with_auth.invoked_logical_tools),
            grant_logical=narrow,
            assertions_passed=True,
        )
        == "out_of_grant_tool"
    )
    skill = Path("skills/saxo-streaming/SKILL.md").read_text(encoding="utf-8")
    assert "saxo_auth_status" in skill
    assert "optional" in skill.lower() or "preflight" in skill.lower()


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


def test_codex_empty_server_saxo_prefixed_protocol_is_not_foreign() -> None:
    """Codex sometimes omits server while emitting Saxo MCP protocol names."""
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "p1",
                        "type": "mcp_tool_call",
                        "server": "",
                        "tool": "",
                        "name": "mcp__saxo_bank_mcp__list_tools",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "p2",
                        "type": "mcp_tool_call",
                        "server": "",
                        "tool": "list_resources",
                        "name": "mcp__saxo_bank_mcp__",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "foreign",
                        "type": "mcp_tool_call",
                        "server": "filesystem",
                        "tool": "read_file",
                    },
                },
            ),
        ),
    )
    trace = parse_codex_model_output(stream)
    assert trace.invoked_logical_tools == ()
    assert trace.non_saxo_mcp_event_count == 1
    assert trace.parse_error == "non_saxo_mcp_event"
    assert trace.mcp_event_count == len(stream.splitlines())


def test_claude_discovery_tools_are_protocol_noise() -> None:
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "d1", "name": "ToolSearch"},
                            {
                                "type": "tool_use",
                                "id": "d2",
                                "name": "ListMcpResourcesTool",
                            },
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "mcp__saxo-bank-mcp__saxo_auth_status",
                            },
                        ],
                    },
                },
            ),
            json.dumps({"type": "result", "result": "auth ok"}),
        ),
    )
    trace = parse_claude_model_output(stream)
    assert trace.invoked_logical_tools == ("saxo_auth_status",)
    assert trace.non_saxo_mcp_event_count == 0
    assert trace.command_event_count == 0
    assert trace.parse_error == ""


def test_claude_stdio_grants_and_mcp_config_shape(tmp_path: Path) -> None:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    dest = tmp_path / "run" / "claude-mcp.json"
    env = {
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "0",
        "SAXO_MCP_TOKEN_CACHE_PATH": str(tmp_path / "token.json"),
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/bin:/opt/homebrew/bin",
    }
    path = write_claude_sim_mcp_config(plugin_root=plugin, dest=dest, env=env)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert path.stat().st_mode & 0o777 == OWNER_FILE_MODE
    server = payload["mcpServers"]["saxo-bank-mcp"]
    # Deterministic absolute uv when PATH can resolve it; else keep bare name.
    assert server["command"] in {"uv", "/opt/homebrew/bin/uv", "/usr/bin/uv"} or server[
        "command"
    ].endswith("/uv")
    assert server["args"][:2] == ["run", "--project"]
    assert server["env"]["SAXO_MCP_ENVIRONMENT"] == "SIM"
    assert server["env"]["SAXO_MCP_ENABLE_LIVE_WRITES"] == "0"

    grants = resolve_tool_grants("claude", ("saxo_auth_status", "saxo_health"))
    assert grants == (
        "mcp__saxo-bank-mcp__saxo_auth_status",
        "mcp__saxo-bank-mcp__saxo_health",
    )
    assert logical_tools_from_grants(grants) == ("saxo_auth_status", "saxo_health")
    command = claude_non_router_command(
        "prompt",
        mcp_config_path=path,
        resolved_grants=grants,
        env=env,
    )
    assert "--plugin-dir" not in command
    assert "--disallowedTools" in command
    disallowed = command[command.index("--disallowedTools") + 1]
    assert "Bash" in disallowed
    assert "Read" in disallowed
    assert "Edit" in disallowed
    assert "--strict-mcp-config" in command
    assert command[command.index("--mcp-config") + 1] == str(path)
    assert command[command.index("--allowedTools") + 1] == ",".join(grants)
    # First argv is absolute claude when resolvable (may end with .exe on Node installs).
    assert command[0] == "claude" or Path(command[0]).name.startswith("claude")


def test_claude_mcp_config_forwards_case_scoped_allowlists(tmp_path: Path) -> None:
    """Claude stdio env must receive account/instrument allowlists when present on the case env.

    Values stay only in the owner-only mcp-config file used by the child server process;
    they are not printed to evidence or logs by this path.
    """
    from saxo_bank_mcp.agent_skill_matrix_env import (  # noqa: PLC0415
        SIM_ORDER_LIFECYCLE_CASE_ID,
        SIM_ORDER_LIFECYCLE_INSTRUMENT_UIC,
        apply_case_eval_allowlists,
    )

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    # Short non-secret canaries so credential regexes stay quiet.
    account_canary = "SIM" + "ACCT01"
    base = {
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
        "SAXO_MCP_ACCOUNT_ALLOWLIST": account_canary,
        "SAXO_MCP_TOKEN_CACHE_PATH": str(tmp_path / "token.json"),
        "HOME": str(tmp_path / "home"),
        "PATH": "/usr/bin",
    }
    lifecycle_env = apply_case_eval_allowlists(base, case_id=SIM_ORDER_LIFECYCLE_CASE_ID)
    non_lifecycle_env = apply_case_eval_allowlists(base, case_id="auth-recovery")

    lifecycle_path = write_claude_sim_mcp_config(
        plugin_root=plugin,
        dest=tmp_path / "lifecycle" / "claude-mcp.json",
        env=lifecycle_env,
    )
    non_path = write_claude_sim_mcp_config(
        plugin_root=plugin,
        dest=tmp_path / "auth" / "claude-mcp.json",
        env=non_lifecycle_env,
    )
    lifecycle_server = json.loads(lifecycle_path.read_text(encoding="utf-8"))["mcpServers"][
        "saxo-bank-mcp"
    ]
    non_server = json.loads(non_path.read_text(encoding="utf-8"))["mcpServers"]["saxo-bank-mcp"]
    assert lifecycle_server["env"]["SAXO_MCP_ACCOUNT_ALLOWLIST"] == account_canary
    assert (
        lifecycle_server["env"]["SAXO_MCP_INSTRUMENT_ALLOWLIST"]
        == SIM_ORDER_LIFECYCLE_INSTRUMENT_UIC
    )
    assert non_server["env"]["SAXO_MCP_ACCOUNT_ALLOWLIST"] == account_canary
    assert "SAXO_MCP_INSTRUMENT_ALLOWLIST" not in non_server["env"]
    # LIVE remains disabled on both paths.
    assert lifecycle_server["env"]["SAXO_MCP_ENABLE_LIVE_WRITES"] == ""
    assert non_server["env"]["SAXO_MCP_ENABLE_LIVE_WRITES"] == ""
    assert lifecycle_server["env"]["SAXO_MCP_ENVIRONMENT"] == "SIM"
    assert lifecycle_path.stat().st_mode & 0o777 == OWNER_FILE_MODE
    assert non_path.stat().st_mode & 0o777 == OWNER_FILE_MODE


def test_codex_server_list_mcp_resources_is_protocol_noise() -> None:
    """Codex discovery: server=codex tool=list_mcp_resources is harness protocol only."""
    stream = "\n".join(
        (
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "d1",
                        "type": "mcp_tool_call",
                        "server": "codex",
                        "tool": "list_mcp_resources",
                        "name": "",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "d2",
                        "type": "mcp_tool_call",
                        "server": "codex",
                        "tool": "list_mcp_resource_templates",
                        "name": "list_mcp_resource_templates",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "ok",
                        "type": "mcp_tool_call",
                        "server": "saxo-bank-mcp",
                        "tool": "saxo_auth_status",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "foreign",
                        "type": "mcp_tool_call",
                        "server": "playwright",
                        "tool": "browser_navigate",
                    },
                },
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "unknown_codex",
                        "type": "mcp_tool_call",
                        "server": "codex",
                        "tool": "run_shell",
                        "name": "run_shell",
                    },
                },
            ),
        ),
    )
    trace = parse_codex_model_output(stream)
    assert trace.invoked_logical_tools == ("saxo_auth_status",)
    assert trace.saxo_event_count == 1
    # playwright + unknown codex helper both fail closed as non-Saxo MCP.
    assert trace.non_saxo_mcp_event_count == TWO
    assert trace.parse_error == "non_saxo_mcp_event"


def test_file_not_found_retry_only_when_process_never_started(tmp_path: Path) -> None:
    case = _minimal_case(id="registered-paged-read")
    grants = resolve_tool_grants("claude", case.exact_tool_grants["claude"])
    calls = {"n": 0}

    class FlakyManager(EvalProcessManager):
        def run(
            self,
            command: tuple[str, ...],
            *,
            cwd: Path,
            env: dict[str, str],
            timeout_seconds: float,
        ) -> ManagedProcessResult:
            del cwd, env, timeout_seconds
            calls["n"] += 1
            if calls["n"] == 1:
                raise FileNotFoundError(2, "No such file or directory", command[0])
            # Usable empty success: process started, zero tools (graded missing, not FNFE).
            return ManagedProcessResult(
                stdout='{"type":"result","result":"ok"}\n',
                stderr="",
                returncode=0,
                timed_out=False,
                created_processes=1,
                terminated_processes=0,
                remaining_processes=0,
                process_cleanup="passed",
            )

    roots = HarnessRoots(
        codex_plugin_root=tmp_path,
        claude_plugin_root=tmp_path,
        codex_home=tmp_path / "codex",
        claude_home=tmp_path / "claude",
    )
    (tmp_path / "codex").mkdir()
    (tmp_path / "claude").mkdir()
    env = {
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "0",
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    record = execute_model_case(
        case,
        "claude",
        grants,
        roots=roots,
        env=env,
        process_manager=FlakyManager(),
    )
    assert calls["n"] == TWO
    # Process started on retry; grade normally (required tools missing is fine here).
    assert record.error != "FileNotFoundError"
    assert record.error in {"required_tool_missing", "transcript_assertion_failed", ""}


def test_plan_only_case_passes_without_tool_calls() -> None:
    case = _minimal_case(
        id="unknown-outcome",
        required_logical_tools=(),
        required_tool_groups=(),
        exact_tool_grants={
            "codex": (
                "saxo_get_safe_request_ledger",
                "saxo_call_registered_endpoint",
                "saxo_safety_status",
            ),
            "claude": (
                "saxo_get_safe_request_ledger",
                "saxo_call_registered_endpoint",
                "saxo_safety_status",
            ),
        },
        transcript_assertions={
            "required_all": (),
            "required_any": ("unknown", "readback", "reconcile before retry"),
            "forbidden": ("retry immediately",),
        },
    )
    trace = _trace(
        assistant_text="Outcome is unknown. Reconcile before retry with concrete readback.",
    )
    error = non_router_error(
        case=case,
        trace=trace,
        invoked_set=frozenset(),
        grant_logical=frozenset(case.exact_tool_grants["codex"]),
        assertions_passed=transcript_passed(case, trace.assistant_text, ()),
    )
    assert error == ""


def test_resolve_cli_executable_prefers_existing_override_and_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare-name fallback is last; absolute executable that still exists wins."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    # Override wins even when PATH cannot see the binary.
    resolved = resolve_cli_executable(
        "claude",
        {"PATH": "/usr/bin:/bin", "EVAL_CLI_CLAUDE_BIN": str(fake)},
    )
    assert resolved == str(fake.absolute())
    # PATH search finds the binary without following a different realpath.
    via_path = resolve_cli_executable("claude", {"PATH": str(bin_dir)})
    assert via_path == str(fake.absolute())
    # Missing name stays bare so Popen can raise FileNotFoundError.
    assert resolve_cli_executable("no-such-cli-xyz", {"PATH": "/usr/bin"}) == "no-such-cli-xyz"


def test_resolve_cli_executable_keeps_symlink_path_not_realpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Homebrew Claude Code is a symlink to claude.exe; realpath breaks MCP/plan mode."""
    real_dir = tmp_path / "pkg" / "bin"
    real_dir.mkdir(parents=True)
    real = real_dir / "claude.exe"
    real.write_text("#!/bin/sh\necho real\n", encoding="utf-8")
    real.chmod(0o755)
    link_dir = tmp_path / "brew" / "bin"
    link_dir.mkdir(parents=True)
    link = link_dir / "claude"
    link.symlink_to(real)
    monkeypatch.setenv("PATH", str(link_dir))
    resolved = resolve_cli_executable("claude", {"PATH": str(link_dir)})
    assert resolved == str(link.absolute())
    assert not resolved.endswith("claude.exe")
    # Stale realpath pins are rewritten to the public symlink via which().
    env = enrich_eval_cli_env(
        {
            "PATH": str(link_dir),
            "HOME": str(tmp_path),
            "EVAL_CLI_CLAUDE_BIN": str(real.absolute()),
        },
    )
    assert env["EVAL_CLI_CLAUDE_BIN"] == str(link.absolute())
    assert not env["EVAL_CLI_CLAUDE_BIN"].endswith("claude.exe")


def test_path_with_cli_dirs_seeds_node_and_cli_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "cli"
    bin_dir.mkdir()
    for name in ("claude", "node", "uv"):
        path = bin_dir / name
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    enriched = path_with_cli_dirs("/usr/bin", "claude")
    # Appends which() parent; does not reorder leading PATH entries.
    assert enriched.startswith("/usr/bin")
    assert str(bin_dir.absolute()) in enriched.split(":")
    env = enrich_eval_cli_env({"PATH": "/usr/bin", "HOME": str(tmp_path)})
    assert "PATH" in env
    assert env.get("EVAL_CLI_CLAUDE_BIN", "").endswith("claude") or "claude" in env["PATH"]


def test_process_manager_keeps_absolute_argv0_and_rejects_missing_cwd(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "tool"
    script.write_text("#!/bin/sh\necho ran\n", encoding="utf-8")
    script.chmod(0o755)
    manager = EvalProcessManager()
    result = manager.run(
        (str(script.resolve()),),
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        timeout_seconds=5,
    )
    assert result.returncode == 0
    assert "ran" in result.stdout
    with pytest.raises(FileNotFoundError) as excinfo:
        manager.run(
            (str(script.resolve()),),
            cwd=tmp_path / "missing-cwd",
            env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
            timeout_seconds=5,
        )
    assert "missing-cwd" in str(excinfo.value.filename)


def test_execute_model_case_maps_missing_cwd_not_executable(
    tmp_path: Path,
) -> None:
    case = _minimal_case(id="streaming-cleanup")
    grants = resolve_tool_grants("claude", case.exact_tool_grants["claude"])
    roots = HarnessRoots(
        codex_plugin_root=tmp_path / "no-codex",
        claude_plugin_root=tmp_path / "no-claude-plugin",
        codex_home=tmp_path / "codex",
        claude_home=tmp_path / "claude",
    )
    env = {
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PATH": "/usr/bin:/bin",
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "0",
    }
    (tmp_path / "home").mkdir()
    (tmp_path / "tmp").mkdir()
    record = execute_model_case(
        case,
        "claude",
        grants,
        roots=roots,
        env=env,
    )
    assert record.error == "cwd_not_found"
    assert record.status == "failed"


def test_lifecycle_prompt_and_skill_require_preview_place_cancel_ledger() -> None:
    from saxo_bank_mcp.agent_skill_eval_models import load_eval_cases  # noqa: PLC0415

    case = next(c for c in load_eval_cases() if c.id == "sim-order-lifecycle")
    prompts = (
        case.natural_prompt,
        case.harness_prompts["codex"],
        case.harness_prompts["claude"],
    )
    for prompt in prompts:
        assert "saxo_create_order_preview" in prompt
        assert "saxo_get_safe_request_ledger" in prompt
        assert "always cancel" in prompt.lower() or "cancel once" in prompt.lower()
        assert "saxo_place_order" in prompt or "saxo_place_sim_order" in prompt
        assert "SIM needs no human approval" in prompt
        assert "final answer must include exactly" in prompt.lower()
        lowered = prompt.lower()
        assert "not cancel" in lowered
        assert "saxo_cancel_orders_by_instrument" in prompt
        assert "saxo_cancel_sim_orders_by_instrument" in prompt
    skill = Path("skills/saxo-trading/SKILL.md").read_text(encoding="utf-8")
    assert "saxo_create_order_preview" in skill
    assert "always" in skill.lower()
    assert "saxo_cancel_orders_by_instrument" in skill
    assert "saxo_get_safe_request_ledger" in skill
    assert "Lifecycle close-out" in skill
    assert "does not cancel" in skill.lower().replace("**", "")
    # Codex dual-eval skip of preview alone must still fail closed.
    missing_preview = _trace(
        invoked=("saxo_call_registered_endpoint", "saxo_auth_status"),
        assistant_text="SIM needs no human approval.",
    )
    assert (
        non_router_error(
            case=case,
            trace=missing_preview,
            invoked_set=frozenset(missing_preview.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["codex"]),
            assertions_passed=True,
        )
        == "required_tool_missing"
    )
    # Full tool path without the mandatory approval sentence still fails transcript.
    tools_only = _trace(
        invoked=(
            "saxo_create_order_preview",
            "saxo_place_sim_order",
            "saxo_create_write_preview",
            "saxo_cancel_sim_orders_by_instrument",
            "saxo_get_safe_request_ledger",
        ),
        assistant_text="Lifecycle finished with cleanup.",
    )
    assert (
        non_router_error(
            case=case,
            trace=tools_only,
            invoked_set=frozenset(tools_only.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["codex"]),
            assertions_passed=transcript_passed(
                case,
                tools_only.assistant_text,
                tools_only.invoked_logical_tools,
            ),
        )
        == "transcript_assertion_failed"
    )
    complete = _trace(
        invoked=tools_only.invoked_logical_tools,
        assistant_text="SIM needs no human approval. cleanup done after cancel.",
    )
    assert (
        non_router_error(
            case=case,
            trace=complete,
            invoked_set=frozenset(complete.invoked_logical_tools),
            grant_logical=frozenset(case.exact_tool_grants["codex"]),
            assertions_passed=transcript_passed(
                case,
                complete.assistant_text,
                complete.invoked_logical_tools,
            ),
        )
        == ""
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
