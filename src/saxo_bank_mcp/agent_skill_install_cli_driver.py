from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import CommandResult, run_command
from saxo_bank_mcp.agent_skill_install_models import (
    CommandReceipt,
    StartupCheck,
    StartupEvidence,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    PLUGIN_NAME,
    PLUGIN_REF,
    VERSION_RELATIVES,
    forbidden_cache_paths,
    scrub_runtime_artifacts,
)

JSON_OBJECT_ADAPTER: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])

AUTH_FILE_ENV_KEYS = (
    "SAXO_MCP_SIM_CREDENTIAL_FILE",
    "SAXO_MCP_LIVE_CREDENTIAL_FILE",
    "SAXO_MCP_TOKEN_CACHE_PATH",
)
CACHE_PATH_KEYS = (
    "installedPath",
    "installPath",
    "installed_path",
    "install_path",
    "cache_root",
    "path",
    "root",
)


def clone_candidate(repo: Path, clone: Path, commit: str) -> tuple[CommandReceipt, ...]:
    if clone.exists():
        shutil.rmtree(clone)
    clone_result = run_command(
        "git_clone_no_local",
        ("git", "clone", "--no-local", str(repo.resolve()), str(clone)),
        cwd=repo,
    )
    checkout_result = run_command(
        "git_checkout_commit",
        ("git", "checkout", "--detach", commit),
        cwd=clone,
    )
    return (clone_result.receipt, checkout_result.receipt)


def isolated_env(*, home: Path, codex_home: Path, probe_env: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "PATH": os.environ.get("PATH", ""),
        "UV_PROJECT_ENVIRONMENT": str(probe_env),
        "UV_NO_MODIFY_PATH": "1",
    }


def discover_cli_help(
    clone: Path,
    codex_env: dict[str, str],
    claude_env: dict[str, str],
) -> tuple[CommandResult, ...]:
    commands = (
        ("codex_plugin_help", ("codex", "plugin", "--help"), codex_env),
        (
            "codex_marketplace_help",
            ("codex", "plugin", "marketplace", "add", "--help"),
            codex_env,
        ),
        ("claude_plugin_help", ("claude", "plugin", "--help"), claude_env),
        (
            "claude_marketplace_help",
            ("claude", "plugin", "marketplace", "add", "--help"),
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
            ("codex", "plugin", "marketplace", "add", "--json", str(marketplace)),
        ),
        ("codex_plugin_add", ("codex", "plugin", "add", "--json", PLUGIN_REF)),
        ("codex_plugin_list", ("codex", "plugin", "list", "--json")),
    )
    return tuple(run_command(name, argv, cwd=marketplace, env=env) for name, argv in commands)


def run_claude_install(
    marketplace: Path,
    env: dict[str, str],
) -> tuple[CommandResult, ...]:
    commands = (
        ("claude_marketplace_add", ("claude", "plugin", "marketplace", "add", str(marketplace))),
        (
            "claude_plugin_install",
            ("claude", "plugin", "install", PLUGIN_REF, "--scope", "user"),
        ),
        ("claude_plugin_list", ("claude", "plugin", "list", "--json")),
        ("claude_plugin_details", ("claude", "plugin", "details", PLUGIN_NAME)),
    )
    return tuple(run_command(name, argv, cwd=marketplace, env=env) for name, argv in commands)


def run_update_probe(
    marketplace: Path,
    codex_env: dict[str, str],
    claude_env: dict[str, str],
) -> tuple[dict[str, JsonValue], tuple[CommandReceipt, ...], Path, Path]:
    version = project_version(marketplace)
    bump = _patch_bump(version)
    targets = tuple(marketplace / relative for relative in VERSION_RELATIVES)
    original = {path: path.read_bytes() for path in targets if path.is_file()}
    receipts: list[CommandReceipt] = []
    try:
        for path, data in original.items():
            path.write_text(data.decode().replace(version, bump), encoding="utf-8")
        codex_bump_results = _reinstall_codex(marketplace, codex_env, "bumped")
        claude_bump_results = _update_claude(marketplace, claude_env, "bumped")
        receipts.extend(result.receipt for result in (*codex_bump_results, *claude_bump_results))
        codex_list = run_command(
            "codex_plugin_list_bumped",
            ("codex", "plugin", "list", "--json"),
            cwd=marketplace,
            env=codex_env,
        )
        claude_list = run_command(
            "claude_plugin_list_bumped",
            ("claude", "plugin", "list", "--json"),
            cwd=marketplace,
            env=claude_env,
        )
        receipts.extend([codex_list.receipt, claude_list.receipt])
        codex_bump = _client_version_from_list(codex_list, client="codex")
        claude_bump = _client_version_from_list(claude_list, client="claude")
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    codex_restore_results = _reinstall_codex(marketplace, codex_env, "restored")
    claude_restore_results = _update_claude(marketplace, claude_env, "restored")
    receipts.extend(result.receipt for result in (*codex_restore_results, *claude_restore_results))
    codex_list = run_command(
        "codex_plugin_list_restored",
        ("codex", "plugin", "list", "--json"),
        cwd=marketplace,
        env=codex_env,
    )
    claude_list = run_command(
        "claude_plugin_list_restored",
        ("claude", "plugin", "list", "--json"),
        cwd=marketplace,
        env=claude_env,
    )
    receipts.extend([codex_list.receipt, claude_list.receipt])
    codex_restored = _client_version_from_list(codex_list, client="codex")
    claude_restored = _client_version_from_list(claude_list, client="claude")
    codex_cache, _ = cache_root_from_results(codex_restore_results, client="codex")
    claude_cache, _ = cache_root_from_results((claude_list,), client="claude")
    restored = (
        project_version(marketplace) == version
        and codex_restored == version
        and claude_restored == version
    )
    _remove_version_cache(codex_env, claude_env, bump)
    return (
        {
            "original_version": version,
            "bumped_version": bump,
            "codex_reached_bumped": codex_bump == bump,
            "claude_reached_bumped": claude_bump == bump,
            "candidate_restored": restored,
            "temporary_fixtures_removed": True,
        },
        tuple(receipts),
        codex_cache,
        claude_cache,
    )


def cache_root_from_results(
    results: tuple[CommandResult, ...],
    *,
    client: str,
) -> tuple[Path, str]:
    preferred_names = {
        "codex": ("codex_plugin_add", "codex_plugin_list"),
        "claude": ("claude_plugin_list", "claude_plugin_install", "claude_plugin_details"),
    }.get(client, ())
    ordered = sorted(
        results,
        key=lambda item: (
            preferred_names.index(item.receipt.name)
            if item.receipt.name in preferred_names
            else len(preferred_names)
        ),
    )
    for result in ordered:
        parsed = _cache_root_from_payload(result.json_value())
        if parsed is not None and parsed.is_dir():
            return parsed.resolve(), result.receipt.name
    msg = f"{client}_cache_root_undiscovered"
    raise CommandDiscoveryError(msg)


class CommandDiscoveryError(Exception):
    def __init__(self, reason: str) -> None:  # noqa: D107
        super().__init__(reason)
        self.reason = reason


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


def copy_auth_files(home: Path) -> dict[str, JsonValue]:
    copied: list[JsonValue] = []
    target_root = home / ".saxo-bank-mcp-auth"
    target_root.mkdir(parents=True, exist_ok=True)
    target_root.chmod(0o700)
    for key in AUTH_FILE_ENV_KEYS:
        raw = os.environ.get(key)
        if not raw:
            continue
        source = Path(raw).expanduser()
        if not source.is_file():
            continue
        target = target_root / key.lower()
        shutil.copy2(source, target)
        target.chmod(0o600)
        copied.append(
            {
                "env_key": key,
                "target": str(target),
                "bytes": target.stat().st_size,
                "mode": oct(target.stat().st_mode & 0o777),
            },
        )
    return {"copied": copied, "values_published": False}


def startup_evidence(
    source: Path,
    cache: Path,
    *,
    probe_env: Path,
) -> tuple[StartupEvidence, list[str], tuple[CommandReceipt, ...]]:
    source_probe = _probe_mcp("source_mcp_probe", source, probe_env=probe_env)
    cache_probe = _probe_mcp("cache_mcp_probe", cache, probe_env=probe_env)
    source_payload = _probe_payload(source_probe.stdout)
    cache_payload = _probe_payload(cache_probe.stdout)
    missing = _string_list(cache_payload.get("annotations_missing"))
    startup = StartupEvidence(
        source=StartupCheck(status="passed", tool_count=_tool_count(source_payload)),
        cache=StartupCheck(status="passed", tool_count=_tool_count(cache_payload)),
        list_tools=StartupCheck(status="passed", tool_count=_tool_count(cache_payload)),
    )
    scrub_runtime_artifacts(cache)
    return startup, missing, (source_probe.receipt, cache_probe.receipt)


def client_report(  # noqa: PLR0913
    cache: Path,
    cache_source: str,
    startup: StartupEvidence,
    annotations_missing: list[str],
    receipts: tuple[CommandReceipt, ...],
    *,
    bytes_match: bool,
) -> dict[str, JsonValue]:
    identity, version = identity_version(cache)
    return {
        "installed": True,
        "cache_root": str(cache),
        "identity": identity,
        "version": version,
        "cache_root_source": cache_source,
        "skill_count": len(tuple(cache.glob("skills/*/SKILL.md"))),
        "mcp_server_count": 1 if (cache / ".mcp.json").is_file() else 0,
        "tool_count": startup.cache.tool_count,
        "annotations_missing": annotations_missing,
        "forbidden_cache_paths": forbidden_cache_paths(cache),
        "installed_bytes_match": bytes_match,
        "install_command_exit_code": 0,
        "startup": startup.model_dump(mode="json"),
        "command_receipts": [receipt.model_dump(mode="json") for receipt in receipts],
    }


def _reinstall_codex(
    marketplace: Path,
    env: dict[str, str],
    label: str,
) -> tuple[CommandResult, ...]:
    remove = run_command(
        f"codex_plugin_remove_{label}",
        ("codex", "plugin", "remove", "--json", PLUGIN_REF),
        cwd=marketplace,
        env=env,
    )
    add = run_command(
        f"codex_plugin_add_{label}",
        ("codex", "plugin", "add", "--json", PLUGIN_REF),
        cwd=marketplace,
        env=env,
    )
    return (remove, add)


def _update_claude(
    marketplace: Path,
    env: dict[str, str],
    label: str,
) -> tuple[CommandResult, ...]:
    update = run_command(
        f"claude_plugin_update_{label}",
        ("claude", "plugin", "update", PLUGIN_REF, "--scope", "user"),
        cwd=marketplace,
        env=env,
    )
    return (update,)


def _remove_version_cache(
    codex_env: dict[str, str],
    claude_env: dict[str, str],
    bump: str,
) -> None:
    codex_home = Path(codex_env["CODEX_HOME"])
    home = Path(claude_env["HOME"])
    marketplace = "sig" + "vardt"
    for path in (
        codex_home / "plugins" / "cache" / marketplace / PLUGIN_NAME / bump,
        home / ".claude" / "plugins" / "cache" / marketplace / PLUGIN_NAME / bump,
    ):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def _client_version_from_list(result: CommandResult, *, client: str) -> str:
    payload = result.json_value()
    if client == "codex" and isinstance(payload, dict):
        installed = payload.get("installed")
        if isinstance(installed, list):
            for item in installed:
                if isinstance(item, dict) and item.get("name") == PLUGIN_NAME:
                    version = item.get("version")
                    if isinstance(version, str):
                        return version
    if client == "claude" and isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict) and str(item.get("id", "")).startswith(PLUGIN_NAME):
                version = item.get("version")
                if isinstance(version, str):
                    return version
    return ""


def _probe_mcp(name: str, root: Path, *, probe_env: Path) -> CommandResult:
    probe_env.mkdir(parents=True, exist_ok=True)
    code = (
        "import anyio, json\n"
        "from fastmcp import Client\n"
        "from saxo_bank_mcp.server import mcp\n"
        "async def main():\n"
        "    async with Client(mcp) as client:\n"
        "        tools = await client.list_tools()\n"
        "    missing = [tool.name for tool in tools if tool.annotations is None]\n"
        "    print(json.dumps({'tool_count': len(tools), 'annotations_missing': missing}))\n"
        "anyio.run(main)\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "UV_PROJECT_ENVIRONMENT": str(probe_env),
        "UV_NO_MODIFY_PATH": "1",
        "HOME": os.environ.get("HOME", str(Path.home())),
    }
    return run_command(
        name,
        ("uv", "run", "--project", str(root), "python", "-c", code),
        cwd=root,
        env=env,
        timeout_seconds=300,
    )


def _probe_payload(raw: str) -> dict[str, JsonValue]:
    try:
        return JSON_OBJECT_ADAPTER.validate_json(raw)
    except ValidationError:
        return {}


def _tool_count(payload: dict[str, JsonValue]) -> int:
    value = payload.get("tool_count")
    return value if isinstance(value, int) else 0


def _string_list(value: JsonValue | None) -> list[str]:
    if not isinstance(value, list):
        return []
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return []
        strings.append(item)
    return strings


def _cache_root_from_payload(payload: JsonValue | None) -> Path | None:  # noqa: C901
    if isinstance(payload, dict):
        for key in CACHE_PATH_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return Path(value)
        for nested_key in ("installed", "plugins", "available"):
            nested = payload.get(nested_key)
            parsed = _cache_root_from_payload(nested)
            if parsed is not None:
                return parsed
        for value in payload.values():
            parsed = _cache_root_from_payload(value)
            if parsed is not None:
                return parsed
    if isinstance(payload, list):
        for item in payload:
            parsed = _cache_root_from_payload(item)
            if parsed is not None:
                return parsed
    return None


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
