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

_OWNER_ONLY_MASK = 0o077
_OWNER_DIRECTORY_MODE = 0o700
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
                or source.name == "__pycache__"
                or source.suffix.casefold() in _PYTHON_CACHE_SUFFIXES
            ):
                continue
            target = runtime_site / item
            resolved_parent = target.parent.resolve()
            if resolved_parent != runtime and not resolved_parent.is_relative_to(runtime):
                raise ValueError(f"locked dependency {name} path escapes the runtime")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _make_owner_only(runtime: Path) -> None:
    for path in sorted(runtime.rglob("*")):
        if path.is_symlink():
            continue
        path.chmod(stat.S_IMODE(path.stat().st_mode) & ~_OWNER_ONLY_MASK)
    runtime.chmod(0o700)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the complete owner-only offline analytics source-matrix runtime.",
    )
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--uv", default="uv")
    arguments = parser.parse_args(argv)
    repository_root = Path(__file__).resolve().parents[1]
    runtime = arguments.runtime.resolve()
    wheel = arguments.wheel.resolve(strict=True)
    if runtime.exists() or runtime == repository_root or repository_root.is_relative_to(runtime):
        raise ValueError("isolated runtime target must be a new dedicated directory")
    manifest = json.loads(
        (repository_root / "data" / "analytics" / "source_matrix_candidate.json").read_text(
            encoding="utf-8",
        ),
    )
    previous_umask = os.umask(0o077)
    try:
        _run(
            (arguments.uv, "lock", "--check", "--offline"),
            cwd=repository_root,
        )
        _run(
            (
                arguments.uv,
                "venv",
                "--offline",
                "--no-project",
                "--python",
                f"{sys.version_info.major}.{sys.version_info.minor}",
                str(runtime),
            ),
            cwd=repository_root,
        )
        _copy_locked_dependencies(runtime, manifest)
        _run(
            (
                arguments.uv,
                "pip",
                "install",
                "--offline",
                "--python",
                str(runtime / "bin" / "python"),
                "--no-deps",
                str(wheel),
            ),
            cwd=repository_root,
        )
        _make_owner_only(runtime)
    finally:
        os.umask(previous_umask)
    launcher = runtime / "bin" / "saxo-bank-analytics-source-matrix"
    generator = runtime / "bin" / "saxo-bank-analytics-source-matrix-generate"
    if (
        not launcher.is_file()
        or not generator.is_file()
        or stat.S_IMODE(launcher.stat().st_mode) != _OWNER_DIRECTORY_MODE
        or stat.S_IMODE(generator.stat().st_mode) != _OWNER_DIRECTORY_MODE
        or any(
            path.name == "__pycache__" or path.suffix.casefold() in _PYTHON_CACHE_SUFFIXES
            for path in runtime.rglob("*")
        )
    ):
        raise ValueError("isolated runtime preparation is incomplete")
    sys.stdout.write(str(launcher) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
