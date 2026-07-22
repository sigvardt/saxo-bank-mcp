from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cache
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import (
    REDACTED,
    REDACTED_EMAIL,
    REDACTED_PERSON,
    redact_json,
)
from saxo_bank_mcp.endpoint_registry import implemented_read_operations

OMITTED_LIVE_RESPONSE: Final = "<omitted-from-live-evidence>"
_PRIVATE_IDENTIFIER_KEYS: Final = frozenset(
    {
        "accountgroupid",
        "accountgroupkey",
        "accountgroupname",
        "accountid",
        "accountkey",
        "accountnumber",
        "accountref",
        "clientid",
        "clientkey",
        "displayname",
        "externalreference",
        "userid",
        "userkey",
    },
)
_SAFE_REDACTED_VALUES: Final = frozenset({REDACTED, REDACTED_EMAIL, REDACTED_PERSON})


class LiveReadPayloadRedactionError(TypeError):
    pass


def sanitize_live_read_payloads(
    payloads: Mapping[str, Mapping[str, JsonValue]],
) -> dict[str, dict[str, JsonValue]]:
    sanitized: dict[str, dict[str, JsonValue]] = {}
    for scenario_name, payload in payloads.items():
        redacted = _sanitize_persisted_routes(redact_json(payload))
        if not isinstance(redacted, Mapping):
            raise LiveReadPayloadRedactionError("live read payload redaction returned non-object")
        item = dict(redacted)
        if "response" in item:
            item["response"] = OMITTED_LIVE_RESPONSE
            item["response_omitted_from_evidence"] = True
        sanitized[scenario_name] = item
    return sanitized


def _sanitize_persisted_routes(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        sanitized: dict[str, JsonValue] = {}
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized == "resolvedpath":
                continue
            if normalized == "path":
                safe_template = _safe_registered_path_template(child)
                if safe_template is not None:
                    sanitized[key] = safe_template
                continue
            sanitized[key] = _sanitize_persisted_routes(child)
        return sanitized
    if isinstance(value, list | tuple):
        return [_sanitize_persisted_routes(child) for child in value]
    return value


def _safe_registered_path_template(value: JsonValue) -> str | None:
    if not isinstance(value, str):
        return None
    return value if value in _identifier_free_read_path_templates() else None


@cache
def _identifier_free_read_path_templates() -> frozenset[str]:
    return frozenset(
        operation.path_template
        for operation in implemented_read_operations()
        if "{" not in operation.path_template
        and "}" not in operation.path_template
        and "?" not in operation.path_template
        and "#" not in operation.path_template
    )


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
            normalized = _normalized_key(key)
            if normalized in {"path", "resolvedpath"} and _is_unsafe_persisted_route(child):
                findings.append(
                    {
                        "path": ".".join(child_path),
                        "key": key,
                        "class": "private_identifier_route",
                    },
                )
            elif _is_private_identifier_key(key) and _is_unredacted_identifier(child):
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
    return _normalized_key(key) in _PRIVATE_IDENTIFIER_KEYS


def _normalized_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _is_unsafe_persisted_route(value: JsonValue) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and value in _SAFE_REDACTED_VALUES:
        return False
    return _safe_registered_path_template(value) is None


def _is_unredacted_identifier(value: JsonValue) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        return bool(value) and value not in _SAFE_REDACTED_VALUES
    return True
