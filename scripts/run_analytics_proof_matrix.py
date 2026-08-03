#!/usr/bin/env python3
"""Plan or validate the redacted per-analysis correctness matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.qa_analytics_evidence import (
    ANALYSIS_KIND_CATALOG_PATH,
    AnalyticsProofMatrixBundle,
    EvidenceCoverageError,
    build_proof_execution_contracts,
    load_analysis_kind_catalog,
    validate_proof_matrix_bundle,
)

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
    parser = argparse.ArgumentParser(
        description="Plan or validate the complete Saxo analytics proof matrix.",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=ANALYSIS_KIND_CATALOG_PATH,
    )
    parser.add_argument("--candidate-commit", default=None)
    parser.add_argument("--receipts", type=Path, default=None)
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
        args.receipts is None
        or not isinstance(args.candidate_commit, str)
        or _COMMIT_PATTERN.fullmatch(args.candidate_commit) is None
    ):
        return _publish(
            args.out,
            {"status": "refused", "reason": "proof_receipts_or_candidate_missing"},
            success=False,
        )
    try:
        payload = json.loads(args.receipts.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _publish(
            args.out,
            {"status": "failed", "reason": "proof_receipts_invalid"},
            success=False,
        )
    if not isinstance(payload, dict):
        return _publish(
            args.out,
            {"status": "failed", "reason": "proof_receipts_invalid"},
            success=False,
        )
    try:
        bundle = TypeAdapter(AnalyticsProofMatrixBundle).validate_python(payload, strict=True)
    except ValidationError:
        return _publish(
            args.out,
            {"status": "failed", "reason": "proof_receipts_invalid"},
            success=False,
        )
    errors = list(
        validate_proof_matrix_bundle(bundle, catalog=catalog, contracts=contracts),
    )
    if bundle.candidate_commit != args.candidate_commit:
        errors.append("candidate_commit_mismatch")
    passed = not errors
    return _publish(
        args.out,
        {
            "status": "passed" if passed else "failed",
            "execution_performed": True,
            "candidate_commit": args.candidate_commit,
            "analysis_kind_count": len(contracts),
            "evidence_receipt_count": len(bundle.analysis_receipts),
            "contract_sha256": _digest(contract_material),
            "receipts_sha256": _digest(bundle.model_dump(mode="json")),
            "errors": errors,
            "live_mutation_calls": 0,
            "broker_write_made": False,
            "redacted_publication": True,
        },
        success=passed,
    )


def _publish(path: Path, payload: dict[str, object], *, success: bool) -> int:
    return 0 if write_scanned_json(path, payload) and success else 1


if __name__ == "__main__":
    sys.exit(main())
