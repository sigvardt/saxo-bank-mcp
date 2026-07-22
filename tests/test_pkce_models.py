from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SaxoEnvironment
from saxo_bank_mcp.pkce import (
    AuthorizationUrlRequest,
    PkcePair,
    build_authorization_url,
    code_challenge_s256,
    create_pkce_pair,
    create_state,
)


def test_pkce_challenge_uses_s256_base64url_without_padding() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    assert code_challenge_s256(verifier) == expected
    assert code_challenge_s256(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_pkce_pair_and_authorization_url_have_no_scope() -> None:
    pkce = create_pkce_pair()
    state = create_state()
    url = build_authorization_url(
        AuthorizationUrlRequest(
            environment=SaxoEnvironment.SIM,
            client_id="client-id",
            redirect_uri="http://127.0.0.1/callback",
            pkce=pkce,
            state=state,
        ),
    )

    assert re.fullmatch(r"[A-Za-z0-9_-]{43,128}", pkce.verifier)
    assert pkce.challenge == code_challenge_s256(pkce.verifier)
    assert state
    assert "code_challenge_method=S256" in url
    assert "scope=" not in url


def test_token_model_redacted_status_excludes_token_values() -> None:
    token = SaxoTokenSet(
        access_token="access-token-value",  # noqa: S106
        refresh_token="refresh-token-value",  # noqa: S106
        code_verifier="verifier-value",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )

    status = token.redacted_status(now=datetime.now(UTC))
    serialized = json.dumps(status)

    assert status["has_access_token"] is True
    assert status["has_refresh_token"] is True
    assert status["has_code_verifier"] is True
    assert "access-token-value" not in serialized
    assert "refresh-token-value" not in serialized
    assert "verifier-value" not in serialized


def test_token_model_rejects_empty_tokens_and_naive_expiry() -> None:
    with pytest.raises(ValidationError):
        SaxoTokenSet(
            access_token="",
            refresh_token="refresh",  # noqa: S106
            code_verifier="verifier",
            expires_at=datetime.now(UTC),
        )
    with pytest.raises(ValidationError):
        SaxoTokenSet(
            access_token="access",  # noqa: S106
            refresh_token="refresh",  # noqa: S106
            code_verifier="verifier",
            expires_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC).replace(tzinfo=None),
        )


def test_pkce_pair_constructor_rejects_wrong_challenge() -> None:
    with pytest.raises(ValidationError):
        PkcePair(verifier="A" * 43, challenge="wrong")
