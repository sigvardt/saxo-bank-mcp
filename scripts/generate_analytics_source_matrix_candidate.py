#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path


def _cleanup_empty(paths: tuple[Path, ...]) -> None:
    try:
        for path in reversed(paths):
            path.rmdir()
    except OSError:
        raise RuntimeError("development run directory is not empty") from None


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    raw_site_packages = sysconfig.get_path("purelib")
    if not raw_site_packages:
        raise ValueError("development site-packages is unavailable")
    site_packages = Path(raw_site_packages).resolve(strict=True)
    run_root = Path(tempfile.mkdtemp(prefix="saxo-source-matrix-generate-dev-"))
    paths = tuple(run_root / name for name in ("cache", "work", "tmp"))
    try:
        for path in paths:
            path.mkdir(mode=0o700)
        cache, work, tmp = paths
        arguments = (
            sys.executable,
            "-I",
            "-B",
            "-S",
            "-X",
            f"pycache_prefix={cache}",
            "-c",
            (
                "import runpy,sys;"
                "sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)];"
                "runpy.run_module("
                "'saxo_bank_mcp.generate_analytics_source_matrix_candidate',"
                "run_name='__main__',alter_sys=True)"
            ),
            os.fspath(repository_root / "src"),
            os.fspath(site_packages),
            "--repository-root",
            os.fspath(repository_root),
            *sys.argv[1:],
        )
        environment = {
            **os.environ,
            "SAXO_BANK_MCP_OFFICIAL_ISOLATED_LAUNCHER": "dev",
            "TMPDIR": os.fspath(tmp),
            "TMP": os.fspath(tmp),
            "TEMP": os.fspath(tmp),
        }
        completed = subprocess.run(
            arguments,
            cwd=work,
            env=environment,
            check=False,
        )
        return completed.returncode
    finally:
        _cleanup_empty((run_root, *paths))


if __name__ == "__main__":
    raise SystemExit(main())
