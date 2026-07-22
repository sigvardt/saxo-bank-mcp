from __future__ import annotations

import json
import string
from pathlib import Path
from textwrap import dedent

import pytest

from saxo_bank_mcp._redaction import scan_secret_paths
from saxo_bank_mcp.secret_scan import scan_secret_text

TOKEN_KEY = f"access{chr(95)}token"
SECRET_VALUE_ADJACENCY = tuple(
    character
    for character in string.printable
    if character not in "'\"{}&?=" and not character.isspace()
)


def test_secret_scan_allows_safe_python_token_field_wiring(tmp_path: Path) -> None:
    source = tmp_path / "safe.py"
    source.write_text(
        dedent(
            """
            def cache(access_token: Annotated[str, Field(min_length=8)]) -> None:
                token = SaxoTokenSet(access_token=access_token)
                payload = {"access_token": PORTAL_ACCESS_FIXTURE}
                fallback = dict(access_token=PORTAL_ACCESS_FIXTURE)
                form = {"refresh_token": refresh.refresh_token}
            """,
        ),
        encoding="utf-8",
    )

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert findings == []


def test_secret_scan_parses_multiline_python_source_fixtures(tmp_path: Path) -> None:
    source = tmp_path / "fixture_source.py"
    delimiter = '"' * 3
    source.write_text(
        f'fixture = {delimiter}\nform = {{"refresh_token": refresh.refresh_token}}\n{delimiter}\n',
        encoding="utf-8",
    )

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert findings == []


def test_secret_scan_still_reports_literal_python_token_values(tmp_path: Path) -> None:
    source = tmp_path / "leak.py"
    field_name = "access" + "_token"
    field_value = "literal-" + "portal-token-value"
    source.write_text(f'{field_name} = "{field_value}"\n', encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert findings == [
        {
            "path": str(source),
            "pattern_class": "credential_regex",
            "pattern": "access_?token['\\\"]?\\s*[:=]\\s*['\\\"]?[^'\\\"\\s{}&?=]{12,}",
        },
    ]


def test_secret_scan_reports_bare_jwt_shaped_token(tmp_path: Path) -> None:
    source = tmp_path / "portal-token.txt"
    token = ".".join(
        (
            "eyJ" + ("A" * 30),
            "b" * 40,
            "c" * 40,
        ),
    )
    source.write_text(token, encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert len(findings) == 1
    assert findings[0]["path"] == str(source)
    assert findings[0]["pattern_class"] == "credential_regex"
    assert "eyJ" in str(findings[0]["pattern"])


def test_secret_scan_reports_email_addresses(tmp_path: Path) -> None:
    source = tmp_path / "email.txt"
    address = "person" + "@example.invalid"
    source.write_text(f"contact={address}\n", encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert findings == [
        {
            "path": str(source),
            "pattern_class": "email_address",
            "pattern": "\\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\\.[A-Z]{2,}\\b",
        },
    ]


@pytest.mark.parametrize("suffix", [".json", ".txt"])
def test_secret_scan_reports_unredacted_private_path_values(
    tmp_path: Path,
    suffix: str,
) -> None:
    source = tmp_path / f"evidence{suffix}"
    private_path = "/Users/fixture/private-token-cache.json"
    text = (
        json.dumps({"auth": {"token_cache_path": private_path}})
        if suffix == ".json"
        else f"token_cache_path={private_path}\n"
    )
    source.write_text(text, encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert findings == [
        {
            "path": str(source),
            "pattern_class": "private_path_value",
            "pattern": "<private-path-value>",
        },
    ]


def test_secret_scan_allows_redacted_private_path_value(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text('{"token_cache_path":"<redacted>"}\n', encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert findings == []


def test_secret_scan_reports_private_path_inside_unrelated_json_field(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    private_path = "/Users/fixture/token-cache.json"
    source.write_text(
        json.dumps({"detail": f"refusing token cache path {private_path}: denied"}),
        encoding="utf-8",
    )

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert [finding["pattern_class"] for finding in findings] == ["private_path_value"]


def test_secret_scan_reports_bare_private_path_inside_unrelated_json_field(
    tmp_path: Path,
) -> None:
    source = tmp_path / "evidence.json"
    private_path = "/Users/fixture/private-artifact.json"
    source.write_text(json.dumps({"output": private_path}), encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert [finding["pattern_class"] for finding in findings] == ["private_path_value"]


def test_secret_scan_reports_exact_financial_value_keys(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text(
        json.dumps({"precheck": {"estimated_cash_required": 1}}),
        encoding="utf-8",
    )

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert [finding["pattern_class"] for finding in findings] == [
        "private_financial_value",
    ]


def test_secret_scan_reports_raw_cash_balance(tmp_path: Path) -> None:
    source = tmp_path / "evidence.json"
    source.write_text(json.dumps({"CashBalance": 123.0}), encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert [finding["pattern_class"] for finding in findings] == [
        "private_financial_value",
    ]


def test_secret_scan_reports_financial_values_in_json_with_non_json_suffix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "evidence.txt"
    source.write_text(
        json.dumps({"precheck": {"estimated_cash_required": 1}}),
        encoding="utf-8",
    )

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert [finding["pattern_class"] for finding in findings] == [
        "private_financial_value",
    ]


def test_secret_scan_reports_financial_values_in_json_with_python_suffix(
    tmp_path: Path,
) -> None:
    source = tmp_path / "evidence.py"
    source.write_text(
        json.dumps({"precheck": {"estimated_cash_required": 1}}),
        encoding="utf-8",
    )

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert [finding["pattern_class"] for finding in findings] == [
        "private_financial_value",
    ]


def test_secret_scan_reports_financial_values_inside_json_string_leaf(
    tmp_path: Path,
) -> None:
    source = tmp_path / "evidence.json"
    source.write_text(
        json.dumps({"detail": json.dumps({"estimated_cash_required": 1})}),
        encoding="utf-8",
    )

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert [finding["pattern_class"] for finding in findings] == [
        "private_financial_value",
    ]


def test_secret_scan_reports_financial_assignment_in_plain_text(tmp_path: Path) -> None:
    source = tmp_path / "evidence.txt"
    source.write_text("estimated_cash_required=1\n", encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert [finding["pattern_class"] for finding in findings] == [
        "private_financial_value",
    ]


def test_secret_scan_reports_configured_person_identifier_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "review.md"
    sensitive_name = "Sensitive Person"
    monkeypatch.setenv("SAXO_MCP_REDACT_PERSON_NAMES", sensitive_name)
    source.write_text("search pattern mentioned Sensitive\n", encoding="utf-8")

    findings, scan_errors = scan_secret_paths([str(source)])

    assert scan_errors == []
    assert findings == [
        {
            "path": str(source),
            "pattern_class": "person_identifier_token",
            "pattern": "<person-identifier-token>",
        },
    ]


@pytest.mark.parametrize(
    "candidate",
    [
        TOKEN_KEY + "=" + TOKEN_KEY + "AAAABBBBCCCCDDDD",
        TOKEN_KEY + "=" + "PORTAL_" + "ACCESS_FIXTUREAAAABBBBCCCCDDDD",
    ],
)
def test_safe_placeholder_cannot_erase_attached_credential(candidate: str) -> None:
    findings, errors = scan_secret_text("candidate.txt", candidate)

    assert errors == []
    assert any(finding["pattern_class"] == "credential_regex" for finding in findings)


@pytest.mark.parametrize("separator", SECRET_VALUE_ADJACENCY)
@pytest.mark.parametrize("label", ["candidate.txt", "candidate.py"])
def test_safe_placeholder_rejects_every_secret_value_attachment(
    separator: str,
    label: str,
) -> None:
    candidate = TOKEN_KEY + "=" + TOKEN_KEY + separator + ("A" * 16)
    text = f"candidate = {candidate!r}\n" if label.endswith(".py") else candidate

    findings, errors = scan_secret_text(label, text)

    assert errors == []
    assert any(finding["pattern_class"] == "credential_regex" for finding in findings)


def test_email_pattern_exemption_does_not_hide_another_address_on_same_line() -> None:
    address = "person" + "@example.invalid"
    findings, errors = scan_secret_text(
        "candidate.txt",
        f"EMAIL_PATTERN = compiled-pattern; owner={address}",
    )

    assert errors == []
    assert any(finding["pattern_class"] == "email_address" for finding in findings)
