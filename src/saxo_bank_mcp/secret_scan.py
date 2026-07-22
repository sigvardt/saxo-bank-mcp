from __future__ import annotations

import re
import shutil
import subprocess
from functools import cache
from os import environ
from pathlib import Path

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.secret_scan_patterns import (
    EMAIL_PATTERN,
    GENERIC_PERSON_TOKENS,
    MAX_SECRET_SCAN_BYTES,
    MIN_PERSON_TOKEN_LENGTH,
    SECRET_REGEXES,
    SKIPPED_SCAN_PARTS,
    SKIPPED_SCAN_SUFFIXES,
    PatternClass,
    SecretPattern,
)
from saxo_bank_mcp.secret_scan_private_paths import (
    PRIVATE_FINANCIAL_VALUE_PATTERN,
    PRIVATE_PATH_PATTERN,
    private_financial_value_finding_count,
    private_path_finding_count,
)
from saxo_bank_mcp.secret_scan_python import (
    python_credential_candidates,
    python_line_credential_candidates,
)
from saxo_bank_mcp.secret_scan_safe_values import (
    SAFE_EMAIL_PATTERN_PARTS,
    SAFE_SECRET_PLACEHOLDERS,
    scrub_bounded_fragments,
)


def secret_scan_pattern_classes() -> tuple[PatternClass, ...]:
    return (
        "credential_regex",
        "email_address",
        "person_identifier_token",
        "private_financial_value",
        "private_path_value",
    )


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


def scan_secret_text(
    label: str,
    text: str,
) -> tuple[list[dict[str, JsonValue]], list[dict[str, JsonValue]]]:
    encoded_size = len(text.encode("utf-8"))
    if encoded_size > MAX_SECRET_SCAN_BYTES:
        return [], [{"path": label, "error": "oversize_not_scanned"}]
    path = Path(label)
    findings: list[dict[str, JsonValue]] = [
        {
            "path": label,
            "pattern_class": pattern.pattern_class,
            "pattern": _public_pattern(pattern),
        }
        for pattern in _secret_patterns_in_text(path, text)
    ]
    return findings, []


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
    private_patterns = list(_private_value_patterns(file_path, text))
    if file_path.suffix == ".py":
        return [*private_patterns, *_python_secret_patterns(text)]
    patterns = private_patterns
    for line in text.splitlines():
        scan_line = _placeholder_scrubbed_line(line)
        patterns.extend(_credential_patterns(scan_line))
        patterns.extend(_email_patterns(scan_line))
        patterns.extend(_person_identifier_patterns(scan_line))
    return patterns


def _python_secret_patterns(text: str) -> list[SecretPattern]:
    candidates = python_credential_candidates(text)
    if candidates is None:
        return _invalid_python_secret_patterns(text)
    patterns: list[SecretPattern] = []
    for candidate in candidates:
        patterns.extend(_credential_patterns(_placeholder_scrubbed_line(candidate)))
    for line in text.splitlines():
        scan_line = _placeholder_scrubbed_line(line)
        patterns.extend(_email_patterns(scan_line))
        patterns.extend(_person_identifier_patterns(scan_line))
    return patterns


def _invalid_python_secret_patterns(text: str) -> list[SecretPattern]:
    patterns: list[SecretPattern] = []
    for line in text.splitlines():
        candidates = python_line_credential_candidates(line)
        if candidates is None:
            candidates = (line,)
        for candidate in candidates:
            patterns.extend(_credential_patterns(_placeholder_scrubbed_line(candidate)))
        scan_line = _placeholder_scrubbed_line(line)
        patterns.extend(_email_patterns(scan_line))
        patterns.extend(_person_identifier_patterns(scan_line))
    return patterns


def _public_pattern(pattern: SecretPattern) -> str:
    if pattern.pattern_class == "person_identifier_token":
        return "<person-identifier-token>"
    if pattern.pattern_class == "private_path_value":
        return "<private-path-value>"
    if pattern.pattern_class == "private_financial_value":
        return "<private-financial-value>"
    return pattern.pattern.pattern


def _private_value_patterns(file_path: Path, text: str) -> tuple[SecretPattern, ...]:
    path_patterns = tuple(
        SecretPattern("private_path_value", PRIVATE_PATH_PATTERN)
        for _index in range(private_path_finding_count(file_path, text))
    )
    financial_patterns = tuple(
        SecretPattern("private_financial_value", PRIVATE_FINANCIAL_VALUE_PATTERN)
        for _index in range(private_financial_value_finding_count(file_path, text))
    )
    return path_patterns + financial_patterns


def _credential_patterns(line: str) -> tuple[SecretPattern, ...]:
    return tuple(
        SecretPattern("credential_regex", pattern)
        for pattern in SECRET_REGEXES
        if pattern.search(line)
    )


def _email_patterns(line: str) -> tuple[SecretPattern, ...]:
    candidate = scrub_bounded_fragments(
        line,
        SAFE_EMAIL_PATTERN_PARTS,
        adjacent_character_pattern=r"[A-Za-z0-9._%+-]",
    )
    return (
        (SecretPattern("email_address", EMAIL_PATTERN),) if EMAIL_PATTERN.search(candidate) else ()
    )


def _person_identifier_patterns(line: str) -> tuple[SecretPattern, ...]:
    return tuple(
        pattern for pattern in _person_identifier_scan_patterns() if pattern.pattern.search(line)
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
            if len(token) >= MIN_PERSON_TOKEN_LENGTH and token.lower() not in GENERIC_PERSON_TOKENS
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


def _placeholder_scrubbed_line(line: str) -> str:
    return scrub_bounded_fragments(
        line,
        SAFE_SECRET_PLACEHOLDERS,
        adjacent_character_pattern="[^'\"\\s{}&?=]",
    )
