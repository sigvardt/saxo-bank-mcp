from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from saxo_bank_mcp.agent_skill_eval_models import (
    EXPECTED_SKILLS,
    LIVE_WRITE_TOOLS,
    RouterExpectation,
    SkillEvalCase,
    TranscriptAssertions,
    load_eval_cases,
    load_route_skills,
    load_scenario_tools,
)

RAW_ACCOUNT_PATTERN: Final = re.compile(r"\b\d{8,12}\b")
SECRET_PATTERN: Final = re.compile(
    r"(?i)(bearer\s+[a-z0-9._-]+|refresh[_ -]?token|access[_ -]?token|appsecret)",
)
HARNESS_PREFIX_PATTERN: Final = re.compile(r"^\s*[/$]saxo-bank-mcp:")
WILDCARD_PATTERN: Final = re.compile(r"[*?]|\bmcp__plugin_[^ \n]*[*]")
CONTROLLED_CLEANUP_TOOLS: Final = frozenset(
    {
        "saxo_create_streaming_price_subscription",
        "saxo_cleanup_streaming_subscriptions",
        "saxo_place_order",
        "saxo_modify_order",
        "saxo_cancel_order",
        "saxo_cancel_orders_by_instrument",
        "saxo_place_multileg_order",
        "saxo_modify_multileg_order",
        "saxo_cancel_multileg_order",
        "saxo_place_sim_order",
        "saxo_modify_sim_order",
        "saxo_cancel_sim_order",
        "saxo_cancel_sim_orders_by_instrument",
        "saxo_place_multileg_sim_order",
        "saxo_modify_multileg_sim_order",
        "saxo_cancel_multileg_sim_order",
    },
)


@dataclass(frozen=True)
class EvalValidationResult:
    status: Literal["passed", "failed"]
    case_count: int
    errors: tuple[str, ...]
    tool_count: int
    skill_count: int

    def line(self) -> str:
        return (
            f"status={self.status} cases={self.case_count} tools={self.tool_count} "
            f"skills={self.skill_count} errors={len(self.errors)}"
        )


def validate_eval_suite(
    *,
    root: Path = Path(),
    case_root: Path = Path("evals"),
) -> EvalValidationResult:
    errors: list[str] = []
    cases = _load_cases(case_root, errors)
    tools = _load_tools(root, errors)
    skills = _load_skills(root, errors)
    if cases and tools:
        errors.extend(_suite_errors(cases, tools, skills))
    status: Literal["passed", "failed"] = "failed" if errors else "passed"
    return EvalValidationResult(
        status=status,
        case_count=len(cases),
        errors=tuple(errors),
        tool_count=len(tools),
        skill_count=len(skills),
    )


def validate_cases(
    cases: Iterable[SkillEvalCase],
    *,
    known_tools: frozenset[str],
    known_skills: frozenset[str],
) -> tuple[str, ...]:
    case_tuple = tuple(cases)
    errors: list[str] = []
    ids = Counter(case.id for case in case_tuple)
    errors.extend(f"duplicate case id: {case_id}" for case_id, count in ids.items() if count > 1)
    for case in case_tuple:
        errors.extend(_case_errors(case, known_tools, known_skills))
    errors.extend(_coverage_errors(case_tuple, known_tools, known_skills))
    return tuple(errors)


def self_test_fixture_errors(fixture: str) -> tuple[str, ...]:
    base = _minimal_case()
    match fixture:
        case "missing-expected-skill":
            case = base.model_copy(update={"expected_skill": ""})
        case "missing-cleanup":
            case = base.model_copy(update={"cleanup_required": False})
        case "wildcard-grant":
            grants = {"codex": ("mcp__plugin_*",), "claude": ("saxo_health",)}
            case = base.model_copy(update={"exact_tool_grants": grants})
        case "unbounded-timeout":
            case = base.model_copy(update={"timeout_seconds": 0})
        case "stale-expected-tool":
            case = base.model_copy(update={"required_logical_tools": ("saxo_missing_tool",)})
        case "malformed-live-write":
            case = base.model_copy(
                update={
                    "id": "bad-live-write",
                    "environment": "LIVE",
                    "required_logical_tools": ("saxo_place_order",),
                    "exact_tool_grants": {
                        "codex": ("saxo_place_order",),
                        "claude": ("saxo_place_order",),
                    },
                },
            )
        case "harness-prefix-prompt":
            case = base.model_copy(update={"natural_prompt": "$saxo-bank-mcp:saxo-reads Plan."})
        case _:
            return (f"unknown fixture: {fixture}",)
    return validate_cases(
        (case,),
        known_tools=frozenset({"saxo_health"}),
        known_skills=frozenset({"saxo-bank"}),
    )


def _load_cases(case_root: Path, errors: list[str]) -> tuple[SkillEvalCase, ...]:
    try:
        return load_eval_cases(case_root)
    except ValueError as exc:
        errors.append(str(exc))
        return ()


def _load_tools(root: Path, errors: list[str]) -> frozenset[str]:
    try:
        return load_scenario_tools(root)
    except ValueError as exc:
        errors.append(str(exc))
        return frozenset()


def _load_skills(root: Path, errors: list[str]) -> frozenset[str]:
    try:
        return load_route_skills(root) | EXPECTED_SKILLS
    except ValueError as exc:
        errors.append(str(exc))
        return EXPECTED_SKILLS


def _suite_errors(
    cases: tuple[SkillEvalCase, ...],
    known_tools: frozenset[str],
    known_skills: frozenset[str],
) -> tuple[str, ...]:
    return validate_cases(cases, known_tools=known_tools, known_skills=known_skills)


def _case_errors(
    case: SkillEvalCase,
    known_tools: frozenset[str],
    known_skills: frozenset[str],
) -> tuple[str, ...]:
    errors: list[str] = []
    if case.expected_skill not in known_skills:
        errors.append(f"{case.id}: missing expected skill")
    errors.extend(_prompt_errors(case))
    errors.extend(_cleanup_errors(case))
    errors.extend(_router_errors(case))
    group_tools = frozenset(tool for group in case.required_tool_groups for tool in group)
    unknown_required = frozenset(case.required_logical_tools) - known_tools
    unknown_groups = group_tools - known_tools
    unknown_forbidden = frozenset(case.forbidden_logical_tools) - known_tools
    unknown_grants = _grants(case) - known_tools
    errors.extend(
        f"{case.id}: stale expected tool {tool}"
        for tool in sorted(unknown_required | unknown_groups | unknown_forbidden | unknown_grants)
    )
    errors.extend(_live_errors(case))
    if _grants(case).intersection(case.forbidden_logical_tools):
        errors.append(f"{case.id}: forbidden tool granted")
    missing_grants = (frozenset(case.required_logical_tools) | group_tools) - _grants(case)
    errors.extend(f"{case.id}: required tool not granted {tool}" for tool in sorted(missing_grants))
    errors.extend(
        f"{case.id}: wildcard grant"
        for grant in sorted(_grants(case))
        if WILDCARD_PATTERN.search(grant)
    )
    return tuple(errors)


def _router_errors(case: SkillEvalCase) -> tuple[str, ...]:
    is_router_proof = "router-proof" in case.tags
    expectation = case.router_expectation
    if not is_router_proof:
        return (f"{case.id}: router expectation requires router-proof tag",) if expectation else ()
    if expectation is None:
        return (f"{case.id}: missing router expectation",)
    return _router_expectation_errors(case, expectation)


def _router_expectation_errors(
    case: SkillEvalCase,
    expectation: RouterExpectation,
) -> tuple[str, ...]:
    errors: list[str] = []
    if case.required_logical_tools or _grants(case):
        errors.append(f"{case.id}: router proof must grant no tools")
    if case.cleanup_required:
        errors.append(f"{case.id}: plan-only router proof cannot require cleanup")
    if expectation.primary_skill is None:
        if case.expected_skill != "saxo-bank":
            errors.append(f"{case.id}: stopped router case must expect saxo-bank")
    elif case.expected_skill != expectation.primary_skill:
        errors.append(f"{case.id}: expected skill differs from primary route")
    if expectation.primary_skill in expectation.follow_on_skills:
        errors.append(f"{case.id}: primary route repeated as follow-on")
    if len(expectation.follow_on_skills) != len(frozenset(expectation.follow_on_skills)):
        errors.append(f"{case.id}: duplicate follow-on route")
    prompt = case.natural_prompt.lower()
    if not all(term in prompt for term in ("do not run commands", "tool", "mcp", "saxo endpoint")):
        errors.append(f"{case.id}: router proof does not explicitly forbid execution")
    return tuple(errors)


def _prompt_errors(case: SkillEvalCase) -> tuple[str, ...]:
    errors: list[str] = []
    if HARNESS_PREFIX_PATTERN.search(case.natural_prompt):
        errors.append(f"{case.id}: harness prefix inside natural prompt")
    if SECRET_PATTERN.search(case.natural_prompt) or RAW_ACCOUNT_PATTERN.search(
        case.natural_prompt,
    ):
        errors.append(f"{case.id}: prompt contains secret or raw account ID")
    return tuple(errors)


def _cleanup_errors(case: SkillEvalCase) -> tuple[str, ...]:
    return (
        (f"{case.id}: missing cleanup",)
        if _needs_cleanup(case) and not case.cleanup_required
        else ()
    )


def _live_errors(case: SkillEvalCase) -> tuple[str, ...]:
    if case.environment != "LIVE":
        return ()
    errors: list[str] = []
    if _grants(case).intersection(LIVE_WRITE_TOOLS):
        errors.append(f"{case.id}: LIVE write tool granted")
    if frozenset(case.required_logical_tools).intersection(LIVE_WRITE_TOOLS):
        errors.append(f"{case.id}: LIVE write tool required")
    return tuple(errors)


def _coverage_errors(
    cases: tuple[SkillEvalCase, ...],
    known_tools: frozenset[str],
    known_skills: frozenset[str],
) -> tuple[str, ...]:
    used_tools = frozenset(
        tool
        for case in cases
        for tool in (
            *case.required_logical_tools,
            *case.forbidden_logical_tools,
            *(member for group in case.required_tool_groups for member in group),
            *case.exact_tool_grants.get("codex", ()),
            *case.exact_tool_grants.get("claude", ()),
        )
    )
    expected_skills = EXPECTED_SKILLS & known_skills
    positive_skills = frozenset(case.expected_skill for case in cases)
    errors = [f"missing tool coverage: {tool}" for tool in sorted(known_tools - used_tools)]
    errors.extend(
        f"missing positive skill trigger: {skill}"
        for skill in sorted(expected_skills - positive_skills)
    )
    errors.extend(
        f"missing negative/forbidden behavior: {skill}"
        for skill in sorted(expected_skills)
        if not any(case.expected_skill == skill and case.forbidden_logical_tools for case in cases)
    )
    return tuple(errors)


def _needs_cleanup(case: SkillEvalCase) -> bool:
    return bool(frozenset(case.required_logical_tools).intersection(CONTROLLED_CLEANUP_TOOLS))


def _grants(case: SkillEvalCase) -> frozenset[str]:
    return frozenset(tool for grants in case.exact_tool_grants.values() for tool in grants)


def _minimal_case() -> SkillEvalCase:
    return SkillEvalCase(
        id="fixture",
        title="fixture",
        tags=("fixture",),
        environment="LOCAL",
        natural_prompt="Check Saxo MCP health without executing a trade.",
        harness_prompts={
            "codex": "Check Saxo MCP health without executing a trade.",
            "claude": "Check Saxo MCP health without executing a trade.",
        },
        expected_skill="saxo-bank",
        required_logical_tools=("saxo_health",),
        forbidden_logical_tools=(),
        exact_tool_grants={"codex": ("saxo_health",), "claude": ("saxo_health",)},
        transcript_assertions=TranscriptAssertions(required_all=("saxo_health",)),
        deterministic_graders=("contains-required-phrases",),
        max_turns=3,
        timeout_seconds=60,
        cleanup_required=False,
    )
