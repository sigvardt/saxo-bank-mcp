"""Single explicit QA CLI command dispatcher. # noqa: SIZE_OK."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path

from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.hard_task_manifest import handle_hard_task_manifest
from saxo_bank_mcp.hard_task_summary import handle_hard_task_summary
from saxo_bank_mcp.loop_manifest import GitState, ManifestSpec, build_manifest
from saxo_bank_mcp.qa_manual_live import handle_manual_live_boundary
from saxo_bank_mcp.qa_nontrade_probes import (
    handle_nontrade_denial_sweep,
    handle_nontrade_denied,
    handle_nontrade_write,
)
from saxo_bank_mcp.qa_order_probes import (
    handle_production_order_mutation,
    handle_sim_order_mutation,
    handle_trade_write_denied,
)
from saxo_bank_mcp.qa_probes import (
    handle_auth_status,
    handle_gitignore_secret,
    handle_health,
    handle_live_read,
    handle_live_read_refusal,
    handle_live_write_refusal,
    handle_secret_scan,
    handle_sim_auth,
    handle_token_cache,
    handle_tool_inventory,
    write_incomplete,
)
from saxo_bank_mcp.qa_prod_readiness import handle_prod_readiness
from saxo_bank_mcp.qa_read_probes import (
    handle_read_smoke,
    handle_registered_endpoint_denied,
    load_registered_endpoint_list,
)
from saxo_bank_mcp.qa_readme_probe import handle_readme_smoke
from saxo_bank_mcp.qa_safety_probes import handle_approval_denied, handle_approval_happy
from saxo_bank_mcp.qa_streaming_probes import handle_stream, handle_stream_cleanup
from saxo_bank_mcp.qa_trade_probes import (
    handle_trade_disclaimer_blocked,
    handle_trade_disclaimer_lookup,
    handle_trade_disclaimer_response,
    handle_trade_multileg_defaults,
    handle_trade_precheck,
)
from saxo_bank_mcp.qa_trading_write_probes import handle_trading_write_matrix
from saxo_bank_mcp.tribunal_index import list_registered_mcp_tool_ids


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run implementation-plan QA probes.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in (
        "health",
        "auth-status",
        "token-cache",
        "sim-auth",
        "approval-happy",
        "read-smoke",
        "nontrade-denial-sweep",
        "nontrade-write",
        "trade-precheck",
        "trade-multileg-defaults",
        "trade-disclaimer-lookup",
        "trade-disclaimer-response",
        "sim-order-mutation",
        "production-order-mutation",
        "trading-write-matrix",
        "stream",
        "readme-smoke",
        "hard-task-manifest",
        "manual-live-boundary",
        "prod-readiness",
        "tool-inventory",
    ):
        add_common(subparsers.add_parser(name))

    hard_task_summary = subparsers.add_parser("hard-task-summary")
    hard_task_summary.add_argument("--out", type=Path, required=True)
    hard_task_summary.add_argument(
        "--receipts-dir",
        type=Path,
        default=Path(".omo/evidence/saxo-bank-mcp/strict-g003-hard-tasks"),
    )
    hard_task_summary.add_argument("--expected-tool", action="append", default=[])
    hard_task_summary.add_argument("--expected-sha", default=None)

    gitignore = subparsers.add_parser("gitignore-secret")
    add_common(gitignore)

    live_read_refusal = subparsers.add_parser("live-read-refusal")
    add_common(live_read_refusal)

    approval_denied = subparsers.add_parser("approval-denied")
    add_common(approval_denied)
    approval_denied.add_argument("--missing", required=True)

    registered_denied = subparsers.add_parser("registered-endpoint-denied")
    add_common(registered_denied)
    registered_denied.add_argument("--method", required=True)
    registered_denied.add_argument("--path", required=True)

    list_registry_stdout = subparsers.add_parser("list-registry-stdout")
    list_registry_stdout.add_argument("--service-group", default=None)
    list_registry_stdout.add_argument("--limit", type=int, default=25)
    list_registry_stdout.add_argument("--offset", type=int, default=0)

    nontrade_denied = subparsers.add_parser("nontrade-denied")
    add_common(nontrade_denied)
    nontrade_denied.add_argument("--service", required=True)

    trade_disclaimer = subparsers.add_parser("trade-disclaimer-blocked")
    add_common(trade_disclaimer)

    trade_write_denied = subparsers.add_parser("trade-write-denied")
    add_common(trade_write_denied)
    trade_write_denied.add_argument("--missing", required=True)

    stream_cleanup = subparsers.add_parser("stream-cleanup")
    add_common(stream_cleanup)
    stream_cleanup.add_argument("--simulate-leak", action="store_true")

    live_read = subparsers.add_parser("live-read")
    add_common(live_read)
    live_read.add_argument("--skip-out", type=Path, required=True)

    live_write_refusal = subparsers.add_parser("live-write-refusal")
    add_common(live_write_refusal)

    secret_scan = subparsers.add_parser("secret-scan")
    add_common(secret_scan)
    secret_scan.add_argument("--paths", nargs="+", required=True)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--out", type=Path, required=True)
    manifest.add_argument("--run-id", required=True)
    manifest.add_argument("--scenario-id", required=True)
    manifest.add_argument("--expected-status", required=True)
    manifest.add_argument("--command", dest="replay_command", required=True)
    manifest.add_argument("--evidence-path", action="append", default=[])
    return parser


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--groups", default=None)
    parser.add_argument("--safe-only", action="store_true")
    parser.add_argument("--classes", default=None)
    parser.add_argument("--require-frame", action="store_true")
    parser.add_argument("--expect-connections", type=int, default=None)
    parser.add_argument("--expect-price-instruments", type=int, default=None)


def main(argv: list[str] | None = None) -> int:  # noqa: C901, PLR0912, PLR0915
    args = build_parser().parse_args(argv)
    command = str(args.command)
    if command == "health":
        result = handle_health(args.out)
    elif command == "auth-status":
        result = handle_auth_status(args.out)
    elif command == "token-cache":
        result = handle_token_cache(args.out)
    elif command == "sim-auth":
        result = handle_sim_auth(args.out)
    elif command == "approval-happy":
        result = handle_approval_happy(args.out)
    elif command == "read-smoke":
        result = handle_read_smoke(args.out, args.groups)
    elif command == "nontrade-write":
        result = handle_nontrade_write(args.out, safe_only=bool(args.safe_only))
    elif command == "nontrade-denial-sweep":
        result = handle_nontrade_denial_sweep(args.out)
    elif command == "gitignore-secret":
        result = handle_gitignore_secret(args.out)
    elif command == "live-read":
        result = handle_live_read(args.out, args.skip_out)
    elif command == "live-write-refusal":
        result = handle_live_write_refusal(args.out)
    elif command == "manual-live-boundary":
        result = handle_manual_live_boundary(args.out)
    elif command == "prod-readiness":
        result = handle_prod_readiness(args.out)
    elif command == "tool-inventory":
        result = handle_tool_inventory(args.out)
    elif command == "live-read-refusal":
        result = handle_live_read_refusal(args.out)
    elif command == "secret-scan":
        result = handle_secret_scan(args.out, list(args.paths))
    elif command == "manifest":
        manifest = build_manifest(
            ManifestSpec(
                run_id=str(args.run_id),
                scenario_id=str(args.scenario_id),
                command=tuple(shlex.split(str(args.replay_command))),
                expected_status=str(args.expected_status),
                evidence_paths=tuple(str(value) for value in args.evidence_path),
            ),
        )
        result = 0 if write_scanned_json(args.out, manifest.to_json_value()) else 1
    elif command == "registered-endpoint-denied":
        result = handle_registered_endpoint_denied(
            args.out,
            method=str(args.method),
            path=str(args.path),
        )
    elif command == "list-registry-stdout":
        payload = load_registered_endpoint_list(
            None if args.service_group is None else str(args.service_group),
            int(args.limit),
            int(args.offset),
        )
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        result = 0 if payload.get("status") == "metadata_only_not_ready_for_trading" else 1
    elif command == "nontrade-denied":
        result = handle_nontrade_denied(args.out, service=str(args.service))
    elif command == "trade-precheck":
        result = handle_trade_precheck(args.out)
    elif command == "trade-multileg-defaults":
        result = handle_trade_multileg_defaults(args.out)
    elif command == "trade-disclaimer-lookup":
        result = handle_trade_disclaimer_lookup(args.out)
    elif command == "trade-disclaimer-response":
        result = handle_trade_disclaimer_response(args.out)
    elif command == "trade-disclaimer-blocked":
        result = handle_trade_disclaimer_blocked(args.out)
    elif command == "sim-order-mutation":
        result = handle_sim_order_mutation(args.out, args.classes)
    elif command == "production-order-mutation":
        result = handle_production_order_mutation(args.out, args.classes)
    elif command == "trading-write-matrix":
        result = handle_trading_write_matrix(args.out)
    elif command == "trade-write-denied":
        result = handle_trade_write_denied(args.out, str(args.missing))
    elif command == "stream":
        result = handle_stream(
            args.out,
            require_frame=bool(args.require_frame),
            expect_connections=args.expect_connections,
            expect_price_instruments=args.expect_price_instruments,
        )
    elif command == "stream-cleanup":
        result = handle_stream_cleanup(args.out, simulate_leak=bool(args.simulate_leak))
    elif command == "readme-smoke":
        result = handle_readme_smoke(args.out)
    elif command == "hard-task-manifest":
        result = handle_hard_task_manifest(args.out, list_registered_mcp_tool_ids())
    elif command == "hard-task-summary":
        expected_tools = tuple(str(value) for value in args.expected_tool)
        git = (
            None
            if args.expected_sha is None
            else GitState(sha=str(args.expected_sha), dirty=False)
        )
        if expected_tools:
            result = handle_hard_task_summary(
                args.out,
                args.receipts_dir,
                expected_tool_ids=expected_tools,
                git=git,
            )
        else:
            result = handle_hard_task_summary(args.out, args.receipts_dir, git=git)
    elif command == "approval-denied":
        result = handle_approval_denied(args.out, str(args.missing))
    else:
        result = write_incomplete(
            args.out,
            command,
            "real FastMCP/Saxo driver is not implemented yet",
        )
    return result


if __name__ == "__main__":
    sys.exit(main())
