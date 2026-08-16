"""Codex-only clean-install evidence for the native analytics proof path."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    cleanup_recorded_groups,
    remaining_live_pgids,
    remaining_live_pids,
)
from saxo_bank_mcp.agent_skill_evidence_io import git_output, resolve_commit
from saxo_bank_mcp.agent_skill_install_cli_driver import (
    CommandDiscoveryError,
    build_client_report,
    clone_candidate,
    discover_codex_cache,
    identity_version,
    project_version,
    run_codex_install,
)
from saxo_bank_mcp.agent_skill_install_discovery import parse_mcp_server_count, skill_inventory
from saxo_bank_mcp.agent_skill_install_env import (
    DisposableCleanupError,
    EnvironmentContainmentError,
    build_isolated_env,
    cleanup_verify_scratch_state,
    require_disposable_cleanup,
    verify_throwaway_roots,
)
from saxo_bank_mcp.agent_skill_install_models import (
    ClientInstallEvidence,
    CloneEvidence,
    CommandReceipt,
    GlobalStateEvidence,
    InstalledByteChecks,
    ProcessCleanup,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    OWNER_ONLY_MODE,
    PLUGIN_NAME,
    codex_global_state_fingerprint,
    ensure_owner_only,
    export_publishable_tree,
    installed_inventory_check,
    owner_only_mode,
    publishable_tracked_files,
)
from saxo_bank_mcp.agent_skill_install_privacy import scan_directory_normalized
from saxo_bank_mcp.agent_skill_install_probe import (
    ProbePayloadError,
    probe_root_stdio,
    startup_from_probes,
)
from saxo_bank_mcp.agent_skill_install_verify_live import codex_registration_errors
from saxo_bank_mcp.qa_codex_native_policy import CODEX_NATIVE_POLICY
from saxo_bank_mcp.secret_scan import scan_secret_text

EXPECTED_SKILLS = 9
EXPECTED_MCP_SERVERS = 1
EXPECTED_TOOLS = 60


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class CodexInstallEvidenceReport(_StrictModel):
    """Exact retained Codex installation bound to one source commit."""

    status: Literal["passed"]
    execution_mode: Literal["codex_installed_verification"]
    harness_policy: Literal["codex_native_v1"]
    repo: Path
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    clone: CloneEvidence
    expected_skills: Literal[9]
    expected_mcp_servers: Literal[1]
    expected_tools: Literal[60]
    global_codex_state: GlobalStateEvidence
    global_codex_state_unchanged: Literal[True]
    codex: ClientInstallEvidence
    installed_byte_checks: InstalledByteChecks
    process_cleanup: ProcessCleanup
    project_version: str = Field(min_length=1)
    run_root: Path
    preserved_modes: dict[str, str]
    owner_only: Literal[True]
    privacy_scan_passed: Literal[True]
    errors: tuple[str, ...]

    @model_validator(mode="after")
    def _require_exact_native_install(self) -> Self:
        startup = self.codex.startup
        if self.errors:
            raise ValueError("codex_install_errors_not_empty")
        if self.global_codex_state.before != self.global_codex_state.after:
            raise ValueError("codex_global_state_changed")
        if set(self.global_codex_state.before) != {"codex"}:
            raise ValueError("codex_global_state_surface_invalid")
        if (
            self.codex.identity != PLUGIN_NAME
            or self.codex.skill_count != EXPECTED_SKILLS
            or self.codex.mcp_server_count != EXPECTED_MCP_SERVERS
            or self.codex.tool_count != EXPECTED_TOOLS
            or self.codex.version != self.project_version
            or self.codex.annotations_missing
            or self.codex.forbidden_cache_paths
            or not self.codex.installed_bytes_match
            or not self.installed_byte_checks.inventory_exact_match
            or self.installed_byte_checks.mismatches
        ):
            raise ValueError("codex_install_contract_invalid")
        for check in (startup.source, startup.cache, startup.list_tools):
            if check.tool_count != EXPECTED_TOOLS or check.annotations_missing:
                raise ValueError("codex_startup_contract_invalid")
        expected_modes = {"run_root", "clone", "home", "codex_home", "codex_cache"}
        if set(self.preserved_modes) != expected_modes or set(self.preserved_modes.values()) != {
            OWNER_ONLY_MODE
        }:
            raise ValueError("codex_owner_only_modes_invalid")
        for receipt in self.codex.command_receipts:
            command_surface = (receipt.name, *receipt.argv)
            if any("claude" in value.lower() for value in command_surface):
                raise ValueError("codex_install_forbidden_harness_surface")
        return self


@dataclass(frozen=True, slots=True)
class CodexInstallOptions:
    repo: Path
    commit: str
    run_root: Path
    codex_global_home: Path
    out: Path
    expected_skills: int = EXPECTED_SKILLS
    expected_tools: int = EXPECTED_TOOLS


def produce_codex_install_report(options: CodexInstallOptions) -> int:  # noqa: C901, PLR0911, PLR0912, PLR0915
    """Install one clean candidate through Codex and retain exact proof inputs."""
    error = _planning_error(options)
    if error is not None:
        _write_failure(options.out, error)
        return 1
    commit = resolve_commit(options.repo, options.commit)
    if commit is None:
        _write_failure(options.out, "commit_unresolved")
        return 1
    run_root = options.run_root.resolve()
    clone = run_root / "source-clone"
    marketplace = run_root / "marketplace-source"
    home = run_root / "home"
    codex_home = run_root / "codex-home"
    probe_env = run_root / "probe-env"
    before_state = codex_global_state_fingerprint(options.codex_global_home)
    receipts: list[CommandReceipt] = []
    try:
        for root in (run_root, home, codex_home, probe_env):
            ensure_owner_only(root)
        git_receipts = clone_candidate(options.repo, clone, commit)
        receipts.extend(git_receipts)
        publishable = export_publishable_tree(clone, marketplace)
        ensure_owner_only(marketplace)
        env = build_isolated_env(
            home=home,
            codex_home=codex_home,
            run_root=run_root,
            probe_env=probe_env,
            include_claude_client=False,
        )
        install_results = run_codex_install(marketplace, env)
        receipts.extend(result.receipt for result in install_results)
        version = project_version(clone)
        cache, cache_source = discover_codex_cache(
            install_results,
            run_root=run_root,
            codex_home=codex_home,
            expected_version=version,
        )
        ensure_owner_only(cache)
        source_probe = probe_root_stdio(
            "codex_native_source_probe",
            clone,
            env=env,
            probe_env=probe_env,
        )
        cache_probe = probe_root_stdio(
            "codex_native_cache_probe",
            cache,
            env=env,
            probe_env=probe_env,
        )
        receipts.extend((source_probe.receipt, cache_probe.receipt))
        startup = startup_from_probes(source_probe, cache_probe, cache_probe)
        inventory = installed_inventory_check(clone, cache, publishable=publishable)
        if inventory.get("inventory_exact_match") is not True:
            _fail("installed_inventory_mismatch")
        client_payload = build_client_report(
            cache=cache,
            cache_source=cache_source,
            startup=startup,
            receipts=tuple(receipts),
            inventory=inventory,
            details_skill_count=None,
        )
        client = ClientInstallEvidence.model_validate(client_payload)
    except CommandFailureError as exc:
        _cleanup_failed_run(run_root, marketplace, exc.receipt.pgid)
        _write_failure(options.out, "codex_install_command_failed")
        return 1
    except (
        CommandDiscoveryError,
        EnvironmentContainmentError,
        FileNotFoundError,
        PermissionError,
        ProbePayloadError,
        ValidationError,
        ValueError,
    ) as exc:
        _cleanup_failed_run(run_root, marketplace, None)
        _write_failure(options.out, str(getattr(exc, "reason", exc)))
        return 1

    observed_pids = tuple(receipt.pid for receipt in receipts if receipt.pid is not None)
    observed_pgids = tuple(receipt.pgid for receipt in receipts if receipt.pgid is not None)
    remaining_pgids = remaining_live_pgids(observed_pgids)
    if remaining_pgids:
        cleanup_recorded_groups(remaining_pgids)
    remaining_pids = remaining_live_pids(observed_pids)
    remaining_pgids = remaining_live_pgids(observed_pgids)
    try:
        require_disposable_cleanup(run_root, extra_paths=(marketplace,))
    except DisposableCleanupError:
        _write_failure(options.out, "codex_install_disposable_cleanup_failed")
        return 1
    after_state = codex_global_state_fingerprint(options.codex_global_home)
    if before_state.get("codex") != after_state.get("codex"):
        _write_failure(options.out, "codex_global_state_changed")
        return 1
    if remaining_pids or remaining_pgids:
        _write_failure(options.out, "codex_install_process_cleanup_failed")
        return 1

    try:
        modes = _preserved_modes(
            run_root=run_root,
            clone=clone,
            home=home,
            codex_home=codex_home,
            cache=cache,
        )
        identity, version = identity_version(clone)
        if identity != PLUGIN_NAME:
            _fail("codex_identity_invalid")
        inventory = installed_inventory_check(clone, cache)
        byte_checks = _installed_byte_checks(inventory)
        report = CodexInstallEvidenceReport(
            status="passed",
            execution_mode="codex_installed_verification",
            harness_policy=CODEX_NATIVE_POLICY.policy_id,
            repo=options.repo.resolve(),
            candidate_commit=commit,
            clone=CloneEvidence(
                path=clone,
                commit=commit,
                source_repo=options.repo.resolve(),
                no_local=True,
                clean=True,
                mode=modes["clone"],
            ),
            expected_skills=EXPECTED_SKILLS,
            expected_mcp_servers=EXPECTED_MCP_SERVERS,
            expected_tools=EXPECTED_TOOLS,
            global_codex_state=GlobalStateEvidence(
                before={"codex": before_state["codex"]},
                after={"codex": after_state["codex"]},
                scope=cast("dict[str, JsonValue]", before_state["scope"]),
            ),
            global_codex_state_unchanged=True,
            codex=client,
            installed_byte_checks=byte_checks,
            process_cleanup=ProcessCleanup(
                complete=True,
                remaining_pids=(),
                remaining_pgids=(),
                observed_pids=observed_pids,
                observed_pgids=observed_pgids,
            ),
            project_version=version,
            run_root=run_root,
            preserved_modes=modes,
            owner_only=True,
            privacy_scan_passed=True,
            errors=(),
        )
        privacy_errors = _privacy_errors(report, clone=clone, cache=cache)
        if privacy_errors:
            _fail("codex_install_privacy_scan_failed")
    except (OSError, ValidationError, ValueError) as exc:
        _write_failure(options.out, str(exc))
        return 1
    write_json(options.out, cast("dict[str, JsonValue]", report.model_dump(mode="json")))
    options.out.chmod(0o600)
    verified, errors = load_verified_codex_install_report(
        options.out,
        codex_global_home=options.codex_global_home,
    )
    if verified is None:
        _write_failure(options.out, "codex_install_self_verify_failed", errors=errors)
        return 1
    return 0


def load_verified_codex_install_report(  # noqa: C901, PLR0912
    path: Path,
    *,
    codex_global_home: Path,
) -> tuple[CodexInstallEvidenceReport | None, tuple[str, ...]]:
    """Independently verify a retained Codex-only installation report."""
    try:
        report = CodexInstallEvidenceReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None, ("invalid_codex_install_report",)
    errors: list[str] = []
    try:
        start_state = codex_global_state_fingerprint(codex_global_home)
    except OSError:
        return None, ("codex_global_state_fingerprint_failed",)
    run_root = report.run_root.resolve()
    clone = report.clone.path.resolve()
    cache = report.codex.cache_root.resolve()
    codex_home = (run_root / "codex-home").resolve()
    if report.global_codex_state.before != report.global_codex_state.after:
        errors.append("codex_global_state_history_mismatch")
    if not clone.is_relative_to(run_root) or not cache.is_relative_to(codex_home):
        errors.append("codex_install_path_binding_invalid")
    if git_output(clone, "rev-parse", "HEAD") != report.candidate_commit:
        errors.append("codex_install_clone_commit_mismatch")
    if git_output(clone, "status", "--porcelain") != "":
        errors.append("codex_install_clone_not_clean")
    inventory = installed_inventory_check(clone, cache)
    if inventory.get("inventory_exact_match") is not True:
        errors.append("installed_inventory_mismatch")
    if skill_inventory(cache) != report.codex.skills:
        errors.append("codex_skill_inventory_mismatch")
    if parse_mcp_server_count(cache) != EXPECTED_MCP_SERVERS:
        errors.append("codex_mcp_server_count_mismatch")
    identity, version = identity_version(cache)
    if identity != PLUGIN_NAME or version != report.project_version:
        errors.append("codex_identity_version_mismatch")
    errors.extend(
        codex_registration_errors(
            codex_home=codex_home,
            cache_root=cache,
            version=report.project_version,
        ),
    )
    try:
        actual_modes = _preserved_modes(
            run_root=run_root,
            clone=clone,
            home=run_root / "home",
            codex_home=codex_home,
            cache=cache,
        )
    except (FileNotFoundError, OSError, PermissionError):
        errors.append("codex_owner_only_modes_invalid")
    else:
        if actual_modes != report.preserved_modes:
            errors.append("codex_owner_only_modes_mismatch")
    if not errors:
        errors.extend(_verify_startup(report, clone=clone, cache=cache))
        errors.extend(_privacy_errors(report, clone=clone, cache=cache))
    try:
        end_state = codex_global_state_fingerprint(codex_global_home)
    except OSError:
        errors.append("codex_global_state_fingerprint_failed")
    else:
        if start_state.get("codex") != end_state.get("codex"):
            errors.append("codex_global_state_verify_window_mismatch")
    return (report, ()) if not errors else (None, tuple(dict.fromkeys(errors)))


def _planning_error(options: CodexInstallOptions) -> str | None:
    if options.expected_skills != EXPECTED_SKILLS or options.expected_tools != EXPECTED_TOOLS:
        return "codex_install_expected_counts_invalid"
    if not options.repo.is_dir() or resolve_commit(options.repo, options.commit) is None:
        return "codex_install_source_invalid"
    if not options.codex_global_home.is_dir():
        return "codex_global_home_invalid"
    run_root = options.run_root.resolve()
    repo = options.repo.resolve()
    if run_root.exists():
        return "codex_install_run_root_exists"
    if run_root == repo or repo.is_relative_to(run_root):
        return "codex_install_run_root_unsafe"
    return None


def _installed_byte_checks(inventory: dict[str, JsonValue]) -> InstalledByteChecks:
    if inventory.get("inventory_exact_match") is not True:
        raise ValueError("installed_inventory_mismatch")
    compared_files = inventory.get("compared_files")
    if not isinstance(compared_files, int) or isinstance(compared_files, bool):
        _fail("installed_inventory_count_invalid")
    return InstalledByteChecks(
        complete=True,
        compared_files=compared_files,
        metadata_exceptions=tuple(cast("list[str]", inventory["metadata_exceptions"])),
        required_files_present=tuple(cast("list[str]", inventory["required_files_present"])),
        forbidden_files_absent=True,
        mismatches=(),
        inventory_exact_match=True,
    )


def _preserved_modes(
    *,
    run_root: Path,
    clone: Path,
    home: Path,
    codex_home: Path,
    cache: Path,
) -> dict[str, str]:
    roots = {
        "run_root": run_root,
        "clone": clone,
        "home": home,
        "codex_home": codex_home,
        "codex_cache": cache,
    }
    modes: dict[str, str] = {}
    for label, root in roots.items():
        if not root.is_dir():
            raise FileNotFoundError(label)
        mode = owner_only_mode(root)
        if mode != OWNER_ONLY_MODE:
            raise PermissionError(label)
        modes[label] = mode
    return modes


def _verify_startup(
    report: CodexInstallEvidenceReport,
    *,
    clone: Path,
    cache: Path,
) -> list[str]:
    run_root = report.run_root.resolve()
    verify_home, verify_codex_home, probe_env = verify_throwaway_roots(run_root)
    receipts: list[CommandReceipt] = []
    errors: list[str] = []
    try:
        for root in (verify_home, verify_codex_home, probe_env):
            ensure_owner_only(root)
        env = build_isolated_env(
            home=verify_home,
            codex_home=verify_codex_home,
            run_root=run_root,
            probe_env=probe_env,
            include_claude_client=False,
        )
        source_probe = probe_root_stdio(
            "codex_native_verify_source",
            clone,
            env=env,
            probe_env=probe_env,
        )
        cache_probe = probe_root_stdio(
            "codex_native_verify_cache",
            cache,
            env=env,
            probe_env=probe_env,
        )
        receipts.extend((source_probe.receipt, cache_probe.receipt))
        startup = startup_from_probes(source_probe, cache_probe, cache_probe)
        if startup != report.codex.startup:
            errors.append("codex_startup_evidence_mismatch")
    except Exception:  # noqa: BLE001
        errors.append("codex_startup_probe_failed")
    finally:
        observed_pids = tuple(receipt.pid for receipt in receipts if receipt.pid is not None)
        observed_pgids = tuple(receipt.pgid for receipt in receipts if receipt.pgid is not None)
        if remaining_live_pids(observed_pids) or remaining_live_pgids(observed_pgids):
            errors.append("codex_verify_process_cleanup_failed")
        if cleanup_verify_scratch_state(run_root):
            errors.append("codex_verify_scratch_cleanup_failed")
    return errors


def _privacy_errors(
    report: CodexInstallEvidenceReport,
    *,
    clone: Path,
    cache: Path,
) -> list[str]:
    findings: list[dict[str, JsonValue]] = []
    scan_errors: list[dict[str, JsonValue]] = []
    run_root = report.run_root.resolve()
    for relative in publishable_tracked_files(clone):
        source = clone / relative
        if not source.is_file():
            scan_errors.append({"path": relative, "error": "missing_path"})
            continue
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        found, failed = scan_secret_text(relative, text)
        findings.extend(found)
        scan_errors.extend(failed)
    cache_findings, cache_errors = scan_directory_normalized(cache, run_root=run_root)
    findings.extend(cache_findings)
    scan_errors.extend(cache_errors)
    rendered = json.dumps(report.model_dump(mode="json"), sort_keys=True)
    rendered = rendered.replace(str(run_root), "${RUN_ROOT}")
    rendered = rendered.replace(str(report.repo.resolve()), "${SOURCE_REPO}")
    report_findings, report_errors = scan_secret_text("codex-install.json", rendered)
    findings.extend(report_findings)
    scan_errors.extend(report_errors)
    return ["codex_install_privacy_scan_failed"] if findings or scan_errors else []


def _cleanup_failed_run(run_root: Path, marketplace: Path, pgid: int | None) -> None:
    if pgid is not None:
        cleanup_recorded_groups((pgid,))
    try:
        require_disposable_cleanup(run_root, extra_paths=(marketplace,))
    except DisposableCleanupError:
        return


def _fail(reason: str) -> NoReturn:
    raise ValueError(reason)


def _write_failure(path: Path, reason: str, *, errors: tuple[str, ...] = ()) -> None:
    write_json(path, {"status": "failed", "reason": reason, "errors": list(errors)})
    path.chmod(0o600)
