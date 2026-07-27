from __future__ import annotations

import shutil

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    remaining_live_pids,
)
from saxo_bank_mcp.agent_skill_evidence_io import resolve_commit
from saxo_bank_mcp.agent_skill_install_cli_driver import (
    CommandDiscoveryError,
    cache_root_from_results,
    client_report,
    clone_candidate,
    copy_auth_files,
    discover_cli_help,
    help_syntax_evidence,
    identity_version,
    isolated_env,
    project_version,
    run_claude_install,
    run_codex_install,
    run_update_probe,
    startup_evidence,
)
from saxo_bank_mcp.agent_skill_install_models import InstallManifestOptions
from saxo_bank_mcp.agent_skill_install_paths import (
    export_publishable_tree,
    global_state_fingerprint,
    installed_byte_check,
    owner_only_mode,
)
from saxo_bank_mcp.agent_skill_install_qa import (
    EXPECTED_MCP_SERVER_COUNT,
    load_verified_install_report,
    planning_error,
)


def real_install_report(options: InstallManifestOptions) -> int:  # noqa: PLR0911, PLR0915
    error = planning_error(options)
    if error is not None:
        write_json(options.out, {"status": "failed", "reason": error})
        return 1
    commit = resolve_commit(options.repo, options.commit)
    if commit is None:
        write_json(options.out, {"status": "failed", "reason": "commit_unresolved"})
        return 1
    if options.codex_global_home is None or options.claude_global_home is None:
        write_json(options.out, {"status": "failed", "reason": "isolated_home_invalid"})
        return 1
    run_root = options.run_root.resolve()
    clone = run_root / "source-clone"
    marketplace = run_root / "marketplace-source"
    home = run_root / "home"
    codex_home = run_root / "codex-home"
    claude_home = run_root / "claude-home"
    probe_env = run_root / "probe-env"
    for root in (run_root, home, codex_home, claude_home, probe_env):
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
    before = global_state_fingerprint(options.codex_global_home, options.claude_global_home)
    try:
        git_receipts = clone_candidate(options.repo, clone, commit)
        export_publishable_tree(clone, marketplace)
        codex_env = isolated_env(home=home, codex_home=codex_home, probe_env=probe_env)
        claude_env = isolated_env(home=home, codex_home=codex_home, probe_env=probe_env)
        auth_files = copy_auth_files(home)
        help_receipts = discover_cli_help(clone, codex_env, claude_env)
        codex_receipts = run_codex_install(marketplace, codex_env)
        claude_receipts = run_claude_install(marketplace, claude_env)
        # Discover once so discovery failure fails closed before the update probe.
        cache_root_from_results(codex_receipts, client="codex")
        cache_root_from_results(claude_receipts, client="claude")
        update_probe, update_receipts, codex_cache, claude_cache = run_update_probe(
            marketplace,
            codex_env,
            claude_env,
        )
        codex_cache_source = "codex_plugin_add_restored"
        claude_cache_source = "claude_plugin_list_restored"
        codex_startup, codex_missing, codex_startup_receipts = startup_evidence(
            clone,
            codex_cache,
            probe_env=probe_env,
        )
        claude_startup, claude_missing, claude_startup_receipts = startup_evidence(
            clone,
            claude_cache,
            probe_env=probe_env,
        )
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
    except CommandDiscoveryError as exc:
        write_json(options.out, {"status": "failed", "reason": exc.reason})
        return 1
    after = global_state_fingerprint(options.codex_global_home, options.claude_global_home)
    codex_bytes = installed_byte_check(clone, codex_cache)
    claude_bytes = installed_byte_check(clone, claude_cache)
    byte_mismatches = [f"codex:{item}" for item in _json_list(codex_bytes, "mismatches")] + [
        f"claude:{item}" for item in _json_list(claude_bytes, "mismatches")
    ]
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
    observed_pids = tuple(
        sorted({receipt.pid for receipt in command_receipts if receipt.pid is not None}),
    )
    observed_pgids = tuple(
        sorted({receipt.pgid for receipt in command_receipts if receipt.pgid is not None}),
    )
    remaining = remaining_live_pids(observed_pids)
    metadata = sorted(
        set(_json_list(codex_bytes, "metadata_exceptions"))
        | set(_json_list(claude_bytes, "metadata_exceptions")),
    )
    preserve_for = options.preserve_for or "unspecified"
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
            "mode": owner_only_mode(clone),
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
            codex_missing,
            (
                *git_receipts,
                *(result.receipt for result in codex_receipts),
                *codex_startup_receipts,
            ),
            bytes_match=not _json_list(codex_bytes, "mismatches")
            and bool(codex_bytes["forbidden_files_absent"]),
        ),
        "claude": client_report(
            claude_cache,
            claude_cache_source,
            claude_startup,
            claude_missing,
            (
                *git_receipts,
                *(result.receipt for result in claude_receipts),
                *claude_startup_receipts,
            ),
            bytes_match=not _json_list(claude_bytes, "mismatches")
            and bool(claude_bytes["forbidden_files_absent"]),
        ),
        "installed_byte_checks": {
            "complete": True,
            "compared_files": _json_int(codex_bytes, "compared_files")
            + _json_int(claude_bytes, "compared_files"),
            "metadata_exceptions": metadata,
            "required_files_present": sorted(
                set(_json_list(codex_bytes, "required_files_present"))
                & set(_json_list(claude_bytes, "required_files_present")),
            ),
            "forbidden_files_absent": forbidden_absent,
            "mismatches": byte_mismatches if forbidden_absent else [*byte_mismatches, "forbidden"],
        },
        "process_cleanup": {
            "complete": not remaining,
            "remaining_pids": list(remaining),
            "observed_pids": list(observed_pids),
            "observed_pgids": list(observed_pgids),
        },
        "fixture_cleanup": {
            "deferred_registered": bool(options.preserve_for),
            "preserve_for": preserve_for,
            "run_root": str(run_root),
            "preserved_paths": [
                str(clone),
                str(codex_cache),
                str(claude_cache),
                str(home),
                str(codex_home),
                str(claude_home),
            ],
            "owner_only": True,
            "teardown_owner": "post-final-completion-gate",
            "consumers": [item.strip() for item in preserve_for.split(",") if item.strip()],
        },
        "help_receipts": [result.receipt.model_dump(mode="json") for result in help_receipts],
        "update_receipts": [receipt.model_dump(mode="json") for receipt in update_receipts],
        "errors": _producer_errors(
            identity,
            version,
            remaining,
            forbidden_absent=forbidden_absent,
            update_probe=update_probe,
        ),
    }
    if marketplace.exists():
        shutil.rmtree(marketplace)
    if probe_env.exists():
        shutil.rmtree(probe_env, ignore_errors=True)
    for preserved in (clone, codex_cache, claude_cache, home, codex_home, claude_home, run_root):
        if preserved.exists():
            preserved.chmod(0o700)
    write_json(options.out, report)
    verified, errors = load_verified_install_report(options.out)
    if verified is None:
        write_json(
            options.out,
            {
                "status": "failed",
                "reason": "producer_evidence_invalid",
                "errors": list(errors),
            },
        )
        return 1
    return 0


def _producer_errors(
    identity: str,
    version: str,
    remaining: tuple[int, ...],
    *,
    forbidden_absent: bool,
    update_probe: dict[str, JsonValue],
) -> list[str]:
    errors: list[str] = []
    if identity != "saxo-bank-mcp" or not version:
        errors.append("identity_version_mismatch")
    if remaining:
        errors.append("process_cleanup_incomplete")
    if not forbidden_absent:
        errors.append("forbidden_cache_paths_present")
    if update_probe.get("codex_reached_bumped") is not True:
        errors.append("codex_update_version_not_reached")
    if update_probe.get("claude_reached_bumped") is not True:
        errors.append("claude_update_version_not_reached")
    if update_probe.get("candidate_restored") is not True:
        errors.append("update_probe_restore_missing")
    return errors


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
