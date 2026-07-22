from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, ValidationError

from saxo_bank_mcp.live_evidence_release_models import (
    LiveReadReport,
    ManualQaReport,
    ProdReadinessReport,
)
from saxo_bank_mcp.live_evidence_release_proof_models import ProofReport, ProofSource
from saxo_bank_mcp.live_evidence_release_safety import raw_release_payload_passed
from saxo_bank_mcp.live_precheck_proof_audit import source_provenance
from saxo_bank_mcp.qa_live_evidence import private_identifier_findings
from saxo_bank_mcp.strict_json import StrictJsonError, parse_json_value

_RELEASE_MODELS: dict[str, type[BaseModel]] = {
    "proof-production.json": ProofReport,
    "live-read.json": LiveReadReport,
    "prod-readiness.json": ProdReadinessReport,
    "manual-qa.json": ManualQaReport,
}


def release_payloads_passed(paths: list[Path]) -> bool:
    if tuple(path.name for path in paths) != tuple(_RELEASE_MODELS):
        return False
    try:
        for path in paths:
            _validate_release_payload(path)
    except (OSError, StrictJsonError, ValidationError):
        return False
    return True


def _validate_release_payload(path: Path) -> None:
    model = _RELEASE_MODELS.get(path.name)
    if model is None:
        raise ReleasePayloadNameError(path.name)
    raw_payload = parse_json_value(path.read_bytes())
    if not raw_release_payload_passed(raw_payload):
        raise ReleasePayloadSafetyError
    validated = model.model_validate(raw_payload, strict=True)
    if isinstance(validated, LiveReadReport) and private_identifier_findings(raw_payload):
        raise ReleasePayloadSafetyError
    if isinstance(validated, ProofReport) and not _proof_source_passed(
        validated.source,
        Path.cwd().resolve(),
    ):
        raise ReleasePayloadSourceError
    if isinstance(validated, ManualQaReport) and not _manual_source_passed(
        validated,
        Path.cwd().resolve(),
    ):
        raise ReleasePayloadSourceError


def _proof_source_passed(source: ProofSource, repo_root: Path) -> bool:
    current = source_provenance(repo_root)
    return bool(
        current.complete
        and source.git_head == current.git_head
        and source.dirty_source_sha256 == current.dirty_source_sha256
    )


def _manual_source_passed(report: ManualQaReport, repo_root: Path) -> bool:
    if len({entry.path for entry in report.source_hashes}) != len(report.source_hashes):
        return False
    hashes: set[str] = set()
    for entry in report.source_hashes:
        candidate = Path(entry.path)
        resolved = (repo_root / candidate).resolve(strict=False)
        if (
            candidate.is_absolute()
            or not resolved.is_relative_to(repo_root)
            or not resolved.is_file()
            or resolved.stat().st_size != entry.bytes
        ):
            return False
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if digest != entry.sha256:
            return False
        hashes.add(digest)
    return report.generator_source_sha256 in hashes


class ReleasePayloadNameError(OSError):
    pass


class ReleasePayloadSourceError(OSError):
    pass


class ReleasePayloadSafetyError(OSError):
    pass
