from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

from fastmcp import Client
from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import now_utc, write_json, write_text
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.loop_schema import CompletionStatus, validate_completion_artifact
from saxo_bank_mcp.server import mcp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index tribunal completion artifacts.")
    parser.add_argument("--root", type=Path, default=Path(".omo/tribunal/saxo-mcp"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tools-file", type=Path)
    parser.add_argument("--self-test-missing-artifact", action="store_true")
    parser.add_argument("--allow-empty-bootstrap", action="store_true")
    return parser


def load_expected_tools(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return frozenset()
    if text.startswith("["):
        try:
            return frozenset(TypeAdapter(list[str]).validate_json(text))
        except ValidationError as exc:
            message = f"invalid tools-file JSON: {exc}"
            raise SystemExit(message) from exc
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


async def _list_registered_mcp_tool_ids() -> frozenset[str]:
    async with Client(mcp) as client:
        tools = await client.list_tools()
    return frozenset(tool.name for tool in tools)


def list_registered_mcp_tool_ids() -> frozenset[str]:
    return asyncio.run(_list_registered_mcp_tool_ids())


def resolve_expected_tools(path: Path | None) -> tuple[frozenset[str], str]:
    if path is not None:
        return load_expected_tools(path), "tools_file"
    return list_registered_mcp_tool_ids(), "fastmcp_tool_list"


def find_completion_files(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(root.glob("*/**/tribunal-completion.json")))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out: Path = args.out
    if args.self_test_missing_artifact:
        write_text(
            out,
            "missing tool_id=self_test_missing_tool\n"
            "network_call_made=false\n"
            "live_write=false\n"
            "order_or_subscription_created=false\n",
        )
        return 1

    expected_tools, expected_source = resolve_expected_tools(args.tools_file)
    artifacts = find_completion_files(args.root)
    validations = tuple(validate_completion_artifact(path) for path in artifacts)
    seen_tool_values = tuple(
        result.tool_id for result in validations if result.tool_id is not None
    )
    seen_tools = frozenset(seen_tool_values)
    missing_expected = tuple(sorted(expected_tools - seen_tools))
    duplicate_tool_ids = tuple(
        sorted(tool_id for tool_id, count in Counter(seen_tool_values).items() if count > 1)
    )
    unexpected_tool_ids = tuple(sorted(seen_tools - expected_tools))
    validation_errors = tuple(
        f"{result.path}: {error}" for result in validations for error in result.errors
    )
    coverage_errors = (
        tuple(
            f"missing expected tool artifact: {tool_id}"
            for tool_id in missing_expected
        )
        + tuple(
            f"duplicate tool artifact: {tool_id}"
            for tool_id in duplicate_tool_ids
        )
        + tuple(
            f"unexpected tool artifact (unregistered tool_id): {tool_id}"
            for tool_id in unexpected_tool_ids
        )
    )
    error_lines = validation_errors + coverage_errors
    if not validations and not expected_tools and not args.allow_empty_bootstrap:
        error_lines += (
            "no tribunal artifacts seen; pass --allow-empty-bootstrap only before MCP tools exist",
        )
    complete_with_remaining_feedback = tuple(
        sorted(
            result.tool_id
            for result in validations
            if result.tool_id is not None
            and any(
                "remaining_actionable_feedback" in error for error in result.errors
            )
        ),
    )
    status_counts = Counter(
        result.status.value for result in validations if result.status is not None
    )
    completed_tool_ids = sorted(
        result.tool_id
        for result in validations
        if result.status is CompletionStatus.COMPLETE and result.tool_id is not None
    )
    refused_tool_ids = sorted(
        result.tool_id
        for result in validations
        if result.status is CompletionStatus.REFUSED and result.tool_id is not None
    )
    incomplete_tool_ids = sorted(
        result.tool_id
        for result in validations
        if result.status is CompletionStatus.INCOMPLETE and result.tool_id is not None
    )
    exempt_tool_ids = sorted(
        result.tool_id
        for result in validations
        if result.status is CompletionStatus.EXEMPT and result.tool_id is not None
    )
    passed = not error_lines
    write_json(
        out,
        {
            "checked_at": now_utc(),
            "command": "tribunal_index",
            "driver": "loop_harness",
            "git": current_git_state().model_dump(mode="json"),
            "status": "passed" if passed else "failed",
            "root": str(args.root),
            "source": expected_source,
            "artifact_count": len(validations),
            "no_artifacts_seen": len(validations) == 0,
            "expected_tool_count": len(expected_tools),
            "seen_tool_count": len(seen_tools),
            "expected_tool_ids": sorted(expected_tools),
            "seen_tool_ids": sorted(seen_tools),
            "missing_tool_ids": list(missing_expected),
            "duplicate_tool_ids": list(duplicate_tool_ids),
            "unexpected_tool_ids": list(unexpected_tool_ids),
            "counts": dict(sorted(status_counts.items())),
            "completed_tool_ids": completed_tool_ids,
            "refused_tool_ids": refused_tool_ids,
            "incomplete_tool_ids": incomplete_tool_ids,
            "incomplete_tool_count": len(incomplete_tool_ids),
            "has_incomplete_tools": bool(incomplete_tool_ids),
            "exempt_tool_ids": exempt_tool_ids,
            "remaining_actionable_feedback_complete_tool_ids": list(
                complete_with_remaining_feedback
            ),
            "invalid_artifact_errors": list(validation_errors),
            "coverage_errors": list(coverage_errors),
            "no_hidden_deferred_state": not error_lines,
            "no_deferred_state": not error_lines and not incomplete_tool_ids,
            "errors": list(error_lines),
        },
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
