#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
# --- How to run ---
# uv run python scripts/generate_agent_skill_catalogs.py --check
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from saxo_bank_mcp.agent_catalog_fixtures import self_test_fixture
from saxo_bank_mcp.agent_catalog_render import generate_catalogs
from saxo_bank_mcp.agent_catalog_runtime import CatalogValidationError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate Saxo agent skill catalogs.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--self-test-fixture", choices=(
        "unknown-tool",
        "stale-operation",
        "missing-status",
        "unclassified-status-module",
        "uncovered-tool",
    ))
    parser.add_argument("ignored", nargs="*")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        if args.self_test_fixture is not None:
            self_test_fixture(root, str(args.self_test_fixture))
            return 1
        summary = generate_catalogs(root, check=bool(args.check))
    except json.JSONDecodeError:
        sys.stderr.write("malformed_json: source\n")
        return 1
    except CatalogValidationError as error:
        sys.stderr.write(error.message() + "\n")
        return 1
    sys.stdout.write(summary.line() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
