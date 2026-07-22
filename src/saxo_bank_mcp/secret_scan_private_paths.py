from __future__ import annotations

import re
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value

PRIVATE_PATH_PATTERN: Final = re.compile(
    r"\b(?:account[ _-]?ref|token[ _-]?cache[ _-]?path)\b"
    r"[^\r\n]{0,160}(?:/Users/|/private/|/Volumes/)",
    re.IGNORECASE,
)
PRIVATE_ROOT_PATTERN: Final = re.compile(r"(?:/Users/|/private/|/Volumes/)")
PRIVATE_FINANCIAL_VALUE_PATTERN: Final = re.compile(
    r"\b(?:estimated_cash_required|estimated_total_cost_in_account_currency)\b",
    re.IGNORECASE,
)
PRIVATE_FINANCIAL_ASSIGNMENT_PATTERN: Final = re.compile(
    r"\b(?:estimated_cash_required|estimated_total_cost_in_account_currency)\b"
    r"\s*[:=]\s*(?![\"']?<redacted>[\"']?)(?!null\b)(?!None\b)\S+",
    re.IGNORECASE,
)
_PRIVATE_PATH_KEYS: Final = frozenset({"accountref", "tokencachepath"})
_PRIVATE_FINANCIAL_KEYS: Final = frozenset(
    {
        "accrual",
        "additionaltransactioncost",
        "bondvalue",
        "cashavailablefortrading",
        "cashbalance",
        "cashblocked",
        "cashblockedfromwithdrawal",
        "cashdeposit",
        "cashreservation",
        "cashwithdrawal",
        "certificatesvalue",
        "commission",
        "estimatedcashrequired",
        "estimatedtotalcostinaccountcurrency",
        "exchangefee",
        "externalcharges",
        "financingaccruals",
        "fundsavailableforsettlement",
        "fundsreservedbyorder",
        "fundsreservedforsettlement",
        "iposubscriptionfee",
        "leveragedknockoutproductsvalue",
        "mutualfundvalue",
        "optionpremium",
        "sharevalue",
        "spendingpower",
        "stampduty",
        "transactionsnotbooked",
        "variationmargincashbalance",
        "warrantpremium",
    },
)
_SPENDING_POWER_DETAIL_KEYS: Final = frozenset({"current", "maximum"})


def private_path_finding_count(file_path: Path, text: str) -> int:
    del file_path
    try:
        value = parse_json_value(text)
    except StrictJsonError:
        return int(PRIVATE_PATH_PATTERN.search(text) is not None)
    return _unredacted_private_path_count(value)


def private_financial_value_finding_count(file_path: Path, text: str) -> int:
    try:
        value = parse_json_value(text)
    except StrictJsonError:
        if file_path.suffix == ".py":
            return 0
        return int(PRIVATE_FINANCIAL_ASSIGNMENT_PATTERN.search(text) is not None)
    return unredacted_private_financial_value_count(value)


def _unredacted_private_path_count(value: JsonValue) -> int:
    if isinstance(value, dict):
        count = 0
        for key, item in value.items():
            protected = bool(
                _normalized_private_key(key) in _PRIVATE_PATH_KEYS
                and item is not None
                and item != "<redacted>"
            )
            count += 1 if protected else _unredacted_private_path_count(item)
        return count
    if isinstance(value, list):
        return sum(_unredacted_private_path_count(item) for item in value)
    if isinstance(value, str):
        return int(value != "<redacted>" and PRIVATE_ROOT_PATTERN.search(value) is not None)
    return 0


def unredacted_private_financial_value_count(
    value: JsonValue,
    parent_key: str = "",
) -> int:
    if isinstance(value, dict):
        count = 0
        for key, item in value.items():
            normalized = _normalized_private_key(key)
            protected = normalized in _PRIVATE_FINANCIAL_KEYS or (
                parent_key == "spendingpowerdetail"
                and normalized in _SPENDING_POWER_DETAIL_KEYS
            )
            if protected and item is not None and item != "<redacted>":
                count += 1
            else:
                count += unredacted_private_financial_value_count(item, normalized)
        return count
    if isinstance(value, list):
        return sum(
            unredacted_private_financial_value_count(item, parent_key) for item in value
        )
    if isinstance(value, str):
        try:
            nested = parse_json_value(value)
        except StrictJsonError:
            return int(PRIVATE_FINANCIAL_ASSIGNMENT_PATTERN.search(value) is not None)
        return unredacted_private_financial_value_count(nested, parent_key)
    return 0


def _normalized_private_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())
