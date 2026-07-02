from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from saxo_bank_mcp._redaction import scan_secret_paths


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
    assert "eyJ" in str(findings[0]["pattern"])
