#!/usr/bin/env python3
"""Plan or validate the redacted per-analysis correctness matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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
)
from saxo_bank_mcp.qa_analytics_proof_publication import (
    CodexNativePublishedResult,
    CodexNativePublishedResultKind,
    build_codex_native_boundary_failure,
    build_codex_native_proof_publication,
)

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0911, PLR0912
    parser = argparse.ArgumentParser(
        description="Plan or validate the complete Saxo analytics proof matrix.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=ANALYSIS_KIND_CATALOG_PATH,
    )
    parser.add_argument("--candidate-commit", default=None)
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
    shared_missing = (
        args.install_report is None
        or args.codex_global_home is None
        or not isinstance(args.candidate_commit, str)
        or _COMMIT_PATTERN.fullmatch(args.candidate_commit) is None
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
                    reason="proof_producer_execution_context_missing",
                ),
                success=False,
            )
        return _publish(
            args.out,
            {"status": "refused", "reason": "proof_producer_execution_context_missing"},
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


if __name__ == "__main__":
    sys.exit(main())
