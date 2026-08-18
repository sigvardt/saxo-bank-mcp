#!/usr/bin/env python3
"""Plan or validate the redacted per-analysis correctness matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_codex_install import (
    CodexInstallEvidenceReport,
    load_verified_codex_install_report,
)
from saxo_bank_mcp.agent_skill_install_models import InstallEvidenceReport
from saxo_bank_mcp.agent_skill_install_qa import load_verified_install_report
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.qa_analytics_evidence import (
    ANALYSIS_KIND_CATALOG_PATH,
    EvidenceCoverageError,
    build_proof_execution_contracts,
    load_analysis_kind_catalog,
)
from saxo_bank_mcp.qa_analytics_proof_producer import (
    CodexNativeBoundaryFailureError,
    CodexNativeProofFailureError,
    ProofProducerError,
    run_verified_codex_native_producer,
    run_verified_installed_producer,
    validate_codex_native_candidate_source_root,
)
from saxo_bank_mcp.qa_analytics_proof_publication import (
    CodexNativePublishedResult,
    CodexNativePublishedResultKind,
    build_codex_native_boundary_failure,
    build_codex_native_proof_publication,
)

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_OWNER_FILE_MODE = 0o600
_CANDIDATE_SOURCE_ENV = "SAXO_ANALYTICS_CANDIDATE_SOURCE_ROOT"
_RUNNER_RELATIVE = Path("scripts/run_analytics_proof_matrix.py")
_PRODUCER_RELATIVE = Path("src/saxo_bank_mcp/qa_analytics_proof_producer.py")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0911, PLR0912, PLR0915
    parser = argparse.ArgumentParser(
        description="Plan or validate the complete Saxo analytics proof matrix.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=ANALYSIS_KIND_CATALOG_PATH,
    )
    parser.add_argument("--candidate-commit", default=None)
    parser.add_argument("--candidate-source-root", type=Path, default=None)
    parser.add_argument("--candidate-root-bound", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install-report", type=Path, default=None)
    parser.add_argument("--codex-global-home", type=Path, default=None)
    parser.add_argument("--claude-global-home", type=Path, default=None)
    parser.add_argument("--fixture-cleanup-ledger", type=Path, default=None)
    parser.add_argument(
        "--harness-policy",
        choices=("dual_v1", "codex_native_v1"),
        default="dual_v1",
    )
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        catalog = load_analysis_kind_catalog(args.catalog)
        contracts = build_proof_execution_contracts(catalog=catalog)
    except EvidenceCoverageError:
        write_scanned_json(
            args.out,
            {"status": "failed", "reason": "analytics_evidence_catalog_invalid"},
        )
        return 1
    contract_material = [contract.model_dump(mode="json") for contract in contracts]
    contract_sha256 = _digest(contract_material)
    if args.plan_only:
        return _publish(
            args.out,
            {
                "status": "planned",
                "execution_performed": False,
                "analysis_kind_count": len(contracts),
                "evidence_receipt_count": len(catalog.evidence_receipt_ids),
                "contract_sha256": contract_sha256,
                "reason": "final_execution_deferred",
            },
            success=True,
        )
    native_source_missing = (
        args.harness_policy == "codex_native_v1" and args.candidate_source_root is None
    )
    shared_missing = (
        args.install_report is None
        or args.codex_global_home is None
        or not isinstance(args.candidate_commit, str)
        or _COMMIT_PATTERN.fullmatch(args.candidate_commit) is None
        or native_source_missing
    )
    dual_missing = args.harness_policy == "dual_v1" and (
        args.claude_global_home is None or args.fixture_cleanup_ledger is None
    )
    if shared_missing or dual_missing:
        if (
            args.harness_policy == "codex_native_v1"
            and isinstance(args.candidate_commit, str)
            and _COMMIT_PATTERN.fullmatch(args.candidate_commit) is not None
        ):
            return _publish_native(
                args.out,
                candidate_commit=args.candidate_commit,
                analysis_kind_count=len(contracts),
                evidence_receipt_count=len(catalog.evidence_receipt_ids),
                contract_sha256=contract_sha256,
                result_kind="boundary_failure",
                result=build_codex_native_boundary_failure(
                    candidate_commit=args.candidate_commit,
                    reason=(
                        "proof_source_root_missing"
                        if native_source_missing
                        else "proof_producer_execution_context_missing"
                    ),
                ),
                success=False,
            )
        return _publish(
            args.out,
            {"status": "refused", "reason": "proof_producer_execution_context_missing"},
            success=False,
        )
    candidate_source_root: Path | None = None
    if args.harness_policy == "codex_native_v1":
        try:
            launch_commit, launch_tree = _load_candidate_launch_binding(
                cast("Path", args.install_report),
                candidate_commit=cast("str", args.candidate_commit),
            )
            candidate_source_root = validate_codex_native_candidate_source_root(
                cast("Path", args.candidate_source_root),
                candidate_commit=launch_commit,
                candidate_tree=launch_tree,
            )
            if args.candidate_root_bound:
                _require_candidate_entrypoint_binding(candidate_source_root)
            else:
                return _run_candidate_entrypoint(
                    candidate_source_root,
                    argv=list(sys.argv[1:] if argv is None else argv),
                    out=args.out,
                )
        except ProofProducerError as error:
            reason = str(error)
            if re.fullmatch(r"proof_[a-z0-9_]{1,122}", reason) is None:
                reason = "proof_source_root_invalid"
            return _publish_native(
                args.out,
                candidate_commit=cast("str", args.candidate_commit),
                analysis_kind_count=len(contracts),
                evidence_receipt_count=len(catalog.evidence_receipt_ids),
                contract_sha256=contract_sha256,
                result_kind="boundary_failure",
                result=build_codex_native_boundary_failure(
                    candidate_commit=cast("str", args.candidate_commit),
                    reason=reason,
                ),
                success=False,
            )
    if args.harness_policy == "codex_native_v1":
        install, install_errors = load_verified_codex_install_report(
            cast("Path", args.install_report),
            codex_global_home=cast("Path", args.codex_global_home),
        )
    else:
        install, install_errors = load_verified_install_report(
            cast("Path", args.install_report),
            codex_global_home=cast("Path", args.codex_global_home),
            claude_global_home=cast("Path", args.claude_global_home),
            fixture_cleanup_ledger=cast("Path", args.fixture_cleanup_ledger),
        )
    if install is None:
        if args.harness_policy == "codex_native_v1":
            return _publish_native(
                args.out,
                candidate_commit=cast("str", args.candidate_commit),
                analysis_kind_count=len(contracts),
                evidence_receipt_count=len(catalog.evidence_receipt_ids),
                contract_sha256=contract_sha256,
                result_kind="boundary_failure",
                result=build_codex_native_boundary_failure(
                    candidate_commit=cast("str", args.candidate_commit),
                    reason="proof_installed_candidate_unverified",
                ),
                success=False,
            )
        return _publish(
            args.out,
            {
                "status": "refused",
                "reason": "proof_installed_candidate_unverified",
                "errors": list(install_errors),
            },
            success=False,
        )
    if install.candidate_commit != args.candidate_commit:
        if args.harness_policy == "codex_native_v1":
            return _publish_native(
                args.out,
                candidate_commit=cast("str", args.candidate_commit),
                analysis_kind_count=len(contracts),
                evidence_receipt_count=len(catalog.evidence_receipt_ids),
                contract_sha256=contract_sha256,
                result_kind="boundary_failure",
                result=build_codex_native_boundary_failure(
                    candidate_commit=cast("str", args.candidate_commit),
                    reason="proof_installed_candidate_mismatch",
                ),
                success=False,
            )
        return _publish(
            args.out,
            {"status": "failed", "reason": "proof_installed_candidate_mismatch"},
            success=False,
        )
    try:
        validated = (
            run_verified_codex_native_producer(
                cast("CodexInstallEvidenceReport", install),
                candidate_commit=args.candidate_commit,
                install_report_path=cast("Path", args.install_report).resolve(),
                source_repo=cast("Path", candidate_source_root),
            )
            if args.harness_policy == "codex_native_v1"
            else run_verified_installed_producer(
                cast("InstallEvidenceReport", install),
                candidate_commit=args.candidate_commit,
            )
        )
    except CodexNativeProofFailureError as error:
        return _publish_native(
            args.out,
            candidate_commit=cast("str", args.candidate_commit),
            analysis_kind_count=len(contracts),
            evidence_receipt_count=len(catalog.evidence_receipt_ids),
            contract_sha256=contract_sha256,
            result_kind="verified_child_failure",
            result=error.receipt,
            success=False,
        )
    except CodexNativeBoundaryFailureError as error:
        return _publish_native(
            args.out,
            candidate_commit=cast("str", args.candidate_commit),
            analysis_kind_count=len(contracts),
            evidence_receipt_count=len(catalog.evidence_receipt_ids),
            contract_sha256=contract_sha256,
            result_kind="boundary_failure",
            result=build_codex_native_boundary_failure(
                candidate_commit=cast("str", args.candidate_commit),
                reason=error.reason,
                boundary_phase=error.boundary_phase,
                command_state=error.command_state,
                cleanup_status=error.cleanup_status,
                runtime_consumption_intent_sha256=(error.runtime_consumption_intent_sha256),
                runtime_cleanup_receipt_sha256=error.runtime_cleanup_receipt_sha256,
            ),
            success=False,
        )
    except ProofProducerError as error:
        if args.harness_policy == "codex_native_v1":
            reason = str(error)
            if re.fullmatch(r"proof_[a-z0-9_]{1,122}", reason) is None:
                reason = "proof_producer_native_boundary_failed"
            return _publish_native(
                args.out,
                candidate_commit=cast("str", args.candidate_commit),
                analysis_kind_count=len(contracts),
                evidence_receipt_count=len(catalog.evidence_receipt_ids),
                contract_sha256=contract_sha256,
                result_kind="boundary_failure",
                result=build_codex_native_boundary_failure(
                    candidate_commit=args.candidate_commit,
                    reason=reason,
                ),
                success=False,
            )
        return _publish(
            args.out,
            {
                "status": "refused",
                "execution_performed": False,
                "reason": str(error),
                "live_mutation_calls": 0,
                "broker_write_made": False,
                "redacted_publication": True,
            },
            success=False,
        )
    except (OSError, ValueError):
        if args.harness_policy == "codex_native_v1":
            return _publish_native(
                args.out,
                candidate_commit=cast("str", args.candidate_commit),
                analysis_kind_count=len(contracts),
                evidence_receipt_count=len(catalog.evidence_receipt_ids),
                contract_sha256=contract_sha256,
                result_kind="boundary_failure",
                result=build_codex_native_boundary_failure(
                    candidate_commit=args.candidate_commit,
                    reason="proof_producer_local_boundary_failed",
                ),
                success=False,
            )
        return _publish(
            args.out,
            {
                "status": "refused",
                "execution_performed": False,
                "reason": "proof_producer_local_boundary_failed",
                "live_mutation_calls": 0,
                "broker_write_made": False,
                "redacted_publication": True,
            },
            success=False,
        )
    if args.harness_policy == "codex_native_v1":
        return _publish_native(
            args.out,
            candidate_commit=cast("str", args.candidate_commit),
            analysis_kind_count=len(contracts),
            evidence_receipt_count=len(catalog.evidence_receipt_ids),
            contract_sha256=contract_sha256,
            result_kind="verified_result",
            result=cast("CodexNativePublishedResult", validated),
            success=validated.status == "validated",
        )
    payload = validated.model_dump(mode="json")
    payload.update(
        {
            "analysis_kind_count": len(contracts),
            "evidence_receipt_count": len(catalog.evidence_receipt_ids),
            "contract_sha256": contract_sha256,
            "redacted_publication": True,
        },
    )
    return _publish(
        args.out,
        payload,
        success=validated.status == "validated",
    )


def _publish(path: Path, payload: Mapping[str, JsonValue], *, success: bool) -> int:
    return 0 if write_scanned_json(path, payload) and success else 1


def _publish_native(  # noqa: PLR0913
    path: Path,
    *,
    candidate_commit: str,
    analysis_kind_count: int,
    evidence_receipt_count: int,
    contract_sha256: str,
    result_kind: CodexNativePublishedResultKind,
    result: CodexNativePublishedResult,
    success: bool,
) -> int:
    publication = build_codex_native_proof_publication(
        candidate_commit=candidate_commit,
        analysis_kind_count=analysis_kind_count,
        evidence_receipt_count=evidence_receipt_count,
        contract_sha256=contract_sha256,
        result_kind=result_kind,
        result=result,
    )
    return _publish(
        path,
        cast("Mapping[str, JsonValue]", publication.model_dump(mode="json")),
        success=success,
    )


def _load_candidate_launch_binding(
    path: Path,
    *,
    candidate_commit: str,
) -> tuple[str, str]:
    try:
        metadata = os.lstat(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProofProducerError("proof_installed_candidate_unverified") from exc
    if not (
        path.is_absolute()
        and stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _OWNER_FILE_MODE
        and isinstance(payload, dict)
    ):
        raise ProofProducerError("proof_installed_candidate_unverified")
    report_commit = payload.get("candidate_commit")
    proof_runtime = payload.get("proof_runtime")
    binding = proof_runtime.get("binding") if isinstance(proof_runtime, dict) else None
    candidate_tree = binding.get("candidate_tree") if isinstance(binding, dict) else None
    if (
        not isinstance(report_commit, str)
        or _COMMIT_PATTERN.fullmatch(report_commit) is None
        or not isinstance(candidate_tree, str)
        or _COMMIT_PATTERN.fullmatch(candidate_tree) is None
    ):
        raise ProofProducerError("proof_installed_candidate_unverified")
    if report_commit != candidate_commit:
        raise ProofProducerError("proof_installed_candidate_mismatch")
    return report_commit, candidate_tree


def _require_candidate_entrypoint_binding(candidate_source_root: Path) -> None:
    expected_runner = candidate_source_root / _RUNNER_RELATIVE
    expected_producer = candidate_source_root / _PRODUCER_RELATIVE
    producer_module = sys.modules.get(run_verified_codex_native_producer.__module__)
    producer_file = getattr(producer_module, "__file__", None)
    if (
        Path(__file__).resolve() != expected_runner
        or not isinstance(producer_file, str)
        or Path(producer_file).resolve() != expected_producer
        or os.environ.get(_CANDIDATE_SOURCE_ENV) != str(candidate_source_root)
    ):
        raise ProofProducerError("proof_source_entrypoint_mismatch")


def _run_candidate_entrypoint(
    candidate_source_root: Path,
    *,
    argv: list[str],
    out: Path,
) -> int:
    command = (
        str(Path(sys.executable).resolve()),
        "-B",
        str(candidate_source_root / _RUNNER_RELATIVE),
        *argv,
        "--candidate-root-bound",
    )
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(candidate_source_root / "src")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env[_CANDIDATE_SOURCE_ENV] = str(candidate_source_root)
    try:
        completed = subprocess.run(
            command,
            cwd=candidate_source_root,
            env=env,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ProofProducerError("proof_candidate_runner_start_failed") from exc
    if not out.is_file():
        raise ProofProducerError("proof_candidate_runner_result_missing")
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
