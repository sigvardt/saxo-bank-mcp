from __future__ import annotations

import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from saxo_bank_mcp.auth import SaxoTokenSet, TokenEnvironment
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.sim_token_refresh import (
    SimRefreshOutcome,
    refresh_sim_token_if_needed,
)
from saxo_bank_mcp.token_cache import load_token_cache, save_token_cache

FIXED_NOW = datetime(2026, 8, 17, 12, tzinfo=UTC)
FIXTURE_ACCESS_VALUE = "fixture-access-token"
FIXTURE_REFRESH_VALUE = "fixture-refresh-token"
NEW_REFRESH_VALUE = "new-refresh"
ROTATED_ACCESS_VALUE = "rotated-access"
ROTATED_REFRESH_VALUE = "rotated-refresh"
TEST_TOKEN_URL = "https://sim.logonvalidation.net/token"  # noqa: S105
OWNER_FILE_MODE = 0o600


def _settings(cache_path: Path) -> SimAuthSettings:
    return SimAuthSettings(
        app_key="fixture-app-key",
        authorization_url="https://sim.logonvalidation.net/authorize",
        token_url=TEST_TOKEN_URL,
        rest_base_url="https://gateway.saxobank.com/sim/openapi/",
        redirect_uri="",
        cache_path=cache_path,
    )


def _token(
    *,
    environment: TokenEnvironment = "SIM",
    expires_at: datetime | None = None,
    refreshable: bool = True,
    refresh_value: str = FIXTURE_REFRESH_VALUE,
) -> SaxoTokenSet:
    return SaxoTokenSet(
        access_token=FIXTURE_ACCESS_VALUE,
        refresh_token=refresh_value if refreshable else None,
        code_verifier="v" * 43 if refreshable else None,
        environment=environment,
        expires_at=expires_at or FIXED_NOW + timedelta(minutes=20),
    )


@pytest.mark.anyio
async def test_fresh_sim_cache_does_not_call_network(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(settings.cache_path, _token())

    def unexpected_request(_request: httpx2.Request) -> httpx2.Response:
        pytest.fail("fresh token must not call Saxo")

    outcome = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(unexpected_request),
    )

    assert outcome == SimRefreshOutcome(status="fresh", network_call_made=False)


@pytest.mark.anyio
async def test_near_expiry_refresh_saves_complete_rotated_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(
        settings.cache_path,
        _token(expires_at=FIXED_NOW + timedelta(minutes=1)),
    )
    requests = 0

    def rotate(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        assert request.url == settings.token_url
        return httpx2.Response(
            200,
            json={
                "access_token": ROTATED_ACCESS_VALUE,
                "refresh_token": ROTATED_REFRESH_VALUE,
                "expires_in": 1200,
            },
        )

    outcome = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(rotate),
    )

    saved = load_token_cache(settings.cache_path)
    assert outcome == SimRefreshOutcome(status="refreshed", network_call_made=True)
    assert requests == 1
    assert saved is not None
    assert saved.access_token == ROTATED_ACCESS_VALUE
    assert saved.refresh_token == ROTATED_REFRESH_VALUE
    assert saved.code_verifier == "v" * 43
    assert saved.environment == "SIM"
    assert stat.S_IMODE(settings.cache_path.stat().st_mode) == OWNER_FILE_MODE


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("cached_token", "expected_status"),
    [
        (_token(environment="LIVE"), "wrong_environment"),
        (_token(refreshable=False), "login_required"),
    ],
)
async def test_non_sim_or_non_refreshable_cache_never_calls_network(
    tmp_path: Path,
    cached_token: SaxoTokenSet,
    expected_status: str,
) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(settings.cache_path, cached_token)

    def unexpected_request(_request: httpx2.Request) -> httpx2.Response:
        pytest.fail("refused cache must not call Saxo")

    outcome = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        minimum_validity=timedelta(hours=1),
        transport=httpx2.MockTransport(unexpected_request),
    )

    assert outcome.status == expected_status
    assert outcome.network_call_made is False


@pytest.mark.anyio
async def test_missing_cache_does_not_call_network(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "token.json")

    def unexpected_request(_request: httpx2.Request) -> httpx2.Response:
        pytest.fail("missing cache must not call Saxo")

    outcome = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(unexpected_request),
    )

    assert outcome == SimRefreshOutcome(status="token_missing", network_call_made=False)


@pytest.mark.anyio
async def test_rejection_marker_suppresses_unchanged_cache_retry(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(
        settings.cache_path,
        _token(expires_at=FIXED_NOW + timedelta(minutes=1)),
    )
    requests = 0

    def reject(_request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(401, json={"error": "invalid_grant"})

    transport = httpx2.MockTransport(reject)
    first = await refresh_sim_token_if_needed(settings, now=FIXED_NOW, transport=transport)
    second = await refresh_sim_token_if_needed(settings, now=FIXED_NOW, transport=transport)

    marker = settings.cache_path.with_name(
        f"{settings.cache_path.name}.sim-refresh-rejected",
    )
    assert first == SimRefreshOutcome(status="refresh_rejected", network_call_made=True)
    assert second == SimRefreshOutcome(
        status="refresh_rejected_unchanged",
        network_call_made=False,
    )
    assert requests == 1
    assert marker.read_text(encoding="utf-8").strip().isdigit()
    assert stat.S_IMODE(marker.stat().st_mode) == OWNER_FILE_MODE


@pytest.mark.anyio
async def test_cache_change_allows_one_new_refresh_attempt(tmp_path: Path) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(
        settings.cache_path,
        _token(expires_at=FIXED_NOW + timedelta(minutes=1)),
    )

    rejected = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(401, json={"error": "invalid_grant"}),
        ),
    )
    save_token_cache(
        settings.cache_path,
        _token(
            expires_at=FIXED_NOW + timedelta(minutes=1),
            refresh_value=NEW_REFRESH_VALUE,
        ),
    )

    recovered = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(
                200,
                json={
                    "access_token": "recovered-access",
                    "refresh_token": "recovered-refresh",
                    "expires_in": 1200,
                },
            ),
        ),
    )

    marker = settings.cache_path.with_name(
        f"{settings.cache_path.name}.sim-refresh-rejected",
    )
    assert rejected == SimRefreshOutcome(status="refresh_rejected", network_call_made=True)
    assert recovered == SimRefreshOutcome(status="refreshed", network_call_made=True)
    assert marker.exists() is False
