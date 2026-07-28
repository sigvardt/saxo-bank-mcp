from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from saxo_bank_mcp._evidence import JsonValue, write_json
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
from saxo_bank_mcp.agent_skill_eval_validation import validate_eval_suite
from saxo_bank_mcp.agent_skill_install_qa import load_install_report_for_consumers
from saxo_bank_mcp.agent_skill_matrix_env import (
    MatrixEnvError,
    cleanup_matrix_isolated_runtime,
    prepare_eval_isolated_runtime,
    promote_rotated_sim_token_cache,
    require_matrix_runtime_cleanup,
)
from saxo_bank_mcp.agent_skill_router_eval_execution import (
    RouterBindingRequest,
    RouterSourceBinding,
    client_versions,
    resolve_router_source_binding,
)

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


@dataclass(frozen=True, slots=True)
class _RuntimeOutcome:
    records: tuple[EvalRunRecord, ...]
    enforced_mode: str
    versions: dict[str, str]
    cleanup: dict[str, JsonValue]
    error: str


def run_eval_suite(options: EvalRunOptions) -> int:
    validation = validate_eval_suite(case_root=options.case_root)
    cases = select_cases(
        load_eval_cases(options.case_root),
        case_id=options.case_id,
        tag=options.tag,
        environment=options.environment,
    )
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

    mode_error = _credential_mode_error(options.credential_mode)
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
    )
    skipped_failure = bool(options.nonzero_on_skip and skipped_count)
    status: Literal["passed", "failed", "skipped", "planned"] = (
        "failed" if failed or skipped_failure else "planned" if planned else "passed"
    )
    router_records = tuple(
        record for record in records if record.router_source_mode == "source_equivalent"
    )
    installation_fixture_preserved = _installation_fixture_preserved(options.install_report)
    source_commit = "" if binding is None else binding.source_commit
    if not source_commit and options.expected_source_commit:
        source_commit = options.expected_source_commit
    report = EvalRunReport(
        status=status,
        harness=options.harness,
        environment=options.environment or "ALL",
        execution_mode="manifest_validation" if options.dry_run else "model_execution",
        selected_case_count=len(cases),
        case_count=len(records),
        records=records,
        cleanup={
            "complete": bool(outcome.cleanup.get("complete", True)),
            "created_processes": 0,
            "remaining_processes": 0,
            "raw_transcripts_persisted": 0,
            "model_prompt_count": len(router_records),
            "model_prompt_counts": {
                harness: sum(1 for record in router_records if record.harness == harness)
                for harness in ("codex", "claude")
            },
            "model_tool_events": sum(
                record.model_tool_event_count or 0 for record in router_records
            ),
            "model_command_events": sum(
                record.model_command_event_count or 0 for record in router_records
            ),
            "created_mcp_calls": (
                0
                if options.dry_run
                else sum(record.model_mcp_event_count or 0 for record in router_records)
            ),
            "model_saxo_events": sum(
                record.model_saxo_event_count or 0 for record in router_records
            ),
            "client_versions": outcome.versions,
            "client_versions_from_records": {
                harness: sorted(
                    {
                        record.client_version
                        for record in router_records
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
        "runtime_cleanup": outcome.cleanup.get("runtime_cleanup", "not_required"),
        "token_promote": outcome.cleanup.get("token_promote", "not_required"),
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
        cleanup={
            "complete": True,
            "runtime_cleanup": "not_required",
            "token_promote": "not_required",
        },
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
    evidence_root = options.out.parent.resolve()
    promote_error: MatrixEnvError | None = None
    cleanup_error: MatrixEnvError | None = None
    records: tuple[EvalRunRecord, ...] = ()
    versions: dict[str, str] = {}
    try:
        runtime = prepare_eval_isolated_runtime(
            evidence_root,
            source_codex_home=options.source_codex_home or options.codex_home,
            source_claude_home=options.source_claude_home or options.claude_home,
        )
    except MatrixEnvError as exc:
        return _RuntimeOutcome(
            records=(),
            enforced_mode=CREDENTIAL_MODE_NONE,
            versions={},
            cleanup={
                "complete": True,
                "runtime_cleanup": "not_required",
                "token_promote": "not_required",
            },
            error=exc.reason,
        )

    try:
        versions = client_versions(env=runtime.env)
        child_roots = HarnessRoots(
            codex_plugin_root=roots.codex_plugin_root,
            claude_plugin_root=roots.claude_plugin_root,
            codex_home=runtime.codex_home,
            claude_home=runtime.home,
        )
        records = tuple(
            execute_model_case(
                case,
                harness,
                resolve_tool_grants(harness, case.exact_tool_grants[harness]),
                roots=child_roots,
                env=runtime.env,
                expected_router_source_sha256=(
                    None if binding is None else binding.router_source_sha256
                ),
            )
            for case in cases
            for harness in selected_harnesses(options.harness)
        )
    finally:
        try:
            promote_rotated_sim_token_cache(runtime)
        except MatrixEnvError as exc:
            promote_error = exc
        try:
            require_matrix_runtime_cleanup(runtime.run_root)
        except MatrixEnvError as exc:
            cleanup_error = exc
            cleanup_matrix_isolated_runtime(runtime.run_root)

    if promote_error is not None or cleanup_error is not None:
        primary = promote_error.reason if promote_error is not None else None
        if cleanup_error is not None and primary is not None:
            reason = f"{primary}+{cleanup_error.reason}"
        elif cleanup_error is not None:
            reason = cleanup_error.reason
        else:
            reason = primary or "eval_runtime_failed"
        return _RuntimeOutcome(
            records=(),
            enforced_mode=CREDENTIAL_MODE_NONE,
            versions={},
            cleanup={
                "complete": cleanup_error is None,
                "runtime_cleanup": "residue" if cleanup_error is not None else "passed",
                "token_promote": "failed" if promote_error is not None else "passed",
            },
            error=reason,
        )
    return _RuntimeOutcome(
        records=records,
        enforced_mode=CREDENTIAL_MODE_EPHEMERAL,
        versions=versions,
        cleanup={
            "complete": True,
            "runtime_cleanup": "passed",
            "token_promote": "passed",
        },
        error="",
    )


def _credential_mode_error(mode: str) -> str:
    if mode not in ALLOWED_CREDENTIAL_MODES:
        return "credential_mode_unknown"
    return ""


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
            error="router_source_digest_mismatch",
            router_decision=record.router_decision,
            router_source_mode=record.router_source_mode,
            router_source_sha256=record.router_source_sha256,
            model_tool_event_count=record.model_tool_event_count,
            model_command_event_count=record.model_command_event_count,
            model_mcp_event_count=record.model_mcp_event_count,
            model_saxo_event_count=record.model_saxo_event_count,
            client_version=record.client_version,
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
            return tuple(f"mcp__plugin_saxo_bank_mcp_saxo_bank_mcp__{tool}" for tool in tools)


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
