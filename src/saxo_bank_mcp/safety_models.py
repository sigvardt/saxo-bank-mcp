from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp._evidence import JsonValue

type SafetyEnvironment = Literal["SIM", "LIVE"]
type SafetyStatus = Literal[
    "preview_created",
    "approved_for_simulation",
    "approved_for_execution",
    "denied",
]

TEST_APPROVAL_FACTOR: Final = "SIM_TEST_APPROVED"
PREVIEW_TTL_SECONDS: Final = 300
DEFAULT_MAX_QUANTITY: Final = 100.0
DEFAULT_MAX_NOTIONAL: Final = 10_000.0
SAFETY_TOOL_VERIFIES: Final[tuple[str, ...]] = (
    "deterministic local write safety gates ran",
    "raw audit event was written outside the repository",
)
SAFETY_TOOL_DOES_NOT_VERIFY: Final[tuple[str, ...]] = (
    "Saxo order placement",
    "Saxo account state changed",
    "market price availability",
    "live-write permission",
)


class AccountCurrencyRisk(BaseModel):
    model_config = ConfigDict(frozen=True)

    cost: float | None
    cash_required: float | None
    margin_impact: float | None
    contract_multiplier: float | None
    conversion_known: bool


class WritePreviewRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    account_key: str
    instrument_uic: int
    quantity: float = Field(gt=0)
    estimated_notional: float = Field(ge=0)
    account_currency: str
    risk: AccountCurrencyRisk
    request_body: dict[str, JsonValue]

    @field_validator("operation_id", "account_key", "account_currency")
    @classmethod
    def validate_present(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError("empty_safety_value", "safety value must not be empty")
        return stripped


class SafetyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    environment: SafetyEnvironment = "SIM"
    live_writes_enabled: bool = False
    global_kill_switch: bool = False
    account_allowlist: frozenset[str]
    instrument_allowlist: frozenset[int]
    max_quantity: float = DEFAULT_MAX_QUANTITY
    max_notional: float = DEFAULT_MAX_NOTIONAL
    audit_dir: Path

    @classmethod
    def from_env(cls) -> SafetyConfig:
        return cls(
            environment=_safety_environment(os.environ.get("SAXO_MCP_ENVIRONMENT")),
            live_writes_enabled=(
                os.environ.get("SAXO_MCP_ENABLE_LIVE_WRITES") == "I_UNDERSTAND_REAL_MONEY_RISK"
            ),
            global_kill_switch=os.environ.get("SAXO_MCP_GLOBAL_KILL_SWITCH") == "1",
            account_allowlist=_string_allowlist(os.environ.get("SAXO_MCP_ACCOUNT_ALLOWLIST")),
            instrument_allowlist=_int_allowlist(os.environ.get("SAXO_MCP_INSTRUMENT_ALLOWLIST")),
            max_quantity=_float_env("SAXO_MCP_MAX_QUANTITY", DEFAULT_MAX_QUANTITY),
            max_notional=_float_env("SAXO_MCP_MAX_NOTIONAL", DEFAULT_MAX_NOTIONAL),
            audit_dir=Path(os.environ["SAXO_MCP_AUDIT_DIR"])
            if "SAXO_MCP_AUDIT_DIR" in os.environ
            else Path.home() / ".local/state/saxo-bank-mcp/audit",
        )


class PreviewResult(TypedDict):
    status: SafetyStatus
    tool_name: str
    environment: SafetyEnvironment
    request_fingerprint: str
    saxo_endpoint_called: bool
    execution_performed: bool
    simulation_only: bool
    order_placed: bool
    verifies: list[str]
    does_not_verify: list[str]
    next_action: str
    preview_token: NotRequired[str]
    preview_token_fingerprint: NotRequired[str]
    preview_token_sensitivity: NotRequired[str]
    preview_token_expires_at: NotRequired[str]
    denial_reasons: NotRequired[list[str]]
    denial_reason: NotRequired[str]
    approval_factor_mode: NotRequired[str]
    approval_prompt: NotRequired[str]
    approval_summary: NotRequired[dict[str, JsonValue]]
    audit_path: NotRequired[str]
    audit_path_inside_repo: NotRequired[bool]
    audit_mode: NotRequired[str | None]


@dataclass(frozen=True, slots=True)
class StoredPreview:
    request: WritePreviewRequest
    request_fingerprint: str
    expires_at: datetime
    environment: SafetyEnvironment


def _safety_environment(raw: str | None) -> SafetyEnvironment:
    match "SIM" if raw is None else raw.upper():
        case "LIVE":
            return "LIVE"
        case "SIM":
            return "SIM"
        case _:
            return "SIM"


def _string_allowlist(raw: str | None) -> frozenset[str]:
    if raw is None:
        return frozenset()
    return frozenset(value.strip() for value in raw.split(",") if value.strip())


def _int_allowlist(raw: str | None) -> frozenset[int]:
    if raw is None:
        return frozenset()
    values: list[int] = []
    for value in raw.split(","):
        stripped = value.strip()
        if stripped:
            values.append(int(stripped))
    return frozenset(values)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return float(raw)
