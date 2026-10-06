from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self

import httpx2
import pytest
from fastmcp import Client

from saxo_bank_mcp.analytics_source_contracts import (
    compare_source_schema,
    source_contracts_by_id,
)
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.server import mcp
from saxo_bank_mcp.token_cache import save_token_cache

EXPECTED_SHA256_LENGTH = 64
RAW_BALANCE = 123456.78
_BALANCE_RESPONSE_FIXTURE = {
    "CashAvailableForTrading": RAW_BALANCE,
    "CashBalance": RAW_BALANCE,
    "Currency": "EUR",
    "FundsAvailableForSettlement": RAW_BALANCE,
    "FundsReservedForSettlement": 0,
    "TransactionsNotBooked": 0,
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_analytics_balance_contract_accepts_registered_read_fixture() -> None:
    comparison = compare_source_schema(
        source_contracts_by_id()["balances_v1"],
        _BALANCE_RESPONSE_FIXTURE,
    )

    assert comparison.compatible is True
    assert comparison.additive_fields == ()
    assert comparison.structural_errors == ()


@pytest.mark.anyio
async def test_fingerprint_only_live_read_hides_balance_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "live-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-access-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))

    def create_live_client(**_kwargs: str) -> _BalanceLiveClient:
        return _BalanceLiveClient()

    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", create_live_client)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": "/port/v1/balances/me",
                "response_mode": "fingerprint_only",
            },
        )

    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "passed"
    assert payload["response"] is None
    assert payload["response_visibility"] == "fingerprint_only"
    assert len(str(payload["response_fingerprint"])) == EXPECTED_SHA256_LENGTH
    assert payload["response_fingerprint_scope"] == "account_money_state_fields"
    assert str(RAW_BALANCE) not in str(payload)


@pytest.mark.anyio
async def test_default_live_balance_read_returns_amounts_and_redacts_identifiers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "live-token-cache.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-access-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))

    def create_live_client(**_kwargs: str) -> _PrivateBalanceLiveClient:
        return _PrivateBalanceLiveClient()

    monkeypatch.setattr("saxo_bank_mcp.read_tools.create_async_client", create_live_client)

    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {
                "method": "GET",
                "path": "/port/v1/balances/me",
            },
        )

    payload = result.structured_content
    assert payload is not None
    assert result.is_error is False
    assert payload["status"] == "passed"
    assert payload["response_visibility"] == "redacted_body"
    body = json.loads(str(payload["response"]))
    assert body["CashBalance"] == RAW_BALANCE
    assert body["CashAvailableForTrading"] == RAW_BALANCE
    assert body["Currency"] == "EUR"
    for field in ("AccountId", "AccountKey", "ClientKey", "AccessToken", "Authorization"):
        assert body[field] == "<redacted>"
    assert "private-account-key" not in str(payload)
    assert "live-access-token" not in str(payload)
    assert payload["response_fingerprint_scope"] == "account_money_state_fields"
    assert payload["live_write_called"] is False
    assert payload["order_or_subscription_created"] is False


class _BalanceLiveClient:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        assert path == "port/v1/balances/me"
        assert params == {}
        assert headers["Authorization"] == "Bearer live-access-token"
        return httpx2.Response(
            200,
            json=_BALANCE_RESPONSE_FIXTURE,
            request=httpx2.Request(
                "GET",
                "https://gateway.saxobank.com/openapi/port/v1/balances/me",
            ),
        )


class _PrivateBalanceLiveClient(_BalanceLiveClient):
    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        headers: Mapping[str, str],
    ) -> httpx2.Response:
        response = await super().get(path, params=params, headers=headers)
        return httpx2.Response(
            200,
            json={
                **_BALANCE_RESPONSE_FIXTURE,
                "AccountId": "private-account-id",
                "AccountKey": "ak",
                "ClientKey": "ck",
                "AccessToken": "live-access-token",
                "Authorization": "Bearer live-access-token",
            },
            request=response.request,
        )
