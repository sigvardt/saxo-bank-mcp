from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_eval_models import load_scenario_tools
from saxo_bank_mcp.agent_skill_matrix import MATRIX_RECONCILIATION_PROOF
from saxo_bank_mcp.qa_analytics_sim import BROKERAGE_STATE_COMPONENTS

ROOT: Final = Path(__file__).resolve().parents[1]
CATALOG_TASK_NUMBER: Final = 13


@dataclass(frozen=True, slots=True)
class InstallFixture:
    report: Path
    commit: str
    clone: Path
    repo: Path


def build_install_fixture(base: Path) -> InstallFixture:
    repo = base / "source"
    clone = base / "clone"
    git("clone", "--no-local", "--quiet", str(ROOT), str(repo), cwd=ROOT)
    _overlay_tracked_worktree(repo)
    router = repo / "skills/saxo-bank/SKILL.md"
    router.parent.mkdir(parents=True, exist_ok=True)
    router.write_text("---\nname: saxo-bank\ndescription: Fixture router.\n---\n", encoding="utf-8")
    git("add", "--all", cwd=repo)
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture",
        "commit",
        "--quiet",
        "-m",
        "fixture router",
        cwd=repo,
    )
    git("clone", "--no-local", "--quiet", str(repo), str(clone), cwd=repo)
    commit = git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    caches = (base / "codex-cache", base / "claude-cache")
    tracked = git("ls-files", cwd=clone).stdout.splitlines()
    for cache in caches:
        for relative in tracked:
            source = clone / relative
            if source.is_file():
                target = cache / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    report = base / "install.json"
    write_json(report, _install_payload(repo, clone, caches, commit, base))
    return InstallFixture(report=report, commit=commit, clone=clone, repo=repo)


def _overlay_tracked_worktree(repo: Path) -> None:
    """Make the disposable install fixture exercise the current tracked candidate."""
    changed = git("diff", "--name-only", "--diff-filter=ACMRTUXB", "HEAD", cwd=ROOT)
    for relative in changed.stdout.splitlines():
        source = ROOT / relative
        target = repo / relative
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    deleted = git("diff", "--name-only", "--diff-filter=D", "HEAD", cwd=ROOT)
    for relative in deleted.stdout.splitlines():
        target = repo / relative
        if target.is_file():
            target.unlink()


def build_release_evidence(
    root: Path,
    installed_report: InstallFixture,
    *,
    include_privacy: bool,
) -> tuple[Path, Path, Path]:
    evidence = root / "evidence"
    commit = installed_report.commit
    for number in range(1, 17):
        task = evidence / f"task-{number}-fixture"
        task.mkdir(parents=True)
        payload: dict[str, JsonValue] = {"status": "passed", "source_commit": commit}
        if number == CATALOG_TASK_NUMBER:
            payload["counts"] = {
                "tools": 60,
                "operations": 294,
                "implemented": 182,
                "refused": 112,
                "service_groups": 17,
            }
        write_json(task / "DoneClaim.json", payload)
    task14 = evidence / "task-14-installed-cache" / "manual"
    task15 = evidence / "task-15-sim" / "manual"
    task16 = evidence / "task-16-live-no-purchase" / "manual"
    for directory in (task14, task15, task16):
        directory.mkdir(parents=True)
    shutil.copy2(installed_report.report, task14 / "install.json")
    names = sorted(load_scenario_tools(ROOT))
    calls: list[JsonValue] = [
        {
            "tool": name,
            "status": "completed",
            "mcp_call_observed": True,
            "result_parsed": True,
            "skipped": False,
            "requested_tool_covered": True,
            "request_digest": "a" * 64,
            "response_digest": "b" * 64,
        }
        for name in names
    ]
    state = _brokerage_state_payload(trade_message_count=0)
    reconciled_state = _brokerage_state_payload(trade_message_count=2)
    write_json(
        task15 / "tool-matrix.json",
        {
            "status": "passed",
            "execution_mode": "sim_execution",
            "environment": "SIM",
            "source_commit": commit,
            "candidate_commit": commit,
            "install_report": str(task14 / "install.json"),
            "install_report_sha256": hashlib.sha256(
                (task14 / "install.json").read_bytes()
            ).hexdigest(),
            "tool_count": len(names),
            "unique_tools": names,
            "missing_tools": [],
            "unexpected_tools": [],
            "expected_call_count": len(calls),
            "tool_calls": calls,
            "preflight": {
                "complete": True,
                "auth_status_completed": True,
                "session_capabilities_completed": True,
                "fixture_reference_validated": True,
                "account_allowlist_resolved": True,
                "disclaimer_response_made": False,
                "disclaimer_refusal_observed": True,
            },
            "transport_ledger": {
                "sim_only": True,
                "live_events": 0,
                "hosts": ["sim.api.saxo.test"],
            },
            "before_state_fingerprint": state,
            "after_state_fingerprint": reconciled_state,
            "cleanup": {
                "complete": True,
                "uncleaned_resources": 0,
                "proof": list(MATRIX_RECONCILIATION_PROOF),
            },
            "unexpected_skips": [],
            "lifecycle_calls": [
                "saxo_create_write_preview",
                "saxo_commit_write_preview",
                "saxo_create_order_preview",
                "saxo_prepare_trading_write",
                "saxo_register_disclaimer_response",
                "saxo_execute_trading_write",
                "saxo_place_order",
                "saxo_modify_order",
                "saxo_cancel_order",
                "saxo_cancel_orders_by_instrument",
                "saxo_place_multileg_order",
                "saxo_modify_multileg_order",
                "saxo_cancel_multileg_order",
                "saxo_place_sim_order",
                "saxo_modify_sim_order",
                "saxo_cancel_sim_order",
                "saxo_cancel_sim_orders_by_instrument",
                "saxo_place_multileg_sim_order",
                "saxo_modify_multileg_sim_order",
                "saxo_cancel_multileg_sim_order",
                "saxo_create_streaming_price_subscription",
                "saxo_cleanup_streaming_subscriptions",
            ],
            "command_receipts": [],
            "errors": [],
        },
    )
    evals: dict[str, JsonValue] = {
        "status": "passed",
        "execution_mode": "model_execution",
        "source_commit": commit,
        "skipped_count": 0,
        "global_state_unchanged": True,
        "cleanup": {"complete": True},
    }
    write_json(task15 / "agent-evals.json", evals)
    write_json(task16 / "agent-evals.json", {**evals, "live_mutation_calls": 0})
    live = task16 / "proof.json"
    write_json(
        live,
        {
            "status": "passed",
            "source_commit": commit,
            "source": {"git_head": commit},
            "environment": "LIVE",
            "state_unchanged": True,
            "ledger_complete": True,
            "negative_proof_available": True,
            "live_mutation_calls": 0,
            "purchase_occurred": False,
            "cleanup": {"complete": True},
        },
    )
    if include_privacy:
        write_json(
            task16 / "privacy-scan.json",
            {
                "status": "passed",
                "source_commit": commit,
                "findings_count": 0,
                "scan_errors_count": 0,
            },
        )
    plan = root / "plan.md"
    plan.write_text("# Fixture plan\n", encoding="utf-8")
    return evidence, plan, live


def _brokerage_state_payload(*, trade_message_count: int) -> dict[str, JsonValue]:
    return {
        "components": [
            {
                "name": name,
                "observed_state": "available",
                "count": trade_message_count if name == "trade_messages" else 0,
                "fingerprint_sha256": hashlib.sha256(name.encode("utf-8")).hexdigest(),
                "mcp_tool_ids": ["saxo_health"],
            }
            for name in BROKERAGE_STATE_COMPONENTS
        ],
    }


def run_cli(
    script: Path,
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout_seconds: int = 180,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env is not None:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=ROOT if cwd is None else cwd,
        env=merged_env,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        msg = "git is required for evidence fixture tests"
        raise RuntimeError(msg)
    return subprocess.run(
        [executable, *args], cwd=cwd, text=True, capture_output=True, check=True, timeout=180
    )


def write_json(path: Path, payload: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def reason(path: Path) -> str:
    return str(json.loads(path.read_text(encoding="utf-8"))["reason"])


def errors(path: Path) -> tuple[str, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(str(item) for item in payload.get("errors", []))


def _install_payload(
    repo: Path,
    clone: Path,
    caches: tuple[Path, Path],
    commit: str,
    run_root: Path,
) -> dict[str, JsonValue]:
    fingerprints: dict[str, JsonValue] = {"codex": "a" * 64, "claude": "b" * 64}
    home = run_root / "home"
    codex_home = run_root / "codex-home"
    claude_home = run_root / "claude-home"
    for path in (run_root, clone, caches[0], caches[1], home, codex_home, claude_home):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    modes = {
        "run_root": "0o700",
        "clone": "0o700",
        "codex_cache": "0o700",
        "claude_cache": "0o700",
        "home": "0o700",
        "codex_home": "0o700",
        "claude_home": "0o700",
    }
    return {
        "status": "passed",
        "execution_mode": "fixture_support",
        "repo": str(repo),
        "candidate_commit": commit,
        "clone": {
            "path": str(clone),
            "commit": commit,
            "source_repo": str(repo),
            "no_local": True,
            "clean": True,
            "mode": "0o700",
        },
        "expected_skills": 9,
        "expected_mcp_servers": 1,
        "expected_tools": 60,
        "global_state": {"before": fingerprints, "after": fingerprints, "scope": {}},
        "global_state_unchanged": True,
        "project_version": "0.1.0",
        "help_syntax": {
            "codex_plugin_help": {"validated": True, "stdout_sha256": "c" * 64},
            "codex_marketplace_help": {"validated": True, "stdout_sha256": "c" * 64},
            "claude_plugin_help": {"validated": True, "stdout_sha256": "c" * 64},
            "claude_marketplace_help": {"validated": True, "stdout_sha256": "c" * 64},
        },
        "update_probe": {
            "original_version": "0.1.0",
            "bumped_version": "0.1.1",
            "codex_reached_bumped": True,
            "claude_reached_bumped": True,
            "candidate_restored": True,
            "temporary_fixtures_removed": True,
        },
        "auth_files": {"copied": [], "values_published": False},
        "codex": _client_payload(caches[0], source="fixture_codex"),
        "claude": _client_payload(caches[1], source="fixture_claude"),
        "installed_byte_checks": {
            "complete": True,
            "compared_files": 1,
            "metadata_exceptions": [],
            "required_files_present": [
                ".mcp.json",
                ".claude-plugin/plugin.json",
                ".codex-plugin/plugin.json",
                "data/saxo/openapi_inventory.json",
                "pyproject.toml",
                "uv.lock",
                "skills/saxo-bank/SKILL.md",
                "skills/saxo-analytics/SKILL.md",
                "skills/saxo-auth-session/SKILL.md",
                "skills/saxo-openapi/SKILL.md",
                "skills/saxo-qa-operations/SKILL.md",
                "skills/saxo-reads/SKILL.md",
                "skills/saxo-safety-recovery/SKILL.md",
                "skills/saxo-streaming/SKILL.md",
                "skills/saxo-trading/SKILL.md",
            ],
            "forbidden_files_absent": True,
            "mismatches": [],
            "inventory_exact_match": True,
        },
        "process_cleanup": {
            "complete": True,
            "remaining_pids": [],
            "remaining_pgids": [],
            "observed_pids": [1000],
            "observed_pgids": [1000],
        },
        "fixture_cleanup": {
            "deferred_registered": True,
            "preserve_for": "task-15,task-16",
            "run_root": str(run_root),
            "preserved_paths": [
                str(clone),
                str(caches[0]),
                str(caches[1]),
                str(home),
                str(codex_home),
                str(claude_home),
            ],
            "modes": modes,
            "owner_only": True,
            "teardown_owner": "post-final-completion-gate",
            "consumers": ["task-15", "task-16"],
        },
        "errors": [],
    }


def _client_payload(cache: Path, *, source: str) -> dict[str, JsonValue]:
    startup: dict[str, JsonValue] = {
        "source": {"status": "passed", "tool_count": 60, "annotations_missing": []},
        "cache": {"status": "passed", "tool_count": 60, "annotations_missing": []},
        "list_tools": {"status": "passed", "tool_count": 60, "annotations_missing": []},
    }
    receipt = {
        "name": source,
        "argv": ["fixture"],
        "cwd": str(cache),
        "pid": 1,
        "pgid": 1,
        "exit_code": 0,
        "stdout_sha256": "d" * 64,
        "stderr_sha256": "e" * 64,
        "timed_out": False,
        "cleanup_attempted": False,
    }
    skills = [
        "saxo-analytics",
        "saxo-auth-session",
        "saxo-bank",
        "saxo-openapi",
        "saxo-qa-operations",
        "saxo-reads",
        "saxo-safety-recovery",
        "saxo-streaming",
        "saxo-trading",
    ]
    return {
        "installed": True,
        "cache_root": str(cache),
        "identity": "saxo-bank-mcp",
        "version": "0.1.0",
        "cache_root_source": source,
        "skill_count": 9,
        "skills": skills,
        "mcp_server_count": 1,
        "tool_count": 60,
        "annotations_missing": [],
        "source_annotations_missing": [],
        "cache_annotations_missing": [],
        "list_tools_annotations_missing": [],
        "forbidden_cache_paths": [],
        "installed_bytes_match": True,
        "install_command_exit_code": 0,
        "startup": startup,
        "command_receipts": [receipt],
        "inventory": {
            "inventory_exact_match": True,
            "forbidden_files_absent": True,
            "mismatches": [],
            "metadata_exceptions": [],
        },
    }
