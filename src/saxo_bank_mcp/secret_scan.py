from __future__ import annotations

import re
import shutil
import subprocess
from functools import cache
from os import environ
from pathlib import Path
from typing import Final, Literal, NamedTuple

from saxo_bank_mcp._evidence import JsonValue

type PatternClass = Literal["credential_regex", "email_address", "person_identifier_token"]


class SecretPattern(NamedTuple):
    pattern_class: PatternClass
    pattern: re.Pattern[str]


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
EMAIL_PATTERN: Final = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
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
    (
        "access-token-value", "client-id", "client-secret", "mocked-access-token",
        "mocked-refresh-token", "new-access-token", "new-refresh-token", "qa-probe-key",
        "refresh-token-value", "sim-app-key", "SIM_TEST_APPROVED", "SIM-ACCOUNT-1",
        "SIM-OVERRIDE", "FIXTURE_ACCOUNT", "LIVE-WRITE-REFUSAL-PROBE", "<redacted>",
        "approval_factor_invalid", "approval_factor_missing", "preview_token_expired",
        "preview_token_invalid", "preview_token_missing",
    ),
)
SAFE_EMAIL_PATTERN_PARTS: Final = frozenset(
    (
        "EMAIL_PATTERN", "_EMAIL_PATTERN", "sensitive.person@example.com",
        "[A-Z0-9._%+-]+@[A-Z0-9.-]+", "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+",
        "@[a-z0-9._%+-]+",
    ),
)
MAX_SECRET_SCAN_BYTES: Final = 2_000_000
SKIPPED_SCAN_PARTS: Final = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"})
SKIPPED_SCAN_SUFFIXES: Final = frozenset({".pyc", ".pyo"})
MIN_PERSON_TOKEN_LENGTH: Final = 5
GENERIC_PERSON_TOKENS: Final = frozenset(
    ("email", "identifier", "local", "operator", "person", "redacted", "token", "user"),
)


def secret_scan_pattern_classes() -> tuple[PatternClass, ...]:
    return ("credential_regex", "email_address", "person_identifier_token")


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
            _scan_file(file_path, findings, scan_errors)
    return findings, scan_errors


def _scan_file(
    file_path: Path,
    findings: list[dict[str, JsonValue]],
    scan_errors: list[dict[str, JsonValue]],
) -> None:
    skipped_part = SKIPPED_SCAN_PARTS.intersection(file_path.parts)
    if skipped_part or file_path.suffix in SKIPPED_SCAN_SUFFIXES:
        return
    try:
        file_size = file_path.stat().st_size
    except OSError as exc:
        scan_errors.append({"path": str(file_path), "error": type(exc).__name__})
        return
    if file_size > MAX_SECRET_SCAN_BYTES:
        scan_errors.append({"path": str(file_path), "error": "oversize_not_scanned"})
        return
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        scan_errors.append({"path": str(file_path), "error": type(exc).__name__})
        return
    findings.extend(
        {
            "path": str(file_path),
            "pattern_class": pattern.pattern_class,
            "pattern": _public_pattern(pattern),
        }
        for pattern in _secret_patterns_in_text(file_path, text)
    )


def _secret_patterns_in_text(file_path: Path, text: str) -> list[SecretPattern]:
    patterns: list[SecretPattern] = []
    for line in text.splitlines():
        if _is_safe_secret_scan_line(file_path, line):
            continue
        scan_line = _placeholder_scrubbed_line(line)
        patterns.extend(_credential_patterns(scan_line))
        patterns.extend(_email_patterns(scan_line))
        patterns.extend(_person_identifier_patterns(scan_line))
    return patterns


def _public_pattern(pattern: SecretPattern) -> str:
    if pattern.pattern_class == "person_identifier_token":
        return "<person-identifier-token>"
    return pattern.pattern.pattern


def _credential_patterns(line: str) -> tuple[SecretPattern, ...]:
    return tuple(
        SecretPattern("credential_regex", pattern)
        for pattern in SECRET_REGEXES
        if pattern.search(line)
    )


def _email_patterns(line: str) -> tuple[SecretPattern, ...]:
    if any(part in line for part in SAFE_EMAIL_PATTERN_PARTS):
        return ()
    return (SecretPattern("email_address", EMAIL_PATTERN),) if EMAIL_PATTERN.search(line) else ()


def _person_identifier_patterns(line: str) -> tuple[SecretPattern, ...]:
    return tuple(
        pattern
        for pattern in _person_identifier_scan_patterns()
        if pattern.pattern.search(line)
    )


@cache
def _person_identifier_scan_patterns_for(
    raw_env_names: str,
    git_tokens: tuple[str, ...],
) -> tuple[SecretPattern, ...]:
    return tuple(
        SecretPattern(
            "person_identifier_token",
            re.compile(rf"\b{re.escape(token)}\b", re.IGNORECASE),
        )
        for token in _person_identifier_tokens(raw_env_names, git_tokens)
    )


def _person_identifier_scan_patterns() -> tuple[SecretPattern, ...]:
    raw_env_names = environ.get("SAXO_MCP_REDACT_PERSON_NAMES", "")
    return _person_identifier_scan_patterns_for(raw_env_names, _git_identity_tokens())


def _person_identifier_tokens(
    raw_env_names: str,
    git_tokens: tuple[str, ...],
) -> tuple[str, ...]:
    tokens = {*_env_person_tokens(raw_env_names), *git_tokens}
    return tuple(
        sorted(
            token
            for token in tokens
            if len(token) >= MIN_PERSON_TOKEN_LENGTH
            and token.lower() not in GENERIC_PERSON_TOKENS
        ),
    )


def _env_person_tokens(raw_env_names: str) -> tuple[str, ...]:
    values = raw_env_names.replace(";", "\n").splitlines()
    return tuple(token for value in values for token in _identity_tokens(value))


@cache
def _git_identity_tokens() -> tuple[str, ...]:
    name = _git_config_value("user.name")
    email = _git_config_value("user.email")
    localpart = "" if "@" not in email else email.split("@", maxsplit=1)[0]
    return (*_identity_tokens(name), *_identity_tokens(localpart))


def _identity_tokens(value: str) -> tuple[str, ...]:
    return tuple(part for part in re.split(r"[^A-Za-z0-9._+-]+", value) if part)


def _git_config_value(key: str) -> str:
    git = shutil.which("git")
    if git is None:
        return ""
    try:
        result = subprocess.run(
            (git, "config", key),
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _is_safe_secret_scan_line(file_path: Path, line: str) -> bool:
    return file_path.suffix == ".py" and any(part in line for part in PYTHON_SAFE_SECRET_LINE_PARTS)


def _placeholder_scrubbed_line(line: str) -> str:
    scrubbed = line
    for placeholder in SAFE_SECRET_PLACEHOLDERS:
        scrubbed = scrubbed.replace(placeholder, "ok")
    return scrubbed
