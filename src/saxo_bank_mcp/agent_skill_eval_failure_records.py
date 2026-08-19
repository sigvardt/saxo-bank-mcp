from __future__ import annotations

import subprocess
from typing import Final

from saxo_bank_mcp.agent_skill_eval_models import (
    EvalRunRecord,
    Harness,
    SkillEvalCase,
)

UNOBSERVABLE_FAILURE_REASONS: Final = frozenset(
    {
        "client_version_cleanup_residue",
        "client_version_cleanup_unknown",
        "client_version_timeout",
        "file_not_found_error",
        "key_error",
        "malformed_output",
        "model_execution_error",
        "os_error",
        "permission_error",
        "process_cleanup_residue",
        "process_cleanup_unknown",
        "process_lookup_error",
        "process_nonzero_exit",
        "structured_output_invalid",
        "timeout_expired",
        "type_error",
        "value_error",
    },
)
_EXCEPTION_NAME_REASONS: Final = {
    "FileNotFoundError": "file_not_found_error",
    "KeyError": "key_error",
    "OSError": "os_error",
    "PermissionError": "permission_error",
    "ProcessLookupError": "process_lookup_error",
    "TimeoutExpired": "timeout_expired",
    "TypeError": "type_error",
    "ValueError": "value_error",
}
_EXCEPTION_TYPE_REASONS: Final = (
    (PermissionError, "permission_error"),
    (FileNotFoundError, "file_not_found_error"),
    (ProcessLookupError, "process_lookup_error"),
    (subprocess.TimeoutExpired, "timeout_expired"),
    (OSError, "os_error"),
    (KeyError, "key_error"),
    (ValueError, "value_error"),
    (TypeError, "type_error"),
)


def normalize_unobservable_failure_reason(error: str | BaseException) -> str:
    """Reduce caught failures to fixed privacy-safe reason codes."""
    if isinstance(error, BaseException):
        return next(
            (
                reason
                for exception_type, reason in _EXCEPTION_TYPE_REASONS
                if isinstance(error, exception_type)
            ),
            "model_execution_error",
        )
    reason = _EXCEPTION_NAME_REASONS.get(error, error)
    return reason if reason in UNOBSERVABLE_FAILURE_REASONS else "model_execution_error"


def unobservable_model_failure_record(  # noqa: PLR0913
    case: SkillEvalCase,
    harness: Harness,
    grants: tuple[str, ...],
    error: str | BaseException,
    *,
    client_version: str = "",
    router_source_sha256: str = "",
) -> EvalRunRecord:
    """Build a post-launch failure without inferring an empty model or tool trace."""
    safe_error = normalize_unobservable_failure_reason(error)
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
