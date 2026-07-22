from __future__ import annotations

import json
import string
from pathlib import Path

import pytest

from saxo_bank_mcp import evidence_publication, qa

EXPECTED_CLIENT_APP_SECRET_FINDINGS = 2


def test_secret_scan_ignores_variable_names(tmp_path: Path) -> None:
    target = tmp_path / "source.py"
    target.write_text("refresh_token = None\n", encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["paths"] == ["<redacted>"]
    assert report["findings"] == []


def test_secret_scan_detects_access_token(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    key_parts = ["access", "token"]
    key = "_".join(key_parts)
    token = string.ascii_lowercase
    target.write_text(f'{key} = "{token}"\n', encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_detects_unquoted_access_token(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    key_parts = ["access", "token"]
    key = "_".join(key_parts)
    target.write_text(f"{key}={string.ascii_lowercase}\n", encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_detects_json_access_token(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    key_parts = ["access", "token"]
    key = "_".join(key_parts)
    target.write_text(f'{{"{key}": "{string.ascii_lowercase}"}}\n', encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_detects_pascalcase_saxo_token(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    target.write_text(f'{{"AccessToken": "{string.ascii_lowercase}"}}\n', encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_detects_pascalcase_client_and_app_secret(tmp_path: Path) -> None:
    target = tmp_path / "secret.txt"
    target.write_text(
        f"ClientSecret={string.ascii_lowercase}\nAppSecret={string.ascii_lowercase}\n",
        encoding="utf-8",
    )
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 1
    assert len(report["findings"]) == EXPECTED_CLIENT_APP_SECRET_FINDINGS


@pytest.mark.parametrize("safe_looking_fragment", ["Field(", "self.", "settings."])
def test_secret_scan_does_not_skip_credential_literals_on_code_lines(
    tmp_path: Path,
    safe_looking_fragment: str,
) -> None:
    target = tmp_path / "source.py"
    target.write_text(
        f'access_token = "{string.ascii_lowercase}"  # {safe_looking_fragment}\n',
        encoding="utf-8",
    )
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8"))["findings"]


def test_secret_scan_ignores_account_key_placeholder(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    target.write_text('{"AccountKey": "{AccountKey}"}\n', encoding="utf-8")
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 0
    assert json.loads(out.read_text(encoding="utf-8"))["findings"] == []


def test_secret_scan_missing_path_fails_closed(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist_at_all"
    out = tmp_path / "scan.json"

    result = qa.main(["secret-scan", "--paths", str(missing_path), "--out", str(out)])

    assert result == 1
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["scan_errors"] == [{"path": "<redacted>", "error": "missing_path"}]


def test_secret_scan_gate_scans_candidate_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "clean.txt"
    target.write_text("clean\n", encoding="utf-8")
    out = tmp_path / "scan.json"

    def reject_candidate(
        _label: str,
        _text: str,
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        return ([{"path": "candidate", "pattern_class": "credential_regex"}], [])

    monkeypatch.setattr(evidence_publication, "scan_secret_text", reject_candidate)

    result = qa.main(["secret-scan", "--paths", str(target), "--out", str(out)])

    assert result == 1
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "reason": "evidence_secret_scan_failed",
        "status": "failed",
    }
