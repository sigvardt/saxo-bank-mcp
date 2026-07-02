from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_core import PydanticCustomError

type CredentialLabel = Literal[
    "App Key",
    "Access Control",
    "Grant Type",
    "Auth endpoint",
    "Token endpoint",
]
type CredentialField = Literal["app_key", "grant_type", "auth_endpoint", "token_endpoint"]

LABEL_TO_FIELD: Final[dict[CredentialLabel, CredentialField | None]] = {
    "App Key": "app_key",
    "Access Control": None,
    "Grant Type": "grant_type",
    "Auth endpoint": "auth_endpoint",
    "Token endpoint": "token_endpoint",
}
REQUIRED_FIELDS: Final[tuple[CredentialField, ...]] = (
    "app_key",
    "grant_type",
    "auth_endpoint",
    "token_endpoint",
)


@dataclass(frozen=True, slots=True)
class CredentialFileError(Exception):
    reason: str

    def __str__(self) -> str:  # noqa: D105
        return self.reason


class SimPkceCredentials(BaseModel):
    model_config = ConfigDict(frozen=True)

    app_key: str
    grant_type: Literal["PKCE"]
    auth_endpoint: str
    token_endpoint: str

    @field_validator("app_key", "auth_endpoint", "token_endpoint")
    @classmethod
    def validate_present(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError("empty_credential_value", "credential value is empty")
        return stripped

    @field_validator("auth_endpoint", "token_endpoint")
    @classmethod
    def validate_https_endpoint(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise PydanticCustomError("credential_endpoint", "credential endpoint must use https")
        return value


def parse_sim_pkce_credentials_file(path: Path) -> SimPkceCredentials:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CredentialFileError("credential file cannot be read") from error
    return parse_sim_pkce_credentials_text(text)


def parse_sim_pkce_credentials_text(text: str) -> SimPkceCredentials:
    values: dict[CredentialField, str] = {}
    pending_label: CredentialLabel | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pending_label = _consume_credential_line(values, pending_label, line)

    if pending_label is not None:
        raise CredentialFileError(f"missing value for credential label: {pending_label}")

    missing = [field for field in REQUIRED_FIELDS if field not in values]
    if missing:
        raise CredentialFileError(f"missing credential labels: {', '.join(missing)}")

    grant_type = values["grant_type"].upper()
    if grant_type != "PKCE":
        raise CredentialFileError("credential grant type must be PKCE")
    try:
        return SimPkceCredentials(
            app_key=values["app_key"],
            grant_type="PKCE",
            auth_endpoint=values["auth_endpoint"],
            token_endpoint=values["token_endpoint"],
        )
    except ValidationError as error:
        raise CredentialFileError("credential file is not valid SIM PKCE credentials") from error


def _credential_label(value: str) -> CredentialLabel | None:
    match value:
        case "App Key":
            return "App Key"
        case "Access Control":
            return "Access Control"
        case "Grant Type":
            return "Grant Type"
        case "Auth endpoint":
            return "Auth endpoint"
        case "Token endpoint":
            return "Token endpoint"
        case _:
            return None


def _consume_credential_line(
    values: dict[CredentialField, str],
    pending_label: CredentialLabel | None,
    line: str,
) -> CredentialLabel | None:
    if pending_label is not None:
        _add_label_value(values, pending_label, line)
        return None
    if ":" in line:
        raw_label, raw_value = line.split(":", maxsplit=1)
        label = _credential_label(raw_label.strip())
        if label is None:
            raise CredentialFileError(f"unsupported credential label: {raw_label.strip()}")
        _add_label_value(values, label, raw_value.strip())
        return None
    return _credential_label(line)


def _add_label_value(
    values: dict[CredentialField, str],
    label: CredentialLabel,
    value: str,
) -> None:
    field = LABEL_TO_FIELD[label]
    if field is None:
        return
    if field in values:
        raise CredentialFileError(f"duplicate credential label: {label}")
    values[field] = value
