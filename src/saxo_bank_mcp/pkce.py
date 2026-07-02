from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Final
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_core import PydanticCustomError

from saxo_bank_mcp.config import SaxoEnvironment, environment_endpoints

PKCE_METHOD: Final = "S256"
_VERIFIER_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43,128}$")


class PkcePair(BaseModel):
    model_config = ConfigDict(frozen=True)

    verifier: str
    challenge: str

    @field_validator("verifier")
    @classmethod
    def validate_verifier(cls, value: str) -> str:
        if _VERIFIER_RE.fullmatch(value) is None:
            raise PydanticCustomError(
                "pkce_verifier",
                "PKCE verifier must be 43-128 URL-safe characters",
            )
        return value

    @model_validator(mode="after")
    def validate_challenge(self) -> PkcePair:
        if self.challenge != code_challenge_s256(self.verifier):
            raise PydanticCustomError("pkce_challenge", "PKCE challenge must match verifier")
        return self


class AuthorizationUrlRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment: SaxoEnvironment
    client_id: str
    redirect_uri: str
    pkce: PkcePair
    state: str
    authorization_url: str | None = None


def create_pkce_pair() -> PkcePair:
    verifier = secrets.token_urlsafe(64)
    return PkcePair(verifier=verifier, challenge=code_challenge_s256(verifier))


def create_state() -> str:
    return secrets.token_urlsafe(32)


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(
    request: AuthorizationUrlRequest,
) -> str:
    endpoint = (
        request.authorization_url or environment_endpoints(request.environment).authorization_url
    )
    query = urlencode(
        {
            "response_type": "code",
            "client_id": request.client_id,
            "redirect_uri": request.redirect_uri,
            "state": request.state,
            "code_challenge": request.pkce.challenge,
            "code_challenge_method": PKCE_METHOD,
        },
    )
    return f"{endpoint}?{query}"
