from __future__ import annotations

import re
from typing import Final

SAFE_SECRET_PLACEHOLDERS: Final = frozenset(
    (
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
        "live-access-token",
        "untagged-access-token",
        "existing-access-token",
        "existing-refresh-token",
        "sim-access-token",
        "expired-access-token",
        "ledger-fixture-token",
        "access_token=access_token",
        "access_token: Annotated[",
        '"access_token": PORTAL_ACCESS_FIXTURE',
        "access_token=PORTAL_ACCESS_FIXTURE",
        '"refresh_token": refresh.refresh_token',
        'client_secret = environ.get("SAXO_MCP_LIVE_CLIENT_SECRET", "")',
        '"refresh_token": token.refresh_token',
        "FIXTURE_ACCOUNT",
        "FIXTURE_CLIENT",
        "fixture-client",
        "fixture-client-key",
        "LIVE-WRITE-REFUSAL-PROBE",
        "<redacted>",
        "approval_factor_invalid",
        "approval_factor_missing",
        "preview_token_expired",
        "preview_token_invalid",
        "preview_token_missing",
    ),
)
SAFE_EMAIL_PATTERN_PARTS: Final = frozenset(
    (
        "EMAIL_PATTERN",
        "_EMAIL_PATTERN",
        "sensitive.person@example.com",
        "[A-Z0-9._%+-]+@[A-Z0-9.-]+",
        "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+",
        "@[a-z0-9._%+-]+",
    ),
)


def scrub_bounded_fragments(
    line: str,
    fragments: frozenset[str],
    *,
    adjacent_character_pattern: str,
) -> str:
    scrubbed = line
    for fragment in fragments:
        scrubbed = re.sub(
            (
                rf"(?<!{adjacent_character_pattern})"
                rf"{re.escape(fragment)}"
                rf"(?!{adjacent_character_pattern})"
            ),
            "ok",
            scrubbed,
        )
    return scrubbed
