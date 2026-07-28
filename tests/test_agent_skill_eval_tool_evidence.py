from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

import pytest

from saxo_bank_mcp.agent_skill_eval_execution import HarnessRoots, execute_model_case
from saxo_bank_mcp.agent_skill_eval_models import SkillEvalCase, load_eval_cases
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager, ManagedProcessResult
from saxo_bank_mcp.agent_skill_eval_runner import resolve_tool_grants

ROOT: Final = Path(__file__).resolve().parents[1]
CASE_ROOT: Final = ROOT / "evals/saxo-bank"


def _case(case_id: str = "qa-evidence-readiness") -> SkillEvalCase:
    return next(item for item in load_eval_cases(CASE_ROOT) if item.id == case_id)


def _roots(tmp_path: Path) -> HarnessRoots:
    return HarnessRoots(
        codex_plugin_root=ROOT,
        claude_plugin_root=ROOT,
        codex_home=tmp_path / "codex",
        claude_home=tmp_path / "home",
    )


def _env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "home").mkdir(exist_ok=True)
    (tmp_path / "codex").mkdir(exist_ok=True)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex"),
        "TMPDIR": str(tmp_path / "tmp"),
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }


def _managed(stdout: str, *, returncode: int = 0) -> ManagedProcessResult:
    return ManagedProcessResult(
        stdout=stdout,
        stderr="",
        returncode=returncode,
        timed_out=False,
        created_processes=1,
        terminated_processes=1,
        remaining_processes=0,
        process_cleanup="passed",
    )


def _codex_success_stream(tools: tuple[str, ...], text: str) -> str:
    lines: list[str] = []
    for index, tool in enumerate(tools):
        lines.append(
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": f"call-{index}",
                        "type": "mcp_tool_call",
                        "server": "saxo_bank_mcp",
                        "tool": tool,
                    },
                },
            ),
        )
    lines.append(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "answer", "type": "agent_message", "text": text},
            },
        ),
    )
    return "\n".join(lines)


def test_prose_only_non_router_fails_without_structured_tool_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a non-router case with required tools and prose-only output.
    case = _case()
    grants = resolve_tool_grants("codex", case.exact_tool_grants["codex"])
    # Valid JSONL assistant text naming tools without any MCP events.
    prose_only = _codex_success_stream(
        (),
        (
            "matched natural prompts 39 logical tools cleanup "
            "LIVE no-purchase proof no wildcard exact tool grants "
            "saxo_health saxo_list_registered_endpoints"
        ),
    )

    def fake_run(
        self: EvalProcessManager,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ManagedProcessResult:
        _ = (self, command, cwd, env, timeout_seconds)
        return _managed(prose_only)

    monkeypatch.setattr(EvalProcessManager, "run", fake_run)

    # When: the case is executed with a mocked client process.
    record = execute_model_case(
        case,
        "codex",
        grants,
        roots=_roots(tmp_path),
        env=_env(tmp_path),
        process_manager=EvalProcessManager(),
    )

    # Then: prose naming tools is not enough.
    assert record.status == "failed"
    assert record.error == "required_tool_missing"
    assert record.no_saxo_call is True
    assert record.invoked_logical_tools == ()
    assert record.grant_status in {"passed", "failed"}


def test_structured_exact_calls_pass_without_real_clients(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: structured Codex JSONL calling every required logical tool in grant order.
    case = _case()
    grants = resolve_tool_grants("codex", case.exact_tool_grants["codex"])
    required = case.required_logical_tools
    text = (
        "matched natural prompts 39 logical tools cleanup "
        "LIVE no-purchase proof no wildcard exact tool grants"
    )
    stream = _codex_success_stream(required, text)

    def fake_run(
        self: EvalProcessManager,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ManagedProcessResult:
        _ = (self, command, cwd, env, timeout_seconds)
        return _managed(stream)

    monkeypatch.setattr(EvalProcessManager, "run", fake_run)

    # When: the case is executed.
    record = execute_model_case(
        case,
        "codex",
        grants,
        roots=_roots(tmp_path),
        env=_env(tmp_path),
        process_manager=EvalProcessManager(),
    )

    # Then: structured events make the case pass with truthful evidence fields.
    assert record.status == "passed", record.error
    assert record.error == ""
    assert record.no_mcp_call is False
    assert record.no_saxo_call is False
    assert record.invoked_logical_tools == required
    assert record.invoked_logical_tool_count == len(required)
    assert record.grant_status == "passed"
    assert record.assertion_status == "passed"
    assert record.model_saxo_event_count == len(required)


def test_out_of_grant_tool_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a case whose grants exclude a called tool.
    case = _case("arbitrary-url-refusal")
    grants = resolve_tool_grants("codex", case.exact_tool_grants["codex"])
    stream = _codex_success_stream(
        ("saxo_list_registered_endpoints", "saxo_place_order"),
        "refused arbitrary url",
    )

    def fake_run(
        self: EvalProcessManager,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ManagedProcessResult:
        _ = (self, command, cwd, env, timeout_seconds)
        return _managed(stream)

    monkeypatch.setattr(EvalProcessManager, "run", fake_run)

    # When: the case is executed.
    record = execute_model_case(
        case,
        "codex",
        grants,
        roots=_roots(tmp_path),
        env=_env(tmp_path),
        process_manager=EvalProcessManager(),
    )

    # Then: out-of-grant Saxo tools fail closed.
    assert record.status == "failed"
    assert record.error in {"out_of_grant_tool", "forbidden_tool_called"}
    assert record.grant_status == "failed"


def test_non_router_command_shape_is_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: a non-router case.
    case = _case()
    grants = resolve_tool_grants("claude", case.exact_tool_grants["claude"])
    captured: dict[str, tuple[str, ...]] = {}

    def fake_run(
        self: EvalProcessManager,
        command: tuple[str, ...],
        *,
        cwd: Path,
        env: dict[str, str],
        timeout_seconds: float,
    ) -> ManagedProcessResult:
        _ = (self, cwd, env, timeout_seconds)
        captured["command"] = command
        return _managed("{}", returncode=1)

    monkeypatch.setattr(EvalProcessManager, "run", fake_run)

    # When: Claude non-router execution builds the process command.
    execute_model_case(
        case,
        "claude",
        grants,
        roots=_roots(tmp_path),
        env=_env(tmp_path),
        process_manager=EvalProcessManager(),
    )

    # Then: the command is autonomous stream-json with exact grants and no builtins.
    command = captured["command"]
    assert command[0] == "claude"
    assert "--permission-mode" in command
    assert "bypassPermissions" in command
    assert "plan" not in command
    assert "stream-json" in command
    assert command[command.index("--tools") + 1] == ""
    assert "*" not in command[command.index("--allowedTools") + 1]
