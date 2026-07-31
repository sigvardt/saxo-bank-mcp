#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path


def main() -> int:
    repository_root = Path(__file__).resolve().parents[1]
    site_packages = sysconfig.get_path("purelib")
    cache_prefix = Path(sys.prefix) / ".saxo-bank-mcp-pycache"
    arguments = [
        sys.executable,
        "-I",
        "-B",
        "-S",
        "-X",
        f"pycache_prefix={cache_prefix}",
        "-c",
        (
            "import os,runpy,sys;"
            "os.makedirs(sys.pycache_prefix,mode=0o700,exist_ok=True);"
            "sys.path[:0]=[sys.argv.pop(1),sys.argv.pop(1)];"
            "runpy.run_module("
            "'saxo_bank_mcp.qa_analytics_source_matrix',"
            "run_name='__main__',alter_sys=True)"
        ),
        str(repository_root / "src"),
        site_packages,
        *sys.argv[1:],
    ]
    environment = {
        **os.environ,
        "SAXO_BANK_MCP_OFFICIAL_ISOLATED_LAUNCHER": "dev",
    }
    os.execve(sys.executable, arguments, environment)  # noqa: S606


if __name__ == "__main__":
    raise SystemExit(main())
