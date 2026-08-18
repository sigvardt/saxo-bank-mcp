# allow: SIZE_OK - dual-harness eval orchestration owns binding, runtime, cleanup, and reports.
from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_eval_commands import child_env_for_case
from saxo_bank_mcp.agent_skill_eval_execution import HarnessRoots, execute_model_case
from saxo_bank_mcp.agent_skill_eval_models import (
    EvalRunRecord,
    EvalRunReport,
    Harness,
    HarnessSelector,
    SkillEvalCase,
    load_eval_cases,
    select_cases,
    selected_harnesses,
)
from saxo_bank_mcp.agent_skill_eval_native_preflight import (
    CodexNativeCaseMcpBinding,
    CodexNativePreflightError,
    CodexNativePreflightReceipt,
    codex_native_case_mcp_config,
    preflight_codex_native_case,
    verify_codex_native_case_mcp_config,
)
from saxo_bank_mcp.agent_skill_eval_process import EvalProcessManager
from saxo_bank_mcp.agent_skill_eval_validation import validate_eval_suite
from saxo_bank_mcp.agent_skill_install_paths import MARKETPLACE_NAME, PLUGIN_NAME
from saxo_bank_mcp.agent_skill_install_qa import load_install_report_for_consumers
from saxo_bank_mcp.agent_skill_matrix_env import (
    MatrixEnvError,
    MatrixIsolatedRuntime,
    cleanup_matrix_isolated_runtime,
    codex_plugin_identity,
    prepare_eval_isolated_runtime,
    promote_rotated_claude_credentials,
    promote_rotated_sim_token_cache,
    require_matrix_runtime_cleanup,
)
from saxo_bank_mcp.agent_skill_router_eval_execution import (
    RouterBindingRequest,
    RouterSourceBinding,
    client_versions,
    codex_client_version,
    resolve_router_source_binding,
)
from saxo_bank_mcp.qa_analytics_sim import analytics_primary_calls
from saxo_bank_mcp.qa_codex_native_policy import HarnessPolicy
from saxo_bank_mcp.server_eval_tool_filter import EvalToolFilterError

CREDENTIAL_MODE_NONE: Final = "none"
CREDENTIAL_MODE_EPHEMERAL: Final = "ephemeral-owner-only-copy"
ALLOWED_CREDENTIAL_MODES: Final = frozenset({CREDENTIAL_MODE_NONE, CREDENTIAL_MODE_EPHEMERAL})


@dataclass(frozen=True)
class EvalRunOptions:
    harness: HarnessSelector
    case_id: str | None
    tag: str | None
    environment: str | None
    case_root: Path
    codex_plugin_root: Path
    claude_plugin_root: Path
    codex_home: Path | None
    claude_home: Path | None
    out: Path
    dry_run: bool
    nonzero_on_skip: bool
    expected_source_commit: str | None = None
    expected_router_source_sha256: str | None = None
    source_repo: Path | None = None
    install_report: Path | None = None
    credential_mode: str = CREDENTIAL_MODE_NONE
    source_codex_home: Path | None = None
    source_claude_home: Path | None = None
    harness_policy: HarnessPolicy = "dual_v1"


@dataclass(frozen=True, slots=True)
class _RuntimeOutcome:
    records: tuple[EvalRunRecord, ...]
    enforced_mode: str
    versions: dict[str, str]
    cleanup: dict[str, JsonValue]
    error: str


def run_eval_suite(options: EvalRunOptions) -> int:
    validation_root = (
        options.case_root.parent if options.case_root.parent.name == "evals" else options.case_root
    )
    validation = validate_eval_suite(case_root=validation_root)
    cases = select_cases(
        load_eval_cases(options.case_root),
        case_id=options.case_id,
        tag=options.tag,
        environment=options.environment,
    )
    cases, native_selection_error = _apply_harness_case_policy(options, cases)
    roots = HarnessRoots(
        codex_plugin_root=options.codex_plugin_root,
        claude_plugin_root=options.claude_plugin_root,
        codex_home=options.codex_home,
        claude_home=options.claude_home,
    )
    before = global_state_fingerprint(
        codex_home=options.codex_home,
        claude_home=options.claude_home,
    )

    mode_error = (
        _credential_mode_error(options.credential_mode)
        or _harness_policy_error(options)
        or native_selection_error
    )
    binding: RouterSourceBinding | None = None
    binding_error = mode_error
    if (
        not binding_error
        and not options.dry_run
        and any(case.router_expectation is not None for case in cases)
    ):
        if not options.expected_source_commit:
            binding_error = "missing_expected_source_commit"
        else:
            resolved = resolve_router_source_binding(
                RouterBindingRequest(
                    repo=options.source_repo or Path(),
                    expected_source_commit=options.expected_source_commit,
                    expected_router_source_sha256=options.expected_router_source_sha256,
                    codex_plugin_root=options.codex_plugin_root,
                    claude_plugin_root=options.claude_plugin_root,
                    require_git_checkout=False,
                ),
            )
            if isinstance(resolved, RouterSourceBinding):
                binding = resolved
            else:
                binding_error = resolved

    outcome = _select_execution_outcome(
        options,
        cases=cases,
        roots=roots,
        binding=binding,
        binding_error=binding_error,
    )
    records = outcome.records
    if (
        binding is not None
        and not options.dry_run
        and not outcome.error
        and any(
            record.router_source_mode == "source_equivalent"
            and record.router_source_sha256 != binding.router_source_sha256
            for record in records
        )
    ):
        binding_error = "record_router_source_digest_mismatch"
        records = _rewrite_digest_mismatches(records, binding.router_source_sha256)
        outcome = _RuntimeOutcome(
            records=records,
            enforced_mode=outcome.enforced_mode,
            versions=outcome.versions,
            cleanup=outcome.cleanup,
            error=binding_error,
        )

    after = global_state_fingerprint(
        codex_home=options.codex_home,
        claude_home=options.claude_home,
    )
    report_error = binding_error or outcome.error
    skipped_count = sum(1 for record in records if record.status == "skipped")
    empty_selection = not cases or not records
    planned = bool(options.dry_run) and not empty_selection and not report_error
    failed = (
        validation.status != "passed"
        or bool(report_error)
        or empty_selection
        or any(record.status == "failed" for record in records)
        or not bool(outcome.cleanup.get("complete", True))
    )
    skipped_failure = bool(options.nonzero_on_skip and skipped_count)
    status: Literal["passed", "failed", "skipped", "planned"] = (
        "failed" if failed or skipped_failure else "planned" if planned else "passed"
    )
    router_records = tuple(
        record for record in records if record.router_source_mode == "source_equivalent"
    )
    model_records = tuple(
        record
        for record in records
        if record.execution_mode == "model_execution" and not record.no_model_call
    )
    installation_fixture_preserved = _installation_fixture_preserved(options.install_report)
    source_commit = "" if binding is None else binding.source_commit
    if not source_commit and options.expected_source_commit:
        source_commit = options.expected_source_commit
    report = EvalRunReport(
        status=status,
        harness=options.harness,
        environment=(
            "LOCAL+SIM"
            if options.harness_policy == "codex_native_v1" and options.environment is None
            else options.environment or "ALL"
        ),
        execution_mode="manifest_validation" if options.dry_run else "model_execution",
        selected_case_count=len(cases),
        case_count=len(records),
        records=records,
        cleanup={
            "complete": bool(outcome.cleanup.get("complete", True)),
            "created_processes": _cleanup_int(outcome.cleanup, "created_processes"),
            "terminated_processes": _cleanup_int(outcome.cleanup, "terminated_processes"),
            "remaining_processes": _cleanup_int(outcome.cleanup, "remaining_processes"),
            "process_cleanup": outcome.cleanup.get("process_cleanup", "not_required"),
            "process_timed_out": bool(outcome.cleanup.get("process_timed_out", False)),
            "raw_transcripts_persisted": 0,
            "model_prompt_count": len(model_records),
            "model_prompt_counts": {
                harness: sum(1 for record in model_records if record.harness == harness)
                for harness in ("codex", "claude")
            },
            "model_tool_events": _sum_observable_counts(
                record.model_tool_event_count for record in model_records
            ),
            "model_command_events": _sum_observable_counts(
                record.model_command_event_count for record in model_records
            ),
            "created_mcp_calls": (
                0
                if options.dry_run
                else _sum_observable_counts(
                    record.model_mcp_event_count for record in model_records
                )
            ),
            "model_saxo_events": _sum_observable_counts(
                record.model_saxo_event_count for record in model_records
            ),
            "invoked_logical_tool_count": _sum_observable_counts(
                record.invoked_logical_tool_count for record in model_records
            ),
            "client_versions": outcome.versions,
            "client_versions_from_records": {
                harness: sorted(
                    {
                        record.client_version
                        for record in model_records
                        if record.harness == harness and record.client_version
                    }
                )
                for harness in ("codex", "claude")
            },
            "source_binding": (
                {
                    "status": "failed",
                    "error": binding_error,
                    "expected_source_commit": options.expected_source_commit or "",
                    "expected_router_source_sha256": (options.expected_router_source_sha256 or ""),
                }
                if binding_error
                else {
                    "status": "passed" if binding is not None else "not_required",
                    "source_commit": "" if binding is None else binding.source_commit,
                    "router_source_sha256": (
                        "" if binding is None else binding.router_source_sha256
                    ),
                    "file_digests": {} if binding is None else binding.file_digests,
                    "codex_plugin_root": ("" if binding is None else binding.codex_plugin_root),
                    "claude_plugin_root": ("" if binding is None else binding.claude_plugin_root),
                }
            ),
            "credential_mode": outcome.enforced_mode,
            "runtime_error": outcome.error,
            "runtime_cleanup": outcome.cleanup.get("runtime_cleanup", "not_required"),
            "token_promote": outcome.cleanup.get("token_promote", "not_required"),
            "installation_fixture_preserved": installation_fixture_preserved,
            "router_case_count": len(router_records),
        },
        before_global_state=before,
        after_global_state=after,
        global_state_unchanged=before == after,
        skipped_count=skipped_count,
        nonzero_on_skip=options.nonzero_on_skip,
        source_commit=source_commit,
        router_source_sha256="" if binding is None else binding.router_source_sha256,
        router_source_file_digests={} if binding is None else binding.file_digests,
    )
    payload = report.to_json_value()
    payload["run_cleanup"] = {
        "complete": bool(outcome.cleanup.get("complete", True)),
        "process_cleanup": outcome.cleanup.get("process_cleanup", "not_required"),
        "runtime_cleanup": outcome.cleanup.get("runtime_cleanup", "not_required"),
        "token_promote": outcome.cleanup.get("token_promote", "not_required"),
        "created_processes": _cleanup_int(outcome.cleanup, "created_processes"),
        "terminated_processes": _cleanup_int(outcome.cleanup, "terminated_processes"),
        "remaining_processes": _cleanup_int(outcome.cleanup, "remaining_processes"),
    }
    payload["installation_fixture_preserved"] = installation_fixture_preserved
    write_json(options.out, payload)
    if not installation_fixture_preserved and options.install_report is not None:
        return 1
    return 0 if status in {"passed", "planned"} else 1


def _select_execution_outcome(
    options: EvalRunOptions,
    *,
    cases: tuple[SkillEvalCase, ...],
    roots: HarnessRoots,
    binding: RouterSourceBinding | None,
    binding_error: str,
) -> _RuntimeOutcome:
    idle = _RuntimeOutcome(
        records=(),
        enforced_mode=CREDENTIAL_MODE_NONE,
        versions={},
        cleanup=_idle_cleanup(),
        error=binding_error,
    )
    if binding_error:
        return idle
    if options.dry_run:
        return _RuntimeOutcome(
            records=tuple(
                _planned_record(case, harness)
                for case in cases
                for harness in selected_harnesses(options.harness)
            ),
            enforced_mode=CREDENTIAL_MODE_NONE,
            versions={},
            cleanup=idle.cleanup,
            error="",
        )
    if not cases:
        return idle
    if options.credential_mode != CREDENTIAL_MODE_EPHEMERAL:
        reason = (
            "credential_mode_required_for_sim"
            if _sim_execution_required(cases, options.environment)
            else "credential_mode_not_enforced"
        )
        return _RuntimeOutcome(
            records=(),
            enforced_mode=CREDENTIAL_MODE_NONE,
            versions={},
            cleanup=idle.cleanup,
            error=reason,
        )
    return _execute_with_ephemeral_runtime(
        options,
        cases=cases,
        roots=roots,
        binding=binding,
    )


def _execute_with_ephemeral_runtime(
    options: EvalRunOptions,
    *,
    cases: tuple[SkillEvalCase, ...],
    roots: HarnessRoots,
    binding: RouterSourceBinding | None,
) -> _RuntimeOutcome:
    process_manager = EvalProcessManager()
    try:
        # source_* = actual CLI auth (omit -> global). *_home = retained install plugin state.
        runtime = prepare_eval_isolated_runtime(
            options.out.parent.resolve(),
            source_codex_home=options.source_codex_home,
            source_claude_home=options.source_claude_home,
            retained_codex_home=options.codex_home,
            retained_claude_home=options.claude_home,
            retained_codex_plugin_root=(
                options.codex_plugin_root if options.codex_home is not None else None
            ),
            harness_policy=options.harness_policy,
        )
    except MatrixEnvError as exc:
        return _RuntimeOutcome(
            records=(),
            enforced_mode=CREDENTIAL_MODE_NONE,
            versions={},
            cleanup=_idle_cleanup(),
            error=exc.reason,
        )
    return _run_cases_then_cleanup(
        options,
        cases=cases,
        roots=roots,
        binding=binding,
        runtime=runtime,
        process_manager=process_manager,
    )


def _run_cases_then_cleanup(  # noqa: PLR0913
    options: EvalRunOptions,
    *,
    cases: tuple[SkillEvalCase, ...],
    roots: HarnessRoots,
    binding: RouterSourceBinding | None,
    runtime: MatrixIsolatedRuntime,
    process_manager: EvalProcessManager,
) -> _RuntimeOutcome:
    process_error: str | None = None
    promote_error: MatrixEnvError | None = None
    cleanup_error: MatrixEnvError | None = None
    execution_error = ""
    records: tuple[EvalRunRecord, ...] = ()
    versions: dict[str, str] = {}
    try:
        versions = (
            {
                "codex": codex_client_version(
                    env=runtime.env,
                    process_manager=process_manager,
                ),
            }
            if options.harness_policy == "codex_native_v1"
            else client_versions(env=runtime.env, process_manager=process_manager)
        )
        records = _execute_selected_cases(
            options,
            cases=cases,
            roots=roots,
            binding=binding,
            runtime=runtime,
            process_manager=process_manager,
        )
        execution_error = _first_record_error(records)
    except OSError as exc:
        execution_error = type(exc).__name__
    finally:
        # Process cleanup must finish before token promotion and runtime deletion.
        process_manager.finalize()
        if process_manager.remaining_processes > 0 or process_manager.process_cleanup not in {
            "passed",
            "not_required",
        }:
            process_error = "process_cleanup_residue"
        try:
            promote_rotated_sim_token_cache(runtime)
        except MatrixEnvError as exc:
            promote_error = exc
        if options.harness_policy == "dual_v1":
            try:
                promote_rotated_claude_credentials(runtime)
            except MatrixEnvError as exc:
                promote_error = _combine_promotion_errors(promote_error, exc)
        try:
            require_matrix_runtime_cleanup(runtime.run_root)
        except MatrixEnvError as exc:
            cleanup_error = exc
            cleanup_matrix_isolated_runtime(runtime.run_root)
    return _RuntimeOutcome(
        records=records,
        enforced_mode=CREDENTIAL_MODE_EPHEMERAL,
        versions=versions,
        cleanup=_cleanup_fields(
            process_manager=process_manager,
            process_error=process_error,
            promote_error=promote_error,
            cleanup_error=cleanup_error,
        ),
        error=_primary_runtime_error(
            execution_error=execution_error,
            process_error=process_error,
            promote_error=promote_error,
            cleanup_error=cleanup_error,
        ),
    )


def _execute_selected_cases(  # noqa: PLR0913
    options: EvalRunOptions,
    *,
    cases: tuple[SkillEvalCase, ...],
    roots: HarnessRoots,
    binding: RouterSourceBinding | None,
    runtime: MatrixIsolatedRuntime,
    process_manager: EvalProcessManager,
) -> tuple[EvalRunRecord, ...]:
    codex_plugin = (
        _disposable_codex_plugin_path(
            runtime.codex_home,
            retained_codex_home=options.codex_home,
            retained_plugin_root=roots.codex_plugin_root,
        )
        or roots.codex_plugin_root
    )
    # Claude uses exact retained --plugin-dir; Codex uses disposable seeded copy.
    child_roots = HarnessRoots(
        codex_plugin_root=codex_plugin,
        claude_plugin_root=roots.claude_plugin_root,
        codex_home=runtime.codex_home,
        claude_home=runtime.home,
    )
    records: list[EvalRunRecord] = []
    for case in cases:
        for harness in selected_harnesses(options.harness):
            grants = resolve_tool_grants(harness, case.exact_tool_grants[harness])
            execution_case = _codex_native_fixture_bound_case(
                case,
                harness=harness,
                harness_policy=options.harness_policy,
            )
            try:
                env = _case_child_env(runtime.env, case=execution_case, harness=harness)
            except EvalToolFilterError as exc:
                records.append(
                    EvalRunRecord(
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
                        no_mcp_call=True,
                        no_saxo_call=True,
                        error=exc.reason,
                        grant_status="failed",
                        assertion_status="failed",
                    ),
                )
                continue
            if (
                options.harness_policy == "codex_native_v1"
                and harness == "codex"
                and execution_case.router_expectation is None
            ):
                mcp_config_binding: CodexNativeCaseMcpBinding | None = None
                native_preflight: CodexNativePreflightReceipt | None = None
                model_record: EvalRunRecord | None = None
                try:
                    with codex_native_case_mcp_config(
                        plugin_root=child_roots.codex_plugin_root,
                        logical_grants=execution_case.exact_tool_grants["codex"],
                        env=env,
                        interpreter=Path(sys.executable),
                    ) as bound_config:
                        mcp_config_binding = bound_config
                        try:
                            native_preflight = preflight_codex_native_case(
                                codex_home=runtime.codex_home,
                                plugin_root=child_roots.codex_plugin_root,
                                logical_grants=execution_case.exact_tool_grants["codex"],
                                env=env,
                                probe_env=runtime.probe_env,
                                mcp_config_binding=bound_config,
                            )
                        except CodexNativePreflightError as exc:
                            record = _native_preflight_failed_record(
                                execution_case,
                                grants,
                                exc,
                                mcp_config_binding=bound_config,
                            )
                        else:
                            verify_codex_native_case_mcp_config(
                                bound_config,
                                logical_grants=execution_case.exact_tool_grants["codex"],
                                env=env,
                            )
                            model_record = execute_model_case(
                                execution_case,
                                harness,
                                grants,
                                roots=child_roots,
                                env=env,
                                expected_router_source_sha256=(
                                    None if binding is None else binding.router_source_sha256
                                ),
                                process_manager=process_manager,
                            )
                            verify_codex_native_case_mcp_config(
                                bound_config,
                                logical_grants=execution_case.exact_tool_grants["codex"],
                                env=env,
                            )
                            record = model_record.model_copy(
                                update={
                                    "mcp_probe_stage": native_preflight.mcp_probe_stage,
                                    "mcp_probe_exit_code": native_preflight.mcp_probe_exit_code,
                                    "mcp_probe_stdout_schema_sha256": (
                                        native_preflight.mcp_probe_stdout_schema_sha256
                                    ),
                                    "mcp_config_sha256": bound_config.config_sha256,
                                    "mcp_config_path_identity_sha256": (
                                        bound_config.path_identity_sha256
                                    ),
                                },
                            )
                except CodexNativePreflightError as exc:
                    if model_record is None:
                        record = _native_preflight_failed_record(
                            execution_case,
                            grants,
                            exc,
                            mcp_config_binding=mcp_config_binding,
                        )
                    else:
                        record = _native_post_model_binding_failed_record(
                            model_record,
                            exc,
                            mcp_config_binding=mcp_config_binding,
                            native_preflight=native_preflight,
                        )
                records.append(record)
                continue
            record = execute_model_case(
                execution_case,
                harness,
                grants,
                roots=child_roots,
                env=env,
                expected_router_source_sha256=(
                    None if binding is None else binding.router_source_sha256
                ),
                process_manager=process_manager,
            )
            records.append(record)
    return tuple(records)


def _native_preflight_failed_record(
    case: SkillEvalCase,
    grants: tuple[str, ...],
    failure: CodexNativePreflightError,
    *,
    mcp_config_binding: CodexNativeCaseMcpBinding | None = None,
) -> EvalRunRecord:
    return EvalRunRecord(
        case_id=case.id,
        harness="codex",
        status="failed",
        execution_mode="model_execution",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=grants,
        transcript_assertions_passed=False,
        no_model_call=True,
        no_mcp_call=failure.mcp_started is False,
        no_saxo_call=True,
        error=failure.reason,
        plugin_list_exit_code=failure.plugin_list_exit_code,
        plugin_list_stdout_schema_sha256=failure.plugin_list_stdout_schema_sha256,
        mcp_probe_stage=failure.mcp_probe_stage,
        mcp_probe_exit_code=failure.mcp_probe_exit_code,
        mcp_probe_stdout_schema_sha256=failure.mcp_probe_stdout_schema_sha256,
        mcp_config_sha256=(
            None if mcp_config_binding is None else mcp_config_binding.config_sha256
        ),
        mcp_config_path_identity_sha256=(
            None if mcp_config_binding is None else mcp_config_binding.path_identity_sha256
        ),
        grant_status="failed",
        assertion_status="failed",
    )


def _native_post_model_binding_failed_record(
    record: EvalRunRecord,
    failure: CodexNativePreflightError,
    *,
    mcp_config_binding: CodexNativeCaseMcpBinding | None,
    native_preflight: CodexNativePreflightReceipt | None,
) -> EvalRunRecord:
    """Retain proven model facts when config verification or restoration fails afterward."""
    return record.model_copy(
        update={
            "status": "failed",
            "error": failure.reason,
            "mcp_probe_stage": (
                failure.mcp_probe_stage
                if native_preflight is None
                else native_preflight.mcp_probe_stage
            ),
            "mcp_probe_exit_code": (
                failure.mcp_probe_exit_code
                if native_preflight is None
                else native_preflight.mcp_probe_exit_code
            ),
            "mcp_probe_stdout_schema_sha256": (
                failure.mcp_probe_stdout_schema_sha256
                if native_preflight is None
                else native_preflight.mcp_probe_stdout_schema_sha256
            ),
            "mcp_config_sha256": (
                None if mcp_config_binding is None else mcp_config_binding.config_sha256
            ),
            "mcp_config_path_identity_sha256": (
                None if mcp_config_binding is None else mcp_config_binding.path_identity_sha256
            ),
        },
    )


def _codex_native_fixture_bound_case(
    case: SkillEvalCase,
    *,
    harness: Harness,
    harness_policy: HarnessPolicy,
) -> SkillEvalCase:
    """Bind the installed skill and safe analytics handles only for native execution."""
    if (
        harness_policy != "codex_native_v1"
        or harness != "codex"
        or case.router_expectation is not None
    ):
        return case
    prompts = dict(case.harness_prompts)
    prompts["codex"] = (
        f"Use $saxo-bank-mcp:{case.expected_skill} for this installed Codex-native hard "
        f"workflow. Execute case ID {case.id}.\n\n{prompts['codex']}"
    )
    case = case.model_copy(update={"harness_prompts": prompts})
    if case.expected_skill != "saxo-analytics":
        return case
    fixtures = dict(analytics_primary_calls())
    if any(tool_id not in fixtures for tool_id in case.required_logical_tools):
        return case
    calls = "\n".join(
        f"- {tool_id} "
        f"{json.dumps(fixtures[tool_id], allow_nan=False, separators=(',', ':'), sort_keys=True)}"
        for tool_id in case.required_logical_tools
    )
    required_phrases = "; ".join(case.transcript_assertions.required_all)
    optional_phrases = "; ".join(case.transcript_assertions.required_any)
    fixture_protocol = (
        "\n\nCodex-native proof fixture protocol:\n"
        "The analytics capability context is already current for this case. "
        "The harness owns cleanup for this contained fixture. "
        "Never call an ungranted capability or deletion tool. "
        "Call every required tool even when an earlier fixture is refused. "
        "Use the calls below in order with exactly these schema-valid synthetic handles; "
        "a degraded or refused result is valid evidence and must never be relabelled as success. "
        "These calls are authorized, contained LOCAL/SIM QA structured-refusal probes: invoking "
        "them does not assert that the handles exist. Do not pre-emptively refuse before making "
        "these controlled QA calls. Never call an ungranted tool.\n"
        f"{calls}\n"
        "For saxo_delete_analytics_data, use the token returned by "
        "saxo_preview_analytics_deletion instead of the empty fixture object. "
        "Use an ID returned by an earlier call when available; otherwise retain the listed "
        "synthetic handle as an independent structured-refusal probe.\n"
        f"Final answer must include: {required_phrases}."
    )
    if optional_phrases:
        fixture_protocol += f" Include at least one of: {optional_phrases}."
    if case.id == "research-to-precheck":
        fixture_protocol += (
            " Final receipt: `analysis_id: <result analysis_id or fixture analysis_id>; "
            "state: <verified|degraded|refused>; stop before broker write`."
        )
    if case.id == "scenario":
        fixture_protocol += (
            " Final scenario receipt must contain this exact ordered text: "
            "explicit numeric shocks -0.10 0.05."
        )
    prompts = dict(case.harness_prompts)
    prompts["codex"] = prompts["codex"] + fixture_protocol
    return case.model_copy(update={"harness_prompts": prompts})


def _case_child_env(
    base_env: dict[str, str],
    *,
    case: SkillEvalCase,
    harness: Harness,
) -> dict[str, str]:
    from saxo_bank_mcp.agent_skill_eval_commands import (  # noqa: PLC0415
        enrich_eval_cli_env,
    )
    from saxo_bank_mcp.agent_skill_matrix_env import (  # noqa: PLC0415
        apply_case_eval_allowlists,
    )

    # Router cases stay plan-only with the shared isolated env (no tool filter).
    if case.router_expectation is not None:
        return enrich_eval_cli_env(apply_case_eval_allowlists(dict(base_env), case_id=case.id))
    filtered = child_env_for_case(base_env, case.exact_tool_grants[harness])
    return enrich_eval_cli_env(apply_case_eval_allowlists(filtered, case_id=case.id))


def _first_record_error(records: tuple[EvalRunRecord, ...]) -> str:
    for record in records:
        if record.status == "failed":
            return record.error or "model_case_failed"
    return ""


def _primary_runtime_error(
    *,
    execution_error: str,
    process_error: str | None,
    promote_error: MatrixEnvError | None,
    cleanup_error: MatrixEnvError | None,
) -> str:
    if execution_error:
        return execution_error
    if process_error is not None:
        return process_error
    if promote_error is not None:
        return promote_error.reason
    if cleanup_error is not None:
        return cleanup_error.reason
    return ""


def _combine_promotion_errors(
    current: MatrixEnvError | None,
    latest: MatrixEnvError,
) -> MatrixEnvError:
    if current is None:
        return latest
    return MatrixEnvError(f"{current.reason}+{latest.reason}")


def _cleanup_fields(
    *,
    process_manager: EvalProcessManager,
    process_error: str | None,
    promote_error: MatrixEnvError | None,
    cleanup_error: MatrixEnvError | None,
) -> dict[str, JsonValue]:
    complete = process_error is None and promote_error is None and cleanup_error is None
    process_cleanup = "residue" if process_error is not None else process_manager.process_cleanup
    return {
        "complete": complete,
        "runtime_cleanup": "residue" if cleanup_error is not None else "passed",
        "token_promote": "failed" if promote_error is not None else "passed",
        "process_cleanup": process_cleanup,
        "created_processes": process_manager.created_processes,
        "terminated_processes": process_manager.terminated_processes,
        "remaining_processes": process_manager.remaining_processes,
        "process_timed_out": process_manager.timed_out,
    }


def _disposable_codex_plugin_path(
    disposable_codex_home: Path,
    *,
    retained_codex_home: Path | None,
    retained_plugin_root: Path,
) -> Path | None:
    plugin = retained_plugin_root.expanduser()
    try:
        plugin_resolved = plugin.resolve()
    except OSError:
        return None
    if retained_codex_home is not None:
        try:
            home = retained_codex_home.expanduser().resolve()
            if plugin_resolved.is_relative_to(home):
                candidate = disposable_codex_home / plugin_resolved.relative_to(home)
                if candidate.is_dir():
                    return candidate.resolve()
        except OSError:
            pass
    marketplace = ""
    plugin_name = ""
    disposable_home: Path | None = None
    try:
        marketplace, plugin_name, version = codex_plugin_identity(plugin_resolved)
        disposable_home = disposable_codex_home.expanduser().resolve(strict=True)
        candidate = (
            disposable_home / "plugins" / "cache" / marketplace / plugin_name / version
        ).resolve(strict=True)
    except (MatrixEnvError, OSError):
        candidate = None
    if (
        candidate is not None
        and disposable_home is not None
        and marketplace == MARKETPLACE_NAME
        and plugin_name == PLUGIN_NAME
        and candidate.is_dir()
        and candidate.is_relative_to(disposable_home)
    ):
        return candidate
    fallback = disposable_codex_home / "plugins" / "cache" / "retained" / plugin_resolved.name
    return fallback.resolve() if fallback.is_dir() else None


def _idle_cleanup() -> dict[str, JsonValue]:
    return {
        "complete": True,
        "runtime_cleanup": "not_required",
        "token_promote": "not_required",
        "process_cleanup": "not_required",
        "created_processes": 0,
        "terminated_processes": 0,
        "remaining_processes": 0,
        "process_timed_out": False,
    }


def _cleanup_int(cleanup: dict[str, JsonValue], key: str) -> int:
    value = cleanup.get(key, 0)
    return value if isinstance(value, int) else 0


def _sum_observable_counts(values: Iterable[int | None]) -> int | None:
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _credential_mode_error(mode: str) -> str:
    if mode not in ALLOWED_CREDENTIAL_MODES:
        return "credential_mode_unknown"
    return ""


def _harness_policy_error(options: EvalRunOptions) -> str:
    if options.harness_policy == "codex_native_v1" and options.harness != "codex":
        return "native_harness_must_be_codex"
    if options.harness_policy not in ("dual_v1", "codex_native_v1"):
        return "harness_policy_unknown"
    return ""


def _apply_harness_case_policy(
    options: EvalRunOptions,
    cases: tuple[SkillEvalCase, ...],
) -> tuple[tuple[SkillEvalCase, ...], str]:
    if options.harness_policy == "dual_v1":
        return tuple(case for case in cases if "codex-native-only" not in case.tags), ""
    safe_cases = tuple(case for case in cases if case.environment in {"LOCAL", "SIM"})
    if options.environment == "LIVE" or (cases and not safe_cases):
        return safe_cases, "native_live_environment_forbidden"
    return safe_cases, ""


def _sim_execution_required(
    cases: tuple[SkillEvalCase, ...],
    environment: str | None,
) -> bool:
    if environment == "SIM":
        return True
    return any(case.environment == "SIM" for case in cases)


def _planned_record(case: SkillEvalCase, harness: Harness) -> EvalRunRecord:
    grants = resolve_tool_grants(harness, case.exact_tool_grants[harness])
    return EvalRunRecord(
        case_id=case.id,
        harness=harness,
        status="planned",
        execution_mode="manifest_validation",
        expected_skill=case.expected_skill,
        required_logical_tools=case.required_logical_tools,
        forbidden_logical_tools=case.forbidden_logical_tools,
        resolved_tool_grants=grants,
        transcript_assertions_passed=True,
        no_model_call=True,
        no_mcp_call=True,
        no_saxo_call=True,
        grant_status="not_required",
        assertion_status="not_required",
    )


def _rewrite_digest_mismatches(
    records: tuple[EvalRunRecord, ...],
    expected_digest: str,
) -> tuple[EvalRunRecord, ...]:
    return tuple(
        record
        if record.router_source_sha256 == expected_digest
        else EvalRunRecord(
            case_id=record.case_id,
            harness=record.harness,
            status="failed",
            execution_mode=record.execution_mode,
            expected_skill=record.expected_skill,
            required_logical_tools=record.required_logical_tools,
            forbidden_logical_tools=record.forbidden_logical_tools,
            resolved_tool_grants=record.resolved_tool_grants,
            transcript_assertions_passed=False,
            no_model_call=record.no_model_call,
            no_mcp_call=record.no_mcp_call,
            no_saxo_call=record.no_saxo_call,
            model_output_observability=record.model_output_observability,
            error="router_source_digest_mismatch",
            router_decision=record.router_decision,
            router_source_mode=record.router_source_mode,
            router_source_sha256=record.router_source_sha256,
            model_tool_event_count=record.model_tool_event_count,
            model_command_event_count=record.model_command_event_count,
            model_mcp_event_count=record.model_mcp_event_count,
            model_saxo_event_count=record.model_saxo_event_count,
            client_version=record.client_version,
            invoked_logical_tools=record.invoked_logical_tools,
            invoked_logical_tool_count=record.invoked_logical_tool_count,
            grant_status=record.grant_status,
            assertion_status="failed",
        )
        for record in records
    )


def _installation_fixture_preserved(install_report: Path | None) -> bool:
    if install_report is None:
        return True
    install, _errors = load_install_report_for_consumers(install_report)
    if install is None:
        return False
    return all(Path(path).exists() for path in install.fixture_cleanup.preserved_paths)


def resolve_tool_grants(harness: Harness, logical_tools: Iterable[str]) -> tuple[str, ...]:
    tools = tuple(sorted(frozenset(logical_tools)))
    match harness:
        case "codex":
            return tuple(f"mcp__saxo_bank_mcp__{tool}" for tool in tools)
        case "claude":
            # Claude 2.x stdio MCP config uses server name saxo-bank-mcp.
            return tuple(f"mcp__saxo-bank-mcp__{tool}" for tool in tools)


def global_state_fingerprint(
    *,
    codex_home: Path | None,
    claude_home: Path | None,
) -> dict[str, JsonValue]:
    paths = tuple(path for path in (codex_home, claude_home) if path is not None)
    return {
        "path_count": len(paths),
        "paths": [
            {
                "path": str(path),
                "exists": path.exists(),
                "fingerprint": _path_fingerprint(path),
            }
            for path in paths
        ],
    }


def _path_fingerprint(path: Path) -> str:
    if not path.exists():
        return "missing"
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(str(path.stat().st_size).encode())
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for item in sorted(child for child in path.rglob("*") if child.is_file()):
        stat = item.stat()
        digest.update(str(item.relative_to(path)).encode())
        digest.update(str(stat.st_size).encode())
    return digest.hexdigest()
