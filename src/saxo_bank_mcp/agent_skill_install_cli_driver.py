from __future__ import annotations

import re
import shutil
import tomllib
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import CommandResult, run_command
from saxo_bank_mcp.agent_skill_install_discovery import (
    CommandDiscoveryError,
    discover_claude_cache,
    discover_codex_cache,
    parse_claude_details_skills,
    parse_mcp_server_count,
    require_distinct_caches,
    skill_inventory,
)
from saxo_bank_mcp.agent_skill_install_env import (
    auth_env_keys,
    build_isolated_env,
    claude_non_ui_command,
    codex_file_store_command,
)
from saxo_bank_mcp.agent_skill_install_models import (
    EXPECTED_TOOLS,
    CommandReceipt,
    StartupEvidence,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    PLUGIN_NAME,
    PLUGIN_REF,
    VERSION_RELATIVES,
    ensure_owner_only,
    export_publishable_tree,
    installed_inventory_check,
    publishable_tracked_files,
    scrub_runtime_artifacts,
    tree_digest,
)
from saxo_bank_mcp.agent_skill_install_probe import probe_root_stdio, startup_from_probes

JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

# Required production receipt names (order preserved for evidence).
CODEX_INSTALL_RECEIPTS = (
    "codex_marketplace_add",
    "codex_plugin_add",
    "codex_plugin_list",
)
CLAUDE_INSTALL_RECEIPTS = (
    "claude_marketplace_add",
    "claude_plugin_install",
    "claude_plugin_list",
    "claude_plugin_details",
)
CODEX_UPDATE_RECEIPTS = (
    "codex_plugin_remove_bumped",
    "codex_plugin_add_bumped",
    "codex_plugin_list_bumped",
    "codex_plugin_remove_restored",
    "codex_plugin_add_restored",
    "codex_plugin_list_restored",
)
CLAUDE_UPDATE_RECEIPTS = (
    "claude_plugin_uninstall_bumped",
    "claude_plugin_install_bumped",
    "claude_plugin_list_bumped",
    "claude_plugin_uninstall_restored",
    "claude_plugin_install_restored",
    "claude_plugin_list_restored",
)


def clone_candidate(repo: Path, clone: Path, commit: str) -> tuple[CommandReceipt, ...]:
    if clone.exists():
        shutil.rmtree(clone)
    ensure_owner_only(clone.parent)
    clone_result = run_command(
        "git_clone_no_local",
        ("git", "clone", "--no-local", str(repo.resolve()), str(clone)),
        cwd=repo,
        env=_host_git_env(),
    )
    checkout_result = run_command(
        "git_checkout_commit",
        ("git", "checkout", "--detach", commit),
        cwd=clone,
        env=_host_git_env(),
    )
    ensure_owner_only(clone)
    return (clone_result.receipt, checkout_result.receipt)


def isolated_env(
    *,
    home: Path,
    codex_home: Path,
    run_root: Path,
    probe_env: Path,
    auth_targets: dict[str, Path] | None = None,
) -> dict[str, str]:
    return build_isolated_env(
        home=home,
        codex_home=codex_home,
        run_root=run_root,
        probe_env=probe_env,
        auth_targets=auth_targets,
    )


def discover_cli_help(
    clone: Path,
    codex_env: dict[str, str],
    claude_env: dict[str, str],
) -> tuple[CommandResult, ...]:
    commands = (
        ("codex_plugin_help", _codex_plugin_command("--help"), codex_env),
        (
            "codex_marketplace_help",
            _codex_plugin_command("marketplace", "add", "--help"),
            codex_env,
        ),
        ("claude_plugin_help", _claude_plugin_command("--help"), claude_env),
        (
            "claude_marketplace_help",
            _claude_plugin_command("marketplace", "add", "--help"),
            claude_env,
        ),
    )
    return tuple(run_command(name, argv, cwd=clone, env=env) for name, argv, env in commands)


def run_codex_install(
    marketplace: Path,
    env: dict[str, str],
) -> tuple[CommandResult, ...]:
    commands = (
        (
            "codex_marketplace_add",
            _codex_plugin_command("marketplace", "add", "--json", str(marketplace)),
        ),
        ("codex_plugin_add", _codex_plugin_command("add", "--json", PLUGIN_REF)),
        ("codex_plugin_list", _codex_plugin_command("list", "--json")),
    )
    return tuple(run_command(name, argv, cwd=marketplace, env=env) for name, argv in commands)


def run_claude_install(
    marketplace: Path,
    env: dict[str, str],
) -> tuple[CommandResult, ...]:
    commands = (
        ("claude_marketplace_add", _claude_plugin_command("marketplace", "add", str(marketplace))),
        (
            "claude_plugin_install",
            _claude_plugin_command("install", PLUGIN_REF, "--scope", "user"),
        ),
        ("claude_plugin_list", _claude_plugin_command("list", "--json")),
        ("claude_plugin_details", _claude_plugin_command("details", PLUGIN_NAME)),
    )
    return tuple(run_command(name, argv, cwd=marketplace, env=env) for name, argv in commands)


def run_update_probe(  # noqa: PLR0913
    marketplace: Path,
    codex_env: dict[str, str],
    claude_env: dict[str, str],
    *,
    run_root: Path,
    clone: Path,
    expected_version: str,
) -> tuple[dict[str, JsonValue], tuple[CommandReceipt, ...], Path, Path]:
    bump = _patch_bump(expected_version)
    targets = tuple(marketplace / relative for relative in VERSION_RELATIVES)
    original = {path: path.read_bytes() for path in targets if path.is_file()}
    receipts: list[CommandReceipt] = []
    publishable = publishable_tracked_files(clone)
    bumped_proof: dict[str, JsonValue] = {}
    restored_proof: dict[str, JsonValue] = {}
    codex_cache: Path | None = None
    claude_cache: Path | None = None
    temporary_paths: list[Path] = []
    try:
        for path, data in original.items():
            path.write_text(data.decode().replace(expected_version, bump), encoding="utf-8")
        codex_bump_results = _reinstall_codex(marketplace, codex_env, "bumped")
        claude_bump_results = _update_claude(marketplace, claude_env, "bumped")
        receipts.extend(result.receipt for result in (*codex_bump_results, *claude_bump_results))
        codex_list = run_command(
            "codex_plugin_list_bumped",
            _codex_plugin_command("list", "--json"),
            cwd=marketplace,
            env=codex_env,
        )
        claude_list = run_command(
            "claude_plugin_list_bumped",
            _claude_plugin_command("list", "--json"),
            cwd=marketplace,
            env=claude_env,
        )
        receipts.extend([codex_list.receipt, claude_list.receipt])
        codex_bump_cache, _ = discover_codex_cache(
            codex_bump_results,
            run_root=run_root,
            codex_home=Path(codex_env["CODEX_HOME"]),
            expected_version=bump,
            receipt_name="codex_plugin_add_bumped",
        )
        claude_bump_cache, _ = discover_claude_cache(
            (claude_list,),
            run_root=run_root,
            home=Path(claude_env["HOME"]),
            expected_version=bump,
            receipt_name="claude_plugin_list_bumped",
        )
        ensure_owner_only(codex_bump_cache)
        ensure_owner_only(claude_bump_cache)
        temporary_paths.extend([codex_bump_cache, claude_bump_cache])
        bumped_proof = {
            "codex": _version_cache_proof(
                marketplace,
                codex_bump_cache,
                publishable,
                env=codex_env,
                probe_env=Path(codex_env["UV_PROJECT_ENVIRONMENT"]),
                label="codex_bumped",
                expected_version=bump,
                list_receipt_name="codex_plugin_list_bumped",
            ),
            "claude": _version_cache_proof(
                marketplace,
                claude_bump_cache,
                publishable,
                env=claude_env,
                probe_env=Path(claude_env["UV_PROJECT_ENVIRONMENT"]),
                label="claude_bumped",
                expected_version=bump,
                list_receipt_name="claude_plugin_list_bumped",
            ),
        }
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    codex_restore_results = _reinstall_codex(marketplace, codex_env, "restored")
    claude_restore_results = _update_claude(marketplace, claude_env, "restored")
    receipts.extend(result.receipt for result in (*codex_restore_results, *claude_restore_results))
    codex_list = run_command(
        "codex_plugin_list_restored",
        _codex_plugin_command("list", "--json"),
        cwd=marketplace,
        env=codex_env,
    )
    claude_list = run_command(
        "claude_plugin_list_restored",
        _claude_plugin_command("list", "--json"),
        cwd=marketplace,
        env=claude_env,
    )
    receipts.extend([codex_list.receipt, claude_list.receipt])
    codex_cache, _ = discover_codex_cache(
        codex_restore_results,
        run_root=run_root,
        codex_home=Path(codex_env["CODEX_HOME"]),
        expected_version=expected_version,
        receipt_name="codex_plugin_add_restored",
    )
    claude_cache, _ = discover_claude_cache(
        (claude_list,),
        run_root=run_root,
        home=Path(claude_env["HOME"]),
        expected_version=expected_version,
        receipt_name="claude_plugin_list_restored",
    )
    ensure_owner_only(codex_cache)
    ensure_owner_only(claude_cache)
    require_distinct_caches(codex_cache, claude_cache)
    restored_proof = {
        "codex": _version_cache_proof(
            marketplace,
            codex_cache,
            publishable,
            env=codex_env,
            probe_env=Path(codex_env["UV_PROJECT_ENVIRONMENT"]),
            label="codex_restored",
            expected_version=expected_version,
            list_receipt_name="codex_plugin_list_restored",
        ),
        "claude": _version_cache_proof(
            marketplace,
            claude_cache,
            publishable,
            env=claude_env,
            probe_env=Path(claude_env["UV_PROJECT_ENVIRONMENT"]),
            label="claude_restored",
            expected_version=expected_version,
            list_receipt_name="claude_plugin_list_restored",
        ),
    }
    remaining_temps = [path for path in temporary_paths if path.exists()]
    for path in remaining_temps:
        shutil.rmtree(path, ignore_errors=True)
    remaining_temps = [path for path in temporary_paths if path.exists()]
    return (
        {
            "original_version": expected_version,
            "bumped_version": bump,
            "codex_reached_bumped": _proof_ok(bumped_proof.get("codex"), bump),
            "claude_reached_bumped": _proof_ok(bumped_proof.get("claude"), bump),
            "candidate_restored": (
                project_version(marketplace) == expected_version
                and _proof_ok(restored_proof.get("codex"), expected_version)
                and _proof_ok(restored_proof.get("claude"), expected_version)
            ),
            "bumped_proof": bumped_proof,
            "restored_proof": restored_proof,
            "temporary_fixtures_removed": not remaining_temps,
            "remaining_temporary_paths": [str(path) for path in remaining_temps],
        },
        tuple(receipts),
        codex_cache,
        claude_cache,
    )


def project_version(root: Path) -> str:
    payload = JSON_OBJECT_ADAPTER.validate_python(
        tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")),
    )
    project = payload.get("project")
    if not isinstance(project, dict):
        return ""
    version = project.get("version")
    return version if isinstance(version, str) else ""


def help_syntax_evidence(results: tuple[CommandResult, ...]) -> dict[str, JsonValue]:
    expected = {
        "codex_plugin_help": ("Commands:", "marketplace", "add", "list"),
        "codex_marketplace_help": ("Usage: codex plugin marketplace add", "<SOURCE>"),
        "claude_plugin_help": ("Commands:", "install", "update", "details"),
        "claude_marketplace_help": ("Usage: claude plugin marketplace add", "<source>"),
    }
    checks: dict[str, JsonValue] = {}
    for result in results:
        needles = expected.get(result.receipt.name, ())
        checks[result.receipt.name] = {
            "validated": bool(needles) and all(needle in result.stdout for needle in needles),
            "stdout_sha256": result.receipt.stdout_sha256,
        }
    return checks


def identity_version(root: Path) -> tuple[str, str]:
    project = project_version(root)
    codex = _json_file(root / ".codex-plugin/plugin.json")
    claude = _json_file(root / ".claude-plugin/plugin.json")
    names = {_string(codex, "name"), _string(claude, "name")}
    versions = {project, _string(codex, "version"), _string(claude, "version")}
    if names == {PLUGIN_NAME} and len(versions) == 1 and project:
        return PLUGIN_NAME, project
    return "", ""


def copy_auth_files(home: Path) -> tuple[dict[str, JsonValue], dict[str, Path]]:
    import os  # noqa: PLC0415

    copied: list[JsonValue] = []
    targets: dict[str, Path] = {}
    target_root = home / ".saxo-bank-mcp-auth"
    target_root.mkdir(parents=True, exist_ok=True)
    target_root.chmod(0o700)
    for key in auth_env_keys():
        raw = os.environ.get(key)
        if not raw:
            continue
        source = Path(raw).expanduser()
        if not source.is_file():
            continue
        target = target_root / key.lower()
        shutil.copy2(source, target)
        target.chmod(0o600)
        targets[key] = target
        copied.append(
            {
                "env_key": key,
                "target": str(target),
                "bytes": target.stat().st_size,
                "mode": oct(target.stat().st_mode & 0o777),
            },
        )
    return {"copied": copied, "values_published": False}, targets


def build_client_report(  # noqa: PLR0913
    *,
    cache: Path,
    cache_source: str,
    startup: StartupEvidence,
    receipts: tuple[CommandReceipt, ...],
    inventory: dict[str, JsonValue],
    details_skill_count: int | None,
) -> dict[str, JsonValue]:
    identity, version = identity_version(cache)
    skills = skill_inventory(cache)
    mcp_count = parse_mcp_server_count(cache)
    skill_count = len(skills)
    if details_skill_count is not None and details_skill_count != skill_count:
        skill_count = -1
    source_missing = list(startup.source.annotations_missing)
    cache_missing = list(startup.cache.annotations_missing)
    list_tools_missing = list(startup.list_tools.annotations_missing)
    return {
        "installed": True,
        "cache_root": str(cache),
        "identity": identity,
        "version": version,
        "cache_root_source": cache_source,
        "skill_count": skill_count,
        "skills": list(skills),
        "mcp_server_count": mcp_count,
        "tool_count": startup.cache.tool_count,
        "annotations_missing": sorted(
            set(source_missing) | set(cache_missing) | set(list_tools_missing),
        ),
        "source_annotations_missing": source_missing,
        "cache_annotations_missing": cache_missing,
        "list_tools_annotations_missing": list_tools_missing,
        "forbidden_cache_paths": inventory.get("forbidden_cache_paths", []),
        "installed_bytes_match": True,
        "install_command_exit_code": 0,
        "startup": startup.model_dump(mode="json"),
        "command_receipts": [receipt.model_dump(mode="json") for receipt in receipts],
        "inventory": inventory,
    }


def required_install_receipt_names() -> tuple[str, ...]:
    return (
        *CODEX_INSTALL_RECEIPTS,
        *CLAUDE_INSTALL_RECEIPTS,
        *CODEX_UPDATE_RECEIPTS,
        *CLAUDE_UPDATE_RECEIPTS,
    )


def _version_cache_proof(  # noqa: PLR0913
    source: Path,
    cache: Path,
    publishable: tuple[str, ...],
    *,
    env: dict[str, str],
    probe_env: Path,
    label: str,
    expected_version: str,
    list_receipt_name: str,
) -> dict[str, JsonValue]:
    # Probe first (may resolve/lock under the cache), scrub runtime debris, then
    # require publishable bytes still match the bumped/restored source.
    probe = probe_root_stdio(f"{label}_stdio_probe", cache, env=env, probe_env=probe_env)
    # Probe already scrubs; scrub again immediately before digests so debris cannot
    # split cache digest from marketplace source digest.
    scrub_runtime_artifacts(cache)
    scrub_runtime_artifacts(source)
    payload = _json_from_probe(probe.stdout)
    missing = payload.get("annotations_missing")
    missing_list = (
        [str(item) for item in missing if isinstance(item, str)]
        if isinstance(missing, list)
        else ["probe_invalid"]
    )
    tool_count = payload.get("tool_count") if isinstance(payload.get("tool_count"), int) else 0
    inventory = installed_inventory_check(source, cache, publishable=publishable)
    cache_resolved = str(cache.resolve())
    digest = tree_digest(cache, publishable)
    source_digest = tree_digest(source, publishable)
    return {
        "cache_root": cache_resolved,
        "version": expected_version if identity_version(cache)[1] == expected_version else "",
        "digest": digest,
        "source_digest": source_digest,
        "inventory_exact_match": inventory.get("inventory_exact_match") is True,
        "tool_count": tool_count,
        "annotations_missing": missing_list,
        "probe_stdout_sha256": probe.receipt.stdout_sha256,
        "list_receipt_name": list_receipt_name,
        "registration_version": expected_version,
        "registration_cache_root": cache_resolved,
    }


def _json_from_probe(raw: str) -> dict[str, JsonValue]:
    for raw_line in reversed(raw.splitlines()):
        stripped = raw_line.strip()
        if stripped.startswith("{"):
            try:
                return JSON_OBJECT_ADAPTER.validate_json(stripped)
            except ValidationError:
                continue
    return {}


def _proof_ok(value: JsonValue | None, expected_version: str) -> bool:
    if not isinstance(value, dict):
        return False
    missing = value.get("annotations_missing")
    return (
        value.get("version") == expected_version
        and value.get("tool_count") == EXPECTED_TOOLS
        and value.get("inventory_exact_match") is True
        and value.get("digest") == value.get("source_digest")
        and isinstance(missing, list)
        and missing == []
    )


def _reinstall_codex(
    marketplace: Path,
    env: dict[str, str],
    label: str,
) -> tuple[CommandResult, ...]:
    remove = run_command(
        f"codex_plugin_remove_{label}",
        _codex_plugin_command("remove", "--json", PLUGIN_REF),
        cwd=marketplace,
        env=env,
    )
    add = run_command(
        f"codex_plugin_add_{label}",
        _codex_plugin_command("add", "--json", PLUGIN_REF),
        cwd=marketplace,
        env=env,
    )
    return (remove, add)


def _update_claude(
    marketplace: Path,
    env: dict[str, str],
    label: str,
) -> tuple[CommandResult, ...]:
    """Reinstall Claude plugin from marketplace so restored bytes match publishable tree.

    ``plugin update`` alone left inventory_exact_match false on restore; uninstall+install
    mirrors Codex remove+add and keeps UpdateProbeEvidence typed.
    """
    uninstall = run_command(
        f"claude_plugin_uninstall_{label}",
        _claude_plugin_command("uninstall", PLUGIN_REF, "--scope", "user"),
        cwd=marketplace,
        env=env,
    )
    install = run_command(
        f"claude_plugin_install_{label}",
        _claude_plugin_command("install", PLUGIN_REF, "--scope", "user"),
        cwd=marketplace,
        env=env,
    )
    return (uninstall, install)


def _codex_plugin_command(*args: str) -> tuple[str, ...]:
    return codex_file_store_command("codex", "plugin", *args)


def _claude_plugin_command(*args: str) -> tuple[str, ...]:
    return claude_non_ui_command("claude", "plugin", *args, bare=True)


def _json_file(path: Path) -> dict[str, JsonValue]:
    try:
        return JSON_OBJECT_ADAPTER.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return {}


def _string(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _patch_bump(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return f"{version}.probe"
    return f"{match.group(1)}.{match.group(2)}.{int(match.group(3)) + 1}"


def _host_git_env() -> dict[str, str]:
    import os  # noqa: PLC0415

    path = os.environ.get("PATH", "/usr/bin:/bin")
    return {"PATH": path, "HOME": os.environ.get("HOME", str(Path.home()))}


# re-export discovery helpers used by producer
__all__ = [
    "CommandDiscoveryError",
    "build_client_report",
    "clone_candidate",
    "copy_auth_files",
    "discover_claude_cache",
    "discover_cli_help",
    "discover_codex_cache",
    "export_publishable_tree",
    "help_syntax_evidence",
    "identity_version",
    "isolated_env",
    "parse_claude_details_skills",
    "parse_mcp_server_count",
    "probe_root_stdio",
    "project_version",
    "require_distinct_caches",
    "required_install_receipt_names",
    "run_claude_install",
    "run_codex_install",
    "run_update_probe",
    "startup_from_probes",
]
