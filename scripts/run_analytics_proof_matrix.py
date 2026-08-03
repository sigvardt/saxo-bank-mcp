#!/usr/bin/env python3
"""Plan or validate the redacted per-analysis correctness matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from saxo_bank_mcp.agent_skill_install_qa import load_verified_install_report
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.qa_analytics_evidence import (
    ANALYSIS_KIND_CATALOG_PATH,
    EvidenceCoverageError,
    build_proof_execution_contracts,
    load_analysis_kind_catalog,
)

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:
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
    if (
        args.install_report is None
        or args.codex_global_home is None
        or args.claude_global_home is None
        or args.fixture_cleanup_ledger is None
        or not isinstance(args.candidate_commit, str)
        or _COMMIT_PATTERN.fullmatch(args.candidate_commit) is None
    ):
        return _publish(
            args.out,
            {"status": "refused", "reason": "proof_producer_execution_context_missing"},
            success=False,
        )
    install, install_errors = load_verified_install_report(
        args.install_report,
        codex_global_home=args.codex_global_home,
        claude_global_home=args.claude_global_home,
        fixture_cleanup_ledger=args.fixture_cleanup_ledger,
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
    # Caller-authored receipt paths are deliberately not accepted. Task 24 must execute the
    # independently verified installed producer and pass its process-owned result directly to
    # the private final-validation path. Until that happens, remain fail closed.
    return _publish(
        args.out,
        {
            "status": "refused",
            "execution_performed": False,
            "reason": "proof_producer_execution_required",
            "candidate_commit": args.candidate_commit,
            "analysis_kind_count": len(contracts),
            "evidence_receipt_count": len(catalog.evidence_receipt_ids),
            "contract_sha256": _digest(contract_material),
            "live_mutation_calls": 0,
            "broker_write_made": False,
            "redacted_publication": True,
        },
        success=False,
    )


def _publish(path: Path, payload: dict[str, object], *, success: bool) -> int:
    return 0 if write_scanned_json(path, payload) and success else 1


if __name__ == "__main__":
    sys.exit(main())
