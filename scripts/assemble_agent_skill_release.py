#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/assemble_agent_skill_release.py --next-release --print-release
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp.agent_skill_release import (
    ReleaseAssembleOptions,
    assemble_release,
    next_release,
    self_test_release_fixture,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble Saxo agent skill release evidence.")
    parser.add_argument("--plan", type=Path, default=None)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--next-release", action="store_true")
    parser.add_argument("--print-release", action="store_true")
    parser.add_argument("--release", default=None)
    parser.add_argument("--source-commit", default="")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--latest", type=Path, default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--verify-live-proof", type=Path, default=None)
    parser.add_argument(
        "--self-test-fixture",
        choices=("stale-commit", "missing-task", "secret-bearing-artifact"),
    )
    args = parser.parse_args(argv)
    if args.self_test_fixture is not None:
        if args.out is None:
            sys.stderr.write("missing --out for --self-test-fixture\n")
            return 1
        return self_test_release_fixture(str(args.self_test_fixture), args.out)
    release = next_release(args.evidence_root) if args.next_release else args.release
    if release is None:
        sys.stderr.write("missing --release or --next-release\n")
        return 1
    if args.print_release:
        sys.stdout.write(f"{release}\n")
        return 0
    if args.out is None or args.latest is None:
        sys.stderr.write("missing --out or --latest\n")
        return 1
    return assemble_release(
        ReleaseAssembleOptions(
            evidence_root=args.evidence_root,
            release=str(release),
            source_commit=str(args.source_commit),
            out=args.out,
            latest=args.latest,
            check=bool(args.check),
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
