#!/usr/bin/env python3
"""Produce or independently verify Codex-only plugin install evidence."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp._evidence import write_json
from saxo_bank_mcp.agent_skill_codex_install import (
    CodexInstallOptions,
    load_verified_codex_install_report,
    produce_codex_install_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate one isolated Codex plugin install.")
    parser.add_argument("--repo", type=Path, default=Path())
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--run-root", type=Path, default=Path(".omo/runtime/codex-install"))
    parser.add_argument("--codex-global-home", type=Path, required=True)
    parser.add_argument("--expected-skills", type=int, default=9)
    parser.add_argument("--expected-tools", type=int, default=60)
    parser.add_argument("--retain-proof-runtime", action="store_true")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--install-report", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.verify_only:
        if args.install_report is None:
            write_json(args.out, {"status": "failed", "reason": "install_report_required"})
            return 1
        report, errors = load_verified_codex_install_report(
            args.install_report,
            codex_global_home=args.codex_global_home,
        )
        if report is None:
            write_json(args.out, {"status": "failed", "errors": list(errors)})
            return 1
        write_json(
            args.out,
            {
                "status": "passed",
                "execution_mode": "codex_installed_verification",
                "harness_policy": "codex_native_v1",
                "candidate_commit": report.candidate_commit,
                "tool_count": report.codex.tool_count,
                "global_codex_state_unchanged": True,
                "errors": [],
            },
        )
        args.out.chmod(0o600)
        return 0
    return produce_codex_install_report(
        CodexInstallOptions(
            repo=args.repo,
            commit=str(args.commit),
            run_root=args.run_root,
            codex_global_home=args.codex_global_home,
            out=args.out,
            expected_skills=int(args.expected_skills),
            expected_tools=int(args.expected_tools),
            retain_proof_runtime=bool(args.retain_proof_runtime),
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
