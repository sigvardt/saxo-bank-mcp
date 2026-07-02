from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue, write_text
from saxo_bank_mcp.final_verify_common import (
    JSON_MAPPING_ADAPTER,
    GitStateProvider,
    render_report,
    validate_evidence_payload,
)

PLAN_MARKERS = (
    "`tribunal-completion.json` schema",
    "Final verification requires four independent checks",
    "python -m saxo_bank_mcp.tribunal_index",
    "python -m saxo_bank_mcp.final_verify plan",
    "python -m saxo_bank_mcp.final_verify code",
    "python -m saxo_bank_mcp.final_verify mcp",
    "python -m saxo_bank_mcp.final_verify scope",
)
CHECKED_TASK_RE = re.compile(r"^- \[[xX]\] ([0-9]+)\.", re.MULTILINE)
PLAN_JSON_EVIDENCE_STATUSES = frozenset(
    {
        "denied",
        "exercised",
        "passed",
        "refused",
        "skipped_no_live_credentials",
        "skipped_no_safe_operation",
    },
)
LOOP_GOALS_PATH = Path(
    ".omo/ulw-loop/257e3ed0-a98d-480f-89ab-4d5d96a5fc9b/goals.json",
)
FINAL_GOAL_ID = "G011-final-verification-run-f1-f4-from-om"
FINAL_SELF_CHECK_CRITERION_ID = "C001"
FINAL_GOAL_ALLOWED_SELF_CHECK_STATUSES = frozenset({"blocked", "in_progress", "pending"})


def verify_plan(plan_path: Path, out: Path, git_state_provider: GitStateProvider) -> int:
    text = plan_path.read_text(encoding="utf-8") if plan_path.exists() else ""
    checks = [
        (marker, marker in text, "present" if marker in text else "missing")
        for marker in PLAN_MARKERS
    ]
    unchecked_count = text.count("- [ ]")
    checks.append(
        (
            "original plan checkboxes tracked by ULW goals",
            True,
            f"{unchecked_count} unchecked static boxes; loop goals are authoritative",
        ),
    )
    checks.append(loop_goals_check(LOOP_GOALS_PATH))
    task_evidence_map = {
        1: [".omo/evidence/saxo-bank-mcp/task-1-gitignore.json"],
        2: [".omo/evidence/saxo-bank-mcp/task-2-sim-auth.json"],
        3: [".omo/evidence/saxo-bank-mcp/task-3-approval-denied.json"],
        4: [".omo/evidence/saxo-bank-mcp/task-4-read-smoke.json"],
        5: [".omo/evidence/saxo-bank-mcp/task-5-denied.json"],
        6: [".omo/evidence/saxo-bank-mcp/task-6-precheck.json"],
        7: [".omo/evidence/saxo-bank-mcp/task-7-sim-order.json"],
        8: [".omo/evidence/saxo-bank-mcp/task-8-stream.json"],
        9: [".omo/evidence/saxo-bank-mcp/task-9-tribunal-index.json"],
        10: [".omo/evidence/saxo-bank-mcp/task-10-live-write-refusal.json"],
    }
    checked_task_numbers = {int(value) for value in CHECKED_TASK_RE.findall(text)}
    for task_num, paths in task_evidence_map.items():
        if task_num in checked_task_numbers:
            checks.extend(
                plan_task_evidence_check(task_num, path, git_state_provider) for path in paths
            )
    passed = plan_path.exists() and all(ok for _, ok, _ in checks)
    write_text(
        out,
        render_report(
            "Plan Compliance",
            passed=passed,
            checks=checks,
            git_state_provider=git_state_provider,
        ),
    )
    return 0 if passed else 1


def plan_task_evidence_check(
    task_num: int,
    path: str,
    git_state_provider: GitStateProvider,
) -> tuple[str, bool, str]:
    evidence_path = Path(path)
    if not evidence_path.exists():
        return f"Task {task_num} evidence {path}", False, "missing despite task marked as completed"
    if evidence_path.suffix != ".json":
        return f"Task {task_num} evidence {path}", True, "present"
    try:
        payload = JSON_MAPPING_ADAPTER.validate_json(evidence_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        return f"Task {task_num} evidence {path}", False, f"invalid evidence: {type(exc).__name__}"
    ok, detail = validate_evidence_payload(
        payload,
        PLAN_JSON_EVIDENCE_STATUSES,
        git_state_provider().sha,
    )
    if not ok:
        return f"Task {task_num} evidence {path}", False, detail
    if path.endswith("task-9-tribunal-index.json") and payload.get("no_artifacts_seen") is True:
        return f"Task {task_num} evidence {path}", False, "tribunal coverage is empty"
    return f"Task {task_num} evidence {path}", True, detail


def loop_goals_check(path: Path) -> tuple[str, bool, str]:
    if not path.exists():
        return "ULW goal state", False, f"missing {path}"
    try:
        payload = JSON_MAPPING_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        return "ULW goal state", False, f"invalid goals: {type(exc).__name__}"
    goals = payload.get("goals")
    if not isinstance(goals, list):
        return "ULW goal state", False, "missing goals list"
    incomplete = _incomplete_goal_labels(goals)
    if incomplete:
        return "ULW goal state", False, "; ".join(incomplete)
    return "ULW goal state", True, f"{len(goals)} goals complete"


def _incomplete_goal_labels(goals: list[JsonValue]) -> list[str]:
    labels: list[str] = []
    for index, item in enumerate(goals, start=1):
        if not isinstance(item, Mapping):
            labels.append(f"goal {index}: invalid")
            continue
        status = item.get("status")
        if status == "complete":
            continue
        raw_id = item.get("id")
        goal_id = raw_id if isinstance(raw_id, str) else f"goal {index}"
        if goal_id == FINAL_GOAL_ID and _final_goal_ready_for_self_check(item):
            continue
        status_text = status if isinstance(status, str) else "missing status"
        labels.append(f"{goal_id}: {status_text}")
    return labels


def _final_goal_ready_for_self_check(goal: Mapping[str, JsonValue]) -> bool:
    criteria = goal.get("successCriteria")
    if not isinstance(criteria, list):
        return False
    seen_self_check = False
    for item in criteria:
        if not isinstance(item, Mapping):
            return False
        criterion_id = item.get("id")
        status = item.get("status")
        if criterion_id == FINAL_SELF_CHECK_CRITERION_ID:
            seen_self_check = status in FINAL_GOAL_ALLOWED_SELF_CHECK_STATUSES
            continue
        if status != "pass":
            return False
    return seen_self_check
