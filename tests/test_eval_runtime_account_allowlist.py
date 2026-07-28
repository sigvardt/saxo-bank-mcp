"""Eval runtime SIM account allowlist discovery and case instrument binding."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from saxo_bank_mcp.agent_skill_matrix_env import (
    SIM_ORDER_LIFECYCLE_CASE_ID,
    SIM_ORDER_LIFECYCLE_INSTRUMENT_UIC,
    MatrixEnvError,
    active_account_keys_from_port_payload,
    apply_case_eval_allowlists,
    discover_exactly_one_active_sim_account,
)
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.token_cache import save_token_cache

ACTIVE_A = "SIM" + "ACCTA"
ACTIVE_B = "SIM" + "ACCTB"
INACTIVE = "SIM" + "INACT"


def _token() -> SaxoTokenSet:
    # Short non-secret canaries keep secret-scan credential regexes quiet.
    return SaxoTokenSet(
        access_token="atok-eval-1",  # noqa: S106
        refresh_token="rtok-eval-1",  # noqa: S106
        code_verifier="cver-eval-1",
        environment="SIM",
        expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
    )


def test_active_account_keys_skip_inactive_and_require_data_rows() -> None:
    keys = active_account_keys_from_port_payload(
        {
            "Data": [
                {"AccountKey": INACTIVE, "Active": False},
                {"AccountKey": ACTIVE_A, "Active": True},
            ]
        }
    )
    assert keys == [ACTIVE_A]
    assert active_account_keys_from_port_payload({"Data": []}) == []
    assert active_account_keys_from_port_payload("bad") == []


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: object,
) -> None:
    real_client = httpx2.Client

    def factory(**kwargs: object) -> httpx2.Client:
        kwargs = dict(kwargs)
        kwargs["transport"] = httpx2.MockTransport(handler)  # type: ignore[arg-type]
        return real_client(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("saxo_bank_mcp.agent_skill_matrix_env.httpx2.Client", factory)


def test_discover_exactly_one_active_sim_account_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "token-cache.json"
    save_token_cache(cache, _token())

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith("/port/v1/accounts/me")
        return httpx2.Response(
            200,
            json={
                "Data": [
                    {"AccountKey": INACTIVE, "Active": False},
                    {"AccountKey": ACTIVE_A, "Active": True},
                ]
            },
            request=request,
        )

    _patch_client(monkeypatch, handler)
    key = discover_exactly_one_active_sim_account(cache)
    assert key == ACTIVE_A


def test_discover_fails_closed_on_zero_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "token-cache.json"
    save_token_cache(cache, _token())

    def zero_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"Data": []}, request=request)

    _patch_client(monkeypatch, zero_handler)
    with pytest.raises(MatrixEnvError, match="eval_account_discovery_zero_active"):
        discover_exactly_one_active_sim_account(cache)


def test_discover_fails_closed_on_multiple_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "token-cache.json"
    save_token_cache(cache, _token())

    def multi_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={
                "Data": [
                    {"AccountKey": ACTIVE_A, "Active": True},
                    {"AccountKey": ACTIVE_B, "Active": True},
                ]
            },
            request=request,
        )

    _patch_client(monkeypatch, multi_handler)
    with pytest.raises(MatrixEnvError, match="eval_account_discovery_multiple_active"):
        discover_exactly_one_active_sim_account(cache)


def test_discover_refuses_live_token(tmp_path: Path) -> None:
    cache = tmp_path / "token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-atok-1",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        ),
    )
    with pytest.raises(MatrixEnvError, match="eval_account_discovery_live_token_refused"):
        discover_exactly_one_active_sim_account(cache)


def test_apply_case_eval_allowlists_binds_lifecycle_uic_only() -> None:
    base = {"SAXO_MCP_ENVIRONMENT": "SIM", "SAXO_MCP_ACCOUNT_ALLOWLIST": ACTIVE_A}
    lifecycle = apply_case_eval_allowlists(base, case_id=SIM_ORDER_LIFECYCLE_CASE_ID)
    assert lifecycle["SAXO_MCP_INSTRUMENT_ALLOWLIST"] == SIM_ORDER_LIFECYCLE_INSTRUMENT_UIC
    assert lifecycle["SAXO_MCP_ACCOUNT_ALLOWLIST"] == ACTIVE_A
    other = apply_case_eval_allowlists(base, case_id="auth-recovery")
    assert "SAXO_MCP_INSTRUMENT_ALLOWLIST" not in other



