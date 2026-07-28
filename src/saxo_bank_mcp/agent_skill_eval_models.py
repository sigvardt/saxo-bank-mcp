from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue

type Harness = Literal["codex", "claude"]
type HarnessSelector = Literal["codex", "claude", "both"]
type EvalEnvironment = Literal["LOCAL", "SIM", "LIVE"]
type RouterEnvironment = Literal["LOCAL", "SIM", "LIVE", "AMBIGUOUS"]
type RouterIntent = Literal["auth", "read", "stream", "trade", "recovery", "QA", "unsupported"]
type RouterMutationRisk = Literal[
    "none",
    "local-state",
    "SIM mutation",
    "LIVE read/precheck",
    "LIVE mutation",
    "ambiguous",
]
type RouterEvidenceNeed = Literal[
    "plan-only",
    "normal execution receipt",
    "readback",
    "cleanup proof",
    "request-ledger proof",
    "privacy evidence",
    "release/QA evidence",
]
type FocusedSkill = Literal[
    "saxo-auth-session",
    "saxo-openapi",
    "saxo-reads",
    "saxo-streaming",
    "saxo-trading",
    "saxo-safety-recovery",
    "saxo-qa-operations",
]

CASE_FILE_NAME: Final = "case.yaml"
DEFAULT_CASE_ROOT: Final = Path("evals/saxo-bank")
SCENARIO_SOURCE: Final = Path("data/saxo/agent_tool_scenarios.json")
ROUTE_SOURCE: Final = Path("data/saxo/agent_tool_routes.json")
EXPECTED_SKILLS: Final = frozenset(
    {
        "saxo-bank",
        "saxo-auth-session",
        "saxo-openapi",
        "saxo-reads",
        "saxo-streaming",
        "saxo-trading",
        "saxo-safety-recovery",
        "saxo-qa-operations",
    },
)
LIVE_WRITE_TOOLS: Final = frozenset(
    {
        "saxo_place_order",
        "saxo_modify_order",
        "saxo_cancel_order",
        "saxo_cancel_orders_by_instrument",
        "saxo_place_multileg_order",
        "saxo_modify_multileg_order",
        "saxo_cancel_multileg_order",
        "saxo_execute_trading_write",
        "saxo_register_disclaimer_response",
    },
)


class TranscriptAssertions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_all: tuple[str, ...] = ()
    required_any: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()


class RouterExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    environment: RouterEnvironment
    intent: RouterIntent
    mutation_risk: RouterMutationRisk
    evidence_need: RouterEvidenceNeed
    primary_skill: FocusedSkill | None
    follow_on_skills: tuple[FocusedSkill, ...]
    requires_environment_clarification: bool
    approval_bypass_refused: bool
    trade_choice_refused: bool


class RouterDecision(RouterExpectation):
    execution_allowed: bool


class SkillEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str = Field(min_length=1)
    tags: tuple[str, ...] = Field(min_length=1)
    environment: EvalEnvironment
    natural_prompt: str = Field(min_length=1)
    harness_prompts: dict[Harness, str]
    expected_skill: str = Field(min_length=1)
    required_logical_tools: tuple[str, ...]
    required_tool_groups: tuple[tuple[str, ...], ...] = ()
    forbidden_logical_tools: tuple[str, ...]
    exact_tool_grants: dict[Harness, tuple[str, ...]]
    transcript_assertions: TranscriptAssertions
    deterministic_graders: tuple[str, ...] = Field(min_length=1)
    max_turns: int = Field(gt=0, le=20)
    timeout_seconds: int = Field(gt=0, le=900)
    cleanup_required: bool
    router_expectation: RouterExpectation | None = None


class EvalRunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    harness: Harness
    status: Literal["passed", "failed", "skipped", "planned"]
    execution_mode: Literal["manifest_validation", "model_execution"]
    expected_skill: str
    required_logical_tools: tuple[str, ...]
    forbidden_logical_tools: tuple[str, ...]
    resolved_tool_grants: tuple[str, ...]
    transcript_assertions_passed: bool
    no_model_call: bool
    no_mcp_call: bool
    no_saxo_call: bool
    error: str = ""
    router_decision: RouterDecision | None = None
    router_source_mode: Literal["source_equivalent"] | None = None
    router_source_sha256: str = ""
    model_tool_event_count: int | None = None
    model_command_event_count: int | None = None
    model_mcp_event_count: int | None = None
    model_saxo_event_count: int | None = None
    client_version: str = ""
    invoked_logical_tools: tuple[str, ...] = ()
    invoked_logical_tool_count: int = 0
    grant_status: Literal["passed", "failed", "not_required"] = "not_required"
    assertion_status: Literal["passed", "failed", "not_required"] = "not_required"


class EvalRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed", "failed", "skipped", "planned"]
    harness: HarnessSelector
    environment: str
    execution_mode: Literal["manifest_validation", "model_execution"]
    selected_case_count: int
    case_count: int
    records: tuple[EvalRunRecord, ...]
    cleanup: dict[str, JsonValue]
    before_global_state: dict[str, JsonValue]
    after_global_state: dict[str, JsonValue]
    global_state_unchanged: bool
    skipped_count: int
    nonzero_on_skip: bool
    source_commit: str = ""
    router_source_sha256: str = ""
    router_source_file_digests: dict[str, str] = {}

    def to_json_value(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


class EvalCaseLoadError(ValueError):
    pass


CASE_ADAPTER: Final[TypeAdapter[SkillEvalCase]] = TypeAdapter(SkillEvalCase)
JSON_OBJECT_ADAPTER: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(dict[str, JsonValue])


def load_json_object(path: Path) -> dict[str, JsonValue]:
    try:
        return JSON_OBJECT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError) as exc:
        raise EvalCaseLoadError(path, f"invalid_json: {type(exc).__name__}") from exc


def load_eval_cases(root: Path = DEFAULT_CASE_ROOT) -> tuple[SkillEvalCase, ...]:
    case_files = tuple(sorted(root.glob(f"*/{CASE_FILE_NAME}")))
    if not case_files:
        raise EvalCaseLoadError(root, "missing eval case files")
    return tuple(_load_case(path) for path in case_files)


def load_scenario_tools(root: Path = Path()) -> frozenset[str]:
    payload = load_json_object(root / SCENARIO_SOURCE)
    raw_scenarios = payload.get("scenarios")
    if not isinstance(raw_scenarios, Sequence):
        raise EvalCaseLoadError(root / SCENARIO_SOURCE, "missing scenarios")
    tools: set[str] = set()
    for item in raw_scenarios:
        if isinstance(item, Mapping):
            tool = item.get("tool")
            if isinstance(tool, str):
                tools.add(tool)
    return frozenset(tools)


def load_route_skills(root: Path = Path()) -> frozenset[str]:
    payload = load_json_object(root / ROUTE_SOURCE)
    raw_routes = payload.get("tool_routes")
    if not isinstance(raw_routes, Sequence):
        raise EvalCaseLoadError(root / ROUTE_SOURCE, "missing tool_routes")
    skills = {
        str(item["owning_skill"])
        for item in raw_routes
        if isinstance(item, Mapping) and isinstance(item.get("owning_skill"), str)
    }
    return frozenset(skills) | {"saxo-qa-operations"}


def select_cases(
    cases: Iterable[SkillEvalCase],
    *,
    case_id: str | None,
    tag: str | None,
    environment: str | None,
) -> tuple[SkillEvalCase, ...]:
    selected: list[SkillEvalCase] = []
    for case in cases:
        if case_id is not None and case.id != case_id:
            continue
        if tag is not None and tag not in case.tags:
            continue
        if environment is not None and case.environment != environment:
            continue
        selected.append(case)
    return tuple(selected)


def selected_harnesses(selector: HarnessSelector) -> tuple[Harness, ...]:
    match selector:
        case "codex":
            return ("codex",)
        case "claude":
            return ("claude",)
        case "both":
            return ("codex", "claude")


def _load_case(path: Path) -> SkillEvalCase:
    try:
        case = CASE_ADAPTER.validate_python(load_json_object(path))
    except ValidationError as exc:
        raise EvalCaseLoadError(path, f"invalid_case: {exc.errors()[0]['type']}") from exc
    if case.harness_prompts.get("codex") != case.natural_prompt:
        raise EvalCaseLoadError(path, "codex prompt differs from natural prompt")
    if case.harness_prompts.get("claude") != case.natural_prompt:
        raise EvalCaseLoadError(path, "claude prompt differs from natural prompt")
    return case
