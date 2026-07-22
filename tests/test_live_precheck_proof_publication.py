from __future__ import annotations

from pathlib import Path

from test_live_precheck_proof_support import JSON_OBJECT_ADAPTER

from saxo_bank_mcp.live_precheck_proof_publication import write_scanned_artifact


def test_proof_artifact_replaces_existing_file_without_backups(tmp_path: Path) -> None:
    out = tmp_path / "proof.json"
    out.write_text('{"status":"old"}\n', encoding="utf-8")

    clean = write_scanned_artifact(out, {"status": "completed"})

    report = JSON_OBJECT_ADAPTER.validate_json(out.read_text(encoding="utf-8"))
    assert clean is True
    assert report["status"] == "completed"
    assert report["secret_scan"] == {
        "clean": True,
        "finding_count": 0,
        "pattern_classes": [
            "credential_regex",
            "email_address",
            "person_identifier_token",
            "private_financial_value",
            "private_path_value",
        ],
        "scan_error_count": 0,
    }
    assert list(tmp_path.glob("*.bak")) == []


def test_proof_artifact_scan_failure_never_persists_secret_or_backup(tmp_path: Path) -> None:
    out = tmp_path / "proof.json"
    secret = ".".join(("eyJ" + ("A" * 30), "B" * 30, "C" * 30))

    clean = write_scanned_artifact(
        out,
        {"status": "completed", "unsafe_note": secret},
    )

    report = JSON_OBJECT_ADAPTER.validate_json(out.read_text(encoding="utf-8"))
    assert clean is False
    assert report["status"] == "aborted"
    assert report["abort_reason"] == "artifact_secret_scan_failed"
    assert secret not in out.read_text(encoding="utf-8")
    assert [path.name for path in tmp_path.iterdir()] == ["proof.json"]
