from __future__ import annotations

import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from saxo_bank_mcp import sim_token_refresh
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


def _attempt_marker(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.name}.sim-refresh-attempt")


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
        marker = _attempt_marker(settings.cache_path)
        assert marker.exists()
        assert stat.S_IMODE(marker.stat().st_mode) == OWNER_FILE_MODE
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
    assert _attempt_marker(settings.cache_path).exists() is False


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
        assert _attempt_marker(settings.cache_path).exists()
        return httpx2.Response(401, json={"error": "invalid_grant"})

    transport = httpx2.MockTransport(reject)
    first = await refresh_sim_token_if_needed(settings, now=FIXED_NOW, transport=transport)
    second = await refresh_sim_token_if_needed(settings, now=FIXED_NOW, transport=transport)

    marker = _attempt_marker(settings.cache_path)
    assert first == SimRefreshOutcome(status="refresh_rejected", network_call_made=True)
    assert second == SimRefreshOutcome(
        status="refresh_attempt_suppressed",
        network_call_made=False,
    )
    assert requests == 1
    assert marker.read_text(encoding="utf-8").strip()
    assert stat.S_IMODE(marker.stat().st_mode) == OWNER_FILE_MODE


@pytest.mark.anyio
async def test_attempt_marker_failure_stops_before_network_and_cannot_blind_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(
        settings.cache_path,
        _token(expires_at=FIXED_NOW + timedelta(minutes=1)),
    )
    marker_writes = 0

    def failed_marker(path: Path, revision: str) -> None:
        nonlocal marker_writes
        marker_writes += 1
        path.write_text(f"{revision}\n", encoding="utf-8")
        path.chmod(OWNER_FILE_MODE)
        raise OSError("injected marker durability failure")

    def unexpected_request(_request: httpx2.Request) -> httpx2.Response:
        pytest.fail("refresh must not start without a durable attempt marker")

    monkeypatch.setattr(sim_token_refresh, "_write_attempt_revision", failed_marker)

    first = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(unexpected_request),
    )
    second = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(unexpected_request),
    )

    assert first == SimRefreshOutcome(status="attempt_marker_failed", network_call_made=False)
    assert second == SimRefreshOutcome(
        status="refresh_attempt_suppressed",
        network_call_made=False,
    )
    assert marker_writes == 1
    assert _attempt_marker(settings.cache_path).exists()


@pytest.mark.anyio
async def test_unknown_refresh_result_retains_attempt_marker_and_suppresses_retry(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(
        settings.cache_path,
        _token(expires_at=FIXED_NOW + timedelta(minutes=1)),
    )
    requests = 0

    def unknown_result(_request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        raise RuntimeError("injected crash-equivalent result")

    transport = httpx2.MockTransport(unknown_result)
    with pytest.raises(RuntimeError, match="crash-equivalent"):
        await refresh_sim_token_if_needed(settings, now=FIXED_NOW, transport=transport)
    second = await refresh_sim_token_if_needed(settings, now=FIXED_NOW, transport=transport)

    assert second == SimRefreshOutcome(
        status="refresh_attempt_suppressed",
        network_call_made=False,
    )
    assert requests == 1
    assert _attempt_marker(settings.cache_path).exists()


@pytest.mark.anyio
async def test_cache_save_failure_retains_attempt_marker_and_suppresses_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "token.json")
    original = _token(expires_at=FIXED_NOW + timedelta(minutes=1))
    save_token_cache(settings.cache_path, original)
    requests = 0

    def rotate(_request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        return httpx2.Response(
            200,
            json={
                "access_token": ROTATED_ACCESS_VALUE,
                "refresh_token": ROTATED_REFRESH_VALUE,
                "expires_in": 1200,
            },
        )

    def failed_save(
        _path: Path,
        _token_to_save: SaxoTokenSet,
        **_kwargs: object,
    ) -> None:
        raise OSError("injected cache save failure")

    monkeypatch.setattr(sim_token_refresh, "save_token_cache", failed_save)
    transport = httpx2.MockTransport(rotate)

    first = await refresh_sim_token_if_needed(settings, now=FIXED_NOW, transport=transport)
    second = await refresh_sim_token_if_needed(settings, now=FIXED_NOW, transport=transport)

    assert first == SimRefreshOutcome(status="cache_save_failed", network_call_made=True)
    assert second == SimRefreshOutcome(
        status="refresh_attempt_suppressed",
        network_call_made=False,
    )
    assert requests == 1
    assert load_token_cache(settings.cache_path) == original
    assert _attempt_marker(settings.cache_path).exists()


@pytest.mark.anyio
async def test_concurrent_cache_change_is_never_overwritten_by_refresh_result(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(
        settings.cache_path,
        _token(expires_at=FIXED_NOW + timedelta(minutes=1)),
    )
    concurrent = _token(
        expires_at=FIXED_NOW + timedelta(minutes=30),
        refresh_value=NEW_REFRESH_VALUE,
    )
    requests = 0

    def uncoordinated_external_write(_request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        settings.cache_path.write_text(
            f"{concurrent.model_dump_json()}\n",
            encoding="utf-8",
        )
        settings.cache_path.chmod(OWNER_FILE_MODE)
        return httpx2.Response(
            200,
            json={
                "access_token": ROTATED_ACCESS_VALUE,
                "refresh_token": ROTATED_REFRESH_VALUE,
                "expires_in": 1200,
            },
        )

    changed = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(uncoordinated_external_write),
    )
    next_run = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(
            lambda _request: pytest.fail("fresh concurrent token must not be refreshed"),
        ),
    )

    assert changed == SimRefreshOutcome(status="cache_changed", network_call_made=True)
    assert next_run == SimRefreshOutcome(status="fresh", network_call_made=False)
    assert requests == 1
    assert load_token_cache(settings.cache_path) == concurrent
    assert _attempt_marker(settings.cache_path).exists()


@pytest.mark.anyio
async def test_late_legitimate_writer_is_not_overwritten_after_revision_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path / "token.json")
    save_token_cache(
        settings.cache_path,
        _token(expires_at=FIXED_NOW + timedelta(minutes=1)),
    )
    concurrent = _token(
        expires_at=FIXED_NOW + timedelta(minutes=30),
        refresh_value=NEW_REFRESH_VALUE,
    )
    writer_started = threading.Event()
    writer_done = threading.Event()
    writer_errors: list[Exception] = []
    writer: threading.Thread | None = None
    real_save = sim_token_refresh._save_refreshed_token  # pyright: ignore[reportPrivateUsage]  # noqa: SLF001

    def write_concurrent_token() -> None:
        writer_started.set()
        try:
            save_token_cache(settings.cache_path, concurrent)
        except Exception as error:  # noqa: BLE001  # pragma: no cover
            writer_errors.append(error)
        finally:
            writer_done.set()

    def interleave_after_revision_check(
        cache_path: Path,
        refreshed: SaxoTokenSet,
        *args: object,
        **kwargs: object,
    ) -> str | None:
        nonlocal writer
        writer = threading.Thread(target=write_concurrent_token, daemon=True)
        writer.start()
        assert writer_started.wait(timeout=1)
        writer_done.wait(timeout=0.25)
        return real_save(cache_path, refreshed, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        sim_token_refresh,
        "_save_refreshed_token",
        interleave_after_revision_check,
    )

    outcome = await refresh_sim_token_if_needed(
        settings,
        now=FIXED_NOW,
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(
                200,
                json={
                    "access_token": ROTATED_ACCESS_VALUE,
                    "refresh_token": ROTATED_REFRESH_VALUE,
                    "expires_in": 1200,
                },
            ),
        ),
    )

    assert writer is not None
    writer.join(timeout=2)
    assert writer.is_alive() is False
    assert writer_errors == []
    assert outcome == SimRefreshOutcome(status="refreshed", network_call_made=True)
    assert load_token_cache(settings.cache_path) == concurrent


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
                    "access_token": "new-access-token",
                    "refresh_token": "new-refresh-token",
                    "expires_in": 1200,
                },
            ),
        ),
    )

    marker = _attempt_marker(settings.cache_path)
    assert rejected == SimRefreshOutcome(status="refresh_rejected", network_call_made=True)
    assert recovered == SimRefreshOutcome(status="refreshed", network_call_made=True)
    assert marker.exists() is False
