from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import venv
from pathlib import Path

import pytest

from saxo_bank_mcp import agent_skill_codex_install as codex_install
from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_codex_install import (
    CodexInstallEvidenceReport,
    load_verified_codex_install_report,
)
from saxo_bank_mcp.agent_skill_evidence_io import git_output
from saxo_bank_mcp.agent_skill_install_discovery import parse_mcp_server_count, skill_inventory
from saxo_bank_mcp.agent_skill_install_paths import (
    codex_global_state_fingerprint,
    export_publishable_tree,
    installed_inventory_check,
    publishable_tracked_files,
    tree_digest,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOL_COUNT = 60
OWNER_DIRECTORY_MODE = 0o700
OWNER_FILE_MODE = 0o600


def _build_report_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    run_root = tmp_path / "run"
    clone = run_root / "source-clone"
    git = shutil.which("git")
    assert git is not None
    subprocess.run(
        (git, "clone", "--no-local", "--quiet", str(ROOT), str(clone)),
        check=True,
        text=True,
        capture_output=True,
    )
    commit = git_output(clone, "rev-parse", "HEAD")
    assert commit is not None
    project = tomllib.loads((clone / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    home = run_root / "home"
    codex_home = run_root / "codex-home"
    cache = codex_home / "plugins" / "cache" / "sigvardt" / "saxo-bank-mcp" / version
    for path in (run_root, clone, home, codex_home):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    export_publishable_tree(clone, cache)
    (codex_home / "config.toml").write_text(
        '[plugins."saxo-bank-mcp@sigvardt"]\nenabled = true\n',
        encoding="utf-8",
    )
    inventory = installed_inventory_check(clone, cache)
    startup: dict[str, JsonValue] = {
        "source": {"status": "passed", "tool_count": 60, "annotations_missing": []},
        "cache": {"status": "passed", "tool_count": 60, "annotations_missing": []},
        "list_tools": {"status": "passed", "tool_count": 60, "annotations_missing": []},
    }
    receipt: dict[str, JsonValue] = {
        "name": "codex_plugin_add",
        "argv": ["codex", "plugin", "add"],
        "cwd": str(clone),
        "exit_code": 0,
        "stdout_sha256": "a" * 64,
        "stderr_sha256": "b" * 64,
        "timed_out": False,
        "cleanup_attempted": True,
    }
    global_home = tmp_path / "global-codex"
    global_home.mkdir()
    state = codex_global_state_fingerprint(global_home)
    payload: dict[str, JsonValue] = {
        "status": "passed",
        "execution_mode": "codex_installed_verification",
        "harness_policy": "codex_native_v1",
        "repo": str(ROOT),
        "candidate_commit": commit,
        "clone": {
            "path": str(clone),
            "commit": commit,
            "source_repo": str(ROOT),
            "no_local": True,
            "clean": True,
            "mode": "0o700",
        },
        "expected_skills": 9,
        "expected_mcp_servers": 1,
        "expected_tools": 60,
        "global_codex_state": {
            "before": {"codex": state["codex"]},
            "after": {"codex": state["codex"]},
            "scope": state["scope"],
        },
        "global_codex_state_unchanged": True,
        "codex": {
            "installed": True,
            "cache_root": str(cache),
            "identity": "saxo-bank-mcp",
            "version": version,
            "cache_root_source": "codex_plugin_add",
            "skill_count": len(skill_inventory(cache)),
            "skills": list(skill_inventory(cache)),
            "mcp_server_count": parse_mcp_server_count(cache),
            "tool_count": 60,
            "annotations_missing": [],
            "source_annotations_missing": [],
            "cache_annotations_missing": [],
            "list_tools_annotations_missing": [],
            "forbidden_cache_paths": [],
            "installed_bytes_match": True,
            "install_command_exit_code": 0,
            "startup": startup,
            "command_receipts": [receipt],
            "inventory": inventory,
        },
        "installed_byte_checks": {
            "complete": True,
            "compared_files": inventory["compared_files"],
            "metadata_exceptions": inventory["metadata_exceptions"],
            "required_files_present": inventory["required_files_present"],
            "forbidden_files_absent": True,
            "mismatches": [],
            "inventory_exact_match": True,
        },
        "process_cleanup": {
            "complete": True,
            "remaining_pids": [],
            "remaining_pgids": [],
            "observed_pids": [],
            "observed_pgids": [],
        },
        "project_version": version,
        "run_root": str(run_root),
        "preserved_modes": {
            "run_root": "0o700",
            "clone": "0o700",
            "home": "0o700",
            "codex_home": "0o700",
            "codex_cache": "0o700",
        },
        "owner_only": True,
        "privacy_scan_passed": True,
        "errors": [],
    }
    report = tmp_path / "install.json"
    write_json(report, payload)
    report.chmod(0o600)
    return report, cache, global_home


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode(),
    ).hexdigest()


def _add_proof_runtime(report_path: Path, cache: Path) -> tuple[Path, Path]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    run_root = Path(payload["run_root"])
    clone = Path(payload["clone"]["path"])
    runtime_root = run_root / "proof-runtime"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime_root)
    runtime_root.chmod(0o700)
    interpreter_path = runtime_root / "bin/python"
    interpreter = interpreter_path.resolve(strict=True)
    publishable = publishable_tracked_files(clone)
    source_digest = tree_digest(clone, publishable)
    cache_digest = tree_digest(cache, publishable)
    producer = cache / "src/saxo_bank_mcp/qa_analytics_proof_producer.py"
    receipt: dict[str, JsonValue] = {
        "name": "codex_proof_runtime_probe",
        "argv": [str(interpreter_path), "-I", "-c", "<bound-runtime-probe>"],
        "cwd": str(cache),
        "pid": 101,
        "pgid": 101,
        "exit_code": 0,
        "stdout_sha256": "c" * 64,
        "stderr_sha256": "d" * 64,
        "timed_out": False,
        "cleanup_attempted": False,
    }
    payload["codex"]["command_receipts"].append(receipt)
    binding_material: dict[str, JsonValue] = {
        "schema_version": "1",
        "receipt_kind": "codex_native_proof_runtime",
        "harness_policy": "codex_native_v1",
        "candidate_commit": payload["candidate_commit"],
        "candidate_tree": git_output(clone, "rev-parse", "HEAD^{tree}"),
        "runtime_root": str(runtime_root.resolve()),
        "producer_root": str(cache.resolve()),
        "interpreter": str(interpreter_path.absolute()),
        "source_inventory_sha256": source_digest,
        "installed_cache_sha256": cache_digest,
        "dependency_lock_sha256": hashlib.sha256((clone / "uv.lock").read_bytes()).hexdigest(),
        "producer_module_sha256": hashlib.sha256(producer.read_bytes()).hexdigest(),
        "interpreter_sha256": hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "interpreter_identity_sha256": hashlib.sha256(str(interpreter).encode()).hexdigest(),
        "python_implementation": "CPython",
        "python_version": (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        "python_cache_tag": sys.implementation.cache_tag or "unknown",
        "probe_receipt_sha256": _digest(receipt),
        "owner_only": True,
    }
    binding = {**binding_material, "binding_sha256": _digest(binding_material)}
    binding_path = run_root / "proof-runtime-binding.json"
    write_json(binding_path, binding)
    binding_path.chmod(0o600)
    payload["proof_runtime"] = {
        "binding_path": str(binding_path),
        "binding": binding,
    }
    payload["preserved_modes"]["proof_runtime"] = "0o700"
    write_json(report_path, payload)
    report_path.chmod(0o600)
    return runtime_root, binding_path


def test_codex_install_report_has_no_claude_surface(tmp_path: Path) -> None:
    report_path, _cache, _global_home = _build_report_fixture(tmp_path)
    report = CodexInstallEvidenceReport.model_validate_json(
        report_path.read_text(encoding="utf-8"),
    )

    assert report.harness_policy == "codex_native_v1"
    assert report.codex.tool_count == EXPECTED_TOOL_COUNT
    payload = report.model_dump(mode="json")
    assert "claude" not in payload
    assert set(report.global_codex_state.before) == {"codex"}
    command_surface = json.dumps(
        [receipt.model_dump(mode="json") for receipt in report.codex.command_receipts],
    )
    assert "claude" not in command_surface.lower()


def test_codex_install_verifier_rejects_changed_cache_bytes(tmp_path: Path) -> None:
    report_path, cache, global_home = _build_report_fixture(tmp_path)
    (cache / "README.md").write_text("tampered\n", encoding="utf-8")

    report, errors = load_verified_codex_install_report(
        report_path,
        codex_global_home=global_home,
    )

    assert report is None
    assert "installed_inventory_mismatch" in errors


def test_codex_install_script_contains_no_claude_arguments() -> None:
    script = (ROOT / "scripts/qa_codex_plugin_install.py").read_text(encoding="utf-8")

    assert "--claude" not in script.lower()
    assert "claude_home" not in script.lower()


def test_codex_install_report_accepts_bound_owner_only_proof_runtime(tmp_path: Path) -> None:
    report_path, cache, _global_home = _build_report_fixture(tmp_path)
    runtime_root, binding_path = _add_proof_runtime(report_path, cache)

    report = CodexInstallEvidenceReport.model_validate_json(
        report_path.read_text(encoding="utf-8"),
    )

    assert report.proof_runtime is not None
    assert report.proof_runtime.binding.runtime_root == runtime_root.resolve()
    assert report.proof_runtime.binding_path == binding_path
    assert report.proof_runtime.binding.owner_only is True
    assert (runtime_root.stat().st_mode & 0o777) == OWNER_DIRECTORY_MODE
    assert (binding_path.stat().st_mode & 0o777) == OWNER_FILE_MODE


def test_codex_install_verifier_rejects_tampered_proof_runtime_binding(
    tmp_path: Path,
) -> None:
    report_path, cache, global_home = _build_report_fixture(tmp_path)
    _runtime_root, binding_path = _add_proof_runtime(report_path, cache)
    binding = json.loads(binding_path.read_text(encoding="utf-8"))
    binding["candidate_tree"] = "f" * 40
    write_json(binding_path, binding)
    binding_path.chmod(0o600)

    report, errors = load_verified_codex_install_report(
        report_path,
        codex_global_home=global_home,
    )

    assert report is None
    assert "codex_proof_runtime_binding_invalid" in errors


def test_codex_proof_runtime_cleanup_is_exact_and_keeps_binding(tmp_path: Path) -> None:
    report_path, cache, _global_home = _build_report_fixture(tmp_path)
    runtime_root, binding_path = _add_proof_runtime(report_path, cache)
    report = CodexInstallEvidenceReport.model_validate_json(
        report_path.read_text(encoding="utf-8"),
    )
    cleanup = getattr(codex_install, "cleanup_codex_proof_runtime", None)
    assert callable(cleanup)
    unrelated = report.run_root / "unrelated-retained"
    unrelated.mkdir(mode=0o700)

    cleanup(report)

    assert not os.path.lexists(runtime_root)
    assert binding_path.is_file()
    assert unrelated.is_dir()


def test_normal_codex_install_report_keeps_existing_cleanup_contract(tmp_path: Path) -> None:
    report_path, _cache, _global_home = _build_report_fixture(tmp_path)

    report = CodexInstallEvidenceReport.model_validate_json(
        report_path.read_text(encoding="utf-8"),
    )

    assert getattr(report, "proof_runtime", None) is None
    assert set(report.preserved_modes) == {
        "run_root",
        "clone",
        "home",
        "codex_home",
        "codex_cache",
    }


def test_proof_runtime_builder_probes_exact_interpreter_without_caller_pythonpath(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    runtime_root = run_root / "proof-runtime"
    producer_root = run_root / "cache"
    package = producer_root / "src/saxo_bank_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    producer = package / "qa_analytics_proof_producer.py"
    producer.write_text("BOUND_RUNTIME = True\n", encoding="utf-8")
    lock = producer_root / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime_root)
    runtime_root.chmod(0o700)
    interpreter = runtime_root / "bin/python"
    site = subprocess.run(
        (str(interpreter), "-I", "-c", "import site; print(site.getsitepackages()[0])"),
        check=True,
        text=True,
        capture_output=True,
    )
    Path(site.stdout.strip(), "proof-runtime.pth").write_text(
        str((producer_root / "src").resolve()) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "must-not-be-used"))
    evidence, receipt = codex_install.build_codex_proof_runtime_evidence(
        run_root=run_root,
        runtime_root=runtime_root,
        producer_root=producer_root,
        candidate_commit="1" * 40,
        candidate_tree="2" * 40,
        source_inventory_sha256="3" * 64,
        installed_cache_sha256="3" * 64,
        dependency_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
        producer_module_sha256=hashlib.sha256(producer.read_bytes()).hexdigest(),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )

    assert receipt.name == "codex_proof_runtime_probe"
    assert receipt.exit_code == 0
    assert receipt.argv[1:3] == ("-I", "-B")
    assert evidence.binding.probe_receipt_sha256 == _digest(receipt.model_dump(mode="json"))
    assert evidence.binding.interpreter == interpreter.absolute()
    assert evidence.binding_path.is_file()
    assert (evidence.binding_path.stat().st_mode & 0o777) == OWNER_FILE_MODE
    assert "PYTHONPATH" not in receipt.argv
    assert not tuple(producer_root.rglob("__pycache__"))


def test_proof_runtime_builder_rejects_matching_module_from_wrong_root(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    runtime_root = run_root / "proof-runtime"
    producer_root = run_root / "cache"
    shadow_root = run_root / "shadow"
    for root in (producer_root, shadow_root):
        package = root / "src/saxo_bank_mcp"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "qa_analytics_proof_producer.py").write_text(
            "BOUND_RUNTIME = True\n",
            encoding="utf-8",
        )
    lock = producer_root / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(runtime_root)
    runtime_root.chmod(0o700)
    interpreter = runtime_root / "bin/python"
    site = subprocess.run(
        (str(interpreter), "-I", "-c", "import site; print(site.getsitepackages()[0])"),
        check=True,
        text=True,
        capture_output=True,
    )
    Path(site.stdout.strip(), "proof-runtime.pth").write_text(
        str((shadow_root / "src").resolve()) + "\n",
        encoding="utf-8",
    )
    producer = producer_root / "src/saxo_bank_mcp/qa_analytics_proof_producer.py"

    with pytest.raises(ValueError, match="codex_proof_runtime_probe_binding_invalid"):
        codex_install.build_codex_proof_runtime_evidence(
            run_root=run_root,
            runtime_root=runtime_root,
            producer_root=producer_root,
            candidate_commit="1" * 40,
            candidate_tree="2" * 40,
            source_inventory_sha256="3" * 64,
            installed_cache_sha256="3" * 64,
            dependency_lock_sha256=hashlib.sha256(lock.read_bytes()).hexdigest(),
            producer_module_sha256=hashlib.sha256(producer.read_bytes()).hexdigest(),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )


def test_codex_install_cli_selects_one_shot_proof_runtime_retention() -> None:
    process = subprocess.run(
        (sys.executable, str(ROOT / "scripts/qa_codex_plugin_install.py"), "--help"),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert process.returncode == 0
    assert "--retain-proof-runtime" in process.stdout
