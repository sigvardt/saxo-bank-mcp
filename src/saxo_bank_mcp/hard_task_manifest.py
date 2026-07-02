from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from saxo_bank_mcp._evidence import JsonValue, now_utc, write_json
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.loop_manifest import GitState, current_git_state
from saxo_bank_mcp.order_mutation_models import ORDER_WRITE_SPECS, OrderWriteClass, OrderWriteSpec

type EndpointClass = Literal["sim_demo", "safe_no_secret_fixture"]
type HardTaskRiskClass = Literal[
    "trade_precheck",
    "trade_lookup",
    "disclaimer_write",
    "money_moving",
    "streaming",
]

DEFAULT_INCOMPLETE_TOOL_IDS: Final[tuple[str, ...]] = (
    "saxo_cancel_multileg_sim_order",
    "saxo_cancel_sim_order",
    "saxo_cancel_sim_orders_by_instrument",
    "saxo_cleanup_streaming_subscriptions",
    "saxo_create_order_preview",
    "saxo_create_streaming_price_subscription",
    "saxo_get_multileg_order_defaults",
    "saxo_get_required_disclaimers",
    "saxo_modify_multileg_sim_order",
    "saxo_modify_sim_order",
    "saxo_place_multileg_sim_order",
    "saxo_place_sim_order",
    "saxo_register_disclaimer_response",
)
RISK_CLASSES_REQUIRING_APPROVAL: Final = frozenset({"disclaimer_write", "money_moving"})
AUTH_MISSING_STATUSES: Final = frozenset({"auth_required", "incomplete_auth_required"})


class HardTaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: str = Field(min_length=1, pattern=r"^saxo_")
    fastmcp_tool_name: str = Field(min_length=1, pattern=r"^saxo_")
    qa_command: tuple[str, ...] = Field(min_length=1)
    endpoint_class: EndpointClass
    risk_class: HardTaskRiskClass
    setup_steps: tuple[str, ...] = Field(min_length=1)
    input_constraints: tuple[str, ...] = Field(min_length=1)
    success_oracle: tuple[str, ...] = Field(min_length=1)
    cleanup_steps: tuple[str, ...] = Field(min_length=1)
    tribunal_task: str = Field(min_length=1)
    allowed_noncompletion_statuses: tuple[str, ...] = Field(min_length=1)
    live_write_allowed: Literal[False] = False
    human_input_required_for_sim: Literal[False] = False
    redaction_required: Literal[True] = True
    secret_scan_required: Literal[True] = True
    requires_two_factor_approval: bool


class HardTaskManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_at: str
    command: Literal["hard_task_manifest"]
    driver: Literal["loop_harness"]
    status: Literal["passed", "failed"]
    expected_tool_count: int
    covered_tool_count: int
    expected_tool_ids: tuple[str, ...]
    covered_tool_ids: tuple[str, ...]
    missing_tool_ids: tuple[str, ...]
    unexpected_tool_ids: tuple[str, ...]
    duplicate_tool_ids: tuple[str, ...]
    registered_missing_tool_ids: tuple[str, ...]
    validation_errors: tuple[str, ...]
    specs: tuple[HardTaskSpec, ...]
    git: GitState

    def to_json_value(self) -> dict[str, JsonValue]:
        return self.model_dump(mode="json")


def validate_hard_task_manifest(
    specs: Sequence[HardTaskSpec] = (),
    *,
    expected_tool_ids: Iterable[str] = DEFAULT_INCOMPLETE_TOOL_IDS,
    registered_tool_ids: Iterable[str] = (),
) -> HardTaskManifest:
    spec_tuple = HARD_TASK_SPECS if not specs else tuple(specs)
    expected_tools = frozenset(expected_tool_ids)
    registered_tools = frozenset(registered_tool_ids)
    covered_tool_ids = tuple(spec.tool_id for spec in spec_tuple)
    covered_tools = frozenset(covered_tool_ids)
    duplicate_tool_ids = tuple(
        sorted(tool_id for tool_id, count in Counter(covered_tool_ids).items() if count > 1),
    )
    errors = (
        tuple(
            f"missing hard task spec: {tool_id}"
            for tool_id in sorted(expected_tools - covered_tools)
        )
        + tuple(
            f"unexpected hard task spec: {tool_id}"
            for tool_id in sorted(covered_tools - expected_tools)
        )
        + tuple(f"duplicate hard task spec: {tool_id}" for tool_id in duplicate_tool_ids)
        + _registered_tool_errors(covered_tools, registered_tools)
        + tuple(error for spec in spec_tuple for error in _guard_errors(spec))
    )
    return HardTaskManifest(
        checked_at=now_utc(),
        command="hard_task_manifest",
        driver="loop_harness",
        status="passed" if not errors else "failed",
        expected_tool_count=len(expected_tools),
        covered_tool_count=len(covered_tools),
        expected_tool_ids=tuple(sorted(expected_tools)),
        covered_tool_ids=tuple(sorted(covered_tools)),
        missing_tool_ids=tuple(sorted(expected_tools - covered_tools)),
        unexpected_tool_ids=tuple(sorted(covered_tools - expected_tools)),
        duplicate_tool_ids=duplicate_tool_ids,
        registered_missing_tool_ids=_registered_missing_tool_ids(covered_tools, registered_tools),
        validation_errors=errors,
        specs=spec_tuple,
        git=current_git_state(),
    )


def handle_hard_task_manifest(out: Path, registered_tool_ids: Iterable[str]) -> int:
    manifest = validate_hard_task_manifest(registered_tool_ids=registered_tool_ids)
    payload = redact_json(manifest.to_json_value())
    if not isinstance(payload, dict):
        raise TypeError("hard task manifest redaction returned non-object")
    write_json(out, payload)
    findings, scan_errors = scan_secret_paths([str(out)])
    payload["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    write_json(out, payload)
    return 0 if manifest.status == "passed" and not findings and not scan_errors else 1


def _registered_tool_errors(
    covered_tools: frozenset[str],
    registered_tools: frozenset[str],
) -> tuple[str, ...]:
    if not registered_tools:
        return ()
    return tuple(
        f"hard task spec references unregistered FastMCP tool: {tool_id}"
        for tool_id in sorted(covered_tools - registered_tools)
    )


def _registered_missing_tool_ids(
    covered_tools: frozenset[str],
    registered_tools: frozenset[str],
) -> tuple[str, ...]:
    if not registered_tools:
        return ()
    return tuple(sorted(covered_tools - registered_tools))


def _guard_errors(spec: HardTaskSpec) -> tuple[str, ...]:
    errors: list[str] = []
    if spec.fastmcp_tool_name != spec.tool_id:
        errors.append(f"{spec.tool_id}: fastmcp_tool_name must match tool_id")
    if spec.live_write_allowed:
        errors.append(f"{spec.tool_id}: hard tasks must not allow live writes")
    if spec.human_input_required_for_sim:
        errors.append(f"{spec.tool_id}: SIM hard tasks must not require human input")
    if not spec.redaction_required:
        errors.append(f"{spec.tool_id}: redaction_required must be true")
    if not spec.secret_scan_required:
        errors.append(f"{spec.tool_id}: secret_scan_required must be true")
    if not AUTH_MISSING_STATUSES.intersection(spec.allowed_noncompletion_statuses):
        errors.append(
            f"{spec.tool_id}: missing credentials must fail as "
            "auth_required/incomplete_auth_required",
        )
    if spec.risk_class in RISK_CLASSES_REQUIRING_APPROVAL and not spec.requires_two_factor_approval:
        errors.append(f"{spec.tool_id}: risky task requires two-factor approval gate")
    return tuple(errors)


def _qa_command(command: str, *args: str) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "python",
        "-m",
        "saxo_bank_mcp.qa",
        command,
        *args,
        "--out",
        "{out}",
    )


def _order_write_task(write_class: OrderWriteClass) -> HardTaskSpec:
    spec = ORDER_WRITE_SPECS[write_class]
    return HardTaskSpec(
        tool_id=spec.tool_name,
        fastmcp_tool_name=spec.tool_name,
        qa_command=_qa_command("sim-order-mutation", "--classes", write_class),
        endpoint_class="sim_demo",
        risk_class="money_moving",
        setup_steps=(
            "Set SIM environment, fixture account allowlist, fixture instrument allowlist, "
            "and test approval factor.",
            f"Create a write preview for {spec.operation_id} through saxo_create_write_preview.",
        ),
        input_constraints=_order_input_constraints(spec),
        success_oracle=(
            "FastMCP call reaches the SIM order tool, not an internal client shortcut.",
            "Successful completion requires Saxo network response parsing and "
            "class-specific readback evidence; delete-by-instrument uses trade-message "
            "readback because the Saxo endpoint cancels by AccountKey/AssetType/Uic.",
            "If auth is missing, the task may only report incomplete_auth_required "
            "and must not claim completion.",
        ),
        cleanup_steps=(
            "Record cleanup_attempted from the write result.",
            "Cancel or verify no open SIM order remains when an order or partial order "
            "may have been created.",
        ),
        tribunal_task=(
            f"Use {spec.tool_name} to perform a realistic SIM {write_class} task, verify state, "
            "explain retry/unknown-state pitfalls, and recommend agent UX/safety improvements."
        ),
        allowed_noncompletion_statuses=("auth_required", "incomplete_auth_required"),
        requires_two_factor_approval=True,
    )


def _order_input_constraints(spec: OrderWriteSpec) -> tuple[str, ...]:
    route_key = "none" if spec.route_key is None else spec.route_key
    query_keys = ", ".join(spec.query_keys) if spec.query_keys else "none"
    return (
        f"OperationId must be {spec.operation_id}.",
        f"HTTP method must be {spec.method} on SIM endpoint {spec.endpoint_path}.",
        f"Route key: {route_key}; query keys: {query_keys}.",
        "AccountKey and order identifiers must be redacted in evidence.",
    )


HARD_TASK_SPECS: Final[tuple[HardTaskSpec, ...]] = (
    _order_write_task("multileg-cancel"),
    _order_write_task("cancel"),
    _order_write_task("cancel-by-instrument"),
    HardTaskSpec(
        tool_id="saxo_cleanup_streaming_subscriptions",
        fastmcp_tool_name="saxo_cleanup_streaming_subscriptions",
        qa_command=_qa_command("stream-cleanup", "--simulate-leak"),
        endpoint_class="sim_demo",
        risk_class="streaming",
        setup_steps=("Create or simulate a local streaming subscription record for cleanup.",),
        input_constraints=(
            "ContextId must use a synthetic non-secret value.",
            "ReferenceId and ContextId must not contain tokens or account identifiers.",
        ),
        success_oracle=(
            "FastMCP cleanup tool is called.",
            "Local leak records are removed and remote cleanup is attempted when auth exists.",
        ),
        cleanup_steps=("Verify local_open_records_after is empty.",),
        tribunal_task=(
            "Use the cleanup tool to remove stale streaming subscriptions and identify "
            "where agents could miss leaked contexts or over-delete active streams."
        ),
        allowed_noncompletion_statuses=("auth_required", "incomplete_auth_required"),
        requires_two_factor_approval=False,
    ),
    HardTaskSpec(
        tool_id="saxo_create_order_preview",
        fastmcp_tool_name="saxo_create_order_preview",
        qa_command=_qa_command("trade-precheck"),
        endpoint_class="safe_no_secret_fixture",
        risk_class="trade_precheck",
        setup_steps=("Use a no-secret precheck fixture and SIM account/instrument placeholders.",),
        input_constraints=(
            "Order body must include account, instrument, quantity, order type, and duration.",
            "AccountKey must be redacted and preview must not place, modify, or cancel an order.",
        ),
        success_oracle=(
            "FastMCP preview tool returns preview_created.",
            "order_placed, order_modified, order_cancelled, and live_write are false.",
        ),
        cleanup_steps=(
            "No order cleanup is required because the preview tool must not mutate orders.",
        ),
        tribunal_task=(
            "Use the preview tool to validate a realistic order without placing it and critique "
            "agent-facing risk/disclaimer guidance."
        ),
        allowed_noncompletion_statuses=("auth_required", "incomplete_auth_required", "denied"),
        requires_two_factor_approval=False,
    ),
    HardTaskSpec(
        tool_id="saxo_create_streaming_price_subscription",
        fastmcp_tool_name="saxo_create_streaming_price_subscription",
        qa_command=_qa_command(
            "stream",
            "--require-frame",
            "--expect-connections",
            "4",
            "--expect-price-instruments",
            "200",
        ),
        endpoint_class="sim_demo",
        risk_class="streaming",
        setup_steps=("Set SIM auth/cache if available and use synthetic context/reference ids.",),
        input_constraints=(
            "ContextId/reference_id must be non-secret and valid for Saxo streaming.",
            "UIC list must stay within documented streaming limits.",
        ),
        success_oracle=(
            "FastMCP streaming tool is called.",
            "Subscription snapshot and websocket frame are captured when auth/network "
            "is available.",
        ),
        cleanup_steps=("Run saxo_cleanup_streaming_subscriptions for the created context.",),
        tribunal_task=(
            "Create a streaming price subscription, inspect subscription/frame evidence, then "
            "cleanup and critique leak prevention for agents."
        ),
        allowed_noncompletion_statuses=("auth_required", "incomplete_auth_required"),
        requires_two_factor_approval=False,
    ),
    HardTaskSpec(
        tool_id="saxo_get_multileg_order_defaults",
        fastmcp_tool_name="saxo_get_multileg_order_defaults",
        qa_command=_qa_command("trade-multileg-defaults"),
        endpoint_class="sim_demo",
        risk_class="trade_lookup",
        setup_steps=("Use SIM auth/cache if available and fixture account/options inputs.",),
        input_constraints=(
            "AccountKey must be redacted.",
            "OptionRootId must be positive and OptionsStrategyType must be explicit.",
        ),
        success_oracle=(
            "FastMCP defaults tool is called.",
            "A successful run returns HTTP 2xx/defaults payload and order_placed=false.",
        ),
        cleanup_steps=("No order cleanup is required because this is a read/defaults lookup.",),
        tribunal_task=(
            "Fetch multileg defaults and critique how clearly the tool separates defaults lookup "
            "from order placement."
        ),
        allowed_noncompletion_statuses=("auth_required", "incomplete_auth_required"),
        requires_two_factor_approval=False,
    ),
    HardTaskSpec(
        tool_id="saxo_get_required_disclaimers",
        fastmcp_tool_name="saxo_get_required_disclaimers",
        qa_command=_qa_command("trade-disclaimer-lookup"),
        endpoint_class="sim_demo",
        risk_class="trade_lookup",
        setup_steps=(
            "Attempt to discover real disclaimer context/tokens from SIM order pre-check.",
            "If no outstanding SIM disclaimer is available, use a synthetic invalid token "
            "only as explicit safe endpoint-exercise coverage.",
        ),
        input_constraints=(
            "DisclaimerTokens must be non-empty.",
            "Exact token values must not be echoed in evidence.",
        ),
        success_oracle=(
            "FastMCP disclaimer lookup tool is called.",
            "A successful real-token run returns sanitized disclaimer details and "
            "order_placed=false.",
            "A synthetic-token fallback may only be marked exercised, with a coverage "
            "limitation that it is not successful user-consent evidence.",
        ),
        cleanup_steps=("No order cleanup is required because this is a disclaimer lookup.",),
        tribunal_task=(
            "Look up required disclaimers and critique whether an agent can decide what must be "
            "shown to a user before a risky action."
        ),
        allowed_noncompletion_statuses=(
            "auth_required",
            "incomplete_auth_required",
            "denied",
            "exercised",
        ),
        requires_two_factor_approval=False,
    ),
    _order_write_task("multileg-modify"),
    _order_write_task("modify"),
    _order_write_task("multileg-place"),
    _order_write_task("place"),
    HardTaskSpec(
        tool_id="saxo_register_disclaimer_response",
        fastmcp_tool_name="saxo_register_disclaimer_response",
        qa_command=_qa_command("trade-disclaimer-response"),
        endpoint_class="sim_demo",
        risk_class="disclaimer_write",
        setup_steps=(
            "Attempt to discover real disclaimer context/tokens from SIM order pre-check.",
            "If no outstanding SIM disclaimer is available, use synthetic invalid context/token "
            "only as explicit safe endpoint-exercise coverage.",
            "Use the SIM test approval factor for the target tool call.",
        ),
        input_constraints=(
            "DisclaimerContext, DisclaimerToken, ResponseType, and approval factor are required.",
            "Approval factor and disclaimer token must not be written to evidence.",
        ),
        success_oracle=(
            "FastMCP disclaimer response tool is called.",
            "A successful run submits the response in SIM and reports live_write=false.",
            "A synthetic-token fallback may only be marked exercised, with a coverage "
            "limitation that it is not successful user-consent evidence.",
        ),
        cleanup_steps=(
            "Record the response receipt; no order cleanup is allowed from disclaimer "
            "response alone.",
        ),
        tribunal_task=(
            "Register a disclaimer response only after the required approval factor and critique "
            "whether agents can avoid accidental consent."
        ),
        allowed_noncompletion_statuses=(
            "auth_required",
            "incomplete_auth_required",
            "denied",
            "exercised",
        ),
        requires_two_factor_approval=True,
    ),
)
