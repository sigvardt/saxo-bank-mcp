from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, model_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.live_evidence_release_proof_models import StrictReleaseModel
from saxo_bank_mcp.qa_live_read_contract import (
    LIVE_READ_SCENARIO_STATUSES,
    live_read_transport_passed,
)

_MANUAL_SCENARIO_CONTRACTS: Final = {
    "generic_fastmcp_validation": (
        "saxo_call_registered_endpoint",
        "invalid_arguments",
    ),
    "live_precheck_validation": ("saxo_precheck_live_order", "invalid_request"),
    "live_write_refusal": ("saxo_place_sim_order", "refused"),
    "disabled_live_read_refusal": (
        "saxo_call_registered_endpoint",
        "live_not_called",
    ),
}


class SecretScan(StrictReleaseModel):
    findings: list[JsonValue] = Field(max_length=0)
    paths: list[str] | None = None
    scan_errors: list[JsonValue] = Field(max_length=0)


class LiveReadReport(StrictReleaseModel):
    status: Literal["passed"]
    checked_at: str | None = None
    command: str | None = None
    detail: str | None = None
    driver: str | None = None
    git: dict[str, JsonValue] | None = None
    requested_environment: Literal["LIVE"]
    read_scenarios_exercised: list[str] = Field(min_length=9, max_length=9)
    read_tools_exercised: list[str] | None = None
    tool_statuses: dict[str, str] = Field(min_length=9, max_length=9)
    tool_results: dict[str, dict[str, JsonValue]] = Field(min_length=9, max_length=9)
    authenticated_registered_read_passed: Literal[True] | None = None
    live_read_coverage: dict[str, JsonValue] | None = None
    network_call_made: Literal[True] | None = None
    network_read_count: Literal[8]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]
    prompted_user: Literal[False] | None = None
    private_identifiers_redacted: Literal[True]
    private_financial_data_omitted: Literal[True]
    private_identifier_findings: list[JsonValue] = Field(max_length=0)
    evidence_redaction_status: Literal["passed"] | None = None
    secret_scan: SecretScan

    @model_validator(mode="after")
    def verify_scenario_contract(self) -> LiveReadReport:
        expected = dict(LIVE_READ_SCENARIO_STATUSES)
        if (
            self.read_scenarios_exercised != list(expected)
            or self.tool_statuses != expected
            or not live_read_transport_passed(self.tool_results)
            or not all(
                result.get("live_write_called") is False
                and result.get("order_or_subscription_created") is False
                for result in self.tool_results.values()
            )
        ):
            raise PydanticCustomError(
                "live_read_scenario_contract",
                "LIVE read scenarios do not match the required identities",
            )
        return self


class ProdReadinessReport(StrictReleaseModel):
    status: Literal["passed"]
    checked_at: str | None = None
    command: str | None = None
    detail: str | None = None
    driver: str | None = None
    git: dict[str, JsonValue] | None = None
    status_scope: Literal["code_safety_checks_only"]
    code_safety_checks_passed: Literal[True]
    production_ready: Literal[False]
    requirement_count: int | None = Field(default=None, gt=0)
    requirements: list[JsonValue] | None = None
    rapid_call_probe: dict[str, JsonValue] | None = None
    live_write_refusal_probe: dict[str, JsonValue] | None = None
    secret_scan: SecretScan
    live_read_ready: Literal[False] | None = None
    live_read_ready_scope: str | None = None
    live_read_evidence_required: str | None = None
    live_write_ready: Literal[False] | None = None
    live_write_missing_requirements: list[JsonValue] | None = None
    transport_constructed: Literal[False]
    network_call_made: Literal[False]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]
    verifies: list[str] | None = None
    does_not_verify: list[str] | None = None
    next_action: str | None = None


class ManualScenario(StrictReleaseModel):
    scenario_id: Literal[
        "generic_fastmcp_validation",
        "live_precheck_validation",
        "live_write_refusal",
        "disabled_live_read_refusal",
    ]
    status: Literal["passed"]
    actual_status: str
    argument_shape: dict[str, JsonValue]
    exception_type: Literal[""]
    expected_status: str
    live_write_called: Literal[False]
    network_call_made: Literal[False]
    order_or_subscription_created: Literal[False]
    rejected_input_absent_from_mcp_result: Literal[True]
    rejected_input_absent_from_warning_logs: Literal[True]
    result_content: list[JsonValue]
    result_is_error: bool
    safety_fields_passed: Literal[True]
    structured_result_keys: list[str]
    tool_name: str
    transport_constructed: Literal[False]
    warning_records: list[JsonValue]


class SourceEntry(StrictReleaseModel):
    path: str = Field(min_length=1)
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class RejectedInput(StrictReleaseModel):
    generated_for_this_run: Literal[True]
    persisted: Literal[False]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ManualQaReport(StrictReleaseModel):
    schema_version: Literal["saxo-manual-live-boundary-v1"]
    status: Literal["passed"]
    checked_at: str = Field(min_length=1)
    scope: Literal["local_fastmcp_live_safety_boundaries"]
    generator: str = Field(min_length=1)
    generator_source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    git: dict[str, JsonValue] | None = None
    replay_command: list[str] = Field(min_length=8, max_length=8)
    source_hash_algorithm: Literal["sha256"]
    source_hashes: list[SourceEntry] = Field(min_length=1)
    rejected_input: RejectedInput
    scenario_count: Literal[4]
    scenarios: list[ManualScenario] = Field(min_length=4, max_length=4)
    warning_log_transcript: list[JsonValue] = Field(min_length=1)
    warning_capture_verified: Literal[True]
    warning_log_transcript_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    network_call_made: Literal[False]
    live_write_called: Literal[False]
    order_or_subscription_created: Literal[False]

    @model_validator(mode="after")
    def verify_scenarios_and_replay(self) -> ManualQaReport:
        expected = {
            "generic_fastmcp_validation",
            "live_precheck_validation",
            "live_write_refusal",
            "disabled_live_read_refusal",
        }
        replay_prefix = [
            "uv",
            "run",
            "python",
            "-m",
            "saxo_bank_mcp.qa",
            "manual-live-boundary",
            "--out",
        ]
        if (
            {item.scenario_id for item in self.scenarios} != expected
            or not all(
                (
                    item.tool_name,
                    item.expected_status,
                    item.actual_status,
                    item.result_is_error,
                )
                == (
                    _MANUAL_SCENARIO_CONTRACTS[item.scenario_id][0],
                    _MANUAL_SCENARIO_CONTRACTS[item.scenario_id][1],
                    _MANUAL_SCENARIO_CONTRACTS[item.scenario_id][1],
                    True,
                )
                for item in self.scenarios
            )
            or self.replay_command[: len(replay_prefix)] != replay_prefix
            or Path(self.replay_command[-1]).name != "manual-qa.json"
            or self.warning_log_transcript_sha256
            != hashlib.sha256(
                json.dumps(self.warning_log_transcript, sort_keys=True).encode(),
            ).hexdigest()
        ):
            raise PydanticCustomError(
                "manual_qa_contract",
                "manual QA scenarios or replay command do not match the required contract",
            )
        return self
