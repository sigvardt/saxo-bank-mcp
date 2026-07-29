#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/qa_dual_plugin_install.py --repo . --commit HEAD --out install.json
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp.agent_skill_install_models import InstallManifestOptions
from saxo_bank_mcp.agent_skill_install_producer import real_install_report
from saxo_bank_mcp.agent_skill_install_qa import (
    manifest_install_report,
    verify_install_report,
    write_install_fixture,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate isolated dual-plugin installation inputs."
    )
    parser.add_argument("--repo", type=Path, default=Path())
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--run-root", type=Path, default=Path(".omo/runtime/install"))
    parser.add_argument("--codex-global-home", type=Path, default=None)
    parser.add_argument("--claude-global-home", type=Path, default=None)
    parser.add_argument("--expected-skills", type=int, default=8)
    parser.add_argument("--expected-tools", type=int, default=39)
    parser.add_argument("--preserve-for", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--install-report", type=Path, default=None)
    parser.add_argument(
        "--fixture-cleanup-ledger",
        type=Path,
        default=None,
        help="Absolute path to the external durable JSONL cleanup ledger",
    )
    parser.add_argument("--privacy-report", type=Path, default=None)
    parser.add_argument("--privacy-self-scan", type=Path, default=None)
    parser.add_argument("--self-test-fixture", choices=("private-file", "version-drift"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.self_test_fixture is not None:
        return write_install_fixture(str(args.self_test_fixture), args.out)
    if args.verify_only:
        if args.install_report is None:
            sys.stderr.write("missing --install-report for --verify-only\n")
            return 1
        return verify_install_report(
            args.install_report,
            args.out,
            codex_global_home=args.codex_global_home,
            claude_global_home=args.claude_global_home,
            fixture_cleanup_ledger=args.fixture_cleanup_ledger,
        )
    runner = manifest_install_report if args.dry_run else real_install_report
    return runner(
        InstallManifestOptions(
            repo=args.repo,
            commit=str(args.commit),
            run_root=args.run_root,
            codex_global_home=args.codex_global_home,
            claude_global_home=args.claude_global_home,
            expected_skills=int(args.expected_skills),
            expected_tools=int(args.expected_tools),
            preserve_for=str(args.preserve_for),
            out=args.out,
            dry_run=bool(args.dry_run),
            fixture_cleanup_ledger=args.fixture_cleanup_ledger,
            privacy_report=args.privacy_report,
            privacy_self_scan=args.privacy_self_scan,
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
