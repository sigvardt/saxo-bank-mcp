from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.read_fingerprints import AccountMoneyState, response_fingerprint

EXPECTED_SHA256_LENGTH = 64


@pytest.mark.parametrize(
    "alias",
    [
        "CashBlocked",
        "CashBlockedFromWithdrawal",
        "FinancingAccruals",
        "SpendingPower",
        "VariationMarginCashBalance",
    ],
)
def test_money_state_fingerprint_changes_when_optional_money_field_changes(
    alias: str,
) -> None:
    base = _balance_body({alias: 1.25})
    changed = _balance_body({alias: 1.5})

    base_digest, base_scope = _fingerprint(base)
    changed_digest, changed_scope = _fingerprint(changed)

    assert base_scope == "account_money_state_fields"
    assert changed_scope == "account_money_state_fields"
    assert base_digest != changed_digest


@pytest.mark.parametrize(
    "alias",
    [
        "Accrual",
        "AdditionalTransactionCost",
        "BondValue",
        "CashDeposit",
        "CashReservation",
        "CashWithdrawal",
        "CertificatesValue",
        "Commission",
        "ExchangeFee",
        "ExternalCharges",
        "FundsReservedByOrder",
        "IpoSubscriptionFee",
        "LeveragedKnockOutProductsValue",
        "MutualFundValue",
        "OptionPremium",
        "ShareValue",
        "StampDuty",
        "WarrantPremium",
    ],
)
def test_money_state_fingerprint_changes_when_nested_detail_field_changes(
    alias: str,
) -> None:
    base = _balance_body({"TransactionsNotBookedDetail": {alias: 1.0}})
    changed = _balance_body({"TransactionsNotBookedDetail": {alias: 2.0}})

    base_digest, base_scope = _fingerprint(base)
    changed_digest, changed_scope = _fingerprint(changed)

    assert base_scope == "account_money_state_fields"
    assert changed_scope == "account_money_state_fields"
    assert base_digest != changed_digest


def test_money_state_rejects_non_finite_declared_nested_field() -> None:
    body = _balance_body({"TransactionsNotBookedDetail": {"Accrual": float("inf")}})

    with pytest.raises(ValidationError):
        AccountMoneyState.model_validate(body)


def test_money_state_accepts_live_shape_without_cash_available_for_trading() -> None:
    body = _balance_body(
        {
            "SpendingPower": 100.0,
            "SpendingPowerDetail": {"Current": 100.0},
        },
    )
    del body["CashAvailableForTrading"]

    digest, scope = _fingerprint(body)

    assert len(digest) == EXPECTED_SHA256_LENGTH
    assert scope == "account_money_state_fields"


def test_money_state_fingerprint_changes_with_spending_power_detail() -> None:
    base = _balance_body({"SpendingPowerDetail": {"Current": 100.0}})
    changed = _balance_body({"SpendingPowerDetail": {"Current": 99.0}})

    base_digest, _ = _fingerprint(base)
    changed_digest, _ = _fingerprint(changed)

    assert base_digest != changed_digest


def test_money_state_fingerprint_is_stable_within_process() -> None:
    body = _balance_body({"SpendingPower": 100.0})

    first_digest, _ = _fingerprint(body)
    second_digest, _ = _fingerprint(body)

    assert first_digest == second_digest


def test_margin_overview_uses_keyed_money_state_fingerprint() -> None:
    body = _balance_body({"SpendingPower": 100.0})

    digest, scope = response_fingerprint(
        "get.port.v1.balances.marginoverview",
        json.dumps(body, allow_nan=False).encode(),
    )

    assert len(digest) == EXPECTED_SHA256_LENGTH
    assert scope == "account_money_state_fields"


def test_money_state_fingerprint_differs_in_fresh_process() -> None:
    body = _balance_body({"SpendingPower": 100.0})
    local_digest, _ = _fingerprint(body)
    child_code = (
        "import json\n"
        "from saxo_bank_mcp.read_fingerprints import response_fingerprint\n"
        "body = {\n"
        "'CashAvailableForTrading': 100.0,\n"
        "'CashBalance': 100.0,\n"
        "'Currency': 'EUR',\n"
        "'FundsAvailableForSettlement': 100.0,\n"
        "'FundsReservedForSettlement': 0.0,\n"
        "'SpendingPower': 100.0,\n"
        "'TransactionsNotBooked': 0.0,\n"
        "}\n"
        "print(response_fingerprint(\n"
        "'get.port.v1.balances.me',\n"
        "json.dumps(body, allow_nan=False).encode(),\n"
        ")[0])\n"
    )

    child = subprocess.run(
        [sys.executable, "-c", child_code],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert child.stdout.strip() != local_digest


def _fingerprint(body: Mapping[str, JsonValue]) -> tuple[str, str]:
    return response_fingerprint(
        "get.port.v1.balances.me",
        json.dumps(body, allow_nan=False).encode(),
    )


def _balance_body(extra: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "CashAvailableForTrading": 100.0,
        "CashBalance": 100.0,
        "Currency": "EUR",
        "FundsAvailableForSettlement": 100.0,
        "FundsReservedForSettlement": 0.0,
        "TransactionsNotBooked": 0.0,
    }
    body.update(extra)
    return body
