from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_install_models import PrivacyEvidenceBinding

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
REQUIRED_SCOPES: Final = (
    "manual_json",
    "installed_caches",
    "auth_copies",
    "client_state",
    "todo13_public_clone",
)
DIGEST_ORDER: Final = "install_then_privacy_report_then_self_scan_then_binding_digests"


def build_privacy_binding(
    *,
    privacy_report: Path,
    privacy_self_scan: Path,
    candidate_commit: str,
    clone_commit: str,
) -> PrivacyEvidenceBinding:
    report = _load_object(privacy_report)
    self_scan = _load_object(privacy_self_scan)
    errors = _privacy_payload_errors(
        report,
        self_scan,
        candidate_commit=candidate_commit,
        clone_commit=clone_commit,
        privacy_report=privacy_report,
        privacy_self_scan=privacy_self_scan,
    )
    if errors:
        msg = ",".join(errors)
        raise ValueError(msg)
    return PrivacyEvidenceBinding(
        privacy_report_path=privacy_report.name,
        privacy_self_scan_path=privacy_self_scan.name,
        privacy_report_sha256=_sha256_file(privacy_report),
        privacy_self_scan_sha256=_sha256_file(privacy_self_scan),
        candidate_commit=candidate_commit,
        clone_commit=clone_commit,
        clean=True,
        findings_count=0,
        scan_errors_count=0,
        scopes_covered=REQUIRED_SCOPES,
        digest_order=DIGEST_ORDER,  # type: ignore[arg-type]
    )


def verify_privacy_binding(
    binding: PrivacyEvidenceBinding,
    *,
    report_dir: Path,
    candidate_commit: str,
    clone_commit: str,
) -> list[str]:
    report_path = _resolve_privacy_path(binding.privacy_report_path, report_dir)
    self_scan_path = _resolve_privacy_path(binding.privacy_self_scan_path, report_dir)
    if report_path is None:
        return ["privacy_report_missing"]
    if self_scan_path is None:
        return ["privacy_self_scan_missing"]
    if _sha256_file(report_path) != binding.privacy_report_sha256:
        return ["privacy_report_digest_mismatch"]
    if _sha256_file(self_scan_path) != binding.privacy_self_scan_sha256:
        return ["privacy_self_scan_digest_mismatch"]
    try:
        report = _load_object(report_path)
        self_scan = _load_object(self_scan_path)
    except ValueError:
        return ["privacy_payload_invalid"]
    return _privacy_payload_errors(
        report,
        self_scan,
        candidate_commit=candidate_commit,
        clone_commit=clone_commit,
        privacy_report=report_path,
        privacy_self_scan=self_scan_path,
        binding=binding,
    )


def write_minimal_privacy_pair(
    directory: Path,
    *,
    candidate_commit: str,
    clone_commit: str,
    run_root: Path,
    scopes: dict[str, list[str]] | None = None,
) -> tuple[Path, Path]:
    """Test helper: write clean privacy-report + self-scan without circular digests."""
    directory.mkdir(parents=True, exist_ok=True)
    report_path = directory / "privacy-report.json"
    self_scan_path = directory / "privacy-self-scan.json"
    scope = scopes or {
        "manual_json": [str(directory / "install.json")],
        "installed_caches": [str(run_root / "codex-cache"), str(run_root / "claude-cache")],
        "auth_copies": [str(run_root / "home" / ".saxo-bank-mcp-auth")],
        "client_state": [str(run_root / "codex-home"), str(run_root / "home")],
        "todo13_public_clone": [str(run_root / "source-clone" / "src")],
    }
    report_payload: dict[str, JsonValue] = {
        "status": "passed",
        "clean": True,
        "candidate_commit": candidate_commit,
        "clone_commit": clone_commit,
        "findings": [],
        "scan_errors": [],
        "scanned_file_count": 1,
        "scope": scope,
        "notes": (
            "Privacy proof order: install report paths first; privacy-report.json next; "
            "privacy-self-scan.json last; binding digests recorded after both files exist."
        ),
    }
    report_path.write_text(json.dumps(report_payload, sort_keys=True) + "\n", encoding="utf-8")
    self_payload: dict[str, JsonValue] = {
        "status": "passed",
        "clean": True,
        "candidate_commit": candidate_commit,
        "clone_commit": clone_commit,
        "findings": [],
        "scan_errors": [],
        "scanned_file_count": 1,
        "privacy_report": report_path.name,
        "scope": {"privacy_report": [report_path.name]},
        "notes": (
            "Self-scan covers privacy-report.json only; "
            "does not embed install binding digests."
        ),
    }
    self_scan_path.write_text(json.dumps(self_payload, sort_keys=True) + "\n", encoding="utf-8")
    return report_path, self_scan_path


def _privacy_payload_errors(  # noqa: C901, PLR0912, PLR0913
    report: dict[str, JsonValue],
    self_scan: dict[str, JsonValue],
    *,
    candidate_commit: str,
    clone_commit: str,
    privacy_report: Path,
    privacy_self_scan: Path,
    binding: PrivacyEvidenceBinding | None = None,
) -> list[str]:
    errors: list[str] = []
    for label, payload in (("privacy_report", report), ("privacy_self_scan", self_scan)):
        if payload.get("status") != "passed" or payload.get("clean") is not True:
            errors.append(f"{label}_not_clean")
        findings = payload.get("findings")
        scan_errors = payload.get("scan_errors")
        if not isinstance(findings, list) or findings:
            errors.append(f"{label}_findings_present")
        if not isinstance(scan_errors, list) or scan_errors:
            errors.append(f"{label}_scan_errors_present")
        if payload.get("candidate_commit") != candidate_commit:
            errors.append(f"{label}_candidate_commit_mismatch")
        if payload.get("clone_commit") != clone_commit:
            errors.append(f"{label}_clone_commit_mismatch")
    scope = report.get("scope")
    if not isinstance(scope, dict):
        errors.append("privacy_scope_missing")
    else:
        missing = [name for name in REQUIRED_SCOPES if name not in scope]
        if missing:
            errors.append("privacy_scope_incomplete")
        for name in REQUIRED_SCOPES:
            value = scope.get(name)
            if not isinstance(value, list) or not value:
                errors.append(f"privacy_scope_empty:{name}")
    if binding is not None:
        if binding.candidate_commit != candidate_commit or binding.clone_commit != clone_commit:
            errors.append("privacy_binding_commit_mismatch")
        dirty = (
            binding.clean is not True
            or binding.findings_count != 0
            or binding.scan_errors_count != 0
        )
        if dirty:
            errors.append("privacy_binding_not_clean")
        if set(binding.scopes_covered) != set(REQUIRED_SCOPES):
            errors.append("privacy_binding_scopes_incomplete")
        if binding.digest_order != DIGEST_ORDER:
            errors.append("privacy_digest_order_invalid")
    _ = (privacy_report, privacy_self_scan)
    return errors


def _load_object(path: Path) -> dict[str, JsonValue]:
    try:
        return _JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        msg = f"privacy_payload_invalid:{path.name}"
        raise ValueError(msg) from exc


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_privacy_path(reported: str, report_dir: Path) -> Path | None:
    direct = Path(reported)
    if direct.is_file():
        return direct.resolve()
    sibling = (report_dir / reported).resolve()
    if sibling.is_file():
        return sibling
    return None
