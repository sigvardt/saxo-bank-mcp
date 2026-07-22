from __future__ import annotations

import io
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import httpx2
import pytest
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.http_client import create_async_client as real_create_async_client
from saxo_bank_mcp.token_cache import save_token_cache

JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])
LIVE_ACCOUNTS_TOOL = "saxo_list_live_accounts"
LIVE_PRECHECK_TOOL = "saxo_precheck_live_order"
FIXTURE_ACCOUNT_KEY = "FIXTURE_ACCOUNT"
FIXTURE_CLIENT_KEY = "FIXTURE_CLIENT"
AMBIGUOUS_ACCOUNT_COUNT = 2
NESTED_QUALIFIER_RESPONSES: tuple[dict[str, JsonValue], ...] = (
    {"PreCheckResult": "Ok", "Orders": [{"PreCheckResult": "Rejected"}]},
    {"PreCheckResult": "Ok", "Orders": [{}]},
    {"PreCheckResult": "Ok", "Cost": {"ErrorCode": "synthetic"}},
    {"PreCheckResult": "Ok", "Cost": {"Disclaimer": {}}},
    {"PreCheckResult": "Ok", "Cost": {"Order_Identifier": "synthetic"}},
    {
        "PreCheckResult": "Ok",
        "PreTradeDisclaimers": {"DisclaimerTokens": []},
    },
)
FASTMCP_LOGGER_NAME: Final = "fastmcp"
FASTMCP_OPERATIONS_LOGGER_NAME: Final = "fastmcp.server.mixins.mcp_operations"


def deeply_nested_cost_payload() -> bytes:
    depth = 10_000
    return b'{"PreCheckResult":"Ok","Cost":{"X":' + (b"[" * depth) + b"0" + (b"]" * depth) + b"}}"


def oversized_integer_cost_payload() -> bytes:
    return b'{"PreCheckResult":"Ok","Cost":{"X":' + (b"9" * 5_000) + b"}}"


DECODER_LIMIT_PAYLOADS: tuple[Callable[[], bytes], ...] = (
    deeply_nested_cost_payload,
    oversized_integer_cost_payload,
)

@dataclass(frozen=True, slots=True)
class HttpFailureCase:
    http_status: int
    headers: dict[str, str]
    expected_status: str
    retry_seconds: float | None
    retry_known: bool


def order_payload(account_ref: str | None = None) -> dict[str, JsonValue]:
    return {
        "account_ref": account_ref,
        "uic": 30031,
        "asset_type": "Stock",
        "amount": 1,
        "buy_sell": "Buy",
    }


def invalid_arguments(
    case: str,
    sensitive_marker: str,
) -> dict[str, JsonValue]:
    if case == "nested_order":
        payload_order = order_payload()
        payload_order["amount"] = "1"
        payload_order["account_key"] = sensitive_marker
        return {"order": payload_order}
    if case == "scalar_order":
        return {"order": sensitive_marker}
    if case == "unexpected_top_level":
        return {"unexpected": sensitive_marker}
    raise AssertionError(case)


@contextmanager
def capture_fastmcp_debug() -> Generator[Callable[[], str]]:
    parent_logger = logging.getLogger(FASTMCP_LOGGER_NAME)
    operations_logger = logging.getLogger(FASTMCP_OPERATIONS_LOGGER_NAME)
    original_parent_level = parent_logger.level
    original_operations_level = operations_logger.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    parent_logger.addHandler(handler)
    parent_logger.setLevel(logging.DEBUG)
    operations_logger.setLevel(logging.DEBUG)
    try:
        yield stream.getvalue
    finally:
        operations_logger.setLevel(original_operations_level)
        parent_logger.setLevel(original_parent_level)
        parent_logger.removeHandler(handler)
        handler.close()


def configure_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = tmp_path / "live-token.json"
    save_token_cache(
        cache,
        SaxoTokenSet(
            access_token="live-access-token",  # noqa: S106
            environment="LIVE",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        ),
    )
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_READS", "1")
    monkeypatch.setenv("SAXO_MCP_LIVE_APP_KEY", "live-app-key")
    monkeypatch.setenv("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", str(cache))


def accounts_body(*, count: int = 1) -> dict[str, JsonValue]:
    return {
        "Data": [
            {
                "AccountId": f"DISPLAY-{index}",
                "AccountKey": f"{FIXTURE_ACCOUNT_KEY}_{index}",
                "ClientKey": FIXTURE_CLIENT_KEY,
                "Active": True,
                "Currency": "EUR",
                "DisplayName": f"Account {index}",
                "AccountType": "Normal",
            }
            for index in range(1, count + 1)
        ],
    }


def instrument_body(*, tradable: bool = True) -> dict[str, JsonValue]:
    return {
        "AssetType": "Stock",
        "IsTradable": tradable,
        "Uic": 30031,
    }


def read_body(request: httpx2.Request) -> dict[str, JsonValue]:
    if request.url.path.endswith("/ref/v1/instruments/details/30031/Stock"):
        return instrument_body()
    return accounts_body()


def install_transport(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    mock_transport = httpx2.MockTransport(handler)

    def create_client(
        *,
        base_url: str = "",
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> httpx2.AsyncClient:
        selected_transport = mock_transport if transport is None else transport
        return real_create_async_client(base_url=base_url, transport=selected_transport)

    monkeypatch.setattr(
        "saxo_bank_mcp.mcp_live_account_tools.create_async_client",
        create_client,
    )
    monkeypatch.setattr(
        "saxo_bank_mcp.mcp_live_trade_tools.create_async_client",
        create_client,
    )
