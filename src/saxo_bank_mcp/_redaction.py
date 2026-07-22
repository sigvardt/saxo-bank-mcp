from __future__ import annotations

import re
from collections.abc import Mapping
from os import environ
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.secret_scan import scan_secret_paths, secret_scan_pattern_classes
from saxo_bank_mcp.secret_scan_private_paths import PRIVATE_ROOT_PATTERN

REDACTED: Final = "<redacted>"
REDACTED_EMAIL: Final = "<redacted-email>"
REDACTED_PERSON: Final = "<redacted-person>"
__all__ = (
    "REDACTED",
    "REDACTED_EMAIL",
    "REDACTED_PERSON",
    "redact_json",
    "redact_text",
    "scan_secret_paths",
    "secret_scan_pattern_classes",
)

_SENSITIVE_KEYS: Final = frozenset(
    {
        "authorization",
        "authorizationurl",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "appsecret",
        "clientkey",
        "clientid",
        "appkey",
        "accountkey",
        "accountid",
        "accountnumber",
        "accountref",
        "accountgroupid",
        "accountgroupkey",
        "accountgroupname",
        "displayname",
        "externalreference",
        "nickname",
        "approvalfactor",
        "orderid",
        "orderids",
        "multilegorderid",
        "userid",
        "userkey",
        "previewtoken",
        "tokencachepath",
        "auditpath",
        "rawauditpath",
        "credentialpath",
        "credentialrealpath",
    },
)
_INLINE_SENSITIVE_KEY_PATTERN: Final = (
    r"(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"app[_-]?secret|client[_-]?(?:key|id)|app[_-]?key|authorization[_-]?url|"
    r"approval[_-]?factor|preview[_-]?token|token[_-]?cache[_-]?path|"
    r"account[_-]?(?:key|number|id|ref)|"
    r"account[_-]?group[_-]?(?:id|key|name)|"
    r"display[_-]?name|external[_-]?reference|nick[_-]?name|user[_-]?(?:id|key)|"
    r"(?:multi[_-]?leg[_-]?)?order[_-]?ids?)"
)
_INLINE_QUOTED_VALUE_PATTERN: Final = re.compile(
    r"(?i)([\"']?"
    + _INLINE_SENSITIVE_KEY_PATTERN
    + r"[\"']?\s*[:=]\s*[\"'])([^\"'\r\n]*)([\"'])",
)
_INLINE_PATTERNS: Final = (
    re.compile(
        r"(?i)(\bauthorization\s*[:=]\s*(?:bearer\s+)?)['\"]?[^'\"\s,;}]+",
    ),
    re.compile(
        r"(?i)([\"']?"
        + _INLINE_SENSITIVE_KEY_PATTERN
        + r"[\"']?\s*[:=]\s*[\"']?)[^'\"\s,;}]+",
    ),
)
_RAW_CREDENTIAL_LINE_PATTERN: Final = re.compile(
    r"(?im)^(?=.*\b(?:credential line|password|private key|api key)\b).+$",
)
_EMAIL_PATTERN: Final = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def redact_text(text: str) -> str:
    redacted = text
    redacted = _INLINE_QUOTED_VALUE_PATTERN.sub(
        lambda match: f"{match.group(1)}{REDACTED}{match.group(3)}",
        redacted,
    )
    for pattern in _INLINE_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}{REDACTED}", redacted)
    redacted = _RAW_CREDENTIAL_LINE_PATTERN.sub(REDACTED, redacted)
    redacted = _EMAIL_PATTERN.sub(REDACTED_EMAIL, redacted)
    for name in _person_names():
        pattern = rf"\b{re.escape(name)}\b"
        redacted = re.sub(pattern, REDACTED_PERSON, redacted, flags=re.IGNORECASE)
    return redacted


def _person_names() -> tuple[str, ...]:
    raw = environ.get("SAXO_MCP_REDACT_PERSON_NAMES", "")
    names = (line.strip() for line in raw.replace(";", "\n").splitlines())
    return tuple(filter(None, names))


def redact_json(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return REDACTED if PRIVATE_ROOT_PATTERN.search(value) is not None else redact_text(value)
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive_key(key) else redact_json(child)
            for key, child in value.items()
        }
    return [redact_json(child) for child in value]


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in _SENSITIVE_KEYS
