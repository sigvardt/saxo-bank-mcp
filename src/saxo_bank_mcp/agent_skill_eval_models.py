from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_eval_tool_protocol import NonSaxoEventDescriptor

type Harness = Literal["codex", "claude"]
type HarnessSelector = Literal["codex", "claude", "both"]
type EvalEnvironment = Literal["LOCAL", "SIM", "LIVE"]
type ModelOutputObservability = Literal["observable", "unknown"]
type RouterEnvironment = Literal["LOCAL", "SIM", "LIVE", "AMBIGUOUS"]
type RouterIntent = Literal[
    "analytics", "auth", "read", "stream", "trade", "recovery", "QA", "unsupported"
]
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
    "saxo-analytics",
    "saxo-auth-session",
    "saxo-openapi",
    "saxo-reads",
    "saxo-streaming",
    "saxo-trading",
    "saxo-safety-recovery",
    "saxo-qa-operations",
]

CASE_FILE_NAME: Final = "case.yaml"
DEFAULT_CASE_ROOT: Final = Path("evals")
SCENARIO_SOURCE: Final = Path("data/saxo/agent_tool_scenarios.json")
ROUTE_SOURCE: Final = Path("data/saxo/agent_tool_routes.json")
EXPECTED_SKILLS: Final = frozenset(
    {
        "saxo-analytics",
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
SHA256_HEX: Final = r"^[a-f0-9]{64}$"


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
    transcript_assertions_passed: bool | None
    no_model_call: bool
    no_mcp_call: bool | None
    no_saxo_call: bool | None
    model_output_observability: ModelOutputObservability = "observable"
    error: str = ""
    router_decision: RouterDecision | None = None
    router_source_mode: Literal["source_equivalent"] | None = None
    router_source_sha256: str = ""
    model_tool_event_count: int | None = None
    model_command_event_count: int | None = None
    model_mcp_event_count: int | None = None
    model_saxo_event_count: int | None = None
    non_saxo_event_descriptors: tuple[NonSaxoEventDescriptor, ...] | None = Field(
        default=None,
        max_length=128,
    )
    plugin_list_exit_code: int | None = None
    plugin_list_stdout_schema_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    mcp_probe_stage: (
        Literal[
            "runtime_binding",
            "contract_validation",
            "command_start",
            "command_exit",
            "payload_parse",
            "tool_visibility",
            "complete",
        ]
        | None
    ) = None
    mcp_probe_exit_code: int | None = None
    mcp_probe_stdout_schema_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    mcp_config_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    mcp_config_path_identity_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    client_version: str = ""
    invoked_logical_tools: tuple[str, ...] | None = ()
    invoked_logical_tool_count: int | None = 0
    grant_status: Literal["passed", "failed", "not_required", "unknown"] = "not_required"
    assertion_status: Literal["passed", "failed", "not_required", "unknown"] = "not_required"
    assistant_message_present: bool | None = None
    required_all_assertion_results: tuple[bool, ...] = Field(default=(), max_length=64)
    required_any_assertion_results: tuple[bool, ...] = Field(default=(), max_length=64)
    forbidden_assertion_absent_results: tuple[bool, ...] = Field(default=(), max_length=64)
    raw_assistant_event_count: int | None = Field(default=None, ge=0, le=1024)
    raw_assistant_events_sha256: str | None = Field(default=None, pattern=SHA256_HEX)
    final_assistant_text_sha256: str | None = Field(default=None, pattern=SHA256_HEX)
    raw_assistant_message_present: bool | None = None
    raw_assistant_required_all_assertion_results: tuple[bool, ...] = Field(
        default=(),
        max_length=64,
    )
    raw_assistant_required_any_assertion_results: tuple[bool, ...] = Field(
        default=(),
        max_length=64,
    )
    raw_assistant_forbidden_assertion_absent_results: tuple[bool, ...] = Field(
        default=(),
        max_length=64,
    )

    @model_validator(mode="after")
    def _validate_mcp_probe_evidence(self) -> EvalRunRecord:  # noqa: C901
        self._validate_model_output_observability()
        if (
            self.non_saxo_event_descriptors is not None
            and self.model_mcp_event_count is not None
            and len(self.non_saxo_event_descriptors) > self.model_mcp_event_count
        ):
            raise ValueError("non-Saxo event descriptors exceed MCP event count")
        paired = (self.mcp_probe_exit_code is None) == (self.mcp_probe_stdout_schema_sha256 is None)
        completed_command = self.mcp_probe_stage in {
            "command_exit",
            "payload_parse",
            "tool_visibility",
            "complete",
        }
        if not paired or (completed_command != (self.mcp_probe_exit_code is not None)):
            raise ValueError("mcp probe command evidence must match its stage")
        if self.mcp_probe_stage is None and self.mcp_probe_exit_code is not None:
            raise ValueError("mcp probe command evidence requires a stage")
        if self.mcp_probe_stage == "command_exit" and self.mcp_probe_exit_code == 0:
            raise ValueError("failed mcp probe command must have a nonzero exit")
        if self.mcp_probe_stage in {"payload_parse", "tool_visibility", "complete"} and (
            self.mcp_probe_exit_code != 0
        ):
            raise ValueError("completed mcp probe command must have exit zero")
        config_paired = (self.mcp_config_sha256 is None) == (
            self.mcp_config_path_identity_sha256 is None
        )
        if not config_paired:
            raise ValueError("mcp config binding evidence must be complete")
        if self.assistant_message_present is None and any(
            (
                self.required_all_assertion_results,
                self.required_any_assertion_results,
                self.forbidden_assertion_absent_results,
            )
        ):
            raise ValueError("assertion outcomes require assistant-message evidence")
        raw_digests = (self.raw_assistant_events_sha256, self.final_assistant_text_sha256)
        if (self.raw_assistant_event_count is None) != any(item is None for item in raw_digests):
            raise ValueError("assistant diagnostic counts and hashes must be complete")
        if self.raw_assistant_message_present is None and any(
            (
                self.raw_assistant_required_all_assertion_results,
                self.raw_assistant_required_any_assertion_results,
                self.raw_assistant_forbidden_assertion_absent_results,
            )
        ):
            raise ValueError("raw assertion outcomes require assistant-event evidence")
        if (
            self.raw_assistant_event_count is None
            and self.raw_assistant_message_present is not None
        ):
            raise ValueError("raw assistant evidence requires diagnostic hashes")
        if (
            self.harness == "codex"
            and self.error == "malformed_output"
            and (
                any(
                    value is not None
                    for value in (
                        self.assistant_message_present,
                        self.raw_assistant_event_count,
                        self.raw_assistant_events_sha256,
                        self.final_assistant_text_sha256,
                        self.raw_assistant_message_present,
                    )
                )
                or any(
                    (
                        self.required_all_assertion_results,
                        self.required_any_assertion_results,
                        self.forbidden_assertion_absent_results,
                        self.raw_assistant_required_all_assertion_results,
                        self.raw_assistant_required_any_assertion_results,
                        self.raw_assistant_forbidden_assertion_absent_results,
                    )
                )
            )
        ):
            raise ValueError("malformed Codex output cannot carry assistant diagnostics")
        return self

    def _validate_model_output_observability(self) -> None:
        parse_derived_fields = (
            self.transcript_assertions_passed,
            self.no_mcp_call,
            self.no_saxo_call,
            self.router_decision,
            self.model_tool_event_count,
            self.model_command_event_count,
            self.model_mcp_event_count,
            self.model_saxo_event_count,
            self.invoked_logical_tools,
            self.invoked_logical_tool_count,
            self.non_saxo_event_descriptors,
        )
        if self.model_output_observability == "unknown":
            if any(value is not None for value in parse_derived_fields):
                raise ValueError("unknown model output cannot carry parse-derived evidence")
            if self.grant_status != "unknown" or self.assertion_status != "unknown":
                raise ValueError("unknown model output requires unknown grading evidence")
            if (
                self.status != "failed"
                or self.execution_mode != "model_execution"
                or self.no_model_call
                or not self.error
            ):
                raise ValueError("unknown model output requires a post-launch failure")
            assistant_derived_fields = (
                self.assistant_message_present,
                self.raw_assistant_event_count,
                self.raw_assistant_events_sha256,
                self.final_assistant_text_sha256,
                self.raw_assistant_message_present,
            )
            assertion_vectors = (
                self.required_all_assertion_results,
                self.required_any_assertion_results,
                self.forbidden_assertion_absent_results,
                self.raw_assistant_required_all_assertion_results,
                self.raw_assistant_required_any_assertion_results,
                self.raw_assistant_forbidden_assertion_absent_results,
            )
            if any(value is not None for value in assistant_derived_fields) or any(
                assertion_vectors
            ):
                raise ValueError("unknown model output cannot carry assertion evidence")
        else:
            if (
                self.transcript_assertions_passed is None
                or self.no_mcp_call is None
                or self.no_saxo_call is None
                or self.invoked_logical_tools is None
                or self.invoked_logical_tool_count is None
            ):
                raise ValueError("observable model output requires call and tool evidence")
            if self.invoked_logical_tool_count != len(self.invoked_logical_tools):
                raise ValueError("invoked logical tool count differs")
            if self.grant_status == "unknown" or self.assertion_status == "unknown":
                raise ValueError("observable model output cannot carry unknown grading evidence")
        if self.error == "malformed_output" and self.model_output_observability != "unknown":
            raise ValueError("malformed output requires unknown parse evidence")


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
    if root.name == "evals":
        case_files = tuple(sorted(root.glob(f"*/*/{CASE_FILE_NAME}")))
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
