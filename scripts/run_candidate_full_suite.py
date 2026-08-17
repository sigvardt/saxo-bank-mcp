#!/usr/bin/env python3
"""Run one guarded exact-candidate full suite with private retained evidence."""

from __future__ import annotations

import argparse
from pathlib import Path

from saxo_bank_mcp.qa_candidate_full_suite import (
    CandidateFullSuiteError,
    run_candidate_full_suite,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = run_candidate_full_suite(
            repo=args.repo,
            candidate_commit=args.candidate_commit,
            evidence_root=args.evidence_root,
        )
    except CandidateFullSuiteError:
        return 2
    return 0 if receipt.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
