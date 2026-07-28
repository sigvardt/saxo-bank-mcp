"""Process-scoped account/order selector privacy and binding tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final

import pytest

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import REDACTED, redact_json
from saxo_bank_mcp.auth import SaxoTokenSet
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

EXPIRES: Final = datetime.now(tz=UTC) + timedelta(hours=1)
FIXTURE_UIC: Final = 211


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


def test_inject_account_selectors_survives_redaction_without_raw_key() -> None:
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
            }
        ]
    }
    injected = inject_account_selectors(payload, token)
    redacted = redact_json(injected)
    assert isinstance(redacted, dict)
    data = redacted["Data"]
    assert isinstance(data, list)
    row = data[0]
    assert isinstance(row, dict)
    assert row["AccountKey"] == REDACTED
    assert row["AccountId"] == REDACTED
    selector = row["SafeAccountSelector"]
    assert isinstance(selector, str)
    assert is_account_selector(selector)
    assert secret not in str(redacted)
    assert (
        resolve_account_selector(
            token,
            accounts_from_port_payload(payload),
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


def test_order_selector_bind_resolve_and_reject_unknown() -> None:
    order = "OID" + "123"
    selector = bind_order_selector(order)
    assert selector.startswith(ORDER_SELECTOR_PREFIX)
    assert order not in selector
    assert public_order_selectors([order]) == [selector]
    resolved, reason = resolve_order_id_input(selector)
    assert resolved == order
    assert reason == ""
    missing, missing_reason = resolve_order_id_input(f"{ORDER_SELECTOR_PREFIX}unknown")
    assert missing is None
    assert missing_reason == "order_selector_invalid"


def test_request_body_resolves_account_and_order_selectors() -> None:
    token = _token()
    raw = "AK" + "C"
    accounts = (AccountRow(account_key=raw),)
    account_sel = account_selector_for(token, raw)
    order = "OID" + "77"
    order_sel = bind_order_selector(order)
    body, reason = resolve_request_body_selectors(
        {
            "AccountKey": account_sel,
            "OrderIds": order_sel,
            "AssetType": "Stock",
            "Uic": FIXTURE_UIC,
        },
        token,
        accounts,
    )
    assert reason == ""
    assert body is not None
    assert body["AccountKey"] == raw
    assert body["OrderIds"] == order


def test_token_generation_mismatch_refuses_account_selector() -> None:
    mint_token = _token(verifier="mint")
    use_token = _token(verifier="use")
    accounts = (AccountRow(account_key="AK" + "1"),)
    selector = account_selector_for(mint_token, "AK" + "1")
    assert resolve_account_selector(use_token, accounts, selector) is None
