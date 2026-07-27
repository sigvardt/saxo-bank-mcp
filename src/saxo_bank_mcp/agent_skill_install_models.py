from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from saxo_bank_mcp._evidence import JsonValue


class CommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = Field(min_length=1)
    pid: int | None = None
    pgid: int | None = None
    exit_code: int
    stdout_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    stderr_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    timed_out: bool = False
    cleanup_attempted: bool = False


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
    identity: str = Field(min_length=1)
    version: str = Field(min_length=1)
    cache_root_source: str = Field(min_length=1)
    skill_count: int
    mcp_server_count: int
    tool_count: int
    annotations_missing: tuple[str, ...]
    forbidden_cache_paths: tuple[str, ...]
    installed_bytes_match: Literal[True]
    install_command_exit_code: Literal[0]
    startup: StartupEvidence
    command_receipts: tuple[CommandReceipt, ...] = ()


class CloneEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    source_repo: Path
    no_local: Literal[True]
    clean: Literal[True]
    mode: str = Field(default="0o700", min_length=1)


class GlobalStateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    before: dict[str, JsonValue]
    after: dict[str, JsonValue]


class InstalledByteChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: Literal[True]
    compared_files: int = Field(ge=1)
    metadata_exceptions: tuple[str, ...]
    required_files_present: tuple[str, ...]
    forbidden_files_absent: Literal[True]
    mismatches: tuple[str, ...]


class ProcessCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: Literal[True]
    remaining_pids: tuple[int, ...]
    observed_pids: tuple[int, ...] = ()
    observed_pgids: tuple[int, ...] = ()


class FixtureCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deferred_registered: Literal[True]
    preserve_for: str = Field(min_length=1)
    run_root: Path
    preserved_paths: tuple[str, ...] = ()
    modes: dict[str, str] = Field(min_length=1)
    owner_only: Literal[True]
    teardown_owner: str = "post-final-completion-gate"
    consumers: tuple[str, ...] = ()


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
    project_version: str = Field(min_length=1)
    help_syntax: dict[str, JsonValue]
    update_probe: dict[str, JsonValue]
    auth_files: dict[str, JsonValue]
    help_receipts: tuple[CommandReceipt, ...] = ()
    update_receipts: tuple[CommandReceipt, ...] = ()
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
    dry_run: bool = False
