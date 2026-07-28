"""Regression tests for Todo 15 dual-harness eval runner defects."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp.agent_skill_eval_commands import (
    claude_non_router_command,
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
    def _discover(_path: Path) -> str:
        return "SIMACCT01"

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_matrix_env.discover_exactly_one_active_sim_account",
        _discover,
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
