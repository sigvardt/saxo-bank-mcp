from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.config import SaxoEnvironment
from saxo_bank_mcp.live_approval import live_approval_statement
from saxo_bank_mcp.trading_write_registry import TradingWriteSpec

TRADING_WRITE_PREVIEW_TTL_SECONDS: Final = 300


class TradingWriteRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    operation_id: str
    path_parameters: dict[str, str] = Field(default_factory=dict)
    query_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    request_body: dict[str, JsonValue] = Field(default_factory=dict)
    account_key: str | None = None
    instrument_uic: int | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, gt=0)
    estimated_notional: float | None = Field(default=None, ge=0)

    @field_validator("operation_id")
    @classmethod
    def validate_operation_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "empty_operation_id",
                "operation_id must not be empty",
            )
        return stripped


@dataclass(frozen=True, slots=True)
class PreparedTradingWrite:
    request: TradingWriteRequest
    spec: TradingWriteSpec
    environment: SaxoEnvironment
    resolved_path: str
    request_fingerprint: str
    preview_token_fingerprint: str
    expected_approval_statement: str | None
    expires_at: datetime


_PREVIEWS: dict[str, PreparedTradingWrite] = {}
_CONSUMED_PREVIEW_TOKEN_FINGERPRINTS: set[str] = set()


def create_trading_write_preview(
    request: TradingWriteRequest,
    spec: TradingWriteSpec,
    environment: SaxoEnvironment,
    resolved_path: str,
) -> tuple[str, PreparedTradingWrite]:
    fingerprint = trading_write_fingerprint(request, environment)
    token = secrets.token_urlsafe(32)
    preview_token_fingerprint = _sha256(token)
    approval = (
        live_approval_statement(f"{fingerprint}:{preview_token_fingerprint}")
        if environment == SaxoEnvironment.LIVE
        else None
    )
    prepared = PreparedTradingWrite(
        request=request,
        spec=spec,
        environment=environment,
        resolved_path=resolved_path,
        request_fingerprint=fingerprint,
        preview_token_fingerprint=preview_token_fingerprint,
        expected_approval_statement=approval,
        expires_at=datetime.now(UTC) + timedelta(seconds=TRADING_WRITE_PREVIEW_TTL_SECONDS),
    )
    _PREVIEWS[token] = prepared
    return token, prepared


def get_trading_write_preview(preview_token: str) -> PreparedTradingWrite | None:
    return _PREVIEWS.get(preview_token)


def trading_write_preview_consumed(prepared: PreparedTradingWrite) -> bool:
    return prepared.preview_token_fingerprint in _CONSUMED_PREVIEW_TOKEN_FINGERPRINTS


def consume_trading_write_preview(prepared: PreparedTradingWrite) -> None:
    _CONSUMED_PREVIEW_TOKEN_FINGERPRINTS.add(prepared.preview_token_fingerprint)


def reset_trading_write_state() -> None:
    _PREVIEWS.clear()
    _CONSUMED_PREVIEW_TOKEN_FINGERPRINTS.clear()


def trading_write_fingerprint(
    request: TradingWriteRequest,
    environment: SaxoEnvironment,
) -> str:
    canonical = json.dumps(
        {
            "environment": environment.value,
            "request": request.model_dump(mode="json"),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _sha256(canonical)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
