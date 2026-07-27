from __future__ import annotations

import contextlib
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    cleanup_recorded_groups,
    remaining_live_pgids,
    remaining_live_pids,
)
from saxo_bank_mcp.agent_skill_evidence_io import resolve_commit
from saxo_bank_mcp.agent_skill_install_cli_driver import (
    CommandDiscoveryError,
    build_client_report,
    clone_candidate,
    copy_auth_files,
    discover_claude_cache,
    discover_cli_help,
    discover_codex_cache,
    help_syntax_evidence,
    identity_version,
    isolated_env,
    parse_claude_details_skills,
    project_version,
    require_distinct_caches,
    required_install_receipt_names,
    run_claude_install,
    run_codex_install,
    run_update_probe,
    startup_from_probes,
)
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt, InstallManifestOptions
from saxo_bank_mcp.agent_skill_install_paths import (
    assert_preserved_modes,
    ensure_owner_only,
    export_publishable_tree,
    global_state_fingerprint,
    installed_inventory_check,
    owner_only_from_modes,
)
from saxo_bank_mcp.agent_skill_install_probe import probe_root_stdio
from saxo_bank_mcp.agent_skill_install_qa import (
    EXPECTED_MCP_SERVER_COUNT,
    load_verified_install_report,
    planning_error,
)


def real_install_report(options: InstallManifestOptions) -> int:  # noqa: C901, PLR0911, PLR0915
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
        ensure_owner_only(root)

    before_scope = global_state_fingerprint(
        options.codex_global_home,
        options.claude_global_home,
    )
    before = {"codex": before_scope["codex"], "claude": before_scope["claude"]}
    scope = before_scope.get("scope", {})
    observed_pids: list[int] = []
    observed_pgids: list[int] = []
    temporary_paths = [marketplace, probe_env]
    git_receipts: tuple[CommandReceipt, ...] = ()
    help_receipts: tuple[CommandResult, ...] = ()
    codex_receipts: tuple[CommandResult, ...] = ()
    claude_receipts: tuple[CommandResult, ...] = ()
    update_receipts: tuple[CommandReceipt, ...] = ()
    try:
        git_receipts = clone_candidate(options.repo, clone, commit)
        version = project_version(clone)
        export_publishable_tree(clone, marketplace)
        ensure_owner_only(marketplace)
        auth_files, auth_targets = copy_auth_files(home)
        codex_env = isolated_env(
            home=home,
            codex_home=codex_home,
            run_root=run_root,
            probe_env=probe_env,
            auth_targets=auth_targets,
        )
        claude_env = isolated_env(
            home=home,
            codex_home=codex_home,
            run_root=run_root,
            probe_env=probe_env,
            auth_targets=auth_targets,
        )
        help_receipts = discover_cli_help(clone, codex_env, claude_env)
        codex_receipts = run_codex_install(marketplace, codex_env)
        claude_receipts = run_claude_install(marketplace, claude_env)
        codex_cache, codex_cache_source = discover_codex_cache(
            codex_receipts,
            run_root=run_root,
            codex_home=codex_home,
            expected_version=version,
        )
        claude_cache, claude_cache_source = discover_claude_cache(
            claude_receipts,
            run_root=run_root,
            home=home,
            expected_version=version,
        )
        require_distinct_caches(codex_cache, claude_cache)
        ensure_owner_only(codex_cache)
        ensure_owner_only(claude_cache)

        update_probe, update_receipts, codex_cache, claude_cache = run_update_probe(
            marketplace,
            codex_env,
            claude_env,
            run_root=run_root,
            clone=clone,
            expected_version=version,
        )
        codex_cache_source = "codex_plugin_add_restored"
        claude_cache_source = "claude_plugin_list_restored"
        ensure_owner_only(codex_cache)
        ensure_owner_only(claude_cache)

        source_probe = probe_root_stdio(
            "source_mcp_probe",
            clone,
            env=codex_env,
            probe_env=probe_env,
        )
        codex_probe = probe_root_stdio(
            "codex_cache_mcp_probe",
            codex_cache,
            env=codex_env,
            probe_env=probe_env,
        )
        claude_probe = probe_root_stdio(
            "claude_cache_mcp_probe",
            claude_cache,
            env=claude_env,
            probe_env=probe_env,
        )
        # Independent list_tools probes (not a duplicated cache payload claim).
        codex_list_tools = probe_root_stdio(
            "codex_list_tools_probe",
            codex_cache,
            env=codex_env,
            probe_env=probe_env,
        )
        claude_list_tools = probe_root_stdio(
            "claude_list_tools_probe",
            claude_cache,
            env=claude_env,
            probe_env=probe_env,
        )
        codex_startup, codex_source_missing, codex_cache_missing = startup_from_probes(
            source_probe,
            codex_probe,
            codex_list_tools,
        )
        claude_startup, claude_source_missing, claude_cache_missing = startup_from_probes(
            source_probe,
            claude_probe,
            claude_list_tools,
        )
        details_count, _ = parse_claude_details_skills(claude_receipts[-1])
        codex_inventory = installed_inventory_check(clone, codex_cache)
        claude_inventory = installed_inventory_check(clone, claude_cache)

        all_results = (
            *help_receipts,
            *codex_receipts,
            *claude_receipts,
            source_probe,
            codex_probe,
            claude_probe,
            codex_list_tools,
            claude_list_tools,
        )
        command_receipts = (
            *git_receipts,
            *(result.receipt for result in all_results),
            *update_receipts,
        )
        observed_pids = [receipt.pid for receipt in command_receipts if receipt.pid is not None]
        observed_pgids = [receipt.pgid for receipt in command_receipts if receipt.pgid is not None]
    except CommandFailureError as exc:
        _cleanup_temps(temporary_paths)
        cleanup_recorded_groups(tuple(observed_pgids))
        write_json(
            options.out,
            {
                "status": "failed",
                "reason": "producer_command_failed",
                "command": exc.receipt.model_dump(mode="json"),
            },
        )
        return 1
    except (CommandDiscoveryError, FileNotFoundError, PermissionError, ValueError) as exc:
        _cleanup_temps(temporary_paths)
        cleanup_recorded_groups(tuple(observed_pgids))
        reason = getattr(exc, "reason", type(exc).__name__)
        write_json(options.out, {"status": "failed", "reason": str(reason)})
        return 1

    after_scope = global_state_fingerprint(
        options.codex_global_home,
        options.claude_global_home,
    )
    after = {"codex": after_scope["codex"], "claude": after_scope["claude"]}
    _cleanup_temps(temporary_paths)
    remaining_temps = [path for path in temporary_paths if path.exists()]
    remaining_pids = remaining_live_pids(tuple(observed_pids))
    remaining_pgids = remaining_live_pgids(tuple(observed_pgids))
    if remaining_pgids:
        remaining_pgids = cleanup_recorded_groups(tuple(remaining_pgids))
        remaining_pids = remaining_live_pids(tuple(observed_pids))

    try:
        preserved_modes = assert_preserved_modes(
            {
                "run_root": run_root,
                "clone": clone,
                "codex_cache": codex_cache,
                "claude_cache": claude_cache,
                "home": home,
                "codex_home": codex_home,
                "claude_home": claude_home,
            },
        )
    except (FileNotFoundError, PermissionError) as exc:
        write_json(
            options.out,
            {"status": "failed", "reason": "preserved_roots_not_owner_only", "error": str(exc)},
        )
        return 1

    owner_only = owner_only_from_modes(preserved_modes)
    identity, version = identity_version(clone)
    byte_mismatches = [f"codex:{item}" for item in _json_list(codex_inventory, "mismatches")] + [
        f"claude:{item}" for item in _json_list(claude_inventory, "mismatches")
    ]
    forbidden_absent = bool(codex_inventory["forbidden_files_absent"]) and bool(
        claude_inventory["forbidden_files_absent"],
    )
    inventory_exact = (
        codex_inventory.get("inventory_exact_match") is True
        and claude_inventory.get("inventory_exact_match") is True
    )
    preserve_for = options.preserve_for or "unspecified"
    repo_root = options.repo.resolve()
    repo_field = "." if repo_root == Path.cwd().resolve() else str(repo_root)
    path_roots = _path_publish_roots(repo_root=repo_root, repo_field=repo_field, run_root=run_root)

    def pub(path: Path) -> str:
        return _public_path(path, path_roots)

    codex_client = build_client_report(
        cache=codex_cache,
        cache_source=codex_cache_source,
        startup=codex_startup,
        source_missing=codex_source_missing,
        cache_missing=codex_cache_missing,
        receipts=(
            *git_receipts,
            *(result.receipt for result in codex_receipts),
            source_probe.receipt,
            codex_probe.receipt,
            codex_list_tools.receipt,
        ),
        inventory=codex_inventory,
        details_skill_count=None,
    )
    claude_client = build_client_report(
        cache=claude_cache,
        cache_source=claude_cache_source,
        startup=claude_startup,
        source_missing=claude_source_missing,
        cache_missing=claude_cache_missing,
        receipts=(
            *git_receipts,
            *(result.receipt for result in claude_receipts),
            source_probe.receipt,
            claude_probe.receipt,
            claude_list_tools.receipt,
        ),
        inventory=claude_inventory,
        details_skill_count=details_count,
    )
    codex_client = _sanitize_client_report(codex_client, path_roots)
    claude_client = _sanitize_client_report(claude_client, path_roots)
    sanitized_update = _sanitize_json_paths(dict(update_probe), path_roots)
    update_probe = dict(sanitized_update) if isinstance(sanitized_update, dict) else {}
    update_probe["temporary_fixtures_removed"] = (
        bool(update_probe.get("temporary_fixtures_removed")) and not remaining_temps
    )

    report: dict[str, JsonValue] = {
        "status": "passed",
        "execution_mode": "installed_verification",
        "repo": repo_field,
        "candidate_commit": commit,
        "clone": {
            "path": pub(clone),
            "commit": commit,
            "source_repo": repo_field,
            "no_local": True,
            "clean": True,
            "mode": preserved_modes["clone"],
        },
        "expected_skills": options.expected_skills,
        "expected_mcp_servers": EXPECTED_MCP_SERVER_COUNT,
        "expected_tools": options.expected_tools,
        "global_state": {"before": before, "after": after, "scope": scope},
        "global_state_unchanged": before == after,
        "project_version": project_version(clone),
        "help_syntax": help_syntax_evidence(help_receipts),
        "update_probe": update_probe,
        "auth_files": _sanitize_auth_files(auth_files, path_roots),
        "codex": codex_client,
        "claude": claude_client,
        "installed_byte_checks": {
            "complete": True,
            "compared_files": _json_int(codex_inventory, "compared_files")
            + _json_int(claude_inventory, "compared_files"),
            "metadata_exceptions": sorted(
                set(_json_list(codex_inventory, "metadata_exceptions"))
                | set(_json_list(claude_inventory, "metadata_exceptions")),
            ),
            "required_files_present": sorted(
                set(_json_list(codex_inventory, "required_files_present"))
                & set(_json_list(claude_inventory, "required_files_present")),
            ),
            "forbidden_files_absent": forbidden_absent,
            "mismatches": byte_mismatches,
            "inventory_exact_match": inventory_exact,
        },
        "process_cleanup": {
            "complete": not remaining_pids and not remaining_pgids,
            "remaining_pids": list(remaining_pids),
            "remaining_pgids": list(remaining_pgids),
            "observed_pids": sorted(set(observed_pids)),
            "observed_pgids": sorted(set(observed_pgids)),
        },
        "fixture_cleanup": {
            "deferred_registered": bool(options.preserve_for),
            "preserve_for": preserve_for,
            "run_root": pub(run_root),
            "preserved_paths": [
                pub(clone),
                pub(codex_cache),
                pub(claude_cache),
                pub(home),
                pub(codex_home),
                pub(claude_home),
            ],
            "modes": preserved_modes,
            "owner_only": owner_only,
            "teardown_owner": "post-final-completion-gate",
            "consumers": [item.strip() for item in preserve_for.split(",") if item.strip()],
        },
        "help_receipts": [
            _sanitize_receipt(result.receipt.model_dump(mode="json"), path_roots)
            for result in help_receipts
        ],
        "update_receipts": [
            _sanitize_receipt(receipt.model_dump(mode="json"), path_roots)
            for receipt in update_receipts
        ],
        "required_receipts": list(required_install_receipt_names()),
        "errors": _producer_errors(
            identity,
            version,
            remaining_pids,
            remaining_pgids,
            forbidden_absent=forbidden_absent,
            inventory_exact=inventory_exact,
            update_probe=update_probe,
            owner_only=owner_only,
        ),
    }
    write_json(options.out, report)
    verified, errors = load_verified_install_report(options.out)
    if verified is None:
        failed = dict(report)
        failed["status"] = "failed"
        failed["reason"] = "producer_evidence_invalid"
        failed["errors"] = list(errors)
        write_json(options.out, failed)
        return 1
    return 0


def _cleanup_temps(paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def _producer_errors(  # noqa: PLR0913
    identity: str,
    version: str,
    remaining_pids: tuple[int, ...],
    remaining_pgids: tuple[int, ...],
    *,
    forbidden_absent: bool,
    inventory_exact: bool,
    update_probe: dict[str, JsonValue],
    owner_only: bool,
) -> list[str]:
    errors: list[str] = []
    if identity != "saxo-bank-mcp" or not version:
        errors.append("identity_version_mismatch")
    if remaining_pids or remaining_pgids:
        errors.append("process_cleanup_incomplete")
    if not forbidden_absent:
        errors.append("forbidden_cache_paths_present")
    if not inventory_exact:
        errors.append("inventory_not_exact")
    if update_probe.get("codex_reached_bumped") is not True:
        errors.append("codex_update_version_not_reached")
    if update_probe.get("claude_reached_bumped") is not True:
        errors.append("claude_update_version_not_reached")
    if update_probe.get("candidate_restored") is not True:
        errors.append("update_probe_restore_missing")
    if update_probe.get("temporary_fixtures_removed") is not True:
        errors.append("temporary_fixtures_remain")
    if not owner_only:
        errors.append("preserved_roots_not_owner_only")
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


@dataclass(frozen=True, slots=True)
class _PathPublishRoots:
    repo_root: Path
    repo_field: str
    run_root: Path
    run_root_field: str | None


_PRIVATE_ROOT_RE = re.compile(r"(?:/Users/|/private/|/Volumes/)")


def _path_publish_roots(
    *,
    repo_root: Path,
    repo_field: str,
    run_root: Path,
) -> _PathPublishRoots:
    resolved_run = run_root.resolve()
    if resolved_run == repo_root or resolved_run.is_relative_to(repo_root):
        return _PathPublishRoots(
            repo_root=repo_root,
            repo_field=repo_field,
            run_root=resolved_run,
            run_root_field=None,
        )
    return _PathPublishRoots(
        repo_root=repo_root,
        repo_field=repo_field,
        run_root=resolved_run,
        run_root_field=str(resolved_run),
    )


def _public_path(path: Path, roots: _PathPublishRoots) -> str:
    resolved = path.expanduser().resolve()
    if resolved == roots.repo_root or resolved.is_relative_to(roots.repo_root):
        relative = resolved.relative_to(roots.repo_root)
        if roots.repo_field == ".":
            return "." if str(relative) == "." else str(relative)
        return str(Path(roots.repo_field) / relative) if str(relative) != "." else roots.repo_field
    if roots.run_root_field is not None and (
        resolved == roots.run_root or resolved.is_relative_to(roots.run_root)
    ):
        relative = resolved.relative_to(roots.run_root)
        if str(relative) == ".":
            return roots.run_root_field
        return str(Path(roots.run_root_field) / relative)
    return f"<redacted-path>/{resolved.name}"


def _sanitize_text(value: str, roots: _PathPublishRoots) -> str:
    replacements: list[tuple[str, str]] = []
    for root, label in (
        (roots.repo_root, roots.repo_field if roots.repo_field != "." else "."),
        (
            roots.run_root,
            roots.run_root_field
            if roots.run_root_field is not None
            else (
                str(roots.run_root.relative_to(roots.repo_root))
                if roots.run_root.is_relative_to(roots.repo_root)
                else None
            ),
        ),
    ):
        if label is None:
            continue
        replacements.append((str(root), label))
        with contextlib.suppress(OSError):
            replacements.append((str(root.resolve()), label))
    # Longer absolute roots first so nested paths rewrite cleanly.
    scrubbed = value
    for absolute, label in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
        scrubbed = scrubbed.replace(absolute, label)
    if _PRIVATE_ROOT_RE.search(scrubbed):
        scrubbed = _PRIVATE_ROOT_RE.sub("<redacted-root>/", scrubbed)
    return scrubbed


def _sanitize_json_paths(value: JsonValue, roots: _PathPublishRoots) -> JsonValue:
    if isinstance(value, dict):
        return {key: _sanitize_json_paths(item, roots) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_json_paths(item, roots) for item in value]
    if isinstance(value, str):
        candidate = Path(value)
        if value.startswith(("/", str(roots.repo_root), str(roots.run_root))) and (
            candidate.exists() or value.startswith(("/", str(roots.repo_root), str(roots.run_root)))
        ):
            try:
                if value.startswith(("/", str(roots.repo_root), str(roots.run_root))):
                    return _sanitize_text(value, roots)
            except OSError:
                return _sanitize_text(value, roots)
        return _sanitize_text(value, roots)
    return value


def _sanitize_client_report(
    report: dict[str, JsonValue],
    roots: _PathPublishRoots,
) -> dict[str, JsonValue]:
    sanitized = dict(report)
    cache_root = report.get("cache_root")
    if isinstance(cache_root, str):
        sanitized["cache_root"] = _public_path(Path(cache_root), roots)
    receipts = report.get("command_receipts")
    if isinstance(receipts, list):
        sanitized["command_receipts"] = [
            _sanitize_receipt(item, roots) if isinstance(item, dict) else item for item in receipts
        ]
    return sanitized


def _sanitize_receipt(
    receipt: dict[str, JsonValue],
    roots: _PathPublishRoots,
) -> dict[str, JsonValue]:
    sanitized = dict(receipt)
    cwd = receipt.get("cwd")
    if isinstance(cwd, str):
        sanitized["cwd"] = _public_path(Path(cwd), roots)
    argv = receipt.get("argv")
    if isinstance(argv, list):
        sanitized["argv"] = [
            _sanitize_text(item, roots) if isinstance(item, str) else item for item in argv
        ]
    return sanitized


def _sanitize_auth_files(
    auth_files: dict[str, JsonValue],
    roots: _PathPublishRoots,
) -> dict[str, JsonValue]:
    copied = auth_files.get("copied")
    if not isinstance(copied, list):
        return auth_files
    sanitized_copied: list[JsonValue] = []
    for item in copied:
        if not isinstance(item, dict):
            continue
        row = dict(item)
        target = row.get("target")
        if isinstance(target, str):
            row["target"] = _public_path(Path(target), roots)
        sanitized_copied.append(row)
    return {"copied": sanitized_copied, "values_published": False}
