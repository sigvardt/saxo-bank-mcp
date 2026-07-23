from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from saxo_bank_mcp.agent_skill_eval_models import EvalRunRecord, Harness, SkillEvalCase


@dataclass(frozen=True)
class HarnessRoots:
    codex_plugin_root: Path
    claude_plugin_root: Path
    codex_home: Path | None
    claude_home: Path | None


def execute_model_case(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    *,
    roots: HarnessRoots,
) -> EvalRunRecord:
    command = _model_command(case, harness, grants, roots)
    env = os.environ.copy()
    if harness == "codex" and roots.codex_home is not None:
        env["CODEX_HOME"] = str(roots.codex_home)
    if harness == "claude" and roots.claude_home is not None:
        env["HOME"] = str(roots.claude_home)
    try:
        result = subprocess.run(
            command,
            cwd=roots.codex_plugin_root if harness == "codex" else roots.claude_plugin_root,
            env=env,
            text=True,
            capture_output=True,
            timeout=case.timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _failed_record(case, harness, grants, type(exc).__name__)
    transcript = f"{result.stdout}\n{result.stderr}"
    assertions_passed = _transcript_passed(case, transcript)
    if result.returncode != 0 or not assertions_passed:
        return _failed_record(case, harness, grants, "model_case_failed")
    return EvalRunRecord(
        case_id=case.id,
        harness=harness,
        status="passed",
        execution_mode="model_execution",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=grants,
        transcript_assertions_passed=True,
        no_model_call=False,
        no_mcp_call=False,
        no_saxo_call=False,
    )


def _model_command(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    roots: HarnessRoots,
) -> tuple[str, ...]:
    match harness:
        case "codex":
            return (
                "codex",
                "exec",
                "--json",
                "--cd",
                str(roots.codex_plugin_root),
                case.harness_prompts["codex"],
            )
        case "claude":
            allowed = ",".join(grants)
            return (
                "claude",
                "--plugin-dir",
                str(roots.claude_plugin_root),
                "--permission-mode",
                "plan",
                "--allowedTools",
                allowed,
                "--output-format",
                "json",
                "--print",
                case.harness_prompts["claude"],
            )


def _failed_record(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    error: str,
) -> EvalRunRecord:
    return EvalRunRecord(
        case_id=case.id,
        harness=harness,
        status="failed",
        execution_mode="model_execution",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=grants,
        transcript_assertions_passed=False,
        no_model_call=False,
        no_mcp_call=False,
        no_saxo_call=False,
        error=error,
    )


def _transcript_passed(case: SkillEvalCase, transcript: str) -> bool:
    lowered = transcript.lower()
    required_all = all(
        value.lower() in lowered for value in case.transcript_assertions.required_all
    )
    required_any = (
        True
        if not case.transcript_assertions.required_any
        else any(value.lower() in lowered for value in case.transcript_assertions.required_any)
    )
    forbidden = any(value.lower() in lowered for value in case.transcript_assertions.forbidden)
    return required_all and required_any and not forbidden
