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
    records = tuple(
        _run_case(
            case,
            selected,
            roots=roots,
            dry_run=options.dry_run,
        )
        for case in cases
        for selected in selected_harnesses(options.harness)
    )
    after = global_state_fingerprint(
        codex_home=options.codex_home,
        claude_home=options.claude_home,
    )
    skipped_count = (
        len(records) if not records else sum(1 for record in records if record.status == "skipped")
    )
    planned = bool(options.dry_run)
    failed = validation.status != "passed" or any(record.status == "failed" for record in records)
    skipped_failure = bool(options.nonzero_on_skip and (not records or skipped_count))
    status: Literal["passed", "failed", "skipped", "planned"] = (
        "failed" if failed or skipped_failure else "planned" if planned else "passed"
    )
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
            "created_mcp_calls": 0 if options.dry_run else None,
        },
        before_global_state=before,
        after_global_state=after,
        global_state_unchanged=before == after,
        skipped_count=skipped_count,
        nonzero_on_skip=options.nonzero_on_skip,
    )
    write_json(options.out, report.to_json_value())
    return 0 if status in {"passed", "planned"} else 1


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
