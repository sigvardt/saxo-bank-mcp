from __future__ import annotations

import hashlib
import importlib.machinery
import json
import os
import shutil
import stat
import subprocess
import sys
import zipimport
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

import saxo_bank_mcp.qa_analytics_source_matrix as matrix_module
from saxo_bank_mcp._evidence import JsonValue

_ROOT = Path(__file__).parents[1]
_OWNER_ONLY_MASK = 0o077


def _projection_json() -> dict[str, JsonValue]:
    project = getattr(matrix_module, "_interpreter_execution_projection", None)
    assert callable(project)
    return cast("dict[str, JsonValue]", project())


def _run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_interpreter_projection_seals_all_import_state_and_loaded_origins(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _projection_json()
    expected_suffixes = {
        *importlib.machinery.SOURCE_SUFFIXES,
        *importlib.machinery.BYTECODE_SUFFIXES,
        *importlib.machinery.EXTENSION_SUFFIXES,
    }
    assert set(cast("list[str]", initial["importable_suffixes"])) == expected_suffixes
    assert set(cast("dict[str, JsonValue]", initial["flags"])) >= {
        "debug",
        "dont_write_bytecode",
        "ignore_environment",
        "isolated",
        "no_site",
        "no_user_site",
        "optimize",
        "safe_path",
        "utf8_mode",
    }

    class MutableFinder:
        def __init__(self) -> None:
            self.policy = "first"

        def find_spec(
            self,
            _fullname: str,
            _path: object = None,
            _target: object = None,
        ) -> None:
            return None

    finder = MutableFinder()
    monkeypatch.setattr(sys, "meta_path", [*sys.meta_path, finder])
    with_finder = _projection_json()
    assert with_finder != initial
    finder.policy = "second"
    assert _projection_json() != with_finder

    hook = MutableFinder()
    monkeypatch.setattr(sys, "path_hooks", [*sys.path_hooks, hook])
    with_hook = _projection_json()
    hook.policy = "changed"
    assert _projection_json() != with_hook

    importer_root = tmp_path / "importer-root"
    importer_root.mkdir()
    monkeypatch.setitem(sys.path_importer_cache, str(importer_root), finder)
    with_cache = _projection_json()
    finder.policy = "third"
    assert _projection_json() != with_cache

    monkeypatch.setitem(
        cast("dict[str, object]", vars(sys)["_xoptions"]),
        "round5-behavior",
        "enabled",
    )
    assert _projection_json() != with_cache

    native_origin = tmp_path / f"undeclared{importlib.machinery.EXTENSION_SUFFIXES[0]}"
    native_origin.write_bytes(b"sealed-native-origin")
    undeclared = ModuleType("round5_undeclared")
    undeclared.__file__ = str(native_origin)
    monkeypatch.setitem(sys.modules, undeclared.__name__, undeclared)
    projection = _projection_json()
    origins = cast("list[dict[str, JsonValue]]", projection["loaded_module_origins"])
    origin_path_sha256 = hashlib.sha256(os.fsencode(native_origin)).hexdigest()
    assert any(item["path_sha256"] == origin_path_sha256 for item in origins)

    validate_machinery = getattr(matrix_module, "_validate_official_import_machinery", None)
    assert callable(validate_machinery)
    beartype_module = sys.modules.get("beartype.claw._importlib._clawimpload")
    assert isinstance(beartype_module, ModuleType)
    beartype_loader = getattr(beartype_module, "BeartypeSourceFileLoader", None)
    assert isinstance(beartype_loader, type)
    beartype_details = (
        (
            importlib.machinery.ExtensionFileLoader,
            importlib.machinery.EXTENSION_SUFFIXES,
        ),
        (beartype_loader, importlib.machinery.SOURCE_SUFFIXES),
        (
            importlib.machinery.SourcelessFileLoader,
            importlib.machinery.BYTECODE_SUFFIXES,
        ),
    )
    standard_details = (
        (
            importlib.machinery.ExtensionFileLoader,
            importlib.machinery.EXTENSION_SUFFIXES,
        ),
        (
            importlib.machinery.SourceFileLoader,
            importlib.machinery.SOURCE_SUFFIXES,
        ),
        (
            importlib.machinery.SourcelessFileLoader,
            importlib.machinery.BYTECODE_SUFFIXES,
        ),
    )
    canonical_root = tmp_path / "canonical-import-root"
    canonical_root.mkdir()
    (canonical_root / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    canonical_finder = importlib.machinery.FileFinder(
        str(canonical_root),
        *beartype_details,
    )
    assert canonical_finder.find_spec("module") is not None
    canonical_meta_path = [
        importlib.machinery.BuiltinImporter,
        importlib.machinery.FrozenImporter,
        importlib.machinery.PathFinder,
    ]
    beartype_hook = importlib.machinery.FileFinder.path_hook(*beartype_details)
    beartype_hook.__module__ = "importlib._bootstrap_external"
    standard_hook = importlib.machinery.FileFinder.path_hook(*standard_details)
    standard_hook.__module__ = "_frozen_importlib_external"
    canonical_path_hooks = [
        zipimport.zipimporter,
        beartype_hook,
        standard_hook,
    ]
    canonical_cache = {str(canonical_root): canonical_finder}
    monkeypatch.setattr(sys, "meta_path", canonical_meta_path)
    monkeypatch.setattr(sys, "path_hooks", canonical_path_hooks)
    monkeypatch.setattr(sys, "path", [str(canonical_root)])
    monkeypatch.setattr(sys, "path_importer_cache", canonical_cache)
    validate_machinery()

    monkeypatch.setattr(sys, "meta_path", [*canonical_meta_path, MutableFinder()])
    with pytest.raises(ValueError, match="meta path"):
        validate_machinery()
    monkeypatch.setattr(sys, "meta_path", canonical_meta_path)
    monkeypatch.setattr(sys, "path_hooks", [*canonical_path_hooks, MutableFinder()])
    with pytest.raises(ValueError, match="path hooks"):
        validate_machinery()
    monkeypatch.setattr(sys, "path_hooks", canonical_path_hooks)
    monkeypatch.setattr(sys, "path_importer_cache", {str(canonical_root): MutableFinder()})
    with pytest.raises(ValueError, match="cache finder"):
        validate_machinery()
    monkeypatch.setattr(sys, "path_importer_cache", canonical_cache)
    path_cache = cast("set[str]", vars(canonical_finder)["_path_cache"])
    path_cache.add("forged.py")
    with pytest.raises(ValueError, match="cache state"):
        validate_machinery()


def test_installed_launcher_builds_a_fresh_locked_isolated_closure(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    matrix_launcher = _ROOT / "scripts" / "saxo-bank-analytics-source-matrix"
    generator_launcher = _ROOT / "scripts" / "saxo-bank-analytics-source-matrix-generate"
    assert matrix_launcher.is_file()
    assert generator_launcher.is_file()

    uv = shutil.which("uv")
    assert uv is not None
    source_copy = tmp_path / "source-copy"
    shutil.copytree(
        _ROOT,
        source_copy,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".ruff_cache",
            ".pytest_cache",
            ".basedpyright",
            "__pycache__",
            "*.pyc",
            "*.pyo",
        ),
    )
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    bootstrap = _run(
        (uv, "build", "--offline", "--wheel", "--out-dir", str(wheel_dir)),
        cwd=source_copy,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr
    bootstrap_wheel = next(wheel_dir.glob("*.whl"))

    bootstrap_runtime = tmp_path / "bootstrap-runtime"
    requirements = tmp_path / "runtime-requirements.txt"
    exported = _run(
        (
            uv,
            "export",
            "--offline",
            "--locked",
            "--no-dev",
            "--no-emit-project",
            "--output-file",
            str(requirements),
        ),
        cwd=source_copy,
    )
    assert exported.returncode == 0, exported.stderr
    assert requirements.read_text(encoding="utf-8").strip()
    prepared = _run(
        (
            sys.executable,
            str(source_copy / "scripts" / "prepare_analytics_source_matrix_runtime.py"),
            "--runtime",
            str(bootstrap_runtime),
            "--wheel",
            str(bootstrap_wheel),
            "--uv",
            uv,
        ),
        cwd=source_copy,
    )
    assert prepared.returncode == 0, prepared.stderr
    assert prepared.stdout.strip() == str(
        bootstrap_runtime / "bin" / "saxo-bank-analytics-source-matrix",
    )

    first_manifest = source_copy / "data" / "analytics" / "source_matrix_candidate.json"
    generated = _run(
        (
            str(bootstrap_runtime / "bin" / "saxo-bank-analytics-source-matrix-generate"),
            "--repository-root",
            str(source_copy),
            "--wheel",
            str(bootstrap_wheel),
            "--out",
            str(first_manifest),
        ),
        cwd=tmp_path,
    )
    assert generated.returncode == 0, generated.stderr
    first_identity = generated.stdout.strip()

    final_wheel_dir = tmp_path / "final-wheel"
    final_wheel_dir.mkdir()
    rebuilt = _run(
        (uv, "build", "--offline", "--wheel", "--out-dir", str(final_wheel_dir)),
        cwd=source_copy,
    )
    assert rebuilt.returncode == 0, rebuilt.stderr
    final_wheel = next(final_wheel_dir.glob("*.whl"))
    runtime = tmp_path / "runtime"
    final_prepared = _run(
        (
            sys.executable,
            str(source_copy / "scripts" / "prepare_analytics_source_matrix_runtime.py"),
            "--runtime",
            str(runtime),
            "--wheel",
            str(final_wheel),
            "--uv",
            uv,
        ),
        cwd=source_copy,
    )
    assert final_prepared.returncode == 0, final_prepared.stderr

    second_manifest = tmp_path / "second-candidate.json"
    generated_again = _run(
        (
            str(runtime / "bin" / "saxo-bank-analytics-source-matrix-generate"),
            "--repository-root",
            str(source_copy),
            "--wheel",
            str(final_wheel),
            "--out",
            str(second_manifest),
        ),
        cwd=tmp_path,
    )
    assert generated_again.returncode == 0, generated_again.stderr
    assert generated_again.stdout.strip() == first_identity
    assert second_manifest.read_bytes() == first_manifest.read_bytes()

    hostile = tmp_path / "hostile"
    hostile.mkdir()
    (hostile / "sitecustomize.py").write_text(
        "raise RuntimeError('ambient startup executed')\n",
        encoding="utf-8",
    )
    foreign_cwd = tmp_path / "foreign-cwd"
    foreign_cwd.mkdir()
    invocation_env = {
        **os.environ,
        "PYTHONPATH": str(hostile),
        "PYTHONUSERBASE": str(hostile),
    }
    launcher = runtime / "bin" / "saxo-bank-analytics-source-matrix"
    help_result = _run((str(launcher), "--help"), cwd=foreign_cwd, env=invocation_env)
    assert help_result.returncode == 0, help_result.stderr
    identities = [
        _run((str(launcher), "--identity"), cwd=foreign_cwd, env=invocation_env) for _ in range(2)
    ]
    assert all(result.returncode == 0 for result in identities)
    assert {result.stdout.strip() for result in identities} == {first_identity}
    preflights = [
        _run((str(launcher), "--preflight"), cwd=foreign_cwd, env=invocation_env) for _ in range(2)
    ]
    assert all(result.returncode == 0 for result in preflights)
    assert preflights[0].stdout == preflights[1].stdout
    preflight = json.loads(preflights[0].stdout)
    assert preflight == {
        "candidate_identity_sha256": first_identity,
        "closure": "sealed",
        "dont_write_bytecode": True,
        "ignore_environment": True,
        "isolated": True,
        "no_site": True,
    }
    assert not any(
        path.name == "__pycache__" or path.suffix.casefold() in {".pyc", ".pyo"}
        for path in runtime.rglob("*")
    )
    assert all(
        path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & _OWNER_ONLY_MASK == 0
        for path in runtime.rglob("*")
    )
