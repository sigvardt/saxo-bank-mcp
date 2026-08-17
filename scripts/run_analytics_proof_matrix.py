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
    CodexNativeProofFailureError,
    ProofProducerError,
    run_verified_codex_native_producer,
    run_verified_installed_producer,
)

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0911
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
    if args.plan_only:
        return _publish(
            args.out,
            {
                "status": "planned",
                "execution_performed": False,
                "analysis_kind_count": len(contracts),
                "evidence_receipt_count": len(catalog.evidence_receipt_ids),
                "contract_sha256": _digest(contract_material),
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
            )
            if args.harness_policy == "codex_native_v1"
            else run_verified_installed_producer(
                cast("InstallEvidenceReport", install),
                candidate_commit=args.candidate_commit,
            )
        )
    except CodexNativeProofFailureError as error:
        payload = error.receipt.model_dump(mode="json")
        payload.update(
            {
                "analysis_kind_count": len(contracts),
                "evidence_receipt_count": len(catalog.evidence_receipt_ids),
            },
        )
        return _publish(args.out, payload, success=False)
    except ProofProducerError as error:
        if args.harness_policy == "codex_native_v1":
            return _publish(
                args.out,
                _unknown_native_failure_payload(
                    candidate_commit=args.candidate_commit,
                    reason="proof_producer_native_boundary_failed",
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
            return _publish(
                args.out,
                _unknown_native_failure_payload(
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
    payload = validated.model_dump(mode="json")
    payload.update(
        {
            "analysis_kind_count": len(contracts),
            "evidence_receipt_count": len(catalog.evidence_receipt_ids),
            "contract_sha256": _digest(contract_material),
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


def _unknown_native_failure_payload(
    *,
    candidate_commit: str,
    reason: str,
) -> dict[str, JsonValue]:
    return {
        "schema_version": "1",
        "status": "refused",
        "harness_policy": "codex_native_v1",
        "candidate_commit": candidate_commit,
        "failure_evidence_status": "missing",
        "producer_authenticated": False,
        "completed_phases": None,
        "current_phase": None,
        "sim_preflight_status": "unknown",
        "sim_preflight": None,
        "network_call_made": None,
        "model_event_count": None,
        "mcp_event_count": None,
        "saxo_event_count": None,
        "execution_performed": None,
        "broker_write_made": None,
        "live_mutation_calls": None,
        "purchase_occurred": None,
        "disclaimer_response_made": None,
        "child_cleanup_status": "unknown",
        "outer_runtime_cleanup_status": "unknown",
        "reason": reason,
        "redacted_publication": True,
    }


if __name__ == "__main__":
    sys.exit(main())
