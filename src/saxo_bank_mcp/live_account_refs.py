from __future__ import annotations

import base64
import hmac
import secrets
from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.strict_json import parse_json_value

LIVE_ACCOUNTS_ENDPOINT: Final = "/port/v1/accounts/me"
ACCOUNT_REF_PREFIX: Final = "live-account-"
_PROCESS_SECRET: Final = secrets.token_bytes(32)


class LiveAccount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", strict=True)

    account_id: str = Field(alias="AccountId", min_length=1)
    account_key: str = Field(alias="AccountKey", min_length=1, repr=False)
    client_key: str = Field(alias="ClientKey", min_length=1, repr=False)
    active: bool = Field(alias="Active")
    currency: str = Field(alias="Currency", min_length=1)
    account_type: str = Field(alias="AccountType", min_length=1)
    display_name: str | None = Field(default=None, alias="DisplayName")


class LiveAccountsResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    data: list[LiveAccount] = Field(alias="Data")
    count: int | None = Field(default=None, alias="__count", ge=0)
    max_rows: int | None = Field(default=None, alias="MaxRows", ge=0)
    next_link: str | None = Field(default=None, alias="__next")

    @field_validator("next_link")
    @classmethod
    def reject_incomplete_pagination(cls, value: str | None) -> str | None:
        if value:
            raise PydanticCustomError(
                "pagination_present",
                "account response pagination must be completed before selection",
            )
        return value

    @model_validator(mode="after")
    def reject_declared_count_mismatch(self) -> LiveAccountsResponse:
        if self.count is not None and self.count != len(self.data):
            raise PydanticCustomError(
                "declared_count_mismatch",
                "declared account count must match returned accounts",
            )
        return self

    @model_validator(mode="after")
    def reject_duplicate_account_identities(self) -> LiveAccountsResponse:
        if len({account.account_id for account in self.data}) != len(self.data):
            raise PydanticCustomError(
                "duplicate_account_id",
                "visible account IDs must be unique",
            )
        if len({account.account_key for account in self.data}) != len(self.data):
            raise PydanticCustomError(
                "duplicate_account_key",
                "technical account keys must be unique",
            )
        return self


_ACCOUNTS_ADAPTER: Final = TypeAdapter(LiveAccountsResponse)


def parse_live_accounts(content: bytes) -> tuple[LiveAccount, ...]:
    parsed = _ACCOUNTS_ADAPTER.validate_python(parse_json_value(content), strict=True)
    return tuple(parsed.data)


def account_ref_for(token: SaxoTokenSet, account_key: str) -> str:
    generation = token.code_verifier or token.access_token
    message = b"\0".join((generation.encode(), account_key.encode()))
    digest = hmac.digest(_PROCESS_SECRET, message, "sha256")[:18]
    encoded = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return f"{ACCOUNT_REF_PREFIX}{encoded}"


def account_summaries(
    token: SaxoTokenSet,
    accounts: Sequence[LiveAccount],
) -> list[dict[str, JsonValue]]:
    return [
        {
            "account_id": account.account_id,
            "account_ref": account_ref_for(token, account.account_key),
            "active": account.active,
            "currency": account.currency,
            "account_type": account.account_type,
        }
        for account in accounts
    ]


def active_live_accounts(accounts: Sequence[LiveAccount]) -> tuple[LiveAccount, ...]:
    return tuple(account for account in accounts if account.active)


def resolve_account_ref(
    token: SaxoTokenSet,
    accounts: Sequence[LiveAccount],
    account_ref: str,
) -> LiveAccount | None:
    matches = tuple(
        account
        for account in accounts
        if hmac.compare_digest(
            account_ref_for(token, account.account_key),
            account_ref,
        )
    )
    return matches[0] if len(matches) == 1 else None
