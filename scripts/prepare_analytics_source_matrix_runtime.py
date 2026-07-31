#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from typing import Any, cast

_SEALED_DIRECTORY_MODE = 0o500
_SEALED_FILE_MODE = 0o400
_SEALED_EXECUTABLE_MODE = 0o500
_PYTHON_CACHE_SUFFIXES = {".pyc", ".pyo"}


def _run(command: tuple[str, ...], *, cwd: Path) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "isolated runtime command failed")


def _is_cache_entry(path: Path) -> bool:
    return path.name == "__pycache__" or path.suffix.casefold() in _PYTHON_CACHE_SUFFIXES


def _validate_base_source(base: Path) -> None:
    for path in (base, *base.rglob("*")):
        metadata = path.lstat()
        if _is_cache_entry(path):
            continue
        if stat.S_ISLNK(metadata.st_mode):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(base):
                raise ValueError("Python base link escapes the copied runtime")
            continue
        if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Python base contains an unsupported entry")


def _copy_python_base(base: Path, target: Path) -> None:
    _validate_base_source(base)

    def ignore(directory: str, names: list[str]) -> set[str]:
        parent = Path(directory)
        return {name for name in names if _is_cache_entry(parent / name)}

    shutil.copytree(base, target, symlinks=False, ignore=ignore)
    for path in target.rglob("*"):
        if path.is_symlink() or _is_cache_entry(path):
            raise ValueError("copied Python base is not a regular cache-free tree")


def _normalize_copied_sysconfig(base: Path, target: Path) -> None:
    configuration_files = tuple(
        (target / "lib" / "python3.12").glob("_sysconfigdata_*.py"),
    )
    if not configuration_files:
        raise ValueError("copied Python build configuration is unavailable")
    normalization = (
        "\nimport os as _saxo_os\n"
        "_saxo_base = _saxo_os.path.dirname(_saxo_os.path.dirname("
        "_saxo_os.path.dirname(__file__)))\n"
        f"_saxo_original = {os.fspath(base)!r}\n"
        "for _saxo_key, _saxo_value in tuple(build_time_vars.items()):\n"
        "    if isinstance(_saxo_value, str):\n"
        "        build_time_vars[_saxo_key] = _saxo_value.replace("
        "_saxo_original, _saxo_base)\n"
        "del _saxo_base, _saxo_key, _saxo_original, _saxo_os, _saxo_value\n"
    ).encode()
    for path in configuration_files:
        if not path.is_file() or path.is_symlink():
            raise ValueError("copied Python build configuration is invalid")
        path.write_bytes(path.read_bytes() + normalization)


def _copy_locked_dependencies(runtime: Path, manifest: dict[str, Any]) -> None:
    runtime_site = runtime / "lib" / "python3.12" / "site-packages"
    dependencies = cast(
        "dict[str, dict[str, Any]]",
        manifest["dependency_distributions"],
    )
    for name, expected in dependencies.items():
        seed = distribution(name)
        if seed.version != expected["version"]:
            raise ValueError(f"locked dependency {name} version is unavailable")
        expected_files = cast("dict[str, str]", expected["files"])
        for relative, expected_sha256 in expected_files.items():
            source = Path(str(seed.locate_file(relative)))
            if (
                not source.is_file()
                or source.is_symlink()
                or hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256
            ):
                raise ValueError(f"locked dependency {name} seed is unsealed")
        for item in seed.files or ():
            source = Path(str(seed.locate_file(item)))
            if (
                not source.is_file()
                or source.is_symlink()
                or _is_cache_entry(source)
            ):
                continue
            target = runtime_site / item
            target_parent = target.parent.resolve(strict=False)
            if not target_parent.is_relative_to(runtime):
                raise ValueError(f"locked dependency {name} path escapes the runtime")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _validate_internal_python_runtime(runtime: Path) -> None:
    interpreter = runtime / "bin" / "python3.12"
    probe = subprocess.run(
        (
            os.fspath(interpreter),
            "-I",
            "-B",
            "-S",
            "-c",
            (
                "import json,os,sys,sysconfig;"
                "paths=[sys.executable,getattr(sys,'_base_executable',sys.executable),"
                "sys.prefix,sys.base_prefix,sysconfig.get_path('stdlib'),"
                "sysconfig.get_config_var('DESTSHARED'),sysconfig.__file__,"
                "sysconfig.get_config_h_filename(),sysconfig.get_makefile_filename()];"
                "enabled=sysconfig.get_config_var('Py_ENABLE_SHARED') in (1,'1');"
                "libdir=sysconfig.get_config_var('LIBDIR');"
                "library=sysconfig.get_config_var('LDLIBRARY');"
                "paths.append(os.path.join(libdir,library)) if enabled else None;"
                "print(json.dumps(paths))"
            ),
        ),
        cwd=runtime.parent,
        env={},
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise ValueError("copied Python runtime probe failed")
    raw_paths = cast("list[object]", json.loads(probe.stdout))
    if not raw_paths or any(
        not isinstance(value, str)
        or not value
        or not Path(value).resolve(strict=True).is_relative_to(runtime)
        for value in raw_paths
    ):
        raise ValueError("copied Python runtime references an external artifact")


def _remove_python_caches(runtime: Path) -> None:
    for path in sorted(
        runtime.rglob("*"),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        if path.name == "__pycache__" and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.suffix.casefold() in _PYTHON_CACHE_SUFFIXES and path.is_file():
            path.unlink()


def _normalize_interpreter_aliases(runtime: Path) -> None:
    binary = runtime / "bin" / "python3.12"
    if binary.is_symlink():
        resolved = binary.resolve(strict=True)
        temporary = binary.with_name(".python3.12.copy")
        shutil.copy2(resolved, temporary)
        binary.unlink()
        temporary.rename(binary)
    if not binary.is_file() or binary.is_symlink():
        raise ValueError("isolated runtime interpreter is not a regular file")
    for name in ("python", "python3"):
        alias = runtime / "bin" / name
        if alias.exists() or alias.is_symlink():
            if alias.is_dir() and not alias.is_symlink():
                raise ValueError("isolated runtime interpreter alias has an invalid type")
            alias.unlink()
        alias.symlink_to("python3.12")
    for activation_script in (runtime / "bin").glob("activate*"):
        if not activation_script.is_file() or activation_script.is_symlink():
            raise ValueError("isolated runtime activation script has an invalid type")
        activation_script.unlink()
    for path in runtime.rglob("*"):
        if path.is_symlink() and path.relative_to(runtime).as_posix() not in {
            "bin/python",
            "bin/python3",
        }:
            raise ValueError("isolated runtime contains an unsupported link")


def _seal_runtime(runtime: Path) -> None:
    executable_files = {
        path
        for path in runtime.rglob("*")
        if path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o111
    }
    for path in runtime.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_file():
            path.chmod(
                _SEALED_EXECUTABLE_MODE if path in executable_files else _SEALED_FILE_MODE,
            )
    for path in sorted(
        (item for item in runtime.rglob("*") if item.is_dir() and not item.is_symlink()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        path.chmod(_SEALED_DIRECTORY_MODE)
    runtime.chmod(_SEALED_DIRECTORY_MODE)


def _validate_portable_runtime(repository_root: Path, runtime: Path) -> None:
    sys.dont_write_bytecode = True
    source_root = repository_root / "src"
    sys.path.insert(0, os.fspath(source_root))
    try:
        from saxo_bank_mcp.analytics_source_runtime import (  # noqa: PLC0415
            _open_descriptor_chain,
            _portable_projection_from_descriptor,
        )

        ancestors, descriptor = _open_descriptor_chain(runtime)
        try:
            projection, _snapshots = _portable_projection_from_descriptor(
                descriptor,
                runtime,
            )
            if projection.entry_count < 1:
                raise ValueError("isolated runtime projection is empty")
        finally:
            os.close(descriptor)
            for ancestor in reversed(ancestors):
                os.close(ancestor)
    finally:
        sys.path.remove(os.fspath(source_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the complete sealed offline analytics source-matrix runtime.",
    )
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--uv", required=True, type=Path)
    arguments = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    if not arguments.runtime.is_absolute():
        raise ValueError("isolated runtime target must be absolute")
    runtime = arguments.runtime.resolve(strict=False)
    wheel = arguments.wheel.resolve(strict=True)
    uv = arguments.uv.resolve(strict=True)
    if (
        runtime.exists()
        or runtime == repository_root
        or repository_root.is_relative_to(runtime)
        or not wheel.is_file()
        or wheel.is_symlink()
        or not uv.is_file()
        or uv.is_symlink()
        or stat.S_IMODE(uv.stat().st_mode) & 0o111 == 0
    ):
        raise ValueError("isolated runtime preparation inputs are invalid")
    manifest = json.loads(
        (repository_root / "data" / "analytics" / "source_matrix_candidate.json").read_text(
            encoding="utf-8",
        ),
    )
    base = Path(sys.base_prefix).resolve(strict=True)
    previous_umask = os.umask(0o077)
    try:
        _run((os.fspath(uv), "lock", "--check", "--offline"), cwd=repository_root)
        runtime.mkdir(mode=0o700)
        _copy_python_base(base, runtime / "_python")
        _normalize_copied_sysconfig(base, runtime / "_python")
        copied_python = runtime / "_python" / "bin" / "python3.12"
        _run(
            (
                os.fspath(copied_python),
                "-I",
                "-B",
                "-S",
                "-m",
                "venv",
                "--copies",
                "--without-pip",
                os.fspath(runtime),
            ),
            cwd=repository_root,
        )
        _validate_internal_python_runtime(runtime)
        _copy_locked_dependencies(runtime, manifest)
        _run(
            (
                os.fspath(uv),
                "pip",
                "install",
                "--offline",
                "--python",
                os.fspath(runtime / "bin" / "python3.12"),
                "--no-deps",
                os.fspath(wheel),
            ),
            cwd=repository_root,
        )
        _remove_python_caches(runtime)
        _normalize_interpreter_aliases(runtime)
        _seal_runtime(runtime)
        _validate_portable_runtime(repository_root, runtime)
    finally:
        os.umask(previous_umask)
    launcher = runtime / "bin" / "saxo-bank-analytics-source-matrix"
    generator = runtime / "bin" / "saxo-bank-analytics-source-matrix-generate"
    if (
        not launcher.is_file()
        or not generator.is_file()
        or stat.S_IMODE(launcher.stat().st_mode) != _SEALED_EXECUTABLE_MODE
        or stat.S_IMODE(generator.stat().st_mode) != _SEALED_EXECUTABLE_MODE
        or any(_is_cache_entry(path) for path in runtime.rglob("*"))
    ):
        raise ValueError("isolated runtime preparation is incomplete")
    sys.stdout.write(os.fspath(launcher) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
