from __future__ import annotations

import argparse
import sys
from pathlib import Path

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import scan_secret_paths
from saxo_bank_mcp.endpoint_registry import load_inventory, validate_inventory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the checked-in Saxo endpoint inventory.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = str(args.command)
    match command:
        case "validate":
            return handle_validate(args.out)
        case unreachable:
            raise SystemExit(f"unsupported inventory command: {unreachable}")


def handle_validate(out: Path | None) -> int:
    report: dict[str, JsonValue] = validate_inventory(load_inventory())
    findings, scan_errors = scan_secret_paths(["data/saxo/openapi_inventory.json"])
    report["secret_scan"] = {"findings": findings, "scan_errors": scan_errors}
    if findings or scan_errors:
        report["status"] = "failed"
    if out is not None:
        write_json(out, report)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
