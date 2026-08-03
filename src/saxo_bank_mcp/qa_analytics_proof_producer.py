"""Process-owned execution boundary for the installed analytics proof producer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    run_command,
)
from saxo_bank_mcp.agent_skill_evidence_io import git_output
from saxo_bank_mcp.agent_skill_install_models import (
    FixtureSupportReport,
    InstallEvidenceReport,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    installed_inventory_check,
    publishable_tracked_files,
    tree_digest,
)
from saxo_bank_mcp.qa_analytics_evidence import (
    build_proof_execution_contracts,
    load_analysis_kind_catalog,
)

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_PRODUCER_MODULE_RELATIVE = Path("src/saxo_bank_mcp/qa_analytics_proof_producer.py")
_COMMAND_NAME = "analytics_proof_producer"
_PROCESS_AUTHORITY = object()


class ProofProducerError(RuntimeError):
    """Fail-closed reason from the private installed-producer boundary."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
    )


class InstalledProofProducerResult(_StrictModel):
    """Typed stdout issued by the producer running from verified installed bytes."""

    schema_version: Literal["1"] = "1"
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    installed_cache_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    producer_module_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["validated", "blocked"]
    execution_performed: Literal[True]
    process_local_activation: bool
    executed_receipt_count: int = Field(ge=1)
    proof_execution_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    validation_errors: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        if self.status == "validated":
            if (
                not self.process_local_activation
                or self.executed_receipt_count < 1
                or self.validation_errors
            ):
                raise ValueError("validated proof production requires executed receipts")
        elif (
            self.process_local_activation
            or self.executed_receipt_count < 1
            or self.validation_errors != ("proof_profiles_not_active",)
        ):
            raise ValueError("blocked proof production must preserve checked-in quarantine")
        return self


class VerifiedInstalledProofValidation(_StrictModel):
    """Redacted validation issued only from one authenticated child execution."""

    schema_version: Literal["1"] = "1"
    status: Literal["validated", "blocked"]
    candidate_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    installed_cache_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    contract_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    producer_authenticated: Literal[True]
    execution_performed: Literal[True]
    process_local_activation: bool
    executed_receipt_count: int = Field(ge=1)
    proof_execution_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    validation_errors: tuple[str, ...]
    network_call_made: Literal[False] = False
    broker_write_made: Literal[False] = False
    disclaimer_response_made: Literal[False] = False


def run_verified_installed_producer(
    install: InstallEvidenceReport | FixtureSupportReport,
    *,
    candidate_commit: str,
) -> VerifiedInstalledProofValidation:
    """Execute the producer from one independently verified production cache.

    This function accepts the typed result of ``load_verified_install_report`` only. Caller
    receipt paths and caller-authored proof JSON are never inputs to this boundary.
    """
    if type(install) is not InstallEvidenceReport:
        raise ProofProducerError("fixture_support_not_production")
    if (
        _COMMIT_PATTERN.fullmatch(candidate_commit) is None
        or install.candidate_commit != candidate_commit
    ):
        raise ProofProducerError("proof_installed_candidate_mismatch")
    _require_clean_source_commit(install.repo, candidate_commit)
    clone_digest, codex_digest, claude_digest = _verified_install_digests(install)
    if clone_digest != codex_digest or clone_digest != claude_digest:
        raise ProofProducerError("proof_installed_cache_digest_mismatch")
    expected_module_sha256 = _installed_producer_module_sha256(install.codex.cache_root)
    command = _producer_command(candidate_commit, codex_digest)
    result = _execute_installed_child(install.codex.cache_root, command)

    _require_clean_source_commit(install.repo, candidate_commit)
    after_digests = _verified_install_digests(install)
    if after_digests != (clone_digest, codex_digest, claude_digest):
        raise ProofProducerError("proof_installed_cache_changed_during_execution")
    if _installed_producer_module_sha256(install.codex.cache_root) != expected_module_sha256:
        raise ProofProducerError("proof_installed_producer_changed_during_execution")
    return _validate_process_owned_result(
        result,
        command=command,
        cache_root=install.codex.cache_root,
        candidate_commit=candidate_commit,
        installed_cache_sha256=codex_digest,
        producer_module_sha256=expected_module_sha256,
        authority=_PROCESS_AUTHORITY,
    )


def produce_installed_result(
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
) -> InstalledProofProducerResult:
    """Issue process-owned receipts for the exact installed proof contract state."""
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise ProofProducerError("proof_candidate_commit_invalid")
    if _SHA256_PATTERN.fullmatch(installed_cache_sha256) is None:
        raise ProofProducerError("proof_installed_cache_digest_invalid")
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    catalog_sha256, contract_sha256 = _installed_contract_digests()
    producer_module_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    executed_receipt_count, proof_execution_sha256 = _execute_installed_contract_receipts(
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        producer_module_sha256=producer_module_sha256,
    )
    inactive = any(contract.proof_activation_state != "active" for contract in contracts)
    if inactive:
        return InstalledProofProducerResult(
            candidate_commit=candidate_commit,
            installed_cache_sha256=installed_cache_sha256,
            producer_module_sha256=producer_module_sha256,
            catalog_sha256=catalog_sha256,
            contract_sha256=contract_sha256,
            status="blocked",
            execution_performed=True,
            process_local_activation=False,
            executed_receipt_count=executed_receipt_count,
            proof_execution_sha256=proof_execution_sha256,
            validation_errors=("proof_profiles_not_active",),
        )
    raise ProofProducerError("active_proof_receipts_require_installed_execution")


def _validate_process_owned_result(  # noqa: PLR0913
    command_result: CommandResult,
    *,
    command: tuple[str, ...],
    cache_root: Path,
    candidate_commit: str,
    installed_cache_sha256: str,
    producer_module_sha256: str,
    authority: object,
) -> VerifiedInstalledProofValidation:
    """Authenticate typed child stdout without accepting any caller evidence path."""
    if authority is not _PROCESS_AUTHORITY:
        raise ProofProducerError("trusted_producer_provenance_missing")
    receipt = command_result.receipt
    stdout_sha256 = hashlib.sha256(command_result.stdout.encode()).hexdigest()
    stderr_sha256 = hashlib.sha256(command_result.stderr.encode()).hexdigest()
    if (
        receipt.name != _COMMAND_NAME
        or receipt.argv != command
        or Path(receipt.cwd).resolve() != cache_root.resolve()
        or receipt.pid is None
        or receipt.pgid is None
        or receipt.exit_code != 0
        or receipt.timed_out
        or not receipt.cleanup_attempted
        or receipt.stdout_sha256 != stdout_sha256
        or receipt.stderr_sha256 != stderr_sha256
    ):
        raise ProofProducerError("proof_producer_command_untrusted")
    try:
        produced = InstalledProofProducerResult.model_validate_json(command_result.stdout)
    except ValidationError as error:
        raise ProofProducerError("proof_producer_result_invalid") from error
    catalog_sha256, contract_sha256 = _installed_contract_digests()
    if (
        produced.candidate_commit != candidate_commit
        or produced.installed_cache_sha256 != installed_cache_sha256
        or produced.producer_module_sha256 != producer_module_sha256
        or produced.catalog_sha256 != catalog_sha256
        or produced.contract_sha256 != contract_sha256
    ):
        raise ProofProducerError("proof_producer_candidate_binding_mismatch")
    expected_count, expected_execution_sha256 = _execute_installed_contract_receipts(
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        producer_module_sha256=producer_module_sha256,
    )
    if (
        produced.executed_receipt_count != expected_count
        or produced.proof_execution_sha256 != expected_execution_sha256
    ):
        raise ProofProducerError("proof_producer_execution_binding_mismatch")
    return VerifiedInstalledProofValidation(
        status=produced.status,
        candidate_commit=candidate_commit,
        installed_cache_sha256=installed_cache_sha256,
        catalog_sha256=catalog_sha256,
        contract_sha256=contract_sha256,
        producer_authenticated=True,
        execution_performed=True,
        process_local_activation=produced.process_local_activation,
        executed_receipt_count=produced.executed_receipt_count,
        proof_execution_sha256=produced.proof_execution_sha256,
        validation_errors=produced.validation_errors,
    )


def _execute_installed_contract_receipts(
    *,
    candidate_commit: str,
    installed_cache_sha256: str,
    producer_module_sha256: str,
) -> tuple[int, str]:
    """Execute exact installed catalog/profile checks and bind every result once.

    These receipts prove the installed candidate's current proof state. Quarantined profiles
    remain quarantined; contract validation alone never activates analytical claims.
    """
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    if len(contracts) != len(catalog.analysis_kinds):
        raise ProofProducerError("proof_contract_coverage_mismatch")
    receipts: list[dict[str, object]] = []
    for analysis_kind, profile_id, contract in zip(
        catalog.analysis_kinds,
        catalog.proof_profile_ids,
        contracts,
        strict=True,
    ):
        if (
            contract.analysis_kind != analysis_kind
            or contract.proof_profile_id != profile_id
            or not contract.cases
        ):
            raise ProofProducerError("proof_contract_binding_mismatch")
        receipts.append(
            {
                "analysis_kind": analysis_kind,
                "case_contract_sha256": _digest(
                    [case.model_dump(mode="json") for case in contract.cases]
                ),
                "profile_activation_state": contract.proof_activation_state,
                "proof_profile_id": profile_id,
                "source_catalog_sha256": contract.source_catalog_sha256,
                "definition_catalog_sha256": contract.definition_catalog_sha256,
            }
        )
    return len(receipts), _digest(
        {
            "candidate_commit": candidate_commit,
            "installed_cache_sha256": installed_cache_sha256,
            "producer_module_sha256": producer_module_sha256,
            "receipts": receipts,
        }
    )


def _producer_command(candidate_commit: str, installed_cache_sha256: str) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--offline",
        "python",
        "-m",
        "saxo_bank_mcp.qa_analytics_proof_producer",
        "--candidate-commit",
        candidate_commit,
        "--installed-cache-sha256",
        installed_cache_sha256,
    )


def _execute_installed_child(
    cache_root: Path,
    command: tuple[str, ...],
) -> CommandResult:
    temp_parent = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
    with tempfile.TemporaryDirectory(prefix="analytics-proof-producer-", dir=temp_parent) as raw:
        runtime_root = Path(raw)
        runtime_root.chmod(0o700)
        env = {
            "HOME": str(runtime_root),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "UV_OFFLINE": "1",
        }
        if uv_cache := os.environ.get("UV_CACHE_DIR"):
            env["UV_CACHE_DIR"] = uv_cache
        try:
            return run_command(
                _COMMAND_NAME,
                command,
                cwd=cache_root.resolve(),
                env=env,
                timeout_seconds=180,
            )
        except CommandFailureError as error:
            raise ProofProducerError("proof_producer_command_failed") from error


def _require_clean_source_commit(repo: Path, candidate_commit: str) -> None:
    resolved = repo.resolve()
    if git_output(resolved, "rev-parse", "HEAD") != candidate_commit:
        raise ProofProducerError("proof_source_candidate_mismatch")
    if git_output(resolved, "status", "--porcelain", "--untracked-files=no") != "":
        raise ProofProducerError("proof_source_worktree_not_clean")


def _verified_install_digests(
    install: InstallEvidenceReport,
) -> tuple[str, str, str]:
    clone = install.clone.path.resolve()
    publishable = publishable_tracked_files(clone)
    if not publishable:
        raise ProofProducerError("proof_installed_inventory_empty")
    digests = [tree_digest(clone, publishable)]
    for cache in (install.codex.cache_root.resolve(), install.claude.cache_root.resolve()):
        inventory = installed_inventory_check(clone, cache, publishable=publishable)
        if inventory.get("inventory_exact_match") is not True:
            raise ProofProducerError("proof_installed_inventory_mismatch")
        digests.append(tree_digest(cache, publishable))
    return digests[0], digests[1], digests[2]


def _installed_producer_module_sha256(cache_root: Path) -> str:
    path = cache_root.resolve() / _PRODUCER_MODULE_RELATIVE
    if path.is_symlink() or not path.is_file():
        raise ProofProducerError("proof_installed_producer_missing")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _installed_contract_digests() -> tuple[str, str]:
    catalog = load_analysis_kind_catalog()
    contracts = build_proof_execution_contracts(catalog=catalog)
    catalog_material = catalog.model_dump(mode="json")
    contract_material = [contract.model_dump(mode="json") for contract in contracts]
    return _digest(catalog_material), _digest(contract_material)


def _digest(value: object) -> str:
    rendered = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(rendered).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the installed analytics proof producer.")
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--installed-cache-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        result = produce_installed_result(
            candidate_commit=str(args.candidate_commit),
            installed_cache_sha256=str(args.installed_cache_sha256),
        )
    except (OSError, ProofProducerError, ValidationError, ValueError):
        return 1
    sys.stdout.write(result.model_dump_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
