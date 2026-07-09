from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue

REDACTED: Final = "<redacted>"
REDACTED_EMAIL: Final = "<redacted-email>"
REDACTED_PERSON: Final = "<redacted-person>"

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
    },
)
_INLINE_SENSITIVE_KEY_PATTERN: Final = (
    r"(?:access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"app[_-]?secret|client[_-]?(?:key|id)|app[_-]?key|authorization[_-]?url|"
    r"approval[_-]?factor|preview[_-]?token|account[_-]?(?:key|number|id)|"
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
_PERSON_PATTERN: Final = re.compile(r"\bJoakim\s+Sigvardt\b", re.IGNORECASE)
SECRET_REGEXES: Final = (
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"authorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"access_?token['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{12,}", re.IGNORECASE),
    re.compile(r"client_?secret['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{12,}", re.IGNORECASE),
    re.compile(r"client_?id['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{8,}", re.IGNORECASE),
    re.compile(r"app_?secret['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{12,}", re.IGNORECASE),
    re.compile(r"client_?key['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{12,}", re.IGNORECASE),
    re.compile(r"refresh_?token['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{12,}", re.IGNORECASE),
    re.compile(r"app_?key['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{12,}", re.IGNORECASE),
    re.compile(r"approval_?factor['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{8,}", re.IGNORECASE),
    re.compile(r"preview_?token['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{12,}", re.IGNORECASE),
    re.compile(r"account_?(key|number)['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{8,}", re.IGNORECASE),
    re.compile(
        r"account_?group_?(id|key|name)['\"]?\s*[:=]\s*['\"]?[^'\"\s{}&?=]{8,}",
        re.IGNORECASE,
    ),
)
PYTHON_SAFE_SECRET_LINE_PARTS: Final = frozenset(
    {
        "# noqa: S106",
        "AliasChoices(",
        "Field(",
        "access_token: Annotated[",
        "access_token=access_token",
        "access_token=PORTAL_ACCESS_FIXTURE",
        '"access_token": PORTAL_ACCESS_FIXTURE',
        "account_key: Annotated[",
        "account_key: Annotated[str",
        "account_key=account_key",
        "account_key = _string(",
        "account_key = _string_or_int(",
        "account_key is None",
        "account_key != expected_account_key",
        '"account_key": request.account_key',
        '"account_key": account.account_key',
        '"account_key": account_key',
        '"account_key": FIXTURE_ACCOUNT',
        '"AccountKey": account_key',
        "expected_account_key: str",
        "account_key = _first_account_key(",
        "account_key=default_account_key",
        "account_key=env_value",
        "approval_factor: Annotated[",
        "approval_factor: str | None",
        "approval_factor=TEST_APPROVAL_FACTOR",
        "approval_factor=approval_factor",
        '"approval_factor": TEST_APPROVAL_FACTOR',
        "compare_digest(approval_factor",
        "client_id: str",
        "request.client_id",
        '"client_id": request.client_id',
        "_env_sim_app_key(",
        "environ.get(",
        "app_key=app_key",
        "parsed.",
        '"preview_token": token',
        "preview_token: str",
        "preview_token: NotRequired[str]",
        "preview_token_fingerprint",
        'preview["preview_token"]',
        'preview.get("preview_token"',
        '"refresh_token": refresh.refresh_token',
        "self.",
        "settings.",
        "token.",
    },
)
SAFE_SECRET_PLACEHOLDERS: Final = frozenset(
    {
        "access-token-value",
        "client-id",
        "client-secret",
        "mocked-access-token",
        "mocked-refresh-token",
        "new-access-token",
        "new-refresh-token",
        "qa-probe-key",
        "refresh-token-value",
        "sim-app-key",
        "SIM_TEST_APPROVED",
        "SIM-ACCOUNT-1",
        "SIM-OVERRIDE",
        "FIXTURE_ACCOUNT",
        "LIVE-WRITE-REFUSAL-PROBE",
        "<redacted>",
        "approval_factor_invalid",
        "approval_factor_missing",
        "preview_token_expired",
        "preview_token_invalid",
        "preview_token_missing",
    },
)
MAX_SECRET_SCAN_BYTES: Final = 2_000_000
SKIPPED_SCAN_PARTS: Final = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"})
SKIPPED_SCAN_SUFFIXES: Final = frozenset({".pyc", ".pyo"})


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
    return _PERSON_PATTERN.sub(REDACTED_PERSON, redacted)


def redact_json(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        return redact_text(value)
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


def _secret_patterns_in_text(file_path: Path, text: str) -> list[str]:
    patterns: list[str] = []
    for line in text.splitlines():
        if _is_safe_secret_scan_line(file_path, line):
            continue
        scan_line = _placeholder_scrubbed_line(line)
        patterns.extend(pattern.pattern for pattern in SECRET_REGEXES if pattern.search(scan_line))
    return patterns


def _is_safe_secret_scan_line(file_path: Path, line: str) -> bool:
    return file_path.suffix == ".py" and any(part in line for part in PYTHON_SAFE_SECRET_LINE_PARTS)


def _placeholder_scrubbed_line(line: str) -> str:
    scrubbed = line
    for placeholder in SAFE_SECRET_PLACEHOLDERS:
        scrubbed = scrubbed.replace(placeholder, "ok")
    return scrubbed


def scan_secret_paths(
    paths: list[str],
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    findings: list[dict[str, JsonValue]] = []
    scan_errors: list[dict[str, JsonValue]] = []
    for raw_path in paths:
        candidate = Path(raw_path)
        if not candidate.exists():
            scan_errors.append({"path": str(candidate), "error": "missing_path"})
            continue
        files = (
            (path for path in candidate.rglob("*") if path.is_file())
            if candidate.is_dir()
            else (candidate,)
        )
        for file_path in files:
            if (
                SKIPPED_SCAN_PARTS.intersection(file_path.parts)
                or file_path.suffix in SKIPPED_SCAN_SUFFIXES
            ):
                continue
            try:
                file_size = file_path.stat().st_size
            except OSError as exc:
                scan_errors.append({"path": str(file_path), "error": type(exc).__name__})
                continue
            if file_size > MAX_SECRET_SCAN_BYTES:
                scan_errors.append({"path": str(file_path), "error": "oversize_not_scanned"})
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                scan_errors.append({"path": str(file_path), "error": type(exc).__name__})
                continue
            findings.extend(
                {"path": str(file_path), "pattern": pattern}
                for pattern in _secret_patterns_in_text(file_path, text)
            )
    return findings, scan_errors
