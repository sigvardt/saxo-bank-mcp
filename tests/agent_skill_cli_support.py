from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from test_agent_skill_evidence_support import ROOT, run_cli

RELEASE_ASSEMBLER = ROOT / "scripts/assemble_agent_skill_release.py"


@dataclass(frozen=True, slots=True)
class ReleaseCommand:
    evidence: Path
    commit: str
    out: Path
    latest: Path
    plan: Path | None = None
    live: Path | None = None
    repo: Path = ROOT


def run_release(command: ReleaseCommand) -> subprocess.CompletedProcess[str]:
    args = [
        "--repo",
        str(command.repo),
        "--evidence-root",
        str(command.evidence),
        "--release",
        "release-v1",
        "--source-commit",
        command.commit,
        "--out",
        str(command.out),
        "--latest",
        str(command.latest),
    ]
    if command.plan is not None:
        args.extend(("--plan", str(command.plan)))
    if command.live is not None:
        args.extend(("--verify-live-proof", str(command.live)))
    return run_cli(RELEASE_ASSEMBLER, *args)
