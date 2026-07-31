#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from saxo_bank_mcp._source_matrix_run_directory import (
    cleanup_held_run_directory,
    create_held_run_directories,
    open_held_run_directory,
)


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    raw_site_packages = sysconfig.get_path("purelib")
    if not raw_site_packages:
        raise ValueError("development site-packages is unavailable")
    site_packages = Path(raw_site_packages).resolve(strict=True)
    run_root = Path(tempfile.mkdtemp(prefix="saxo-source-matrix-dev-"))
    names = ("cache", "work", "tmp")
    paths = tuple(run_root / name for name in names)
    held = open_held_run_directory(run_root)
    try:
        create_held_run_directories(held, names)
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
                "'saxo_bank_mcp.qa_analytics_source_matrix',"
                "run_name='__main__',alter_sys=True)"
            ),
            os.fspath(repository_root / "src"),
            os.fspath(site_packages),
            *sys.argv[1:],
        )
        environment = {
            **os.environ,
            "SAXO_BANK_MCP_OFFICIAL_ISOLATED_LAUNCHER": "dev",
            "TMPDIR": os.fspath(tmp),
        }
        completed = subprocess.run(
            arguments,
            cwd=work,
            env=environment,
            check=False,
        )
        return completed.returncode
    finally:
        cleanup_held_run_directory(held, remove_names=names, remove_root=True)


if __name__ == "__main__":
    raise SystemExit(main())
