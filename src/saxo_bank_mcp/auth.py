from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Self, TypedDict

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core import PydanticCustomError

type TokenEnvironment = Literal["SIM", "LIVE"]


class TokenStatus(TypedDict):
    has_access_token: bool
    has_refresh_token: bool
    has_code_verifier: bool
    environment: TokenEnvironment | None
    expires_at: str
    is_expired: bool


@dataclass(frozen=True, slots=True)
class RefreshTokenMaterial:
    refresh_token: str
    code_verifier: str


class SaxoTokenSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    refresh_token: str | None = None
    code_verifier: str | None = None
    environment: TokenEnvironment | None = None
    expires_at: datetime

    @field_validator("access_token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError("empty_token", "token value must not be empty")
        return value

    @field_validator("refresh_token", "code_verifier")
    @classmethod
    def validate_optional_token(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise PydanticCustomError("empty_token", "token value must not be empty")
        return value

    @model_validator(mode="after")
    def validate_refresh_pair(self) -> Self:
        refresh_present = self.refresh_token is not None
        verifier_present = self.code_verifier is not None
        if refresh_present != verifier_present:
            raise PydanticCustomError(
                "partial_refresh_material",
                "refresh_token and code_verifier must both be present or both be absent",
            )
        return self

    @field_validator("expires_at")
    @classmethod
    def validate_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise PydanticCustomError("naive_expiry", "token expiry must include timezone")
        return value

    def refresh_material(self) -> RefreshTokenMaterial | None:
        if self.refresh_token is None or self.code_verifier is None:
            return None
        return RefreshTokenMaterial(
            refresh_token=self.refresh_token,
            code_verifier=self.code_verifier,
        )

    def redacted_status(self, *, now: datetime | None = None) -> TokenStatus:
        checked_at = datetime.now(UTC) if now is None else now
        return {
            "has_access_token": True,
            "has_refresh_token": self.refresh_token is not None,
            "has_code_verifier": self.code_verifier is not None,
            "environment": self.environment,
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
