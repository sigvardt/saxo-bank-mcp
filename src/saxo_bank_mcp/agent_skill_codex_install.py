"""Codex-only clean-install evidence for the native analytics proof path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    run_command,
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
    tree_digest,
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
_COMMIT_PATTERN = r"^[a-f0-9]{40}$"
_SHA256_PATTERN = r"^[a-f0-9]{64}$"
_PROOF_RUNTIME_NAME = "proof-runtime"
_PROOF_RUNTIME_BINDING_NAME = "proof-runtime-binding.json"
_PROOF_RUNTIME_CONSUMPTION_INTENT_NAME = "proof-runtime-consumption-intent.json"
_PROOF_RUNTIME_CLEANUP_NAME = "proof-runtime-cleanup.json"
_OWNER_FILE_MODE = 0o600
_OWNER_DIRECTORY_MODE = 0o700


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


class CodexProofRuntimeBinding(_StrictModel):
    """One install-time probed interpreter retained only through the sealed proof."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_proof_runtime"] = "codex_native_proof_runtime"
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    candidate_tree: str = Field(pattern=_COMMIT_PATTERN)
    runtime_root: Path
    producer_root: Path
    interpreter: Path
    source_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    installed_cache_sha256: str = Field(pattern=_SHA256_PATTERN)
    dependency_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    producer_module_sha256: str = Field(pattern=_SHA256_PATTERN)
    interpreter_sha256: str = Field(pattern=_SHA256_PATTERN)
    interpreter_identity_sha256: str = Field(pattern=_SHA256_PATTERN)
    python_implementation: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    python_cache_tag: str = Field(min_length=1)
    probe_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)
    owner_only: Literal[True]
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        material = self.model_dump(mode="json", exclude={"binding_sha256"})
        if self.binding_sha256 != _digest(material):
            raise ValueError("codex_proof_runtime_binding_digest_invalid")
        if self.source_inventory_sha256 != self.installed_cache_sha256:
            raise ValueError("codex_proof_runtime_inventory_mismatch")
        return self


class CodexProofRuntimeEvidence(_StrictModel):
    binding_path: Path
    binding: CodexProofRuntimeBinding


class CodexProofRuntimeConsumptionIntent(_StrictModel):
    """Durable authorization written before the one-shot runtime is removed."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_proof_runtime_consumption_intent"] = (
        "codex_native_proof_runtime_consumption_intent"
    )
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    candidate_tree: str = Field(pattern=_COMMIT_PATTERN)
    runtime_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_target: Literal["bound_proof_runtime"] = "bound_proof_runtime"
    runtime_present: Literal[True] = True
    owner_only: Literal[True] = True
    intent_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_intent(self) -> Self:
        material = self.model_dump(mode="json", exclude={"intent_sha256"})
        if self.intent_sha256 != _digest(material):
            raise ValueError("codex_proof_runtime_consumption_intent_digest_invalid")
        return self


class CodexProofRuntimeCleanupReceipt(_StrictModel):
    """Authenticated proof that the authorized runtime consumption completed."""

    schema_version: Literal["1"] = "1"
    receipt_kind: Literal["codex_native_proof_runtime_cleanup"] = (
        "codex_native_proof_runtime_cleanup"
    )
    harness_policy: Literal["codex_native_v1"] = "codex_native_v1"
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    candidate_tree: str = Field(pattern=_COMMIT_PATTERN)
    runtime_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    consumption_intent_sha256: str = Field(pattern=_SHA256_PATTERN)
    cleanup_status: Literal["complete"] = "complete"
    runtime_absent: Literal[True] = True
    owner_only: Literal[True] = True
    cleanup_receipt_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_cleanup(self) -> Self:
        material = self.model_dump(mode="json", exclude={"cleanup_receipt_sha256"})
        if self.cleanup_receipt_sha256 != _digest(material):
            raise ValueError("codex_proof_runtime_cleanup_receipt_digest_invalid")
        return self


class CodexProofRuntimeConsumptionEvidence(_StrictModel):
    intent_path: Path
    intent: CodexProofRuntimeConsumptionIntent
    cleanup_path: Path
    cleanup: CodexProofRuntimeCleanupReceipt


class ProofRuntimeCleanupError(ValueError):
    """The one exact retained proof runtime could not be safely removed."""


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
    proof_runtime: CodexProofRuntimeEvidence | None = None
    errors: tuple[str, ...]

    @model_validator(mode="after")
    def _require_exact_native_install(self) -> Self:  # noqa: C901, PLR0912
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
        if self.proof_runtime is not None:
            expected_modes.add("proof_runtime")
        if set(self.preserved_modes) != expected_modes or set(self.preserved_modes.values()) != {
            OWNER_ONLY_MODE
        }:
            raise ValueError("codex_owner_only_modes_invalid")
        if self.proof_runtime is not None:
            runtime = self.proof_runtime
            binding = runtime.binding
            expected_root = self.run_root.resolve() / _PROOF_RUNTIME_NAME
            expected_binding = self.run_root.resolve() / _PROOF_RUNTIME_BINDING_NAME
            if (
                binding.candidate_commit != self.candidate_commit
                or binding.runtime_root != expected_root
                or binding.producer_root != self.codex.cache_root.resolve()
                or not binding.interpreter.absolute().is_relative_to(expected_root)
                or runtime.binding_path != expected_binding
            ):
                raise ValueError("codex_proof_runtime_report_binding_invalid")
            receipts = tuple(
                receipt
                for receipt in self.codex.command_receipts
                if receipt.name == "codex_proof_runtime_probe"
            )
            if len(receipts) != 1 or _digest(receipts[0].model_dump(mode="json")) != (
                binding.probe_receipt_sha256
            ):
                raise ValueError("codex_proof_runtime_probe_receipt_invalid")
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
    retain_proof_runtime: bool = False


def build_codex_proof_runtime_evidence(  # noqa: PLR0913
    *,
    run_root: Path,
    runtime_root: Path,
    producer_root: Path,
    candidate_commit: str,
    candidate_tree: str,
    source_inventory_sha256: str,
    installed_cache_sha256: str,
    dependency_lock_sha256: str,
    producer_module_sha256: str,
    env: dict[str, str],
) -> tuple[CodexProofRuntimeEvidence, CommandReceipt]:
    """Probe and bind the exact retained interpreter without caller Python/uv paths."""
    runtime = runtime_root.resolve()
    producer = producer_root.resolve()
    expected_runtime = run_root.resolve() / _PROOF_RUNTIME_NAME
    interpreter_path = runtime / "bin/python"
    if runtime != expected_runtime or runtime.is_symlink() or not runtime.is_dir():
        raise ValueError("codex_proof_runtime_root_invalid")
    if owner_only_mode(runtime) != OWNER_ONLY_MODE:
        raise ValueError("codex_proof_runtime_mode_invalid")
    try:
        interpreter = interpreter_path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("codex_proof_runtime_interpreter_missing") from exc
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError("codex_proof_runtime_interpreter_invalid")
    clean_env = {
        key: value
        for key, value in env.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("UV_")
    }
    clean_env["PYTHONDONTWRITEBYTECODE"] = "1"
    clean_env["PYTHONNOUSERSITE"] = "1"
    code = (
        "import hashlib, importlib.util, json, platform, sys\n"
        "from pathlib import Path\n"
        "spec = importlib.util.find_spec('saxo_bank_mcp.qa_analytics_proof_producer')\n"
        "assert spec is not None and spec.origin is not None\n"
        "module_path = Path(spec.origin).resolve(strict=True)\n"
        "executable = Path(sys.executable).resolve(strict=True)\n"
        "print(json.dumps({\n"
        "  'status': 'passed',\n"
        "  'producer_module_path': str(module_path),\n"
        "  'producer_module_sha256': hashlib.sha256(module_path.read_bytes()).hexdigest(),\n"
        "  'interpreter_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),\n"
        "  'interpreter_identity_sha256': "
        "hashlib.sha256(str(executable).encode()).hexdigest(),\n"
        "  'python_implementation': platform.python_implementation(),\n"
        "  'python_version': platform.python_version(),\n"
        "  'python_cache_tag': sys.implementation.cache_tag,\n"
        "}, sort_keys=True))\n"
    )
    result = run_command(
        "codex_proof_runtime_probe",
        (str(interpreter_path), "-I", "-B", "-c", code),
        cwd=producer,
        env=clean_env,
        timeout_seconds=60,
    )
    try:
        decoded: object = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ValueError("codex_proof_runtime_probe_invalid") from exc
    if not isinstance(decoded, dict):
        raise TypeError("codex_proof_runtime_probe_invalid")
    payload = cast("dict[str, object]", decoded)
    actual_lock = hashlib.sha256((producer / "uv.lock").read_bytes()).hexdigest()
    actual_producer = hashlib.sha256(
        (producer / "src/saxo_bank_mcp/qa_analytics_proof_producer.py").read_bytes(),
    ).hexdigest()
    expected_payload = {
        "status": "passed",
        "producer_module_path": str(
            (producer / "src/saxo_bank_mcp/qa_analytics_proof_producer.py").resolve(strict=True),
        ),
        "producer_module_sha256": producer_module_sha256,
        "interpreter_sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "interpreter_identity_sha256": hashlib.sha256(str(interpreter).encode()).hexdigest(),
    }
    if (
        any(payload.get(key) != value for key, value in expected_payload.items())
        or actual_lock != dependency_lock_sha256
        or actual_producer != producer_module_sha256
    ):
        raise ValueError("codex_proof_runtime_probe_binding_invalid")
    receipt_sha256 = _digest(result.receipt.model_dump(mode="json"))
    material = {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_runtime",
        "harness_policy": "codex_native_v1",
        "candidate_commit": candidate_commit,
        "candidate_tree": candidate_tree,
        "runtime_root": str(runtime),
        "producer_root": str(producer),
        "interpreter": str(interpreter_path.absolute()),
        "source_inventory_sha256": source_inventory_sha256,
        "installed_cache_sha256": installed_cache_sha256,
        "dependency_lock_sha256": dependency_lock_sha256,
        "producer_module_sha256": producer_module_sha256,
        "interpreter_sha256": str(payload["interpreter_sha256"]),
        "interpreter_identity_sha256": str(payload["interpreter_identity_sha256"]),
        "python_implementation": str(payload.get("python_implementation", "")),
        "python_version": str(payload.get("python_version", "")),
        "python_cache_tag": str(payload.get("python_cache_tag", "")),
        "probe_receipt_sha256": receipt_sha256,
        "owner_only": True,
    }
    binding = CodexProofRuntimeBinding.model_validate_json(
        json.dumps({**material, "binding_sha256": _digest(material)}, sort_keys=True),
    )
    binding_path = run_root.resolve() / _PROOF_RUNTIME_BINDING_NAME
    write_json(
        binding_path,
        cast("dict[str, JsonValue]", binding.model_dump(mode="json")),
    )
    binding_path.chmod(_OWNER_FILE_MODE)
    return CodexProofRuntimeEvidence(binding_path=binding_path, binding=binding), result.receipt


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
    probe_env = run_root / (_PROOF_RUNTIME_NAME if options.retain_proof_runtime else "probe-env")
    retained_runtime = probe_env if options.retain_proof_runtime else None
    before_state = codex_global_state_fingerprint(options.codex_global_home)
    receipts: list[CommandReceipt] = []
    proof_runtime: CodexProofRuntimeEvidence | None = None
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
        if options.retain_proof_runtime:
            candidate_tree = git_output(clone, "rev-parse", "HEAD^{tree}")
            if candidate_tree is None:
                _fail("codex_proof_runtime_candidate_tree_missing")
            proof_runtime, proof_receipt = build_codex_proof_runtime_evidence(
                run_root=run_root,
                runtime_root=probe_env,
                producer_root=cache,
                candidate_commit=commit,
                candidate_tree=candidate_tree,
                source_inventory_sha256=tree_digest(clone, publishable),
                installed_cache_sha256=tree_digest(cache, publishable),
                dependency_lock_sha256=hashlib.sha256((cache / "uv.lock").read_bytes()).hexdigest(),
                producer_module_sha256=hashlib.sha256(
                    (cache / "src/saxo_bank_mcp/qa_analytics_proof_producer.py").read_bytes(),
                ).hexdigest(),
                env=env,
            )
            receipts.append(proof_receipt)
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
        _cleanup_failed_run(
            run_root,
            marketplace,
            exc.receipt.pgid,
            retained_runtime=retained_runtime,
        )
        _write_failure(options.out, "codex_install_command_failed")
        return 1
    except (
        CommandDiscoveryError,
        EnvironmentContainmentError,
        FileNotFoundError,
        PermissionError,
        ProbePayloadError,
        ValidationError,
        TypeError,
        ValueError,
    ) as exc:
        _cleanup_failed_run(
            run_root,
            marketplace,
            None,
            retained_runtime=retained_runtime,
        )
        _write_failure(options.out, str(getattr(exc, "reason", exc)))
        return 1

    observed_pids = tuple(receipt.pid for receipt in receipts if receipt.pid is not None)
    observed_pgids = tuple(receipt.pgid for receipt in receipts if receipt.pgid is not None)
    # Successful run_command returns are already closed by the shared birth-bound cleanup.
    # Receipt PID/PGID values are immutable evidence, never later liveness/signal targets.
    remaining_pids: tuple[int, ...] = ()
    remaining_pgids: tuple[int, ...] = ()
    try:
        require_disposable_cleanup(run_root, extra_paths=(marketplace,))
    except DisposableCleanupError:
        _cleanup_failed_run(
            run_root,
            marketplace,
            None,
            retained_runtime=retained_runtime,
        )
        _write_failure(options.out, "codex_install_disposable_cleanup_failed")
        return 1
    after_state = codex_global_state_fingerprint(options.codex_global_home)
    if before_state.get("codex") != after_state.get("codex"):
        _cleanup_failed_run(
            run_root,
            marketplace,
            None,
            retained_runtime=retained_runtime,
        )
        _write_failure(options.out, "codex_global_state_changed")
        return 1
    if remaining_pids or remaining_pgids:
        _cleanup_failed_run(
            run_root,
            marketplace,
            None,
            retained_runtime=retained_runtime,
        )
        _write_failure(options.out, "codex_install_process_cleanup_failed")
        return 1

    try:
        modes = _preserved_modes(
            run_root=run_root,
            clone=clone,
            home=home,
            codex_home=codex_home,
            cache=cache,
            proof_runtime=(
                proof_runtime.binding.runtime_root if proof_runtime is not None else None
            ),
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
            proof_runtime=proof_runtime,
            errors=(),
        )
        privacy_errors = _privacy_errors(report, clone=clone, cache=cache)
        if privacy_errors:
            _fail("codex_install_privacy_scan_failed")
    except (OSError, ValidationError, ValueError) as exc:
        _cleanup_failed_run(
            run_root,
            marketplace,
            None,
            retained_runtime=retained_runtime,
        )
        _write_failure(options.out, str(exc))
        return 1
    write_json(options.out, cast("dict[str, JsonValue]", report.model_dump(mode="json")))
    options.out.chmod(0o600)
    verified, errors = load_verified_codex_install_report(
        options.out,
        codex_global_home=options.codex_global_home,
    )
    if verified is None:
        _cleanup_failed_run(
            run_root,
            marketplace,
            None,
            retained_runtime=retained_runtime,
        )
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
    errors.extend(_proof_runtime_errors(report, clone=clone, cache=cache))
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
            proof_runtime=(
                report.proof_runtime.binding.runtime_root
                if report.proof_runtime is not None
                else None
            ),
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


def cleanup_codex_proof_runtime(  # noqa: C901
    report: CodexInstallEvidenceReport,
) -> CodexProofRuntimeConsumptionEvidence:
    """Authorize, remove, and durably prove consumption of the one-shot runtime."""
    evidence = report.proof_runtime
    if evidence is None:
        raise ProofRuntimeCleanupError("codex_proof_runtime_missing")
    expected = report.run_root.resolve() / _PROOF_RUNTIME_NAME
    declared = evidence.binding.runtime_root
    if declared != expected or declared.is_symlink():
        raise ProofRuntimeCleanupError("codex_proof_runtime_cleanup_target_invalid")
    try:
        metadata = os.lstat(declared)
    except OSError as exc:
        raise ProofRuntimeCleanupError("codex_proof_runtime_cleanup_target_missing") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != _OWNER_DIRECTORY_MODE
    ):
        raise ProofRuntimeCleanupError("codex_proof_runtime_cleanup_target_unsafe")
    intent_path = report.run_root.resolve() / _PROOF_RUNTIME_CONSUMPTION_INTENT_NAME
    cleanup_path = report.run_root.resolve() / _PROOF_RUNTIME_CLEANUP_NAME
    if os.path.lexists(intent_path) or os.path.lexists(cleanup_path):
        raise ProofRuntimeCleanupError("codex_proof_runtime_consumption_receipt_exists")
    intent_material = {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_runtime_consumption_intent",
        "harness_policy": "codex_native_v1",
        "candidate_commit": report.candidate_commit,
        "candidate_tree": evidence.binding.candidate_tree,
        "runtime_binding_sha256": evidence.binding.binding_sha256,
        "cleanup_target": "bound_proof_runtime",
        "runtime_present": True,
        "owner_only": True,
    }
    intent = CodexProofRuntimeConsumptionIntent.model_validate(
        {**intent_material, "intent_sha256": _digest(intent_material)},
        strict=True,
    )
    try:
        write_json(
            intent_path,
            cast("dict[str, JsonValue]", intent.model_dump(mode="json")),
        )
        intent_path.chmod(_OWNER_FILE_MODE)
    except OSError as exc:
        raise ProofRuntimeCleanupError(
            "codex_proof_runtime_consumption_intent_write_failed",
        ) from exc
    try:
        intent_metadata = os.lstat(intent_path)
        persisted_intent = CodexProofRuntimeConsumptionIntent.model_validate_json(
            intent_path.read_text(encoding="utf-8"),
        )
    except (OSError, ValidationError) as exc:
        raise ProofRuntimeCleanupError(
            "codex_proof_runtime_consumption_intent_invalid",
        ) from exc
    if (
        persisted_intent != intent
        or not stat.S_ISREG(intent_metadata.st_mode)
        or stat.S_ISLNK(intent_metadata.st_mode)
        or intent_metadata.st_uid != os.getuid()
        or intent_metadata.st_nlink != 1
        or stat.S_IMODE(intent_metadata.st_mode) != _OWNER_FILE_MODE
    ):
        raise ProofRuntimeCleanupError("codex_proof_runtime_consumption_intent_invalid")
    try:
        shutil.rmtree(declared)
    except OSError as exc:
        raise ProofRuntimeCleanupError("codex_proof_runtime_cleanup_failed") from exc
    if os.path.lexists(declared):
        raise ProofRuntimeCleanupError("codex_proof_runtime_cleanup_residue")

    cleanup_material = {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_runtime_cleanup",
        "harness_policy": "codex_native_v1",
        "candidate_commit": report.candidate_commit,
        "candidate_tree": evidence.binding.candidate_tree,
        "runtime_binding_sha256": evidence.binding.binding_sha256,
        "consumption_intent_sha256": intent.intent_sha256,
        "cleanup_status": "complete",
        "runtime_absent": True,
        "owner_only": True,
    }
    cleanup = CodexProofRuntimeCleanupReceipt.model_validate(
        {
            **cleanup_material,
            "cleanup_receipt_sha256": _digest(cleanup_material),
        },
        strict=True,
    )
    try:
        write_json(
            cleanup_path,
            cast("dict[str, JsonValue]", cleanup.model_dump(mode="json")),
        )
        cleanup_path.chmod(_OWNER_FILE_MODE)
    except OSError as exc:
        raise ProofRuntimeCleanupError("codex_proof_runtime_cleanup_receipt_write_failed") from exc
    return verify_codex_proof_runtime_consumed(report)


def verify_codex_proof_runtime_consumed(
    report: CodexInstallEvidenceReport,
) -> CodexProofRuntimeConsumptionEvidence:
    """Verify post-consumption state without requiring the deleted runtime to exist."""
    evidence = report.proof_runtime
    if evidence is None:
        raise ProofRuntimeCleanupError("codex_proof_runtime_missing")
    runtime_root = report.run_root.resolve() / _PROOF_RUNTIME_NAME
    intent_path = report.run_root.resolve() / _PROOF_RUNTIME_CONSUMPTION_INTENT_NAME
    cleanup_path = report.run_root.resolve() / _PROOF_RUNTIME_CLEANUP_NAME
    try:
        intent_metadata = os.lstat(intent_path)
        cleanup_metadata = os.lstat(cleanup_path)
        binding_metadata = os.lstat(evidence.binding_path)
        intent = CodexProofRuntimeConsumptionIntent.model_validate_json(
            intent_path.read_text(encoding="utf-8"),
        )
        cleanup = CodexProofRuntimeCleanupReceipt.model_validate_json(
            cleanup_path.read_text(encoding="utf-8"),
        )
        on_disk_binding = CodexProofRuntimeBinding.model_validate_json(
            evidence.binding_path.read_text(encoding="utf-8"),
        )
    except (OSError, ValidationError) as exc:
        raise ProofRuntimeCleanupError(
            "codex_proof_runtime_consumption_receipt_invalid",
        ) from exc
    file_metadata = (intent_metadata, cleanup_metadata, binding_metadata)
    invalid_file = any(
        not stat.S_ISREG(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_uid != os.getuid()
        or item.st_nlink != 1
        or stat.S_IMODE(item.st_mode) != _OWNER_FILE_MODE
        for item in file_metadata
    )
    invalid_binding = (
        on_disk_binding != evidence.binding
        or intent.candidate_commit != report.candidate_commit
        or intent.candidate_tree != evidence.binding.candidate_tree
        or intent.runtime_binding_sha256 != evidence.binding.binding_sha256
        or cleanup.candidate_commit != report.candidate_commit
        or cleanup.candidate_tree != evidence.binding.candidate_tree
        or cleanup.runtime_binding_sha256 != evidence.binding.binding_sha256
        or cleanup.consumption_intent_sha256 != intent.intent_sha256
    )
    if os.path.lexists(runtime_root) or invalid_file or invalid_binding:
        raise ProofRuntimeCleanupError("codex_proof_runtime_consumption_receipt_invalid")
    return CodexProofRuntimeConsumptionEvidence(
        intent_path=intent_path,
        intent=intent,
        cleanup_path=cleanup_path,
        cleanup=cleanup,
    )


def _proof_runtime_errors(
    report: CodexInstallEvidenceReport,
    *,
    clone: Path,
    cache: Path,
) -> list[str]:
    evidence = report.proof_runtime
    if evidence is None:
        return []
    binding = evidence.binding
    run_root = report.run_root.resolve()
    runtime_root = run_root / _PROOF_RUNTIME_NAME
    binding_path = run_root / _PROOF_RUNTIME_BINDING_NAME
    try:
        runtime_metadata = os.lstat(runtime_root)
        binding_metadata = os.lstat(binding_path)
        interpreter_metadata = os.lstat(binding.interpreter)
        interpreter = binding.interpreter.resolve(strict=True)
        interpreter_target_metadata = interpreter.stat()
        on_disk = CodexProofRuntimeBinding.model_validate_json(
            binding_path.read_text(encoding="utf-8"),
        )
    except (OSError, ValidationError):
        return ["codex_proof_runtime_binding_invalid"]
    publishable = publishable_tracked_files(clone)
    producer = cache / "src/saxo_bank_mcp/qa_analytics_proof_producer.py"
    candidate_tree = git_output(clone, "rev-parse", "HEAD^{tree}")
    receipt = tuple(
        item for item in report.codex.command_receipts if item.name == "codex_proof_runtime_probe"
    )
    invalid = (
        on_disk != binding or binding.runtime_root != runtime_root or binding.producer_root != cache
    )
    invalid = bool(invalid) or (
        not stat.S_ISDIR(runtime_metadata.st_mode)
        or stat.S_ISLNK(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != os.getuid()
        or stat.S_IMODE(runtime_metadata.st_mode) != _OWNER_DIRECTORY_MODE
        or not stat.S_ISREG(binding_metadata.st_mode)
        or stat.S_ISLNK(binding_metadata.st_mode)
        or binding_metadata.st_uid != os.getuid()
        or binding_metadata.st_nlink != 1
        or stat.S_IMODE(binding_metadata.st_mode) != _OWNER_FILE_MODE
        or not binding.interpreter.absolute().is_relative_to(runtime_root)
        or not (
            stat.S_ISREG(interpreter_metadata.st_mode) or stat.S_ISLNK(interpreter_metadata.st_mode)
        )
        or not stat.S_ISREG(interpreter_target_metadata.st_mode)
        or not os.access(interpreter, os.X_OK)
        or candidate_tree != binding.candidate_tree
        or tree_digest(clone, publishable) != binding.source_inventory_sha256
        or tree_digest(cache, publishable) != binding.installed_cache_sha256
        or hashlib.sha256((clone / "uv.lock").read_bytes()).hexdigest()
        != binding.dependency_lock_sha256
        or hashlib.sha256(producer.read_bytes()).hexdigest() != binding.producer_module_sha256
        or hashlib.sha256(interpreter.read_bytes()).hexdigest() != binding.interpreter_sha256
        or hashlib.sha256(str(interpreter).encode()).hexdigest()
        != binding.interpreter_identity_sha256
        or len(receipt) != 1
        or _digest(receipt[0].model_dump(mode="json")) != binding.probe_receipt_sha256
    )
    return ["codex_proof_runtime_binding_invalid"] if invalid else []


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


def _preserved_modes(  # noqa: PLR0913
    *,
    run_root: Path,
    clone: Path,
    home: Path,
    codex_home: Path,
    cache: Path,
    proof_runtime: Path | None = None,
) -> dict[str, str]:
    roots = {
        "run_root": run_root,
        "clone": clone,
        "home": home,
        "codex_home": codex_home,
        "codex_cache": cache,
    }
    if proof_runtime is not None:
        roots["proof_runtime"] = proof_runtime
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
        # probe_root_stdio only returns after run_command's semantic cleanup closes.
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


def _cleanup_failed_run(
    run_root: Path,
    marketplace: Path,
    historical_pgid: int | None,
    *,
    retained_runtime: Path | None = None,
) -> None:
    # The failing run_command already performed identity-bound cleanup. Never signal the
    # historical numeric group again; it may have been reused before this file cleanup.
    _ = historical_pgid
    try:
        extra_paths = (
            (marketplace,) if retained_runtime is None else (marketplace, retained_runtime)
        )
        require_disposable_cleanup(run_root, extra_paths=extra_paths)
    except DisposableCleanupError:
        return


def _fail(reason: str) -> NoReturn:
    raise ValueError(reason)


def _write_failure(path: Path, reason: str, *, errors: tuple[str, ...] = ()) -> None:
    write_json(path, {"status": "failed", "reason": reason, "errors": list(errors)})
    path.chmod(0o600)
