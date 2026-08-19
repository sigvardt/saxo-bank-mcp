from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from saxo_bank_mcp.agent_skill_eval_commands import (
    enrich_eval_cli_env,
    non_router_model_command,
    write_claude_sim_mcp_config,
)
from saxo_bank_mcp.agent_skill_eval_models import EvalRunRecord, Harness, SkillEvalCase
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager, ManagedProcessResult
from saxo_bank_mcp.agent_skill_eval_tool_protocol import (
    ModelToolTrace,
    logical_tools_from_grants,
    parse_claude_model_output,
    parse_codex_model_output,
)
from saxo_bank_mcp.agent_skill_router_eval_execution import (
    RouterCaseContext,
    RouterHomes,
    execute_router_model_case,
)


@dataclass(frozen=True)
class HarnessRoots:
    codex_plugin_root: Path
    claude_plugin_root: Path
    codex_home: Path | None
    claude_home: Path | None


class _AssistantDiagnosticFields(TypedDict):
    raw_assistant_event_count: int | None
    raw_assistant_events_sha256: str | None
    final_assistant_text_sha256: str | None
    raw_assistant_message_present: bool | None
    raw_assistant_required_all_assertion_results: tuple[bool, ...]
    raw_assistant_required_any_assertion_results: tuple[bool, ...]
    raw_assistant_forbidden_assertion_absent_results: tuple[bool, ...]


def execute_model_case(  # noqa: PLR0913
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    *,
    roots: HarnessRoots,
    env: dict[str, str],
    expected_router_source_sha256: str | None = None,
    process_manager: EvalProcessManager | None = None,
) -> EvalRunRecord:
    manager = process_manager or EvalProcessManager()
    if case.router_expectation is not None:
        plugin_root = roots.codex_plugin_root if harness == "codex" else roots.claude_plugin_root
        return execute_router_model_case(
            case,
            harness,
            grants,
            RouterCaseContext(
                plugin_root=plugin_root,
                homes=RouterHomes(
                    codex_home=roots.codex_home,
                    claude_home=roots.claude_home,
                ),
                expected_router_source_sha256=expected_router_source_sha256,
            ),
            env=env,
            process_manager=manager,
        )
    return _execute_non_router_case(case, harness, grants, roots=roots, env=env, manager=manager)


def _execute_non_router_case(  # noqa: PLR0913, PLR0911
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    *,
    roots: HarnessRoots,
    env: dict[str, str],
    manager: EvalProcessManager,
) -> EvalRunRecord:
    plugin_cwd = roots.codex_plugin_root if harness == "codex" else roots.claude_plugin_root
    # Harden PATH/CLI absolute resolution once per case so Node shebang CLIs and
    # bare-name fallbacks do not raise FileNotFoundError mid dual-harness run.
    launch_env = enrich_eval_cli_env(env)
    if not plugin_cwd.is_dir():
        return _failed_record(case, harness, grants, "cwd_not_found")
    command = _model_command(case, harness, grants, roots, env=launch_env)
    try:
        result = manager.run(
            command,
            cwd=plugin_cwd,
            env=launch_env,
            timeout_seconds=case.timeout_seconds,
        )
    except FileNotFoundError as first_exc:
        # Process never started: one retry after re-enriching CLI PATH and bins.
        # Zero MCP/Saxo calls are guaranteed because Popen never launched.
        if _fnfe_is_cwd(first_exc, plugin_cwd):
            return _failed_record(case, harness, grants, "cwd_not_found")
        try:
            retry_env = enrich_eval_cli_env(launch_env)
            retry_command = _model_command(case, harness, grants, roots, env=retry_env)
            result = manager.run(
                retry_command,
                cwd=plugin_cwd,
                env=retry_env,
                timeout_seconds=case.timeout_seconds,
            )
        except FileNotFoundError as retry_exc:
            if _fnfe_is_cwd(retry_exc, plugin_cwd):
                return _failed_record(case, harness, grants, "cwd_not_found")
            return _failed_record(
                case,
                harness,
                grants,
                "executable_not_found",
            )
        except OSError as exc:
            return _process_error_record(case, harness, grants, manager, type(exc).__name__)
    except OSError as exc:
        return _process_error_record(case, harness, grants, manager, type(exc).__name__)
    return _record_from_process(case, harness, grants, result)


def _process_error_record(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    manager: EvalProcessManager,
    error: str,
) -> EvalRunRecord:
    """Distinguish a pre-launch OSError from an unobservable post-launch failure."""
    if manager.process_cleanup == "pending" or manager.remaining_processes is None:
        return _unobservable_failed_record(case, harness, grants, error)
    return _failed_record(case, harness, grants, error)


def _fnfe_is_cwd(exc: FileNotFoundError, cwd: Path) -> bool:
    filename = getattr(exc, "filename", None)
    if filename is None:
        return False
    try:
        return Path(str(filename)).resolve() == cwd.resolve()
    except OSError:
        return str(filename) == str(cwd)


def _model_command(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    roots: HarnessRoots,
    *,
    env: dict[str, str],
) -> tuple[str, ...]:
    plugin_root = roots.codex_plugin_root if harness == "codex" else roots.claude_plugin_root
    claude_mcp_config_path = None
    if harness == "claude":
        tmp = Path(env.get("TMPDIR") or env.get("HOME") or ".")
        claude_mcp_config_path = write_claude_sim_mcp_config(
            plugin_root=plugin_root,
            dest=tmp / f"claude-mcp-{case.id}.json",
            env=env,
        )
    return non_router_model_command(
        harness=harness,
        prompt=case.harness_prompts[harness],
        resolved_grants=grants,
        plugin_root=plugin_root,
        codex_home=roots.codex_home,
        claude_mcp_config_path=claude_mcp_config_path,
        env=env,
    )


def _record_from_process(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    result: ManagedProcessResult,
) -> EvalRunRecord:
    if result.timed_out:
        return _unobservable_failed_record(case, harness, grants, "TimeoutExpired")
    if result.remaining_processes is None or result.process_cleanup == "unknown":
        return _unobservable_failed_record(case, harness, grants, "process_cleanup_unknown")
    if result.remaining_processes > 0 or result.process_cleanup == "residue":
        return _unobservable_failed_record(case, harness, grants, "process_cleanup_residue")
    return _record_from_stdout(case, harness, grants, result)


def _record_from_stdout(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    result: ManagedProcessResult,
) -> EvalRunRecord:
    try:
        trace = _parse_trace(harness, result.stdout)
    except (ValueError, TypeError):
        error = "process_nonzero_exit" if result.returncode != 0 else "malformed_output"
        return _unobservable_failed_record(case, harness, grants, error)
    if trace.parse_error == "malformed_output":
        return _unobservable_failed_record(case, harness, grants, trace.parse_error)
    if result.returncode != 0 and not _claude_nonzero_output_usable(harness, trace):
        return _failed_record(case, harness, grants, "process_nonzero_exit", trace=trace)
    if trace.parse_error:
        return _failed_record(
            case,
            harness,
            grants,
            trace.parse_error,
            trace=trace,
            assertions_passed=False,
        )
    return _evaluate_trace(case, harness, grants, trace)


def _claude_nonzero_output_usable(harness: Harness, trace: ModelToolTrace) -> bool:
    """Claude 2.x may exit non-zero after emitting usable stream-json; still grade it."""
    if harness != "claude":
        return False
    return bool(trace.saxo_event_count or trace.assistant_text.strip())


def _parse_trace(harness: Harness, stdout: str) -> ModelToolTrace:
    match harness:
        case "codex":
            return parse_codex_model_output(stdout)
        case "claude":
            return parse_claude_model_output(stdout)


def _evaluate_trace(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    trace: ModelToolTrace,
) -> EvalRunRecord:
    grant_logical = frozenset(logical_tools_from_grants(grants))
    if not grant_logical:
        grant_logical = frozenset(case.exact_tool_grants[harness])
    invoked = trace.invoked_logical_tools
    invoked_set = frozenset(invoked)
    assertion_evidence = transcript_assertion_evidence(case, trace.assistant_text, invoked)
    raw_assertion_evidence = transcript_assertion_evidence(
        case,
        "\n".join(trace.assistant_event_texts),
        invoked,
    )
    diagnostic_fields = (
        _assistant_diagnostic_fields(
            trace,
            raw_assertion_evidence=raw_assertion_evidence,
        )
        if harness == "codex"
        else _empty_assistant_diagnostic_fields()
    )
    assertions_passed = assertion_evidence.passed
    error = non_router_error(
        case=case,
        trace=trace,
        invoked_set=invoked_set,
        grant_logical=grant_logical,
        assertions_passed=assertions_passed,
    )
    grant_ok = not (invoked_set - grant_logical) and trace.non_saxo_mcp_event_count == 0
    status = "passed" if not error else "failed"
    return EvalRunRecord(
        case_id=case.id,
        harness=harness,
        status=status,
        execution_mode="model_execution",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=grants,
        transcript_assertions_passed=assertions_passed,
        no_model_call=False,
        no_mcp_call=trace.mcp_event_count == 0,
        no_saxo_call=trace.saxo_event_count == 0,
        error=error,
        model_tool_event_count=trace.tool_event_count,
        model_command_event_count=trace.command_event_count,
        model_mcp_event_count=trace.mcp_event_count,
        model_saxo_event_count=trace.saxo_event_count,
        invoked_logical_tools=invoked,
        invoked_logical_tool_count=len(invoked),
        grant_status="passed" if grant_ok else "failed",
        assertion_status="passed" if assertions_passed else "failed",
        assistant_message_present=assertion_evidence.assistant_message_present,
        required_all_assertion_results=assertion_evidence.required_all,
        required_any_assertion_results=assertion_evidence.required_any,
        forbidden_assertion_absent_results=assertion_evidence.forbidden_absent,
        **diagnostic_fields,
    )


def _side_channel_error(trace: ModelToolTrace) -> str:
    if trace.command_event_count:
        return "command_event"
    if trace.file_event_count:
        return "file_event"
    if trace.web_event_count:
        return "web_event"
    if trace.app_event_count:
        return "app_event"
    if trace.non_saxo_mcp_event_count:
        return "non_saxo_mcp_event"
    return ""


def non_router_error(
    *,
    case: SkillEvalCase,
    trace: ModelToolTrace,
    invoked_set: frozenset[str],
    grant_logical: frozenset[str],
    assertions_passed: bool,
) -> str:
    side = _side_channel_error(trace)
    if side:
        return side
    forbidden = frozenset(case.forbidden_logical_tools)
    if invoked_set & forbidden:
        return "forbidden_tool_called"
    if invoked_set - grant_logical:
        return "out_of_grant_tool"
    if not _required_tools_satisfied(case, invoked_set, saxo_event_count=trace.saxo_event_count):
        return "required_tool_missing"
    if not assertions_passed:
        return "transcript_assertion_failed"
    return ""


def _required_tools_satisfied(
    case: SkillEvalCase,
    invoked_set: frozenset[str],
    *,
    saxo_event_count: int,
) -> bool:
    """All required tools plus one member of each required group; needs real Saxo events."""
    required = tuple(case.required_logical_tools)
    if required and not all(tool in invoked_set for tool in required):
        return False
    for group in case.required_tool_groups:
        if group and not any(tool in invoked_set for tool in group):
            return False
    needs_tools = bool(required) or any(bool(group) for group in case.required_tool_groups)
    return not needs_tools or saxo_event_count > 0


def _failed_record(  # noqa: PLR0913
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    error: str,
    *,
    trace: ModelToolTrace | None = None,
    assertions_passed: bool = False,
    model_output_observable: bool = True,
) -> EvalRunRecord:
    assistant_output_observable = (
        model_output_observable and trace is not None and trace.parse_error != "malformed_output"
    )
    assertion_evidence = (
        None
        if not assistant_output_observable or trace is None
        else transcript_assertion_evidence(case, trace.assistant_text, trace.invoked_logical_tools)
    )
    raw_assertion_evidence = (
        None
        if not assistant_output_observable or trace is None
        else transcript_assertion_evidence(
            case,
            "\n".join(trace.assistant_event_texts),
            trace.invoked_logical_tools,
        )
    )
    diagnostic_fields = (
        _empty_assistant_diagnostic_fields()
        if harness != "codex" or trace is None or raw_assertion_evidence is None
        else _assistant_diagnostic_fields(
            trace,
            raw_assertion_evidence=raw_assertion_evidence,
        )
    )
    return EvalRunRecord(
        case_id=case.id,
        harness=harness,
        status="failed",
        execution_mode="model_execution",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=grants,
        transcript_assertions_passed=(assertions_passed if model_output_observable else None),
        no_model_call=False,
        no_mcp_call=(
            None if not model_output_observable else trace is None or trace.mcp_event_count == 0
        ),
        no_saxo_call=(
            None if not model_output_observable else trace is None or trace.saxo_event_count == 0
        ),
        model_output_observability=("observable" if model_output_observable else "unknown"),
        error=error,
        model_tool_event_count=(
            None if not model_output_observable or trace is None else trace.tool_event_count
        ),
        model_command_event_count=(
            None if not model_output_observable or trace is None else trace.command_event_count
        ),
        model_mcp_event_count=(
            None if not model_output_observable or trace is None else trace.mcp_event_count
        ),
        model_saxo_event_count=(
            None if not model_output_observable or trace is None else trace.saxo_event_count
        ),
        invoked_logical_tools=(
            None
            if not model_output_observable
            else ()
            if trace is None
            else trace.invoked_logical_tools
        ),
        invoked_logical_tool_count=(
            None
            if not model_output_observable
            else 0
            if trace is None
            else len(trace.invoked_logical_tools)
        ),
        grant_status="failed" if model_output_observable else "unknown",
        assertion_status=(
            "unknown"
            if not model_output_observable
            else "failed"
            if not assertions_passed
            else "passed"
        ),
        assistant_message_present=(
            None if assertion_evidence is None else assertion_evidence.assistant_message_present
        ),
        required_all_assertion_results=(
            () if assertion_evidence is None else assertion_evidence.required_all
        ),
        required_any_assertion_results=(
            () if assertion_evidence is None else assertion_evidence.required_any
        ),
        forbidden_assertion_absent_results=(
            () if assertion_evidence is None else assertion_evidence.forbidden_absent
        ),
        **diagnostic_fields,
    )


def _unobservable_failed_record(
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    error: str,
) -> EvalRunRecord:
    """Return a strict failure without inferring an empty model/tool trace."""
    safe_error = {
        "OSError": "os_error",
        "ProcessLookupError": "process_lookup_error",
        "TimeoutExpired": "timeout_expired",
    }.get(error, error)
    return _failed_record(
        case,
        harness,
        grants,
        safe_error,
        model_output_observable=False,
    )


def _assistant_diagnostic_fields(
    trace: ModelToolTrace,
    *,
    raw_assertion_evidence: TranscriptAssertionEvidence,
) -> _AssistantDiagnosticFields:
    """Return content-free evidence for decoded events and the final parsed text."""
    raw_material = json.dumps(
        trace.assistant_event_texts,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    return {
        "raw_assistant_event_count": len(trace.assistant_event_texts),
        "raw_assistant_events_sha256": hashlib.sha256(raw_material).hexdigest(),
        "final_assistant_text_sha256": hashlib.sha256(trace.assistant_text.encode()).hexdigest(),
        "raw_assistant_message_present": raw_assertion_evidence.assistant_message_present,
        "raw_assistant_required_all_assertion_results": raw_assertion_evidence.required_all,
        "raw_assistant_required_any_assertion_results": raw_assertion_evidence.required_any,
        "raw_assistant_forbidden_assertion_absent_results": (
            raw_assertion_evidence.forbidden_absent
        ),
    }


def _empty_assistant_diagnostic_fields() -> _AssistantDiagnosticFields:
    return {
        "raw_assistant_event_count": None,
        "raw_assistant_events_sha256": None,
        "final_assistant_text_sha256": None,
        "raw_assistant_message_present": None,
        "raw_assistant_required_all_assertion_results": (),
        "raw_assistant_required_any_assertion_results": (),
        "raw_assistant_forbidden_assertion_absent_results": (),
    }


@dataclass(frozen=True, slots=True)
class TranscriptAssertionEvidence:
    assistant_message_present: bool
    required_all: tuple[bool, ...]
    required_any: tuple[bool, ...]
    forbidden_absent: tuple[bool, ...]

    @property
    def passed(self) -> bool:
        return (
            all(self.required_all)
            and (not self.required_any or any(self.required_any))
            and all(self.forbidden_absent)
        )


def transcript_assertion_evidence(
    case: SkillEvalCase,
    transcript: str,
    invoked_logical_tools: tuple[str, ...] | frozenset[str] = (),
) -> TranscriptAssertionEvidence:
    """Return only allowlisted booleans for each transcript assertion."""
    lowered = transcript.lower()
    invoked = frozenset(invoked_logical_tools)
    return TranscriptAssertionEvidence(
        assistant_message_present=bool(transcript.strip()),
        required_all=tuple(
            _transcript_value_satisfied(value, lowered=lowered, invoked=invoked)
            for value in case.transcript_assertions.required_all
        ),
        required_any=tuple(
            _transcript_value_satisfied(value, lowered=lowered, invoked=invoked)
            for value in case.transcript_assertions.required_any
        ),
        forbidden_absent=tuple(
            value.lower() not in lowered for value in case.transcript_assertions.forbidden
        ),
    )


def transcript_passed(
    case: SkillEvalCase,
    transcript: str,
    invoked_logical_tools: tuple[str, ...] | frozenset[str] = (),
) -> bool:
    """Require safety prose; tool-name requirements may be satisfied by real invocations."""
    return transcript_assertion_evidence(case, transcript, invoked_logical_tools).passed


def _transcript_value_satisfied(
    value: str,
    *,
    lowered: str,
    invoked: frozenset[str],
) -> bool:
    if value.lower() in lowered:
        return True
    # Logical tool IDs named in assertions are agent-realistic when actually invoked.
    return value.startswith("saxo_") and value in invoked
