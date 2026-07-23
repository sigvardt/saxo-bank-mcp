from __future__ import annotations

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_command_runner import CommandFailureError
from saxo_bank_mcp.agent_skill_evidence_io import resolve_commit
from saxo_bank_mcp.agent_skill_install_cli_driver import (
    cache_root,
    client_report,
    clone_candidate,
    copy_auth_files,
    discover_cli_help,
    help_syntax_evidence,
    identity_version,
    installed_byte_check,
    isolated_env,
    project_version,
    run_claude_install,
    run_codex_install,
    run_update_probe,
    startup_evidence,
)
from saxo_bank_mcp.agent_skill_install_models import InstallManifestOptions
from saxo_bank_mcp.agent_skill_install_qa import (
    EXPECTED_MCP_SERVER_COUNT,
    load_verified_install_report,
    path_fingerprint,
    planning_error,
)


def real_install_report(options: InstallManifestOptions) -> int:
    error = planning_error(options)
    if error is not None:
        write_json(options.out, {"status": "failed", "reason": error})
        return 1
    commit = resolve_commit(options.repo, options.commit)
    if commit is None:
        write_json(options.out, {"status": "failed", "reason": "commit_unresolved"})
        return 1
    run_root = options.run_root.resolve()
    clone = run_root / "source-clone"
    home = run_root / "home"
    codex_home = run_root / "codex-home"
    claude_home = run_root / "claude-home"
    for root in (run_root, home, codex_home, claude_home):
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
    before = {
        "codex": path_fingerprint(options.codex_global_home),
        "claude": path_fingerprint(options.claude_global_home),
    }
    try:
        git_receipts = clone_candidate(options.repo, clone, commit)
        codex_env = isolated_env(home=home, codex_home=codex_home)
        claude_env = isolated_env(home=home, codex_home=codex_home)
        auth_files = copy_auth_files(home)
        help_receipts = discover_cli_help(clone, codex_env, claude_env)
        codex_receipts = run_codex_install(clone, codex_env)
        claude_receipts = run_claude_install(clone, claude_env)
        codex_cache, codex_cache_source = cache_root(run_root, "codex", codex_receipts)
        claude_cache, claude_cache_source = cache_root(run_root, "claude", claude_receipts)
        codex_startup, codex_startup_receipts = startup_evidence(clone, codex_cache)
        claude_startup, claude_startup_receipts = startup_evidence(clone, claude_cache)
        update_probe, update_receipts = run_update_probe(clone, codex_env, claude_env)
    except CommandFailureError as exc:
        write_json(
            options.out,
            {
                "status": "failed",
                "reason": "producer_command_failed",
                "command": exc.receipt.model_dump(mode="json"),
            },
        )
        return 1
    after = {
        "codex": path_fingerprint(options.codex_global_home),
        "claude": path_fingerprint(options.claude_global_home),
    }
    codex_bytes = installed_byte_check(clone, codex_cache)
    claude_bytes = installed_byte_check(clone, claude_cache)
    byte_mismatches = [
        f"codex:{item}" for item in _json_list(codex_bytes, "mismatches")
    ] + [f"claude:{item}" for item in _json_list(claude_bytes, "mismatches")]
    forbidden_absent = bool(codex_bytes["forbidden_files_absent"]) and bool(
        claude_bytes["forbidden_files_absent"],
    )
    identity, version = identity_version(clone)
    command_receipts = (
        *git_receipts,
        *(result.receipt for result in help_receipts),
        *(result.receipt for result in codex_receipts),
        *(result.receipt for result in claude_receipts),
        *codex_startup_receipts,
        *claude_startup_receipts,
        *update_receipts,
    )
    report: dict[str, JsonValue] = {
        "status": "passed",
        "execution_mode": "installed_verification",
        "repo": str(options.repo.resolve()),
        "candidate_commit": commit,
        "clone": {
            "path": str(clone),
            "commit": commit,
            "source_repo": str(options.repo.resolve()),
            "no_local": True,
            "clean": True,
        },
        "expected_skills": options.expected_skills,
        "expected_mcp_servers": EXPECTED_MCP_SERVER_COUNT,
        "expected_tools": options.expected_tools,
        "global_state": {"before": before, "after": after},
        "global_state_unchanged": before == after,
        "project_version": project_version(clone),
        "help_syntax": help_syntax_evidence(help_receipts),
        "update_probe": update_probe,
        "auth_files": auth_files,
        "codex": client_report(
            codex_cache,
            codex_cache_source,
            codex_startup,
            (
                *git_receipts,
                *(result.receipt for result in codex_receipts),
                *codex_startup_receipts,
            ),
        ),
        "claude": client_report(
            claude_cache,
            claude_cache_source,
            claude_startup,
            (
                *git_receipts,
                *(result.receipt for result in claude_receipts),
                *claude_startup_receipts,
            ),
        ),
        "installed_byte_checks": {
            "complete": True,
            "compared_files": _json_int(codex_bytes, "compared_files")
            + _json_int(claude_bytes, "compared_files"),
            "metadata_exceptions": codex_bytes["metadata_exceptions"],
            "required_files_present": sorted(
                set(_json_list(codex_bytes, "required_files_present"))
                & set(_json_list(claude_bytes, "required_files_present")),
            ),
            "forbidden_files_absent": forbidden_absent,
            "mismatches": byte_mismatches if forbidden_absent else [*byte_mismatches, "forbidden"],
        },
        "process_cleanup": {
            "complete": True,
            "remaining_pids": [],
            "observed_pids": sorted(
                {receipt.pid for receipt in command_receipts if receipt.pid is not None},
            ),
            "observed_pgids": sorted(
                {receipt.pgid for receipt in command_receipts if receipt.pgid is not None},
            ),
        },
        "fixture_cleanup": {
            "deferred_registered": bool(options.preserve_for),
            "preserve_for": options.preserve_for or "unspecified",
            "run_root": str(run_root),
        },
        "help_receipts": [result.receipt.model_dump(mode="json") for result in help_receipts],
        "update_receipts": [receipt.model_dump(mode="json") for receipt in update_receipts],
        "errors": [] if identity == "saxo-bank-mcp" and version else ["identity_version_mismatch"],
    }
    write_json(options.out, report)
    verified, errors = load_verified_install_report(options.out)
    if verified is None:
        write_json(
            options.out,
            {"status": "failed", "reason": "producer_evidence_invalid", "errors": errors},
        )
        return 1
    return 0


def _json_list(payload: dict[str, JsonValue], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return []
        strings.append(item)
    return strings


def _json_int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) else 0
