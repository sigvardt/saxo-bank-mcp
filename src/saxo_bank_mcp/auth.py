from __future__ import annotations

from datetime import UTC, datetime
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticCustomError


class TokenStatus(TypedDict):
    has_access_token: bool
    has_refresh_token: bool
    has_code_verifier: bool
    expires_at: str
    is_expired: bool


class SaxoTokenSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str
    code_verifier: str
    expires_at: datetime

    @field_validator("access_token", "refresh_token", "code_verifier")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError("empty_token", "token value must not be empty")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PydanticCustomError("naive_expiry", "token expiry must include timezone")
        return value

    def redacted_status(self, *, now: datetime | None = None) -> TokenStatus:
        checked_at = datetime.now(UTC) if now is None else now
        return {
            "has_access_token": True,
            "has_refresh_token": True,
            "has_code_verifier": True,
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "is_expired": self.expires_at <= checked_at,
        }


class SaxoPendingAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    state: str
    code_verifier: str
    redirect_uri: str
    created_at: datetime

    @field_validator("state", "code_verifier", "redirect_uri")
    @classmethod
    def validate_present(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError("empty_pending_authorization", "value must not be empty")
        return value

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PydanticCustomError("naive_created_at", "created_at must include timezone")
        return value
