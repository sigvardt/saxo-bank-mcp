#!/usr/bin/env python3
"""Stdlib-only startup receipt for the installed Codex-native proof child."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Final, cast

_COMMIT_PATTERN: Final = re.compile(r"^[a-f0-9]{40}$")
_SHA256_PATTERN: Final = re.compile(r"^[a-f0-9]{64}$")
_CONFIG_FAILURE_EXIT: Final = 78
_ARGUMENT_FAILURE_EXIT: Final = 2
_MAX_SHELL_EXIT: Final = 255
_MAX_CHILD_STDOUT_BYTES: Final = 1_048_576
_OWNER_FILE_MODE: Final = 0o600
_OWNER_DIRECTORY_MODE: Final = 0o700
_PRODUCER_MODULE: Final = "saxo_bank_mcp.qa_analytics_proof_producer"
_PRODUCER_MODULE_RELATIVE: Final = Path("src/saxo_bank_mcp/qa_analytics_proof_producer.py")
_RUNTIME_BINDING_KIND: Final = "codex_native_proof_runtime"
_MAX_BINDING_BYTES: Final = 65_536
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
    parser.add_argument("--producer-root", required=True)
    parser.add_argument("--producer-python", required=True)
    parser.add_argument("--runtime-binding-path", required=True)
    parser.add_argument("--runtime-binding-sha256", required=True)
    parser.add_argument("--install-report-path", required=True)
    parser.add_argument("--install-report-sha256", required=True)
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
        "runtime_binding_sha256": args.runtime_binding_sha256,
        "install_report_sha256": args.install_report_sha256,
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
                args.runtime_binding_sha256,
                args.install_report_sha256,
            )
        )
        and hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == args.bootstrap_module_sha256
    )


def _bound_producer_root(args: argparse.Namespace) -> Path | None:
    root = Path(args.producer_root)
    try:
        root_metadata = os.lstat(root)
    except OSError:
        return None
    if (
        not root.is_absolute()
        or not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_metadata.st_uid != os.getuid()
    ):
        return None
    producer = root / _PRODUCER_MODULE_RELATIVE
    try:
        producer_metadata = os.lstat(producer)
        producer_digest = hashlib.sha256(producer.read_bytes()).hexdigest()
    except OSError:
        return None
    if (
        not stat.S_ISREG(producer_metadata.st_mode)
        or stat.S_ISLNK(producer_metadata.st_mode)
        or producer_metadata.st_uid != os.getuid()
        or producer_digest != args.producer_module_sha256
    ):
        return None
    return root


def _safe_owner_input(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except OSError:
        return False
    return (
        path.is_absolute()
        and stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == _OWNER_FILE_MODE
    )


def _sha256_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _runtime_binding(  # noqa: C901, PLR0911, PLR0912
    args: argparse.Namespace,
    producer_root: Path,
) -> tuple[Path, Path] | None:
    binding_path = Path(args.runtime_binding_path)
    install_report_path = Path(args.install_report_path)
    if (
        not _safe_owner_input(binding_path)
        or not _safe_owner_input(install_report_path)
        or _sha256_file(binding_path) is None
        or _sha256_file(install_report_path) != args.install_report_sha256
    ):
        return None
    try:
        if binding_path.stat().st_size > _MAX_BINDING_BYTES:
            return None
        decoded: object = json.loads(binding_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    binding = cast("dict[str, object]", decoded)
    expected_keys = {
        "schema_version",
        "receipt_kind",
        "harness_policy",
        "candidate_commit",
        "candidate_tree",
        "runtime_root",
        "producer_root",
        "interpreter",
        "source_inventory_sha256",
        "installed_cache_sha256",
        "dependency_lock_sha256",
        "producer_module_sha256",
        "interpreter_sha256",
        "interpreter_identity_sha256",
        "python_implementation",
        "python_version",
        "python_cache_tag",
        "probe_receipt_sha256",
        "owner_only",
        "binding_sha256",
    }
    if set(binding) != expected_keys:
        return None
    material = dict(binding)
    claimed_digest = material.pop("binding_sha256", None)
    if (
        claimed_digest != args.runtime_binding_sha256
        or _digest(material) != args.runtime_binding_sha256
        or binding.get("schema_version") != "1"
        or binding.get("receipt_kind") != _RUNTIME_BINDING_KIND
        or binding.get("harness_policy") != "codex_native_v1"
        or binding.get("candidate_commit") != args.candidate_commit
        or binding.get("installed_cache_sha256") != args.installed_cache_sha256
        or binding.get("producer_module_sha256") != args.producer_module_sha256
        or binding.get("producer_root") != str(producer_root)
        or binding.get("owner_only") is not True
    ):
        return None
    digest_fields = (
        "source_inventory_sha256",
        "installed_cache_sha256",
        "dependency_lock_sha256",
        "producer_module_sha256",
        "interpreter_sha256",
        "interpreter_identity_sha256",
        "probe_receipt_sha256",
    )
    if any(
        not isinstance(binding.get(key), str)
        or _SHA256_PATTERN.fullmatch(cast("str", binding[key])) is None
        for key in digest_fields
    ):
        return None
    candidate_tree = binding.get("candidate_tree")
    if not isinstance(candidate_tree, str) or _COMMIT_PATTERN.fullmatch(candidate_tree) is None:
        return None
    for key in ("python_implementation", "python_version", "python_cache_tag"):
        if not isinstance(binding.get(key), str) or not cast("str", binding[key]):
            return None
    runtime_root = Path(cast("str", binding.get("runtime_root")))
    producer_python = Path(args.producer_python).absolute()
    if binding.get("interpreter") != str(producer_python):
        return None
    try:
        runtime_metadata = os.lstat(runtime_root)
        interpreter_metadata = os.lstat(producer_python)
        interpreter = producer_python.resolve(strict=True)
        resolved_metadata = interpreter.stat()
    except OSError:
        return None
    if (
        not runtime_root.is_absolute()
        or not stat.S_ISDIR(runtime_metadata.st_mode)
        or stat.S_ISLNK(runtime_metadata.st_mode)
        or runtime_metadata.st_uid != os.getuid()
        or stat.S_IMODE(runtime_metadata.st_mode) != _OWNER_DIRECTORY_MODE
        or not producer_python.is_relative_to(runtime_root)
        or not (
            stat.S_ISREG(interpreter_metadata.st_mode) or stat.S_ISLNK(interpreter_metadata.st_mode)
        )
        or not stat.S_ISREG(resolved_metadata.st_mode)
        or not os.access(interpreter, os.X_OK)
        or _sha256_file(interpreter) != binding.get("interpreter_sha256")
        or hashlib.sha256(str(interpreter).encode()).hexdigest()
        != binding.get("interpreter_identity_sha256")
        or _sha256_file(producer_root / "uv.lock") != binding.get("dependency_lock_sha256")
    ):
        return None
    return runtime_root, producer_python


def _safe_child_stdout(raw: str, args: argparse.Namespace) -> str:
    if not raw or len(raw.encode("utf-8", errors="replace")) > _MAX_CHILD_STDOUT_BYTES:
        return ""
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(decoded, dict):
        return ""
    decoded_mapping = cast("dict[str, object]", decoded)
    expected = {
        "schema_version": "1",
        "harness_policy": "codex_native_v1",
        "candidate_commit": args.candidate_commit,
        "installed_cache_sha256": args.installed_cache_sha256,
        "producer_module_sha256": args.producer_module_sha256,
        "catalog_sha256": args.catalog_sha256,
        "contract_sha256": args.contract_sha256,
    }
    if any(decoded_mapping.get(key) != value for key, value in expected.items()):
        return ""
    receipt_kind = decoded_mapping.get("receipt_kind")
    if receipt_kind not in {None, "codex_native_child_failure"}:
        return ""
    return raw


def _normalized_child_exit(return_code: int) -> int:
    return return_code if 0 <= return_code <= _MAX_SHELL_EXIT else 1


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

    producer_root = _bound_producer_root(args)
    bound_runtime = None if producer_root is None else _runtime_binding(args, producer_root)
    if producer_root is None or bound_runtime is None:
        _write_failure(
            path,
            args,
            completed_phases=("entry",),
            current_phase="producer_import",
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

    _runtime_root, producer_python = bound_runtime
    producer_argv = (
        str(producer_python),
        "-I",
        "-m",
        _PRODUCER_MODULE,
        "--candidate-commit",
        args.candidate_commit,
        "--installed-cache-sha256",
        args.installed_cache_sha256,
        "--harness-policy",
        "codex_native_v1",
    )
    child_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"} and not key.startswith("UV_")
    }
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["PYTHONNOUSERSITE"] = "1"
    child_env["UV_OFFLINE"] = "1"
    try:
        child = subprocess.run(
            producer_argv,
            cwd=producer_root,
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        exit_code = 1
        _write_failure(
            path,
            args,
            completed_phases=("entry", "producer_import", "producer_handoff"),
            current_phase="producer_execution",
            child_exit_code=1,
            known_inactive=False,
            reason="proof_bootstrap_installed_runtime_failed",
        )
        return exit_code

    safe_stdout = _safe_child_stdout(child.stdout, args)
    exit_code = _normalized_child_exit(child.returncode)
    if exit_code != 0:
        _write_failure(
            path,
            args,
            completed_phases=("entry", "producer_import", "producer_handoff"),
            current_phase="producer_execution",
            child_exit_code=exit_code,
            known_inactive=False,
            reason=(
                "proof_bootstrap_invalid_arguments"
                if exit_code == _ARGUMENT_FAILURE_EXIT
                else "proof_bootstrap_producer_nonzero"
            ),
        )
        if safe_stdout:
            sys.stdout.write(safe_stdout)
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
    if not _atomic_write(path, complete):
        return _CONFIG_FAILURE_EXIT
    if safe_stdout:
        sys.stdout.write(safe_stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
