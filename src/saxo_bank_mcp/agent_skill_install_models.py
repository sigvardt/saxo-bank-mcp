from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from saxo_bank_mcp._evidence import JsonValue


class StartupCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    tool_count: int


class StartupEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: StartupCheck
    cache: StartupCheck
    list_tools: StartupCheck


class ClientInstallEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    installed: Literal[True]
    cache_root: Path
    skill_count: int
    mcp_server_count: int
    tool_count: int
    annotations_missing: tuple[str, ...]
    forbidden_cache_paths: tuple[str, ...]
    installed_bytes_match: Literal[True]
    install_command_exit_code: Literal[0]
    startup: StartupEvidence


class CloneEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_repo: Path
    no_local: Literal[True]
    clean: Literal[True]


class GlobalStateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    before: dict[str, JsonValue]
    after: dict[str, JsonValue]


class InstalledByteChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: Literal[True]
    mismatches: tuple[str, ...]


class ProcessCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: Literal[True]
    remaining_pids: tuple[int, ...]


class FixtureCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deferred_registered: Literal[True]
    preserve_for: str = Field(min_length=1)
    run_root: Path


class InstallEvidenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    execution_mode: Literal["installed_verification"]
    repo: Path
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    clone: CloneEvidence
    expected_skills: int
    expected_mcp_servers: int
    expected_tools: int
    global_state: GlobalStateEvidence
    global_state_unchanged: Literal[True]
    codex: ClientInstallEvidence
    claude: ClientInstallEvidence
    installed_byte_checks: InstalledByteChecks
    process_cleanup: ProcessCleanup
    fixture_cleanup: FixtureCleanup
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InstallManifestOptions:
    repo: Path
    commit: str
    run_root: Path
    codex_global_home: Path | None
    claude_global_home: Path | None
    expected_skills: int
    expected_tools: int
    preserve_for: str
    out: Path
