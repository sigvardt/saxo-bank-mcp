from __future__ import annotations

import re

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.secret_scan_private_paths import (
    unredacted_private_financial_value_count,
)

_UNSAFE_BOOLEAN_KEYS = frozenset({"livewritecalled", "orderorsubscriptioncreated"})


def raw_release_payload_passed(value: JsonValue) -> bool:
    if unredacted_private_financial_value_count(value) != 0:
        return False
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _UNSAFE_BOOLEAN_KEYS and item is not False:
                return False
            if not raw_release_payload_passed(item):
                return False
        return True
    if isinstance(value, list):
        return all(raw_release_payload_passed(item) for item in value)
    return True
