from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import sys
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue, now_utc
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.live_evidence_gate_validation import raw_gate_manifest_passed
from saxo_bank_mcp.live_evidence_release_validation import release_payloads_passed
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.secret_scan import scan_secret_paths

_REQUIRED_RELEASE_PAYLOADS: Final = (
    "proof-production.json",
    "live-read.json",
    "prod-readiness.json",
    "manual-qa.json",
)
_BUNDLE_MANIFEST_NAME: Final = "bundle-manifest.json"


def publish_live_evidence_bundle(payload_dir: Path, receipt: Path) -> int:
    payload = payload_dir.resolve(strict=False)
    receipt = receipt.resolve(strict=False)
    if receipt.is_relative_to(payload):
        return 1
    lock_fd = _publication_lock(receipt)
    if lock_fd is None:
        return 1
    try:
        if receipt.exists():
            return 1
        return _publish_locked(payload, receipt)
    finally:
        _unlock_publication(lock_fd)


def _publish_locked(payload: Path, receipt: Path) -> int:
    raw_manifest_path = payload / "raw-gates" / "manifest.json"
    required_paths = [payload / name for name in _REQUIRED_RELEASE_PAYLOADS]
    bundle_path = payload / _BUNDLE_MANIFEST_NAME
    manifest_valid, failure_reason = _validated_raw_manifest(
        raw_manifest_path,
        required_paths,
        bundle_path,
    )
    if not manifest_valid:
        _write_failure(receipt, failure_reason)
        return 1

    release_payloads = [_file_entry(path, payload) for path in required_paths]
    release_payloads_valid = release_payloads_passed(required_paths)
    release_payloads_unchanged = release_payloads == [
        _file_entry(path, payload) for path in required_paths
    ]
    if not release_payloads_valid or not release_payloads_unchanged:
        reason = (
            "release_payload_status_failed"
            if not release_payloads_valid
            else "release_payload_changed_during_validation"
        )
        _write_failure(receipt, reason)
        return 1

    findings, scan_errors = scan_secret_paths([str(payload)])
    if findings or scan_errors:
        _write_failure(receipt, "payload_secret_scan_failed")
        return 1

    retained_files = sorted(
        path
        for path in payload.rglob("*")
        if path.is_file() and path != bundle_path
    )
    bundle: dict[str, JsonValue] = {
        "schema_version": "saxo-live-evidence-bundle-v1",
        "status": "passed",
        "created_at": now_utc(),
        "hash_algorithm": "sha256",
        "path_base": "payload_directory",
        "git": current_git_state().model_dump(mode="json"),
        "generator": "saxo_bank_mcp.live_evidence_bundle",
        "generator_source_sha256": _sha256(Path(__file__)),
        "release_payload_hash_algorithm": "sha256",
        "release_payloads": release_payloads,
        "artifact_count": len(retained_files),
        "artifacts": [_file_entry(path, payload) for path in retained_files],
        "whole_payload_secret_scan": {
            "findings": 0,
            "scan_errors": 0,
        },
    }
    if not write_scanned_json(bundle_path, bundle):
        _write_failure(receipt, "bundle_manifest_publication_failed")
        return 1

    receipt_payload: dict[str, JsonValue] = {
        "schema_version": "saxo-live-evidence-receipt-v1",
        "status": "passed",
        "created_at": now_utc(),
        "bundle_manifest_path": str(bundle_path.relative_to(payload.parent)),
        "bundle_manifest_bytes": bundle_path.stat().st_size,
        "bundle_manifest_sha256": _sha256(bundle_path),
        "artifact_count": len(retained_files),
        "hash_algorithm": "sha256",
    }
    return 0 if write_scanned_json(receipt, receipt_payload) else 1


def _publication_lock(receipt: Path) -> int | None:
    lock_fd: int | None = None
    try:
        lock_fd = os.open(receipt.parent, os.O_RDONLY | os.O_CLOEXEC)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if lock_fd is not None:
            os.close(lock_fd)
        return None
    return lock_fd


def _unlock_publication(lock_fd: int) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _validated_raw_manifest(
    manifest_path: Path,
    required_paths: list[Path],
    bundle_path: Path,
) -> tuple[bool, str]:
    if not manifest_path.is_file() or any(not path.is_file() for path in required_paths):
        return False, "required_payload_missing"
    if bundle_path.exists():
        return False, "bundle_manifest_already_exists"
    if not raw_gate_manifest_passed(manifest_path, Path.cwd().resolve()):
        return False, "raw_gate_integrity_failed"
    return True, ""


def _file_entry(path: Path, payload_dir: Path) -> dict[str, JsonValue]:
    return {
        "path": path.relative_to(payload_dir).as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_failure(receipt: Path, reason: str) -> None:
    write_scanned_json(receipt, {"status": "failed", "reason": reason})


def main() -> None:
    parser = argparse.ArgumentParser(description="Bind a Saxo LIVE evidence payload to hashes.")
    parser.add_argument("--payload-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    sys.exit(publish_live_evidence_bundle(args.payload_dir, args.receipt))


if __name__ == "__main__":
    main()
