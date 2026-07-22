from __future__ import annotations

import sys
from pathlib import Path

from saxo_bank_mcp.evidence_publication import write_scanned_text
from saxo_bank_mcp.final_verify_common import GitStateProvider, command_check, render_report

CODE_REQUIRED_PATHS = (
    "pyproject.toml",
    "src/saxo_bank_mcp",
    "tests",
    ".gitignore",
)


def verify_code(out: Path, git_state_provider: GitStateProvider) -> int:
    checks = [
        (path, Path(path).exists(), "present" if Path(path).exists() else "missing")
        for path in CODE_REQUIRED_PATHS
    ]
    checks.append(
        (
            "FastMCP server package",
            Path("src/saxo_bank_mcp/server.py").exists(),
            "required before final code approval",
        ),
    )
    checks.extend(
        (
            command_check("pytest", ("uv", "run", "pytest"), timeout=300),
            command_check("ruff", ("uv", "run", "ruff", "check", ".")),
            command_check("basedpyright", ("uv", "run", "basedpyright")),
            command_check(
                "secret-scan",
                (
                    sys.executable,
                    "-m",
                    "saxo_bank_mcp.qa",
                    "secret-scan",
                    "--paths",
                    "README.md",
                    "src",
                    "tests",
                    ".github",
                    ".gitignore",
                    "--out",
                    ".omo/evidence/saxo-bank-mcp/final-code-secret-scan.json",
                ),
            ),
            command_check(
                "live-write-refusal",
                (
                    sys.executable,
                    "-m",
                    "saxo_bank_mcp.qa",
                    "live-write-refusal",
                    "--out",
                    ".omo/evidence/saxo-bank-mcp/final-code-live-write-refusal.json",
                ),
            ),
            command_check(
                "live-read-refusal",
                (
                    sys.executable,
                    "-m",
                    "saxo_bank_mcp.qa",
                    "live-read-refusal",
                    "--out",
                    ".omo/evidence/saxo-bank-mcp/final-code-live-read-refusal.json",
                ),
            ),
        ),
    )
    passed = all(ok for _, ok, _ in checks)
    published = write_scanned_text(
        out,
        render_report(
            "Code Quality Gate",
            passed=passed,
            checks=checks,
            git_state_provider=git_state_provider,
        ),
    )
    return 0 if passed and published else 1
