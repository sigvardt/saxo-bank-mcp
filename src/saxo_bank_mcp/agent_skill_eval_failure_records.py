from __future__ import annotations

from saxo_bank_mcp.agent_skill_eval_models import (
    EvalRunRecord,
    Harness,
    SkillEvalCase,
)


def unobservable_model_failure_record(  # noqa: PLR0913
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    error: str,
    *,
    client_version: str = "",
    router_source_sha256: str = "",
) -> EvalRunRecord:
    """Build a post-launch failure without inferring an empty model or tool trace."""
    safe_error = {
        "OSError": "os_error",
        "ProcessLookupError": "process_lookup_error",
        "TimeoutExpired": "timeout_expired",
    }.get(error, error)
    return EvalRunRecord(
        case_id=case.id,
        harness=harness,
        status="failed",
        execution_mode="model_execution",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=grants,
        transcript_assertions_passed=None,
        no_model_call=False,
        no_mcp_call=None,
        no_saxo_call=None,
        model_output_observability="unknown",
        error=safe_error,
        router_source_mode="source_equivalent" if router_source_sha256 else None,
        router_source_sha256=router_source_sha256,
        model_tool_event_count=None,
        model_command_event_count=None,
        model_mcp_event_count=None,
        model_saxo_event_count=None,
        client_version=client_version,
        invoked_logical_tools=None,
        invoked_logical_tool_count=None,
        grant_status="unknown",
        assertion_status="unknown",
    )
