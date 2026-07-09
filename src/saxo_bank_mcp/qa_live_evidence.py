from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import (
    REDACTED,
    REDACTED_EMAIL,
    REDACTED_PERSON,
    redact_json,
)

OMITTED_LIVE_RESPONSE: Final = "<omitted-from-live-evidence>"
_PRIVATE_IDENTIFIER_KEYS: Final = frozenset(
    {
        "accountgroupid",
        "accountgroupkey",
        "accountgroupname",
        "accountid",
        "accountkey",
        "accountnumber",
        "clientid",
        "clientkey",
        "displayname",
        "externalreference",
        "userid",
        "userkey",
    },
)
_SAFE_REDACTED_VALUES: Final = frozenset({REDACTED, REDACTED_EMAIL, REDACTED_PERSON})


def sanitize_live_read_payloads(
    payloads: Mapping[str, Mapping[str, JsonValue]],
) -> dict[str, dict[str, JsonValue]]:
    sanitized: dict[str, dict[str, JsonValue]] = {}
    for scenario_name, payload in payloads.items():
        redacted = redact_json(payload)
        if not isinstance(redacted, Mapping):
            raise TypeError("live read payload redaction returned non-object")
        item = dict(redacted)
        if "response" in item:
            item["response"] = OMITTED_LIVE_RESPONSE
            item["response_omitted_from_evidence"] = True
        sanitized[scenario_name] = item
    return sanitized


def private_identifier_findings(value: JsonValue) -> list[dict[str, JsonValue]]:
    findings: list[dict[str, JsonValue]] = []
    _collect_private_identifier_findings(value, (), findings)
    return findings


def _collect_private_identifier_findings(
    value: JsonValue,
    path: tuple[str, ...],
    findings: list[dict[str, JsonValue]],
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = (*path, key)
            if _is_private_identifier_key(key) and _is_unredacted_identifier(child):
                findings.append(
                    {
                        "path": ".".join(child_path),
                        "key": key,
                        "class": "private_identifier",
                    },
                )
            _collect_private_identifier_findings(child, child_path, findings)
        return
    if isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _collect_private_identifier_findings(child, (*path, str(index)), findings)


def _is_private_identifier_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _PRIVATE_IDENTIFIER_KEYS


def _is_unredacted_identifier(value: JsonValue) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value) and value not in _SAFE_REDACTED_VALUES
    return True
