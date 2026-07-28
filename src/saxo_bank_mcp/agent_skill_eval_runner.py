from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
from saxo_bank_mcp.agent_skill_router_eval_execution import (
    RouterBindingRequest,
    RouterSourceBinding,
    client_versions,
    resolve_router_source_binding,
)


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
    credential_mode: str = "none"


def run_eval_suite(
    options: EvalRunOptions,
) -> int:
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
    binding: RouterSourceBinding | None = None
    binding_error = ""
    needs_binding = (not options.dry_run) and any(
        case.router_expectation is not None for case in cases
    )
    if needs_binding:
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

    records: tuple[EvalRunRecord, ...]
    if binding_error:
        records = ()
    else:
        records = tuple(
            _run_case(
                case,
                selected,
                roots=roots,
                dry_run=options.dry_run,
                expected_router_source_sha256=(
                    None if binding is None else binding.router_source_sha256
                ),
            )
            for case in cases
            for selected in selected_harnesses(options.harness)
        )
    if (
        binding is not None
        and not options.dry_run
        and any(
            record.router_source_mode == "source_equivalent"
            and record.router_source_sha256 != binding.router_source_sha256
            for record in records
        )
    ):
        binding_error = "record_router_source_digest_mismatch"
        records = tuple(
            record
            if record.router_source_sha256 == binding.router_source_sha256
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
    after = global_state_fingerprint(
        codex_home=options.codex_home,
        claude_home=options.claude_home,
    )
    skipped_count = (
        len(records) if not records else sum(1 for record in records if record.status == "skipped")
    )
    empty_selection = not cases or not records
    planned = bool(options.dry_run) and not empty_selection and not binding_error
    failed = (
        validation.status != "passed"
        or bool(binding_error)
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
    versions = (
        {}
        if options.dry_run or binding_error
        else client_versions(
            codex_home=options.codex_home,
            claude_home=options.claude_home,
        )
    )
    installation_fixture_preserved = _installation_fixture_preserved(options.install_report)
    source_commit = (
        ""
        if binding is None
        else binding.source_commit
    )
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
            "complete": True,
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
            "client_versions": versions,
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
                    "expected_router_source_sha256": (
                        options.expected_router_source_sha256 or ""
                    ),
                }
                if binding_error
                else {
                    "status": "passed" if binding is not None else "not_required",
                    "source_commit": "" if binding is None else binding.source_commit,
                    "router_source_sha256": (
                        "" if binding is None else binding.router_source_sha256
                    ),
                    "file_digests": {} if binding is None else binding.file_digests,
                    "codex_plugin_root": (
                        "" if binding is None else binding.codex_plugin_root
                    ),
                    "claude_plugin_root": (
                        "" if binding is None else binding.claude_plugin_root
                    ),
                }
            ),
            "credential_mode": options.credential_mode,
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
    payload["run_cleanup"] = {"complete": True}
    payload["installation_fixture_preserved"] = installation_fixture_preserved
    write_json(options.out, payload)
    if not installation_fixture_preserved and options.install_report is not None:
        return 1
    return 0 if status in {"passed", "planned"} else 1


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


def _run_case(
    case: SkillEvalCase,
    harness: Harness,
    *,
    roots: HarnessRoots,
    dry_run: bool,
    expected_router_source_sha256: str | None,
) -> EvalRunRecord:
    grants = resolve_tool_grants(harness, case.exact_tool_grants[harness])
    if dry_run:
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
    return execute_model_case(
        case,
        harness,
        grants,
        roots=roots,
        expected_router_source_sha256=expected_router_source_sha256,
    )


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
