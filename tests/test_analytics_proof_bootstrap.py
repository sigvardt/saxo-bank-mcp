from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

BOOTSTRAP = (
    Path(__file__).resolve().parents[1] / "src/saxo_bank_mcp/qa_analytics_proof_bootstrap.py"
)
CANDIDATE = "1" * 40
CACHE_SHA256 = "2" * 64
CATALOG_SHA256 = "4" * 64
CONTRACT_SHA256 = "5" * 64
CONFIG_FAILURE_EXIT = 78
ARGUMENT_FAILURE_EXIT = 2
CRASH_EXIT = 17
PRODUCER_FAILURE_EXIT = 9
UNTYPED_FAILURE_EXIT = 7
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700


@dataclass(frozen=True, slots=True)
class _BootstrapRun:
    process: subprocess.CompletedProcess[str]
    envelope_path: Path
    producer_marker: Path


def _run_bootstrap(
    tmp_path: Path,
    producer_source: str,
    *,
    envelope_path: Path | None = None,
    launcher_mode: str = "working",
    isolated_path: bool = False,
) -> _BootstrapRun:
    producer_root = tmp_path / "installed-root"
    package = producer_root / "src/saxo_bank_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    producer = package / "qa_analytics_proof_producer.py"
    producer.write_text(
        producer_source
        + "\nimport sys as _bootstrap_test_sys\n"
        + "if __name__ == '__main__':\n"
        + "    raise SystemExit(main(_bootstrap_test_sys.argv[1:]))\n",
        encoding="utf-8",
    )
    producer_sha256 = hashlib.sha256(producer.read_bytes()).hexdigest()
    (producer_root / "pyproject.toml").write_text(
        '[project]\nname = "proof-env-test"\nversion = "0.1.0"\n'
        'requires-python = ">=3.12"\ndependencies = []\n',
        encoding="utf-8",
    )
    lock = producer_root / "uv.lock"
    lock.write_text(
        'version = 1\nrevision = 3\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "proof-env-test"\nversion = "0.1.0"\n'
        'source = { virtual = "." }\n',
        encoding="utf-8",
    )
    lock_sha256 = hashlib.sha256(lock.read_bytes()).hexdigest()
    bootstrap_sha256 = hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest()
    evidence = tmp_path / "private-evidence"
    evidence.mkdir(mode=OWNER_DIRECTORY_MODE)
    target = envelope_path or evidence / "bootstrap.json"
    marker = tmp_path / "producer-imported"
    runtime_root = tmp_path / "proof-runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime_root)
    runtime_root.chmod(OWNER_DIRECTORY_MODE)
    producer_python = runtime_root / "bin/python"
    site = subprocess.run(
        (
            str(producer_python),
            "-I",
            "-c",
            "import site; print(site.getsitepackages()[0])",
        ),
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    Path(site.stdout.strip(), "proof-runtime.pth").write_text(
        str((producer_root / "src").resolve()) + "\n",
        encoding="utf-8",
    )
    interpreter = producer_python.resolve(strict=True)
    binding_material: dict[str, object] = {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_runtime",
        "harness_policy": "codex_native_v1",
        "candidate_commit": CANDIDATE,
        "candidate_tree": "a" * 40,
        "runtime_root": str(runtime_root.resolve()),
        "producer_root": str(producer_root.resolve()),
        "interpreter": str(producer_python.absolute()),
        "source_inventory_sha256": CACHE_SHA256,
        "installed_cache_sha256": CACHE_SHA256,
        "dependency_lock_sha256": lock_sha256,
        "producer_module_sha256": producer_sha256,
        "interpreter_sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "interpreter_identity_sha256": hashlib.sha256(str(interpreter).encode()).hexdigest(),
        "python_implementation": "CPython",
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "python_cache_tag": sys.implementation.cache_tag,
        "probe_receipt_sha256": hashlib.sha256(b"bootstrap-test-probe").hexdigest(),
        "owner_only": True,
    }
    binding_sha256 = hashlib.sha256(
        json.dumps(
            binding_material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    binding_path = tmp_path / "proof-runtime-binding.json"
    binding_path.write_text(
        json.dumps({**binding_material, "binding_sha256": binding_sha256}, sort_keys=True),
        encoding="utf-8",
    )
    binding_path.chmod(OWNER_FILE_MODE)
    install_report = tmp_path / "install-report.json"
    install_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    install_report.chmod(OWNER_FILE_MODE)
    if launcher_mode == "missing":
        producer_python.unlink()
    elif launcher_mode == "broken":
        payload = json.loads(binding_path.read_text(encoding="utf-8"))
        payload["candidate_tree"] = "b" * 40
        binding_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        binding_path.chmod(OWNER_FILE_MODE)
    elif launcher_mode != "working":
        raise AssertionError("unknown launcher fixture")
    environment = {
        "PATH": "" if isolated_path else os.environ.get("PATH", ""),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            str(BOOTSTRAP),
            "--envelope-path",
            str(target),
            "--candidate-commit",
            CANDIDATE,
            "--installed-cache-sha256",
            CACHE_SHA256,
            "--bootstrap-module-sha256",
            bootstrap_sha256,
            "--producer-module-sha256",
            producer_sha256,
            "--catalog-sha256",
            CATALOG_SHA256,
            "--contract-sha256",
            CONTRACT_SHA256,
            "--harness-policy",
            "codex_native_v1",
            "--producer-root",
            str(producer_root),
            "--producer-python",
            str(producer_python),
            "--runtime-binding-path",
            str(binding_path),
            "--runtime-binding-sha256",
            binding_sha256,
            "--install-report-path",
            str(install_report),
            "--install-report-sha256",
            hashlib.sha256(install_report.read_bytes()).hexdigest(),
        ),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return _BootstrapRun(process=process, envelope_path=target, producer_marker=marker)


def _read_authenticated_envelope(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    digest = payload.pop("envelope_sha256")
    expected = hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    assert digest == expected
    payload["envelope_sha256"] = digest
    return payload


def test_bootstrap_source_imports_only_stdlib() -> None:
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert imported_roots <= sys.stdlib_module_names | {"__future__"}


def test_bootstrap_import_failure_retains_authenticated_unknown_execution(
    tmp_path: Path,
) -> None:
    result = _run_bootstrap(
        tmp_path,
        "raise RuntimeError('access_' + 'token=must-not-leak')\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == 1
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["completed_bootstrap_phases"] == [
        "entry",
        "producer_import",
        "producer_handoff",
    ]
    assert envelope["current_bootstrap_phase"] == "producer_execution"
    assert envelope["child_exit_code"] == 1
    assert envelope["execution_performed"] is None
    assert envelope["network_call_made"] is None
    assert envelope["model_event_count"] is None
    assert envelope["mcp_event_count"] is None
    assert envelope["saxo_event_count"] is None
    assert envelope["broker_write_made"] is None
    assert envelope["live_mutation_calls"] is None
    assert envelope["purchase_occurred"] is None
    assert envelope["disclaimer_response_made"] is None
    assert envelope["reason"] == "proof_bootstrap_producer_nonzero"
    assert "access_token" not in json.dumps(envelope)
    assert stat.S_IMODE(result.envelope_path.parent.stat().st_mode) == OWNER_DIRECTORY_MODE
    assert stat.S_IMODE(result.envelope_path.stat().st_mode) == OWNER_FILE_MODE


def test_bootstrap_catches_producer_argument_system_exit(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "def main(argv):\n    del argv\n    raise SystemExit(2)\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == ARGUMENT_FAILURE_EXIT
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["current_bootstrap_phase"] == "producer_execution"
    assert envelope["child_exit_code"] == ARGUMENT_FAILURE_EXIT
    assert envelope["execution_performed"] is None
    assert envelope["network_call_made"] is None
    assert envelope["reason"] == "proof_bootstrap_invalid_arguments"


def test_bootstrap_catches_pre_tracker_failure_without_raw_error(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "def main(argv):\n    del argv\n    raise RuntimeError('account_' + 'id=must-not-leak')\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == 1
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["child_exit_code"] == 1
    assert envelope["execution_performed"] is None
    assert envelope["broker_write_made"] is None
    assert envelope["reason"] == "proof_bootstrap_producer_nonzero"
    assert "account_id" not in json.dumps(envelope)


def test_bootstrap_suppresses_untyped_child_stdout(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "import sys\n"
        "def main(argv):\n"
        "    del argv\n"
        "    sys.stdout.write('access_' + 'token=must-not-leak')\n"
        f"    return {UNTYPED_FAILURE_EXIT}\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == UNTYPED_FAILURE_EXIT
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert "access_token" not in json.dumps(envelope)


def test_bootstrap_entry_write_failure_stops_before_producer_import(tmp_path: Path) -> None:
    marker = tmp_path / "producer-imported"
    blocking_parent = tmp_path / "not-a-directory"
    blocking_parent.write_text("blocked", encoding="utf-8")
    result = _run_bootstrap(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        envelope_path=blocking_parent / "bootstrap.json",
    )

    assert result.process.returncode == CONFIG_FAILURE_EXIT
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert not marker.exists()
    assert not result.envelope_path.exists()


def test_bootstrap_child_crash_retains_unknown_handoff(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "import os\ndef main(argv):\n    del argv\n    os._exit(17)\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == CRASH_EXIT
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["completed_bootstrap_phases"] == [
        "entry",
        "producer_import",
        "producer_handoff",
    ]
    assert envelope["current_bootstrap_phase"] == "producer_execution"
    assert envelope["child_exit_code"] == CRASH_EXIT
    assert envelope["execution_performed"] is None
    assert envelope["network_call_made"] is None
    assert envelope["broker_write_made"] is None


def test_bootstrap_normal_nonzero_is_sanitized(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "def main(argv):\n    del argv\n    return 9\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == PRODUCER_FAILURE_EXIT
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["child_exit_code"] == PRODUCER_FAILURE_EXIT
    assert envelope["execution_performed"] is None
    assert envelope["reason"] == "proof_bootstrap_producer_nonzero"


def test_bootstrap_normal_success_retains_completed_receipt(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "import sys\n"
        "def main(argv):\n"
        "    del argv\n"
        '    sys.stdout.write(\'{"status":"ok"}\')\n'
        "    return 0\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == 0
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "complete"
    assert envelope["completed_bootstrap_phases"] == [
        "entry",
        "producer_import",
        "producer_handoff",
        "producer_execution",
    ]
    assert envelope["current_bootstrap_phase"] == "complete"
    assert envelope["child_exit_code"] == 0
    assert envelope["reason"] == "proof_bootstrap_complete"


@pytest.mark.parametrize("launcher_mode", ["missing", "broken"])
def test_bootstrap_runtime_binding_failure_retains_authenticated_inactivity(
    tmp_path: Path,
    launcher_mode: str,
) -> None:
    marker = tmp_path / "producer-imported"
    result = _run_bootstrap(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "def main(argv):\n    del argv\n    return 0\n",
        launcher_mode=launcher_mode,
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode != 0
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["completed_bootstrap_phases"] == ["entry"]
    assert envelope["current_bootstrap_phase"] == "producer_import"
    assert envelope["execution_performed"] is False
    assert envelope["network_call_made"] is False
    assert envelope["reason"] == "proof_bootstrap_producer_binding_failed"
    assert not marker.exists()


def test_bootstrap_direct_invocation_works_with_isolated_path(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "def main(argv):\n    del argv\n    return 0\n",
        isolated_path=True,
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == 0
    assert envelope["bootstrap_state"] == "complete"


def test_bootstrap_uv_child_writes_only_below_owner_run_root(tmp_path: Path) -> None:
    uv_raw = shutil.which("uv")
    assert uv_raw is not None
    uv = str(Path(uv_raw).resolve(strict=True))
    result = _run_bootstrap(
        tmp_path,
        "import os\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "def main(argv):\n"
        "    del argv\n"
        "    root = Path(__file__).resolve().parents[2]\n"
        f"    child = subprocess.run(({uv!r}, 'run', '--offline', '--project', "
        "str(root), 'python', '-c', 'pass'), cwd=root, env=dict(os.environ), "
        "capture_output=True, text=True, check=False)\n"
        "    return child.returncode\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    installed_root = tmp_path / "installed-root"
    proof_environment = tmp_path / "proof-runtime"
    proof_child_runtime = result.envelope_path.parent / "proof-child-runtime"
    expected_installed_files = {
        Path("pyproject.toml"),
        Path("src/saxo_bank_mcp/__init__.py"),
        Path("src/saxo_bank_mcp/qa_analytics_proof_producer.py"),
        Path("uv.lock"),
    }

    def installed_files() -> set[Path]:
        return {
            path.relative_to(installed_root)
            for path in installed_root.rglob("*")
            if path.is_file() or path.is_symlink()
        }

    assert result.process.returncode == 0
    assert envelope["bootstrap_state"] == "complete"
    assert not (installed_root / ".venv").exists()
    assert installed_files() == expected_installed_files
    assert proof_environment.is_dir()
    assert not proof_environment.resolve().is_relative_to(installed_root.resolve())
    assert stat.S_IMODE(proof_environment.stat().st_mode) == OWNER_DIRECTORY_MODE
    assert stat.S_IMODE(proof_child_runtime.stat().st_mode) == OWNER_DIRECTORY_MODE
    assert {path.name for path in proof_child_runtime.iterdir() if path.is_dir()} == {
        "uv-cache",
        "uv-python",
    }
    assert all(
        stat.S_IMODE(path.stat().st_mode) == OWNER_DIRECTORY_MODE
        for path in proof_child_runtime.iterdir()
    )

    shutil.rmtree(proof_child_runtime)
    shutil.rmtree(proof_environment)
    assert not proof_child_runtime.exists()
    assert not proof_environment.exists()
    assert installed_files() == expected_installed_files


def test_real_uv_offline_post_cleanup_launch_uses_bound_retained_interpreter(
    tmp_path: Path,
) -> None:
    """Catch any return to an offline uv launch after install cleanup.

    The old producer command must fail with a fresh cache and project environment. The
    bootstrap is expected to succeed through the already-probed retained interpreter without
    PYTHONPATH or either fresh uv directory.
    """
    uv_raw = shutil.which("uv")
    assert uv_raw is not None
    uv = Path(uv_raw).resolve(strict=True)
    producer_root = tmp_path / "clean-installed-root"
    package = producer_root / "src/saxo_bank_mcp"
    package.mkdir(parents=True)
    producer_root.chmod(OWNER_DIRECTORY_MODE)
    (package / "__init__.py").write_text("", encoding="utf-8")
    producer = package / "qa_analytics_proof_producer.py"
    producer.write_text(
        "def main(argv):\n"
        "    del argv\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    import sys\n"
        "    raise SystemExit(main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[1]
    shutil.copy2(repo_root / "pyproject.toml", producer_root / "pyproject.toml")
    shutil.copy2(repo_root / "uv.lock", producer_root / "uv.lock")

    runtime_root = tmp_path / "proof-runtime"
    uv_cache = tmp_path / "fresh-uv-cache"
    uv_project = tmp_path / "fresh-project-env"
    uv_python = tmp_path / "fresh-uv-python"
    task_home = tmp_path / "home"
    task_tmp = tmp_path / "tmp"
    for directory in (uv_cache, uv_project, uv_python, task_home, task_tmp):
        directory.mkdir(mode=OWNER_DIRECTORY_MODE)
    environment = {
        "HOME": str(task_home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(task_tmp),
        "TMP": str(task_tmp),
        "TEMP": str(task_tmp),
        "UV_CACHE_DIR": str(uv_cache),
        "UV_PROJECT_ENVIRONMENT": str(uv_project),
        "UV_PYTHON_INSTALL_DIR": str(uv_python),
        "UV_OFFLINE": "1",
    }
    created = subprocess.run(
        (
            str(uv),
            "venv",
            "--offline",
            "--python",
            sys.executable,
            str(runtime_root),
        ),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert created.returncode == 0
    runtime_root.chmod(OWNER_DIRECTORY_MODE)
    producer_python = runtime_root / "bin/python"
    site = subprocess.run(
        (
            str(producer_python),
            "-I",
            "-c",
            "import site; print(site.getsitepackages()[0])",
        ),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    Path(site.stdout.strip(), "proof-runtime.pth").write_text(
        str((producer_root / "src").resolve()) + "\n",
        encoding="utf-8",
    )

    old_launch = subprocess.run(
        (
            str(uv),
            "run",
            "--offline",
            "--project",
            str(producer_root),
            "python",
            "-m",
            "saxo_bank_mcp.qa_analytics_proof_producer",
            "--candidate-commit",
            CANDIDATE,
            "--installed-cache-sha256",
            CACHE_SHA256,
            "--harness-policy",
            "codex_native_v1",
        ),
        cwd=producer_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert old_launch.returncode != 0
    assert "PYTHONPATH" not in environment

    interpreter = producer_python.resolve(strict=True)
    lock_sha256 = hashlib.sha256((producer_root / "uv.lock").read_bytes()).hexdigest()
    binding_material: dict[str, object] = {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_runtime",
        "harness_policy": "codex_native_v1",
        "candidate_commit": CANDIDATE,
        "candidate_tree": "a" * 40,
        "runtime_root": str(runtime_root.resolve()),
        "producer_root": str(producer_root.resolve()),
        "interpreter": str(producer_python.absolute()),
        "source_inventory_sha256": CACHE_SHA256,
        "installed_cache_sha256": CACHE_SHA256,
        "dependency_lock_sha256": lock_sha256,
        "producer_module_sha256": hashlib.sha256(producer.read_bytes()).hexdigest(),
        "interpreter_sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "interpreter_identity_sha256": hashlib.sha256(str(interpreter).encode()).hexdigest(),
        "python_implementation": "CPython",
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "python_cache_tag": sys.implementation.cache_tag,
        "probe_receipt_sha256": hashlib.sha256(b"retained-runtime-probe").hexdigest(),
        "owner_only": True,
    }
    binding_sha256 = hashlib.sha256(
        json.dumps(
            binding_material,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    binding = {**binding_material, "binding_sha256": binding_sha256}
    binding_path = tmp_path / "proof-runtime-binding.json"
    binding_path.write_text(json.dumps(binding, sort_keys=True), encoding="utf-8")
    binding_path.chmod(OWNER_FILE_MODE)
    install_report = tmp_path / "install-report.json"
    install_report.write_text('{"status":"passed"}\n', encoding="utf-8")
    install_report.chmod(OWNER_FILE_MODE)
    evidence = tmp_path / "private-evidence-real"
    evidence.mkdir(mode=OWNER_DIRECTORY_MODE)
    bootstrap_sha256 = hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest()
    process = subprocess.run(
        (
            sys.executable,
            "-I",
            "-S",
            str(BOOTSTRAP),
            "--envelope-path",
            str(evidence / "bootstrap.json"),
            "--candidate-commit",
            CANDIDATE,
            "--installed-cache-sha256",
            CACHE_SHA256,
            "--bootstrap-module-sha256",
            bootstrap_sha256,
            "--producer-module-sha256",
            str(binding["producer_module_sha256"]),
            "--catalog-sha256",
            CATALOG_SHA256,
            "--contract-sha256",
            CONTRACT_SHA256,
            "--producer-root",
            str(producer_root),
            "--producer-python",
            str(producer_python),
            "--runtime-binding-path",
            str(binding_path),
            "--runtime-binding-sha256",
            binding_sha256,
            "--install-report-path",
            str(install_report),
            "--install-report-sha256",
            hashlib.sha256(install_report.read_bytes()).hexdigest(),
            "--harness-policy",
            "codex_native_v1",
        ),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert process.returncode == 0
    envelope = _read_authenticated_envelope(evidence / "bootstrap.json")
    assert envelope["bootstrap_state"] == "complete"
    assert (envelope["runtime_binding_sha256"], tuple(producer_root.rglob("__pycache__"))) == (
        binding_sha256,
        (),
    )
