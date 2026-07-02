from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from shutil import which

from pydantic import BaseModel, ConfigDict, Field

from saxo_bank_mcp._evidence import JsonValue, now_utc


class GitState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha: str
    dirty: bool
    unavailable_reason: str | None = None


class LoopManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    command: tuple[str, ...] = Field(min_length=1)
    expected_status: str = Field(min_length=1)
    evidence_paths: tuple[str, ...] = ()
    created_at: str
    git: GitState

    def to_json_value(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "command": list(self.command),
            "expected_status": self.expected_status,
            "evidence_paths": list(self.evidence_paths),
            "created_at": self.created_at,
            "git": self.git.model_dump(mode="json"),
        }


@dataclass(frozen=True)
class ManifestSpec:
    run_id: str
    scenario_id: str
    command: tuple[str, ...]
    expected_status: str
    evidence_paths: tuple[str, ...]


def current_git_state(cwd: Path = Path()) -> GitState:
    git = which("git")
    if git is None:
        return GitState(sha="unavailable", dirty=True, unavailable_reason="git_not_found")
    try:
        sha = subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        status = subprocess.run(
            [git, "status", "--porcelain"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        return GitState(sha="unavailable", dirty=True, unavailable_reason=type(exc).__name__)
    return GitState(sha=sha, dirty=bool(status.strip()))


def build_manifest(spec: ManifestSpec, cwd: Path = Path()) -> LoopManifest:
    return LoopManifest(
        run_id=spec.run_id,
        scenario_id=spec.scenario_id,
        command=spec.command,
        expected_status=spec.expected_status,
        evidence_paths=spec.evidence_paths,
        created_at=now_utc(),
        git=current_git_state(cwd),
    )
