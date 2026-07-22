from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx2
import pytest
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import LIVE_ENDPOINTS
from saxo_bank_mcp.http_client import create_async_client
from saxo_bank_mcp.live_precheck_proof import main
from saxo_bank_mcp.token_cache import save_token_cache

RAW_ACCOUNT = "raw-account-identifier-7654321"
RAW_ORDER = "raw-order-identifier-7654321"
RAW_POSITION = "raw-position-identifier-7654321"
RAW_MESSAGE = "raw-message-identifier-7654321"
RAW_BALANCE = 987654.32
ACCESS_TOKEN = "mocked-access-token"  # noqa: S105
EXPECTED_MAX_CONNECTIONS = 200
EXPECTED_TOKEN_VALIDITY_SECONDS = 210
EXPECTED_SHA256_LENGTH = 64
MULTIPLE_OAUTH_POSTS = 2
JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
JSON_ROWS_ADAPTER: TypeAdapter[list[dict[str, JsonValue]]] = TypeAdapter(
    list[dict[str, JsonValue]],
)


@dataclass(frozen=True, slots=True)
class TransportScenario:
    account_count: int = 1
    instrument_tradable: bool = True
    inject_extra_post: bool = False
    inject_other_host_get: bool = False
    inject_oauth_posts: int = 0
    orders_top_level_list: bool = False
    messages_envelope: bool = False
    duplicate_order_count: bool = False
    nonzero_state: bool = False
    mutate_after_precheck: Literal["orders", "positions", "trade_messages"] | None = None


DEFAULT_SCENARIO = TransportScenario()


def configure_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_path = tmp_path / "live-token.json"
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache_path))
    save_token_cache(
        cache_path,
        SaxoTokenSet(
            access_token=ACCESS_TOKEN,
            refresh_token="mocked-refresh-token",  # noqa: S106
            code_verifier="proof-code-verifier",
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )


def accounts_body(count: int) -> dict[str, list[dict[str, str | bool]]]:
    return {
        "Data": [
            {
                "AccountId": f"DISPLAY-{index}",
                "AccountKey": f"{RAW_ACCOUNT}-{index}",
                "ClientKey": "fixture-client-key",
                "Active": True,
                "Currency": "EUR",
                "AccountType": "Normal",
            }
            for index in range(1, count + 1)
        ],
    }


def response_for(
    request: httpx2.Request,
    scenario: TransportScenario,
    mutated_collection: str | None,
) -> httpx2.Response:
    path = request.url.path
    if path == "/openapi/port/v1/accounts/me":
        body = accounts_body(scenario.account_count)
    elif path == "/openapi/ref/v1/instruments/details/30031/Stock":
        body = {
            "AssetType": "Stock",
            "IsTradable": scenario.instrument_tradable,
            "TradingStatus": "Tradable" if scenario.instrument_tradable else "NotDefined",
            "Uic": 30031,
        }
    elif path == "/openapi/port/v1/orders/me":
        if scenario.duplicate_order_count:
            return httpx2.Response(
                200,
                content=b'{"Data":[],"__count":1,"__count":0}',
                headers={"Content-Type": "application/json"},
                request=request,
            )
        order_id = f"{RAW_ORDER}-changed" if mutated_collection == "orders" else RAW_ORDER
        rows = [{"OrderId": order_id}] if scenario.nonzero_state else []
        body = (
            rows
            if scenario.orders_top_level_list
            else {"Data": rows, "__count": int(scenario.nonzero_state)}
        )
    elif path == "/openapi/port/v1/positions/me":
        position_id = (
            f"{RAW_POSITION}-changed" if mutated_collection == "positions" else RAW_POSITION
        )
        body = {
            "Data": [{"PositionId": position_id}] if scenario.nonzero_state else [],
            "__count": int(scenario.nonzero_state),
        }
    elif path == "/openapi/port/v1/balances/me":
        body = {
            "CashAvailableForTrading": RAW_BALANCE,
            "CashBalance": RAW_BALANCE,
            "Currency": "EUR",
            "FundsAvailableForSettlement": RAW_BALANCE,
            "FundsReservedForSettlement": 0,
            "TransactionsNotBooked": 0,
        }
    elif path == "/openapi/trade/v1/messages":
        message_id = (
            f"{RAW_MESSAGE}-changed" if mutated_collection == "trade_messages" else RAW_MESSAGE
        )
        message_rows = [{"MessageId": message_id}]
        body = {"Data": message_rows} if scenario.messages_envelope else message_rows
    elif path == "/openapi/trade/v2/orders/precheck":
        body = {
            "PreCheckResult": "Ok",
            "EstimatedCashRequired": 10.5,
            "EstimatedCashRequiredCurrency": "EUR",
            "EstimatedTotalCostInAccountCurrency": 10.75,
        }
    elif path == "/openapi/trade/v2/orders":
        body = {}
    else:
        pytest.fail(f"unexpected request path: {path}")
    return httpx2.Response(200, json=body, request=request)


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    scenario: TransportScenario,
) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []
    extra_post_sent = False
    other_host_get_sent = False
    oauth_posts_sent = False
    precheck_seen = False

    async def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal extra_post_sent, oauth_posts_sent, other_host_get_sent, precheck_seen
        requests.append((request.method, request.url.path))
        if (
            scenario.inject_extra_post
            and not extra_post_sent
            and request.url.path == "/openapi/port/v1/orders/me"
        ):
            extra_post_sent = True
            async with create_async_client(base_url=LIVE_ENDPOINTS.rest_base_url) as client:
                await client.post("trade/v2/orders", json={"AccountKey": RAW_ACCOUNT})
        if (
            scenario.inject_other_host_get
            and not other_host_get_sent
            and request.url.path == "/openapi/port/v1/orders/me"
        ):
            other_host_get_sent = True
            async with create_async_client(base_url="https://unexpected.invalid") as client:
                await client.get("health")
        if request.url.host == "unexpected.invalid":
            return httpx2.Response(200, json={}, request=request)
        if (
            scenario.inject_oauth_posts
            and not oauth_posts_sent
            and request.url.path == "/openapi/port/v1/orders/me"
        ):
            oauth_posts_sent = True
            async with create_async_client(
                base_url="https://live.logonvalidation.net",
            ) as client:
                for _ in range(scenario.inject_oauth_posts):
                    await client.post("token")
        if request.url.host == "live.logonvalidation.net":
            return httpx2.Response(200, json={}, request=request)
        if request.url.path == "/openapi/trade/v2/orders/precheck":
            precheck_seen = True
        mutated_collection = scenario.mutate_after_precheck if precheck_seen else None
        return response_for(
            request,
            scenario,
            mutated_collection,
        )

    transport = httpx2.MockTransport(handler)

    def transport_factory(
        *,
        http2: bool,
        retries: int,
        limits: httpx2.Limits,
        socket_options: list[tuple[int, int, int]],
    ) -> httpx2.AsyncBaseTransport:
        assert http2 is True
        assert retries in {0, 3}
        assert limits.max_connections == EXPECTED_MAX_CONNECTIONS
        assert socket_options
        return transport

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport_factory)
    return requests


def run_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: TransportScenario = DEFAULT_SCENARIO,
) -> tuple[int, dict[str, JsonValue], list[tuple[str, str]]]:
    configure_live(tmp_path, monkeypatch)
    requests = install_transport(monkeypatch, scenario)
    out = tmp_path / "proof.json"
    result = main(
        [
            "--allow-live",
            "--out",
            str(out),
            "--uic",
            "30031",
            "--asset-type",
            "Stock",
            "--amount",
            "1",
            "--buy-sell",
            "Buy",
        ],
    )
    report = JSON_OBJECT_ADAPTER.validate_json(out.read_text(encoding="utf-8"))
    return result, report, requests
