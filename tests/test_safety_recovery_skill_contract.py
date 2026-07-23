from __future__ import annotations

from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
SKILL_ROOT: Final = ROOT / "skills/saxo-safety-recovery"
SKILL_TEXT: Final = SKILL_ROOT / "SKILL.md"
STATUS_REFERENCE: Final = SKILL_ROOT / "references/status-recovery.md"
DIRECT_REFERENCES: Final = (
    SKILL_ROOT / "references/privacy-evidence.md",
    SKILL_ROOT / "references/unknown-outcome-recovery.md",
)
OPENAI_METADATA: Final = SKILL_ROOT / "agents/openai.yaml"
RISK_STATUSES: Final = (
    "completed_unverified",
    "partial_success",
    "unknown_state",
    "duplicate_or_conflict",
    "network_error",
    "rate_limited",
)
NEGATIVE_PROOF_PHRASES: Final = (
    "Only complete non-evicted ledger with `negative_proof_available=true` supports absence proof.",
    "An incomplete ledger cannot prove that no request or purchase occurred.",
    "An evicted ledger cannot prove that no request or purchase occurred.",
)
PLAN_ONLY_PHRASES: Final = (
    "answer entirely from the injected skill context",
    "do not run shell commands, browse, call mcp/tools, inspect files, invoke saxo, "
    "or create saxo events",
)
PRIVACY_PHRASES: Final = (
    "Validation errors name field/rule, never submitted value.",
    "Account numbers are usable internal selectors and not inherently secret, but "
    "user-facing output prefers alias.",
    "Raw technical identifiers and `DisplayName` must not be unnecessarily returned or published.",
    "Account-number-shaped text alone is diagnostic unless it is associated with a submitted "
    "value, raw account field, or secret-bearing key.",
)
CANARY_ONE: Final = ("Evelyn", "Canary", "Primary", "Brokerage")
CANARY_TWO: Final = ("ACC", "KEY", "CANARY", "123456")
CANARY_THREE: Final = ("tok", "canary", "should", "not", "echo", "123456")
CANARY_FOUR: Final = ("canary", "authorization", "header", "123456")
CANARY_FIVE: Final = ("submitted", "canary", "limit", "price", "49.995")
CANARY_VALUES: Final = (
    " ".join(CANARY_ONE),
    "-".join(CANARY_TWO),
    "_".join(CANARY_THREE),
    " ".join(("Bearer", "_".join(CANARY_FOUR))),
    "-".join(CANARY_FIVE),
)


def test_safety_recovery_skill_package_has_required_files_and_metadata() -> None:
    for path in (SKILL_TEXT, STATUS_REFERENCE, *DIRECT_REFERENCES, OPENAI_METADATA):
        assert path.is_file(), path

    metadata = OPENAI_METADATA.read_text(encoding="utf-8")
    assert "Use $saxo-bank-mcp:saxo-safety-recovery" in metadata
    assert "Saxo safety recovery" in metadata


def test_safety_recovery_contract_covers_status_ledger_privacy_and_plan_only() -> None:
    combined = _combined_skill_text()

    for phrase in (*NEGATIVE_PROOF_PHRASES, *PLAN_ONLY_PHRASES, *PRIVACY_PHRASES):
        assert phrase.lower() in combined.lower()
    for status in RISK_STATUSES:
        assert status in combined

    assert "$saxo-bank-mcp:saxo-safety-recovery" in combined
    assert "/saxo-bank-mcp:saxo-safety-recovery" in combined
    assert "reconcile before retry" in combined.lower()
    assert "incident evidence" in combined.lower()
    assert "environment-specific recovery" in combined.lower()


def test_generated_status_reference_has_complete_recovery_contract() -> None:
    rows = _status_rows()
    assert rows

    for row in rows:
        assert row["Status"]
        assert row["Mutation possible"] in {"true", "false"}
        assert row["Retry class"]
        assert row["Next action"]
        assert row["Safe wording"]
        assert row["Source"]

    by_status = {row["Status"]: row for row in rows}
    for status in RISK_STATUSES:
        assert status in by_status

    assert by_status["completed"]["Safe wording"] == "The mutation completed."
    assert by_status["completed_unverified"]["Retry class"] == "blind_retry_forbidden"
    assert by_status["partial_success"]["Retry class"] == "blind_retry_forbidden"
    assert by_status["unknown_state"]["Retry class"] == "blind_retry_forbidden"
    assert by_status["duplicate_or_conflict"]["Retry class"] == "blind_retry_forbidden"


def test_privacy_canaries_are_not_baked_into_skill_guidance() -> None:
    combined = _combined_skill_text()

    for canary in CANARY_VALUES:
        assert canary not in combined


def _combined_skill_text() -> str:
    paths = (SKILL_TEXT, *DIRECT_REFERENCES)
    return "\n".join(path.read_text(encoding="utf-8") for path in paths)


def _status_rows() -> list[dict[str, str]]:
    lines = STATUS_REFERENCE.read_text(encoding="utf-8").splitlines()
    table_lines = [line for line in lines if line.startswith("| ")]
    header = [cell.strip() for cell in table_lines[0].strip("|").split("|")]
    return [
        dict(zip(header, [cell.strip() for cell in line.strip("|").split("|")], strict=True))
        for line in table_lines[2:]
    ]
