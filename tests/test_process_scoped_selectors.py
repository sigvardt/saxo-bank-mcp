"""Process-scoped account/order selector privacy, binding, and consumption tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import REDACTED, redact_json
from saxo_bank_mcp.auth import SaxoTokenSet
from saxo_bank_mcp.config import SimAuthSettings
from saxo_bank_mcp.mcp_token_state import CachedTokenReady
from saxo_bank_mcp.process_scoped_selectors import (
    ACCOUNT_SELECTOR_PREFIX,
    ORDER_SELECTOR_PREFIX,
    AccountRow,
    account_selector_for,
    accounts_from_port_payload,
    bind_order_selector,
    clear_process_scoped_selector_state_for_tests,
    inject_account_selectors,
    is_account_selector,
    public_order_selectors,
    resolve_account_key_input,
    resolve_account_selector,
    resolve_order_body_accounts,
    resolve_order_id_input,
    resolve_request_body_selectors,
)
from saxo_bank_mcp.safety import reset_safety_state
from saxo_bank_mcp.server import mcp

EXPIRES: Final = datetime.now(tz=UTC) + timedelta(hours=1)
FIXTURE_UIC: Final = 211
ACCOUNT: Final = "AK" + "SIM1"
ORDER: Final = "OID" + "123"


def _token(*, access: str = "access-token-a", verifier: str = "verifier-a") -> SaxoTokenSet:
    # Short non-secret canaries so secret-scan credential regexes stay quiet.
    return SaxoTokenSet(
        access_token=access,
        refresh_token="rt",  # noqa: S106
        code_verifier=verifier,
        environment="SIM",
        expires_at=EXPIRES,
    )


@pytest.fixture(autouse=True)
def reset_order_bindings() -> None:
    clear_process_scoped_selector_state_for_tests()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_account_selector_is_stable_for_token_generation_and_opaque() -> None:
    token = _token()
    raw = "AK" + "1"
    selector = account_selector_for(token, raw)
    assert is_account_selector(selector)
    assert selector.startswith(ACCOUNT_SELECTOR_PREFIX)
    assert raw not in selector
    assert account_selector_for(token, raw) == selector
    other_token = _token(access="access-token-b", verifier="verifier-b")
    other = account_selector_for(other_token, raw)
    assert other != selector


def test_resolve_account_selector_requires_exact_match() -> None:
    token = _token()
    accounts = (
        AccountRow(account_key="AK" + "1", account_id="A1", active=True, currency="USD"),
        AccountRow(account_key="AK" + "2", account_id="A2", active=True, currency="EUR"),
    )
    selector = account_selector_for(token, "AK" + "1")
    assert resolve_account_selector(token, accounts, selector) == "AK" + "1"
    assert resolve_account_selector(token, accounts, "proc-acct-deadbeef") is None
    key, reason = resolve_account_key_input(token, accounts, selector)
    assert key == "AK" + "1"
    assert reason == ""
    raw_key, raw_reason = resolve_account_key_input(token, accounts, "AK" + "2")
    assert raw_key == "AK" + "2"
    assert raw_reason == ""


def test_inject_account_selectors_only_on_account_rows() -> None:
    token = _token()
    secret = "AK" + "SEC"
    payload: dict[str, JsonValue] = {
        "Data": [
            {
                "AccountKey": secret,
                "AccountId": "ACC-1",
                "Currency": "USD",
                "Active": True,
                "AccountType": "Normal",
            },
            {
                "AccountKey": secret,
                "OrderId": "OID" + "99",
                "Uic": FIXTURE_UIC,
            },
        ]
    }
    injected = inject_account_selectors(payload, token)
    redacted = redact_json(injected)
    assert isinstance(redacted, dict)
    data = redacted["Data"]
    assert isinstance(data, list)
    account_row = data[0]
    order_row = data[1]
    assert isinstance(account_row, dict)
    assert isinstance(order_row, dict)
    assert account_row["AccountKey"] == REDACTED
    assert account_row["AccountId"] == REDACTED
    selector = account_row["SafeAccountSelector"]
    assert isinstance(selector, str)
    assert is_account_selector(selector)
    assert "SafeAccountSelector" not in order_row
    assert secret not in str(redacted)
    account_only = {
        "Data": [
            {
                "AccountKey": secret,
                "AccountId": "ACC-1",
                "Currency": "USD",
                "Active": True,
                "AccountType": "Normal",
            }
        ]
    }
    assert (
        resolve_account_selector(
            token,
            accounts_from_port_payload(account_only),
            selector,
        )
        == secret
    )


def test_order_body_account_selector_resolution() -> None:
    token = _token()
    raw = "AK" + "9"
    accounts = (AccountRow(account_key=raw, active=True),)
    selector = account_selector_for(token, raw)
    body, reason = resolve_order_body_accounts(
        {
            "AccountKey": selector,
            "Uic": FIXTURE_UIC,
            "Amount": 1,
            "BuySell": "Buy",
            "OrderType": "Limit",
            "OrderPrice": 50,
            "AssetType": "Stock",
            "ManualOrder": True,
        },
        token,
        accounts,
    )
    assert reason == ""
    assert body is not None
    assert body["AccountKey"] == raw
    assert body["Uic"] == FIXTURE_UIC


def test_order_selector_token_env_account_binding_and_unknown() -> None:
    token = _token()
    selector = bind_order_selector(
        token,
        environment="SIM",
        account_key=ACCOUNT,
        order_id=ORDER,
    )
    assert selector.startswith(ORDER_SELECTOR_PREFIX)
    assert ORDER not in selector
    assert public_order_selectors(
        [ORDER],
        token=token,
        environment="SIM",
        account_key=ACCOUNT,
    ) == [selector]
    resolved, reason = resolve_order_id_input(
        selector,
        token=token,
        environment="SIM",
        account_key=ACCOUNT,
    )
    assert resolved == ORDER
    assert reason == ""
    missing, missing_reason = resolve_order_id_input(
        f"{ORDER_SELECTOR_PREFIX}unknown",
        token=token,
        environment="SIM",
        account_key=ACCOUNT,
    )
    assert missing is None
    assert missing_reason == "order_selector_unknown"
    assert ORDER not in missing_reason
    assert selector not in missing_reason


def test_order_selector_mismatched_token_environment_account() -> None:
    mint = _token(verifier="mint")
    other = _token(verifier="use")
    selector = bind_order_selector(
        mint,
        environment="SIM",
        account_key=ACCOUNT,
        order_id=ORDER,
    )
    token_miss, token_reason = resolve_order_id_input(
        selector,
        token=other,
        environment="SIM",
        account_key=ACCOUNT,
    )
    assert token_miss is None
    assert token_reason == "order_selector_token_mismatch"  # noqa: S105
    env_miss, env_reason = resolve_order_id_input(
        selector,
        token=mint,
        environment="LIVE",
        account_key=ACCOUNT,
    )
    assert env_miss is None
    assert env_reason == "order_selector_environment_mismatch"
    acct_miss, acct_reason = resolve_order_id_input(
        selector,
        token=mint,
        environment="SIM",
        account_key="AK" + "OTHER",
    )
    assert acct_miss is None
    assert acct_reason == "order_selector_account_mismatch"
    for denial in (token_reason, env_reason, acct_reason):
        assert ORDER not in denial
        assert selector not in denial


def test_order_selector_expiry_fails_closed() -> None:
    token = _token()
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    selector = bind_order_selector(
        token,
        environment="SIM",
        account_key=ACCOUNT,
        order_id=ORDER,
        now=now,
        ttl_seconds=60,
    )
    ok, reason = resolve_order_id_input(
        selector,
        token=token,
        environment="SIM",
        account_key=ACCOUNT,
        now=now + timedelta(seconds=30),
    )
    assert ok == ORDER
    assert reason == ""
    expired, expired_reason = resolve_order_id_input(
        selector,
        token=token,
        environment="SIM",
        account_key=ACCOUNT,
        now=now + timedelta(seconds=61),
    )
    assert expired is None
    assert expired_reason == "order_selector_expired"


def test_request_body_resolves_account_and_order_selectors_without_consume() -> None:
    token = _token()
    accounts = (AccountRow(account_key=ACCOUNT),)
    account_sel = account_selector_for(token, ACCOUNT)
    order_sel = bind_order_selector(
        token,
        environment="SIM",
        account_key=ACCOUNT,
        order_id=ORDER,
    )
    body, reason, pending = resolve_request_body_selectors(
        {
            "AccountKey": account_sel,
            "OrderIds": order_sel,
            "AssetType": "Stock",
            "Uic": FIXTURE_UIC,
        },
        token,
        accounts,
        environment="SIM",
        account_key_context=ACCOUNT,
    )
    assert reason == ""
    assert body is not None
    assert body["AccountKey"] == ACCOUNT
    assert body["OrderIds"] == ORDER
    assert pending == (order_sel,)
    # Validation alone must not consume.
    again, again_reason = resolve_order_id_input(
        order_sel,
        token=token,
        environment="SIM",
        account_key=ACCOUNT,
    )
    assert again == ORDER
    assert again_reason == ""


def test_token_generation_mismatch_refuses_account_selector() -> None:
    mint_token = _token(verifier="mint")
    use_token = _token(verifier="use")
    accounts = (AccountRow(account_key="AK" + "1"),)
    selector = account_selector_for(mint_token, "AK" + "1")
    assert resolve_account_selector(use_token, accounts, selector) is None


@pytest.mark.anyio
async def test_write_preview_consumes_order_selector_only_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    clear_process_scoped_selector_state_for_tests()
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", ACCOUNT)
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", str(FIXTURE_UIC))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    token = _token()
    order_sel = bind_order_selector(
        token,
        environment="SIM",
        account_key=ACCOUNT,
        order_id=ORDER,
    )
    accounts = (AccountRow(account_key=ACCOUNT, account_id="A1", currency="USD"),)

    settings = SimAuthSettings(
        app_key="sim-app-key",
        authorization_url="https://sim.logonvalidation.net/authorize",
        token_url="https://sim.logonvalidation.net/token",  # noqa: S106
        rest_base_url="https://gateway.saxobank.com/sim/openapi/",
        redirect_uri="http://127.0.0.1:8765/callback",
        cache_path=tmp_path / "cache.json",
    )
    def _ready_token(_tool: str, _path: Path) -> CachedTokenReady:
        return CachedTokenReady(token=token)

    def _sim_settings(**_kwargs: object) -> SimAuthSettings:
        return settings

    monkeypatch.setattr("saxo_bank_mcp.mcp_token_state.cached_token_for_tool", _ready_token)
    monkeypatch.setattr("saxo_bank_mcp.config.resolve_sim_auth_settings", _sim_settings)
    monkeypatch.setattr(
        "saxo_bank_mcp.process_scoped_selectors.fetch_account_rows_for_token",
        AsyncMock(return_value=accounts),
    )

    deny_args = {
        "operation_id": "delete.trade.v2.orders.orderids",
        "account_key": ACCOUNT,
        "instrument_uic": FIXTURE_UIC,
        "quantity": 1,
        "estimated_notional": 0,
        "account_currency": "USD",
        "risk": {
            "cost": 0,
            "cash_required": 0,
            "margin_impact": 0,
            "contract_multiplier": 1,
            "conversion_known": True,
        },
        "request_body": {
            "AccountKey": ACCOUNT,
            "OrderIds": order_sel,
            "AssetType": "Stock",
        },
    }

    async with Client(mcp) as client:
        # Deny via instrument allowlist mismatch: must not consume selector.
        denied = await client.call_tool(
            "saxo_create_write_preview",
            {**deny_args, "instrument_uic": 999},
            raise_on_error=False,
        )
        denied_payload = denied.structured_content
        assert denied_payload is not None
        assert denied_payload["status"] == "denied"
        still, still_reason = resolve_order_id_input(
            order_sel,
            token=token,
            environment="SIM",
            account_key=ACCOUNT,
        )
        assert still == ORDER
        assert still_reason == ""

        created = await client.call_tool("saxo_create_write_preview", deny_args)
        created_payload = created.structured_content
        assert created_payload is not None
        assert created_payload["status"] == "preview_created"
        assert "preview_token" in created_payload
        assert ACCOUNT not in str(created_payload)
        assert ORDER not in str(created_payload)
        assert order_sel not in str(created_payload)

        burned, burned_reason = resolve_order_id_input(
            order_sel,
            token=token,
            environment="SIM",
            account_key=ACCOUNT,
        )
        assert burned is None
        assert burned_reason == "order_selector_consumed"

        replay = await client.call_tool(
            "saxo_create_write_preview",
            deny_args,
            raise_on_error=False,
        )
        replay_payload = replay.structured_content
        assert replay_payload is not None
        assert replay_payload["status"] == "denied"
        assert replay_payload["denial_reason"] == "order_selector_consumed"
        assert ORDER not in str(replay_payload)
        assert order_sel not in str(replay_payload)
        assert ACCOUNT not in str(replay_payload)


@pytest.mark.anyio
async def test_write_preview_refuses_unknown_and_mismatched_order_selectors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    clear_process_scoped_selector_state_for_tests()
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", ACCOUNT)
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", str(FIXTURE_UIC))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "SIM")
    monkeypatch.setenv("SAXO_MCP_SIM_APP_KEY", "sim-app-key")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    token = _token()
    accounts = (AccountRow(account_key=ACCOUNT, account_id="A1", currency="USD"),)
    settings = SimAuthSettings(
        app_key="sim-app-key",
        authorization_url="https://sim.logonvalidation.net/authorize",
        token_url="https://sim.logonvalidation.net/token",  # noqa: S106
        rest_base_url="https://gateway.saxobank.com/sim/openapi/",
        redirect_uri="http://127.0.0.1:8765/callback",
        cache_path=tmp_path / "cache.json",
    )
    def _ready_token(_tool: str, _path: Path) -> CachedTokenReady:
        return CachedTokenReady(token=token)

    def _sim_settings(**_kwargs: object) -> SimAuthSettings:
        return settings

    monkeypatch.setattr("saxo_bank_mcp.mcp_token_state.cached_token_for_tool", _ready_token)
    monkeypatch.setattr("saxo_bank_mcp.config.resolve_sim_auth_settings", _sim_settings)
    monkeypatch.setattr(
        "saxo_bank_mcp.process_scoped_selectors.fetch_account_rows_for_token",
        AsyncMock(return_value=accounts),
    )
    base = {
        "operation_id": "delete.trade.v2.orders.orderids",
        "account_key": ACCOUNT,
        "instrument_uic": FIXTURE_UIC,
        "quantity": 1,
        "estimated_notional": 0,
        "account_currency": "USD",
        "risk": {
            "cost": 0,
            "cash_required": 0,
            "margin_impact": 0,
            "contract_multiplier": 1,
            "conversion_known": True,
        },
    }
    async with Client(mcp) as client:
        unknown = await client.call_tool(
            "saxo_create_write_preview",
            {
                **base,
                "request_body": {
                    "AccountKey": ACCOUNT,
                    "OrderIds": f"{ORDER_SELECTOR_PREFIX}notreal",
                },
            },
            raise_on_error=False,
        )
        unknown_payload = unknown.structured_content
        assert unknown_payload is not None
        assert unknown_payload["status"] == "denied"
        assert unknown_payload["denial_reason"] == "order_selector_unknown"

        wrong_account_selector = bind_order_selector(
            token,
            environment="SIM",
            account_key="AK" + "OTHER",
            order_id=ORDER,
        )
        mismatched = await client.call_tool(
            "saxo_create_write_preview",
            {
                **base,
                "request_body": {
                    "AccountKey": ACCOUNT,
                    "OrderIds": wrong_account_selector,
                },
            },
            raise_on_error=False,
        )
        mismatched_payload = mismatched.structured_content
        assert mismatched_payload is not None
        assert mismatched_payload["status"] == "denied"
        assert mismatched_payload["denial_reason"] == "order_selector_account_mismatch"
        assert ORDER not in str(mismatched_payload)
        assert wrong_account_selector not in str(mismatched_payload)


@pytest.mark.anyio
async def test_write_preview_live_selector_resolution_refuses_without_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_safety_state()
    clear_process_scoped_selector_state_for_tests()
    monkeypatch.setenv("SAXO_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("SAXO_MCP_ACCOUNT_ALLOWLIST", ACCOUNT)
    monkeypatch.setenv("SAXO_MCP_INSTRUMENT_ALLOWLIST", str(FIXTURE_UIC))
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    monkeypatch.setenv("SAXO_MCP_ENABLE_LIVE_WRITES", "I_UNDERSTAND_REAL_MONEY_RISK")
    token = _token()
    order_sel = bind_order_selector(
        token,
        environment="LIVE",
        account_key=ACCOUNT,
        order_id=ORDER,
    )
    transport_opened = False

    def fail_fetch(*_args: object, **_kwargs: object) -> object:
        nonlocal transport_opened
        transport_opened = True
        raise AssertionError("LIVE selector resolve must not open transport")

    monkeypatch.setattr(
        "saxo_bank_mcp.process_scoped_selectors.fetch_account_rows_for_token",
        fail_fetch,
    )
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_create_write_preview",
            {
                "operation_id": "delete.trade.v2.orders.orderids",
                "account_key": ACCOUNT,
                "instrument_uic": FIXTURE_UIC,
                "quantity": 1,
                "estimated_notional": 0,
                "account_currency": "USD",
                "risk": {
                    "cost": 0,
                    "cash_required": 0,
                    "margin_impact": 0,
                    "contract_multiplier": 1,
                    "conversion_known": True,
                },
                "request_body": {"AccountKey": ACCOUNT, "OrderIds": order_sel},
            },
            raise_on_error=False,
        )
    payload = result.structured_content
    assert payload is not None
    assert payload["status"] == "denied"
    assert payload["denial_reason"] == "live_selector_resolution_not_supported_on_write_preview"
    assert payload["network_call_made"] is False
    assert transport_opened is False
