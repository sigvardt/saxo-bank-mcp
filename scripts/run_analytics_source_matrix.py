#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from saxo_bank_mcp.qa_analytics_source_matrix import (
    ANALYTICS_SOURCE_CANDIDATE,
    DEFAULT_EVIDENCE_PATH,
    SourceMatrixFixtures,
    execute_analytics_source_matrix_once,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the one-shot Saxo SIM analytics source-contract matrix.",
    )
    parser.add_argument("--environment", required=True, choices=("SIM",))
    parser.add_argument("--candidate-commit", default=ANALYTICS_SOURCE_CANDIDATE)
    parser.add_argument("--out", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument("--instrument-uic", type=int, default=211)
    parser.add_argument("--asset-type", default="Stock")
    parser.add_argument("--option-root-id", type=int, default=120)
    parser.add_argument("--allow-controlled-activity", action="store_true")
    args = parser.parse_args(argv)
    if args.environment != "SIM":
        return 1
    fixtures = SourceMatrixFixtures(
        account_key=os.environ.get("SAXO_MCP_QA_ACCOUNT_KEY", ""),
        client_key=os.environ.get("SAXO_MCP_QA_CLIENT_KEY", ""),
        instrument_uic=args.instrument_uic,
        asset_type=args.asset_type,
        option_root_id=args.option_root_id,
    )
    return execute_analytics_source_matrix_once(
        out=args.out,
        env=os.environ,
        fixtures=fixtures,
        candidate_commit=args.candidate_commit,
        allow_controlled_activity=args.allow_controlled_activity,
    )


if __name__ == "__main__":
    sys.exit(main())
