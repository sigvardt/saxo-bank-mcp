from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from saxo_bank_mcp._evidence import JsonValue

SHA256_HEX = r"^[a-f0-9]{64}$"
COMMIT_HEX = r"^[a-f0-9]{40}$"
EXPECTED_TOOLS = 39
ALLOWED_PRODUCTION_CACHE_SOURCES: frozenset[str] = frozenset(
    {
        "codex_plugin_add",
        "codex_plugin_add_bumped",
        "codex_plugin_add_restored",
        "claude_plugin_list",
        "claude_plugin_list_bumped",
        "claude_plugin_list_restored",
    },
)
REQUIRED_FIXTURE_CONSUMERS: tuple[str, ...] = (
    "task-15",
    "task-16",
    "final-f3",
    "final-f4",
    "post-final-h1",
)
FIXTURE_TEARDOWN_OWNER = "post-final-completion-gate"


class CommandReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = Field(min_length=1)
    pid: int | None = None
    pgid: int | None = None
    exit_code: int
    stdout_sha256: str = Field(pattern=SHA256_HEX)
    stderr_sha256: str = Field(pattern=SHA256_HEX)
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
    skills: tuple[str, ...] = ()
    mcp_server_count: int
    tool_count: int
    annotations_missing: tuple[str, ...]
    source_annotations_missing: tuple[str, ...] = ()
    cache_annotations_missing: tuple[str, ...] = ()
    forbidden_cache_paths: tuple[str, ...]
    installed_bytes_match: Literal[True]
    install_command_exit_code: Literal[0]
    startup: StartupEvidence
    command_receipts: tuple[CommandReceipt, ...] = Field(min_length=1)
    inventory: dict[str, JsonValue] = Field(default_factory=dict)


class CloneEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    commit: str = Field(pattern=COMMIT_HEX)
    source_repo: Path
    no_local: Literal[True]
    clean: Literal[True]
    mode: str = Field(min_length=1)


class GlobalStateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    before: dict[str, JsonValue]
    after: dict[str, JsonValue]
    scope: dict[str, JsonValue] = Field(default_factory=dict)


class InstalledByteChecks(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: Literal[True]
    compared_files: int = Field(ge=1)
    metadata_exceptions: tuple[str, ...]
    required_files_present: tuple[str, ...]
    forbidden_files_absent: Literal[True]
    mismatches: tuple[str, ...]
    inventory_exact_match: Literal[True] = True


class ProcessCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: Literal[True]
    remaining_pids: tuple[int, ...]
    remaining_pgids: tuple[int, ...] = ()
    observed_pids: tuple[int, ...] = ()
    observed_pgids: tuple[int, ...] = ()


class VersionCacheProof(BaseModel):
    """Typed proof for one client cache at a bumped or restored version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cache_root: str = Field(min_length=1)
    version: str = Field(min_length=1)
    digest: str = Field(pattern=SHA256_HEX)
    source_digest: str = Field(pattern=SHA256_HEX)
    inventory_exact_match: Literal[True]
    tool_count: Literal[39]
    annotations_missing: tuple[str, ...]
    probe_stdout_sha256: str = Field(pattern=SHA256_HEX)
    # Privacy-safe fields captured from actual discovery so random digests cannot pass.
    list_receipt_name: str = Field(min_length=1)
    registration_version: str = Field(min_length=1)
    registration_cache_root: str = Field(min_length=1)

    @model_validator(mode="after")
    def _require_clean_annotations(self) -> VersionCacheProof:
        if self.annotations_missing:
            msg = "annotations_missing must be empty"
            raise ValueError(msg)
        if self.digest != self.source_digest:
            msg = "digest must equal source_digest"
            raise ValueError(msg)
        if self.registration_version != self.version:
            msg = "registration_version must equal version"
            raise ValueError(msg)
        if self.registration_cache_root != self.cache_root:
            msg = "registration_cache_root must equal cache_root"
            raise ValueError(msg)
        return self


class ClientVersionProofBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    codex: VersionCacheProof
    claude: VersionCacheProof


class UpdateProbeEvidence(BaseModel):
    """Mandatory typed update probe; deleting any nested field fails validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    original_version: str = Field(min_length=1)
    bumped_version: str = Field(min_length=1)
    codex_reached_bumped: Literal[True]
    claude_reached_bumped: Literal[True]
    candidate_restored: Literal[True]
    bumped_proof: ClientVersionProofBundle
    restored_proof: ClientVersionProofBundle
    temporary_fixtures_removed: Literal[True]
    remaining_temporary_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _versions_and_proofs(self) -> UpdateProbeEvidence:
        if self.bumped_version == self.original_version:
            msg = "bumped_version must differ from original_version"
            raise ValueError(msg)
        if self.bumped_proof.codex.version != self.bumped_version:
            msg = "bumped_proof.codex.version mismatch"
            raise ValueError(msg)
        if self.bumped_proof.claude.version != self.bumped_version:
            msg = "bumped_proof.claude.version mismatch"
            raise ValueError(msg)
        if self.restored_proof.codex.version != self.original_version:
            msg = "restored_proof.codex.version mismatch"
            raise ValueError(msg)
        if self.restored_proof.claude.version != self.original_version:
            msg = "restored_proof.claude.version mismatch"
            raise ValueError(msg)
        if self.remaining_temporary_paths:
            msg = "remaining_temporary_paths must be empty"
            raise ValueError(msg)
        return self


class PrivacyEvidenceBinding(BaseModel):
    """Privacy-safe binding of privacy-report.json and privacy-self-scan.json.

    Order (non-circular):
    1. Write provisional install.json without privacy digests.
    2. Write privacy-report.json over retained targets (includes provisional install).
    3. Write privacy-self-scan.json over privacy-report + canonical install form.
    4. Finalize install.json with digests of the two privacy files only.
    Verify rescans targets; install content scan normalizes only the two fixed-shape
    privacy digest fields so binding digests do not create a hash cycle.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    privacy_report_path: str = Field(min_length=1)
    privacy_self_scan_path: str = Field(min_length=1)
    privacy_report_sha256: str = Field(pattern=SHA256_HEX)
    privacy_self_scan_sha256: str = Field(pattern=SHA256_HEX)
    candidate_commit: str = Field(pattern=COMMIT_HEX)
    clone_commit: str = Field(pattern=COMMIT_HEX)
    clean: Literal[True]
    findings_count: Literal[0]
    scan_errors_count: Literal[0]
    scopes_covered: tuple[str, ...] = Field(min_length=1)
    digest_order: Literal[
        "provisional_install_then_privacy_report_then_self_scan_then_finalize_digests"
    ] = "provisional_install_then_privacy_report_then_self_scan_then_finalize_digests"


class FixtureLedgerBinding(BaseModel):
    """Privacy-safe metadata for an external JSONL cleanup ledger entry.

    Does not publish the absolute external ledger path. Binds via opaque
    sha256 of the canonical absolute ledger path plus the exact event digest.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ledger_path_sha256: str = Field(pattern=SHA256_HEX)
    event_sha256: str = Field(pattern=SHA256_HEX)
    candidate_commit: str = Field(pattern=COMMIT_HEX)
    consumers: tuple[str, ...] = Field(min_length=1)
    teardown_owner: Literal["post-final-completion-gate"]
    owner_only: Literal[True]
    cleanup_deadline: str = Field(min_length=1)
    run_root: str = Field(min_length=1)

    @field_validator("consumers")
    @classmethod
    def _require_exact_consumers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(value) != REQUIRED_FIXTURE_CONSUMERS:
            msg = "consumers must match required retained fixture consumers"
            raise ValueError(msg)
        return value


class VerifyReceipt(BaseModel):
    """Bound successful production verify-only receipt for consumers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    execution_mode: Literal["installed_verification"]
    candidate_commit: str = Field(pattern=COMMIT_HEX)
    install_report_sha256: str = Field(pattern=SHA256_HEX)
    privacy_report_sha256: str = Field(pattern=SHA256_HEX)
    privacy_self_scan_sha256: str = Field(pattern=SHA256_HEX)
    ledger_event_sha256: str = Field(pattern=SHA256_HEX)
    ledger_path_sha256: str = Field(pattern=SHA256_HEX)
    expected_skills: int
    expected_mcp_servers: int
    expected_tools: int
    global_state_unchanged: Literal[True]
    global_state_recomputed: Literal[True]
    startup_verified: Literal[True]
    errors: tuple[str, ...] = ()


class FixtureCleanup(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    deferred_registered: Literal[True]
    preserve_for: str = Field(min_length=1)
    run_root: Path
    preserved_paths: tuple[str, ...] = ()
    modes: dict[str, str] = Field(min_length=1)
    owner_only: Literal[True]
    teardown_owner: str = FIXTURE_TEARDOWN_OWNER
    consumers: tuple[str, ...] = ()
    ledger: FixtureLedgerBinding | None = None


class InstallEvidenceReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    execution_mode: Literal["installed_verification"]
    repo: Path
    candidate_commit: str = Field(pattern=COMMIT_HEX)
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
    update_probe: UpdateProbeEvidence
    auth_files: dict[str, JsonValue]
    help_receipts: tuple[CommandReceipt, ...] = Field(min_length=1)
    update_receipts: tuple[CommandReceipt, ...] = Field(min_length=1)
    required_receipts: tuple[str, ...] = ()
    privacy: PrivacyEvidenceBinding
    errors: tuple[str, ...]


class FixtureSupportReport(BaseModel):
    """Non-production install fixture used only by downstream unit tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["passed"]
    execution_mode: Literal["fixture_support"]
    repo: Path
    candidate_commit: str = Field(pattern=COMMIT_HEX)
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
    fixture_cleanup_ledger: Path | None = None
    privacy_report: Path | None = None
    privacy_self_scan: Path | None = None
