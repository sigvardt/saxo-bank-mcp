#!/usr/bin/env python3
"""Stdlib-only startup receipt for the installed Codex-native proof child."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, Final

_COMMIT_PATTERN: Final = re.compile(r"^[a-f0-9]{40}$")
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_CONFIG_FAILURE_EXIT: Final = 78
_OWNER_FILE_MODE: Final = 0o600
_OWNER_DIRECTORY_MODE: Final = 0o700
_PRODUCER_MODULE: Final = "saxo_bank_mcp.qa_analytics_proof_producer"
_BOOTSTRAP_PHASES: Final = (
    "entry",
    "producer_import",
    "producer_handoff",
    "producer_execution",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    parser.add_argument("--envelope-path", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--installed-cache-sha256", required=True)
    parser.add_argument("--bootstrap-module-sha256", required=True)
    parser.add_argument("--producer-module-sha256", required=True)
    parser.add_argument("--catalog-sha256", required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--harness-policy", required=True)
    return parser


def _digest(value: object) -> str:
    rendered = json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(rendered).hexdigest()


def _base_material(args: argparse.Namespace) -> dict[str, object]:
    return {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_bootstrap",
        "harness_policy": "codex_native_v1",
        "candidate_commit": args.candidate_commit,
        "installed_cache_sha256": args.installed_cache_sha256,
        "bootstrap_module_sha256": args.bootstrap_module_sha256,
        "producer_module_sha256": args.producer_module_sha256,
        "catalog_sha256": args.catalog_sha256,
        "contract_sha256": args.contract_sha256,
    }


def _activity_material(*, known_inactive: bool) -> dict[str, object]:
    known = False if known_inactive else None
    zero = 0 if known_inactive else None
    return {
        "sim_preflight_status": "not_started" if known_inactive else "unknown",
        "network_call_made": known,
        "model_event_count": zero,
        "mcp_event_count": zero,
        "saxo_event_count": zero,
        "execution_performed": known,
        "broker_write_made": known,
        "live_mutation_calls": zero,
        "purchase_occurred": known,
        "disclaimer_response_made": known,
        "child_cleanup_status": "complete" if known_inactive else "unknown",
        "child_remaining_process_count": zero,
        "child_remaining_process_group_count": zero,
    }


def _envelope(  # noqa: PLR0913
    args: argparse.Namespace,
    *,
    state: str,
    completed_phases: tuple[str, ...],
    current_phase: str,
    child_exit_code: int | None,
    known_inactive: bool,
    reason: str,
) -> dict[str, object]:
    material = {
        **_base_material(args),
        "bootstrap_state": state,
        "completed_bootstrap_phases": completed_phases,
        "current_bootstrap_phase": current_phase,
        "child_exit_code": child_exit_code,
        **_activity_material(known_inactive=known_inactive),
        "reason": reason,
        "redacted_publication": True,
    }
    return {**material, "envelope_sha256": _digest(material)}


def _safe_existing_target(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _OWNER_FILE_MODE
    )


def _safe_parent(path: Path) -> bool:
    try:
        metadata = os.lstat(path.parent)
    except OSError:
        return False
    return (
        path.is_absolute()
        and stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == _OWNER_DIRECTORY_MODE
    )


def _atomic_write(path: Path, payload: dict[str, object]) -> bool:
    if not _safe_parent(path) or not _safe_existing_target(path):
        return False
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_temporary)
        os.fchmod(descriptor, _OWNER_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(payload, handle, allow_nan=False, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        temporary = None
        path.chmod(_OWNER_FILE_MODE)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()
    return True


def _valid_bindings(args: argparse.Namespace) -> bool:
    return (
        args.harness_policy == "codex_native_v1"
        and _COMMIT_PATTERN.fullmatch(args.candidate_commit) is not None
        and all(
            _SHA256_PATTERN.fullmatch(value) is not None
            for value in (
                args.installed_cache_sha256,
                args.bootstrap_module_sha256,
                args.producer_module_sha256,
                args.catalog_sha256,
                args.contract_sha256,
            )
        )
        and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == args.bootstrap_module_sha256
    )


def _write_failure(  # noqa: PLR0913
    path: Path,
    args: argparse.Namespace,
    *,
    completed_phases: tuple[str, ...],
    current_phase: str,
    child_exit_code: int,
    known_inactive: bool,
    reason: str,
) -> bool:
    return _atomic_write(
        path,
        _envelope(
            args,
            state="failed",
            completed_phases=completed_phases,
            current_phase=current_phase,
            child_exit_code=child_exit_code,
            known_inactive=known_inactive,
            reason=reason,
        ),
    )


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0911
    try:
        args = _parser().parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        return _CONFIG_FAILURE_EXIT
    path = Path(args.envelope_path)
    if not _valid_bindings(args):
        return _CONFIG_FAILURE_EXIT
    entry = _envelope(
        args,
        state="entered",
        completed_phases=("entry",),
        current_phase="producer_import",
        child_exit_code=None,
        known_inactive=True,
        reason="proof_bootstrap_entered",
    )
    if not _atomic_write(path, entry):
        return _CONFIG_FAILURE_EXIT

    try:
        producer: Any = importlib.import_module(_PRODUCER_MODULE)
    except BaseException:  # noqa: BLE001
        _write_failure(
            path,
            args,
            completed_phases=("entry",),
            current_phase="producer_import",
            child_exit_code=1,
            known_inactive=True,
            reason="proof_bootstrap_import_failed",
        )
        return 1

    producer_file = getattr(producer, "__file__", None)
    try:
        producer_digest = (
            hashlib.sha256(Path(producer_file).read_bytes()).hexdigest()
            if isinstance(producer_file, str)
            else ""
        )
    except OSError:
        producer_digest = ""
    entrypoint = getattr(producer, "main", None)
    if producer_digest != args.producer_module_sha256 or not callable(entrypoint):
        _write_failure(
            path,
            args,
            completed_phases=("entry", "producer_import"),
            current_phase="producer_handoff",
            child_exit_code=1,
            known_inactive=True,
            reason="proof_bootstrap_producer_binding_failed",
        )
        return 1

    imported = _envelope(
        args,
        state="producer_imported",
        completed_phases=("entry", "producer_import"),
        current_phase="producer_handoff",
        child_exit_code=None,
        known_inactive=True,
        reason="proof_bootstrap_producer_imported",
    )
    if not _atomic_write(path, imported):
        return _CONFIG_FAILURE_EXIT
    started = _envelope(
        args,
        state="producer_started",
        completed_phases=("entry", "producer_import", "producer_handoff"),
        current_phase="producer_execution",
        child_exit_code=None,
        known_inactive=False,
        reason="proof_bootstrap_producer_started",
    )
    if not _atomic_write(path, started):
        return _CONFIG_FAILURE_EXIT

    producer_argv = [
        "--candidate-commit",
        args.candidate_commit,
        "--installed-cache-sha256",
        args.installed_cache_sha256,
        "--harness-policy",
        "codex_native_v1",
    ]
    try:
        raw_exit = entrypoint(producer_argv)
    except SystemExit as error:
        exit_code = error.code if isinstance(error.code, int) and error.code != 0 else 1
        _write_failure(
            path,
            args,
            completed_phases=("entry", "producer_import", "producer_handoff"),
            current_phase="producer_execution",
            child_exit_code=exit_code,
            known_inactive=False,
            reason="proof_bootstrap_invalid_arguments",
        )
        return exit_code
    except BaseException:  # noqa: BLE001
        _write_failure(
            path,
            args,
            completed_phases=("entry", "producer_import", "producer_handoff"),
            current_phase="producer_execution",
            child_exit_code=1,
            known_inactive=False,
            reason="proof_bootstrap_producer_exception",
        )
        return 1
    exit_code = raw_exit if isinstance(raw_exit, int) and not isinstance(raw_exit, bool) else 1
    if exit_code != 0:
        _write_failure(
            path,
            args,
            completed_phases=("entry", "producer_import", "producer_handoff"),
            current_phase="producer_execution",
            child_exit_code=exit_code,
            known_inactive=False,
            reason="proof_bootstrap_producer_nonzero",
        )
        return exit_code
    complete = _envelope(
        args,
        state="complete",
        completed_phases=_BOOTSTRAP_PHASES,
        current_phase="complete",
        child_exit_code=0,
        known_inactive=False,
        reason="proof_bootstrap_complete",
    )
    return 0 if _atomic_write(path, complete) else _CONFIG_FAILURE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
