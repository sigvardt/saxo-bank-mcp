from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import now_utc, write_json, write_text
from saxo_bank_mcp.loop_manifest import current_git_state
from saxo_bank_mcp.loop_schema import CompletionStatus, validate_completion_artifact


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


def find_completion_files(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(root.glob("*/**/tribunal-completion.json")))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out: Path = args.out
    if args.self_test_missing_artifact:
        write_text(out, "missing tool_id=self_test_missing_tool\n")
        return 1

    expected_tools = load_expected_tools(args.tools_file)
    artifacts = find_completion_files(args.root)
    validations = tuple(validate_completion_artifact(path) for path in artifacts)
    seen_tools = frozenset(result.tool_id for result in validations if result.tool_id is not None)
    missing_expected = tuple(sorted(expected_tools - seen_tools))
    error_lines = tuple(
        f"{result.path}: {error}" for result in validations for error in result.errors
    ) + tuple(f"missing expected tool artifact: {tool_id}" for tool_id in missing_expected)
    if not validations and not expected_tools and not args.allow_empty_bootstrap:
        error_lines += (
            "no tribunal artifacts seen; pass --allow-empty-bootstrap only before MCP tools exist",
        )
    status_counts = Counter(
        result.status.value for result in validations if result.status is not None
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
            "artifact_count": len(validations),
            "no_artifacts_seen": len(validations) == 0,
            "expected_tool_count": len(expected_tools),
            "seen_tool_count": len(seen_tools),
            "counts": dict(sorted(status_counts.items())),
            "completed_tool_ids": sorted(
                result.tool_id
                for result in validations
                if result.status is CompletionStatus.COMPLETE and result.tool_id is not None
            ),
            "refused_tool_ids": sorted(
                result.tool_id
                for result in validations
                if result.status is CompletionStatus.REFUSED and result.tool_id is not None
            ),
            "incomplete_tool_ids": sorted(
                result.tool_id
                for result in validations
                if result.status is CompletionStatus.INCOMPLETE and result.tool_id is not None
            ),
            "exempt_tool_ids": sorted(
                result.tool_id
                for result in validations
                if result.status is CompletionStatus.EXEMPT and result.tool_id is not None
            ),
            "errors": list(error_lines),
        },
    )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
