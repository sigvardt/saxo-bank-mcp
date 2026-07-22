from __future__ import annotations

import re
from typing import Final, Literal, NamedTuple

type PatternClass = Literal[
    "credential_regex",
    "email_address",
    "person_identifier_token",
    "private_financial_value",
    "private_path_value",
]


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
MAX_SECRET_SCAN_BYTES: Final = 2_000_000
SKIPPED_SCAN_PARTS: Final = frozenset(
    {"__pycache__", ".pytest_cache", ".ruff_cache", ".venv"},
)
SKIPPED_SCAN_SUFFIXES: Final = frozenset({".pyc", ".pyo"})
MIN_PERSON_TOKEN_LENGTH: Final = 5
GENERIC_PERSON_TOKENS: Final = frozenset(
    ("email", "identifier", "local", "operator", "person", "redacted", "token", "user"),
)
