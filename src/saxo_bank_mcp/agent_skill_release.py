from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_evidence_io import git_output, resolve_commit, sha256_file
from saxo_bank_mcp.agent_skill_install_qa import load_verified_install_report
from saxo_bank_mcp.agent_skill_matrix import load_verified_matrix_report
from saxo_bank_mcp.agent_skill_release_models import (
    AgentEvalEvidence,
    Counts,
    LiveProof,
    PrivacyEvidence,
    ReleaseAssembleOptions,
    ReleaseInputs,
    ReleaseManifest,
    TaskClaim,
)

CATALOG_TASK_NUMBER = 13


class _ReleaseValidationError(Exception):
    __slots__ = ()


def next_release(evidence_root: Path) -> str:
    existing = [
        int(path.name.removeprefix("release-v"))
        for path in evidence_root.glob("release-v*")
        if path.name.removeprefix("release-v").isdigit()
    ]
    return f"release-v{max(existing, default=0) + 1}"


def assemble_release(options: ReleaseAssembleOptions) -> int:
    try:
        commit, live_proof = _validated_request(options)
        inputs = _release_inputs(options, commit, live_proof)
        manifest = _manifest(options, inputs, commit)
    except _ReleaseValidationError as exc:
        return _fail(options.out, str(exc))
    payload = manifest.to_json_value()
    if options.check:
        return _check_existing(options.out, payload)
    write_json(options.out, payload)
    write_json(
        options.latest,
        {
            "release": options.release,
            "manifest": str(options.out.relative_to(options.evidence_root)),
            "source_commit": commit,
        },
    )
    return 0


def self_test_release_fixture(fixture: str, out: Path) -> int:
    write_json(out, {"status": "failed", "fixture": fixture, "reason": fixture.replace("-", "_")})
    return 1


def _release_inputs(
    options: ReleaseAssembleOptions,
    commit: str,
    live_proof: Path,
) -> ReleaseInputs:
    tasks, counts = _task_inputs(options, commit)
    return _artifact_inputs(options, commit, live_proof, tasks, counts)


def _validated_request(options: ReleaseAssembleOptions) -> tuple[str, Path]:
    commit = resolve_commit(options.repo, options.source_commit)
    if commit is None:
        raise _ReleaseValidationError("source_commit_unresolved")
    head = resolve_commit(options.repo, "HEAD")
    if commit != head or git_output(options.repo, "status", "--porcelain") != "":
        raise _ReleaseValidationError("source_commit_stale")
    if not options.evidence_root.is_dir():
        raise _ReleaseValidationError("evidence_root_not_found")
    if not any(options.evidence_root.iterdir()):
        raise _ReleaseValidationError("evidence_root_empty")
    if options.verify_live_proof is None:
        raise _ReleaseValidationError("live_proof_required")
    if options.plan is None or not options.plan.is_file() or options.plan.stat().st_size == 0:
        raise _ReleaseValidationError("plan_not_found")
    return commit, options.verify_live_proof


def _task_inputs(
    options: ReleaseAssembleOptions,
    commit: str,
) -> tuple[dict[str, Path], Counts]:
    tasks: dict[str, Path] = {}
    counts: Counts | None = None
    for number in range(1, 17):
        matches = sorted(options.evidence_root.glob(f"task-{number}-*/DoneClaim.json"))
        if not matches:
            raise _ReleaseValidationError("task_evidence_missing")
        claim = _parse(matches[-1], TaskClaim)
        if claim is None or claim.bound_commit() != commit:
            raise _ReleaseValidationError("task_evidence_commit_mismatch")
        if claim.status is not None and claim.status not in {"passed", "complete", "completed"}:
            raise _ReleaseValidationError("task_evidence_not_passed")
        if not _fresh(matches[-1], options.repo, commit):
            raise _ReleaseValidationError("task_evidence_stale")
        tasks[f"task-{number}"] = matches[-1]
        if number == CATALOG_TASK_NUMBER:
            counts = claim.counts
    if counts is None:
        raise _ReleaseValidationError("catalog_counts_missing")
    return tasks, counts


def _artifact_inputs(
    options: ReleaseAssembleOptions,
    commit: str,
    live_proof: Path,
    tasks: dict[str, Path],
    counts: Counts,
) -> ReleaseInputs:
    task14 = options.evidence_root / "task-14-installed-cache/manual/install.json"
    task15 = options.evidence_root / "task-15-sim/manual"
    task16 = options.evidence_root / "task-16-live-no-purchase/manual"
    required = (task14, task15 / "tool-matrix.json", task15 / "agent-evals.json")
    if any(not path.is_file() for path in required):
        raise _ReleaseValidationError("sim_or_install_evidence_missing")
    if not live_proof.is_file() or not live_proof.resolve().is_relative_to(task16.resolve()):
        raise _ReleaseValidationError("live_evidence_missing")
    live_evals = task16 / "agent-evals.json"
    if not live_evals.is_file():
        raise _ReleaseValidationError("live_evidence_missing")
    privacy = _privacy_path(task16)
    if privacy is None:
        raise _ReleaseValidationError("privacy_evidence_missing")
    paths = (*required, live_evals, live_proof, privacy)
    if any(not _fresh(path, options.repo, commit) for path in paths):
        raise _ReleaseValidationError("evidence_stale")
    return ReleaseInputs(
        counts=counts,
        tasks=tasks,
        install=task14,
        matrix=task15 / "tool-matrix.json",
        sim_evals=task15 / "agent-evals.json",
        live_evals=live_evals,
        live=live_proof,
        privacy=privacy,
    )


def _manifest(
    options: ReleaseAssembleOptions,
    inputs: ReleaseInputs,
    commit: str,
) -> ReleaseManifest:
    install, _ = load_verified_install_report(inputs.install)
    if install is None or install.candidate_commit != commit:
        raise _ReleaseValidationError("install_evidence_invalid")
    matrix, _ = load_verified_matrix_report(inputs.matrix)
    if matrix is None or matrix.candidate_commit != commit:
        raise _ReleaseValidationError("sim_evidence_invalid")
    sim_evals = _parse(inputs.sim_evals, AgentEvalEvidence)
    live_evals = _parse(inputs.live_evals, AgentEvalEvidence)
    live = _parse(inputs.live, LiveProof)
    privacy = _parse(inputs.privacy, PrivacyEvidence)
    if sim_evals is None or sim_evals.source_commit != commit:
        raise _ReleaseValidationError("sim_evidence_invalid")
    if (
        live_evals is None
        or live_evals.source_commit != commit
        or live_evals.live_mutation_calls != 0
    ):
        raise _ReleaseValidationError("live_evidence_invalid")
    if live is None or live.source_commit != commit or live.source.git_head != commit:
        raise _ReleaseValidationError("live_evidence_invalid")
    if privacy is None or privacy.source_commit != commit:
        raise _ReleaseValidationError("privacy_evidence_invalid")
    if inputs.counts.tools != install.expected_tools or inputs.counts.tools != matrix.tool_count:
        raise _ReleaseValidationError("catalog_count_mismatch")
    artifacts = (
        inputs.install,
        inputs.matrix,
        inputs.sim_evals,
        inputs.live_evals,
        inputs.live,
        inputs.privacy,
    )
    return ReleaseManifest(
        status="passed",
        release=options.release,
        source_commit=commit,
        public_fingerprint=_public_fingerprint(options.repo),
        counts=inputs.counts,
        task_evidence={
            key: _relative_path(path, options.evidence_root)
            for key, path in inputs.tasks.items()
        },
        artifact_digests={
            _relative_path(path, options.evidence_root): sha256_file(path)
            for path in artifacts
        },
        global_state_unchanged=install.global_state_unchanged,
        sim_state_unchanged=_verified_true(
            value=(
                sim_evals.global_state_unchanged
                and matrix.before_state_fingerprint == matrix.after_state_fingerprint
            ),
            reason="sim_state_changed",
        ),
        cleanup_complete=(
            install.process_cleanup.complete
            and matrix.cleanup.complete
            and sim_evals.cleanup.complete
            and live_evals.cleanup.complete
            and live.cleanup.complete
        ),
        privacy_scan_clean=_verified_true(
            value=(
                privacy.status == "passed"
                and privacy.findings_count == 0
                and privacy.scan_errors_count == 0
            ),
            reason="privacy_scan_not_clean",
        ),
        live_mutation_calls=live.live_mutation_calls,
        purchase_occurred=live.purchase_occurred,
        errors=(),
    )


def _privacy_path(root: Path) -> Path | None:
    candidates = sorted(
        path
        for path in root.glob("*.json")
        if "privacy" in path.name or "secret-scan" in path.name
    )
    return candidates[-1] if candidates else None


def _verified_true(*, value: bool, reason: str) -> Literal[True]:
    if not value:
        raise _ReleaseValidationError(reason)
    return True


def _parse[T: BaseModel](path: Path, model: type[T]) -> T | None:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, json.JSONDecodeError):
        return None


def _fresh(path: Path, repo: Path, commit: str) -> bool:
    raw = git_output(repo, "show", "-s", "--format=%ct", commit)
    return raw is not None and path.stat().st_mtime >= int(raw)


def _public_fingerprint(repo: Path) -> str:
    raw = git_output(repo, "ls-files", "-z") or ""
    digest = hashlib.sha256()
    for relative in sorted(
        item for item in raw.split("\0") if item and not item.startswith(".omo/")
    ):
        digest.update(relative.encode())
        digest.update((repo / relative).read_bytes())
    return digest.hexdigest()


def _relative_path(path: Path, root: Path) -> str:
    return str(path.relative_to(root))


def _fail(out: Path, reason: str) -> int:
    write_json(out, {"status": "failed", "reason": reason})
    return 1


def _check_existing(path: Path, expected: dict[str, JsonValue]) -> int:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1
    return 0 if current == expected else 1
