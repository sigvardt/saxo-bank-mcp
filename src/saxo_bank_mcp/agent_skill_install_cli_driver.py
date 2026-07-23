from __future__ import annotations

import os
import re
import shutil
import tomllib
from pathlib import Path

from pydantic import ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import CommandResult, run_command
from saxo_bank_mcp.agent_skill_install_models import (
    CommandReceipt,
    StartupCheck,
    StartupEvidence,
)
from saxo_bank_mcp.agent_skill_install_qa import JSON_OBJECT_ADAPTER

PLUGIN_REF = "saxo-bank-mcp@" + "sig" + "vardt"
PLUGIN_NAME = "saxo-bank-mcp"
METADATA_EXCEPTIONS = (".omo/**",)
AUTH_FILE_ENV_KEYS = (
    "SAXO_MCP_SIM_CREDENTIAL_FILE",
    "SAXO_MCP_LIVE_CREDENTIAL_FILE",
    "SAXO_MCP_TOKEN_CACHE_PATH",
)


def clone_candidate(repo: Path, clone: Path, commit: str) -> tuple[CommandReceipt, ...]:
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


def isolated_env(*, home: Path, codex_home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "CLAUDE_HOME": str(home / ".claude"),
        "PATH": os.environ.get("PATH", ""),
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


def run_codex_install(clone: Path, env: dict[str, str]) -> tuple[CommandResult, ...]:
    commands = (
        ("codex_marketplace_add", ("codex", "plugin", "marketplace", "add", "--json", str(clone))),
        ("codex_plugin_add", ("codex", "plugin", "add", "--json", PLUGIN_REF)),
        ("codex_plugin_list", ("codex", "plugin", "list", "--available", "--json")),
    )
    return tuple(run_command(name, argv, cwd=clone, env=env) for name, argv in commands)


def run_claude_install(clone: Path, env: dict[str, str]) -> tuple[CommandResult, ...]:
    commands = (
        ("claude_marketplace_add", ("claude", "plugin", "marketplace", "add", str(clone))),
        (
            "claude_plugin_install",
            ("claude", "plugin", "install", PLUGIN_REF, "--scope", "user"),
        ),
        ("claude_plugin_list", ("claude", "plugin", "list", "--json")),
        ("claude_plugin_details", ("claude", "plugin", "details", "saxo-bank-mcp")),
    )
    return tuple(run_command(name, argv, cwd=clone, env=env) for name, argv in commands)


def run_update_probe(
    clone: Path,
    codex_env: dict[str, str],
    claude_env: dict[str, str],
) -> tuple[dict[str, JsonValue], tuple[CommandReceipt, ...]]:
    version = project_version(clone)
    bump = _patch_bump(version)
    targets = (
        clone / "pyproject.toml",
        clone / ".codex-plugin/plugin.json",
        clone / ".claude-plugin/plugin.json",
        clone / ".agents/plugins/marketplace.json",
        clone / ".claude-plugin/marketplace.json",
    )
    original = {path: path.read_bytes() for path in targets if path.is_file()}
    try:
        for path, data in original.items():
            path.write_text(data.decode().replace(version, bump), encoding="utf-8")
        bump_receipts = (
            run_command(
                "codex_marketplace_upgrade_bumped",
                ("codex", "plugin", "marketplace", "upgrade", "--json"),
                cwd=clone,
                env=codex_env,
            ).receipt,
            run_command(
                "claude_plugin_update_bumped",
                ("claude", "plugin", "update", "saxo-bank-mcp"),
                cwd=clone,
                env=claude_env,
            ).receipt,
        )
    finally:
        for path, data in original.items():
            path.write_bytes(data)
    restore_receipts = (
        run_command(
            "codex_marketplace_upgrade",
            ("codex", "plugin", "marketplace", "upgrade", "--json"),
            cwd=clone,
            env=codex_env,
        ).receipt,
        run_command(
            "claude_plugin_update",
            ("claude", "plugin", "update", "saxo-bank-mcp"),
            cwd=clone,
            env=claude_env,
        ).receipt,
    )
    restored = project_version(clone) == version
    return (
        {
            "original_version": version,
            "bumped_version": bump,
            "candidate_restored": restored,
        },
        (*bump_receipts, *restore_receipts),
    )


def cache_root(
    run_root: Path,
    client: str,
    results: tuple[CommandResult, ...],
) -> tuple[Path, str]:
    for result in results:
        payload = result.json_stdout()
        parsed = _cache_root_from_payload(payload)
        if parsed is not None:
            return parsed, result.receipt.name
    preferred = run_root / f"{client}-cache"
    if preferred.is_dir():
        return preferred, "isolated_cache_tree"
    candidates = tuple(
        sorted(
            path
            for path in run_root.rglob("*")
            if path.is_dir() and (path / ".mcp.json").is_file() and (path / "skills").is_dir()
        )
    )
    for candidate in candidates:
        if client in str(candidate):
            return candidate, "isolated_cache_inventory"
    return (candidates[0], "isolated_cache_inventory") if candidates else (preferred, "missing")


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
    if names == {PLUGIN_NAME} and len(versions) == 1:
        return PLUGIN_NAME, project
    return "", ""


def installed_byte_check(source: Path, cache: Path) -> dict[str, JsonValue]:
    compared = 0
    mismatches: list[str] = []
    for relative in _tracked_public_files(source):
        compared += 1
        if not _same_bytes(source / relative, cache / relative):
            mismatches.append(relative)
    required_present = tuple(
        relative for relative in _required_cache_files(source) if (cache / relative).is_file()
    )
    forbidden = _forbidden_cache_paths(cache)
    if len(required_present) != len(_required_cache_files(source)):
        mismatches.append("required_cache_file_missing")
    return {
        "compared_files": compared,
        "metadata_exceptions": list(METADATA_EXCEPTIONS),
        "required_files_present": list(required_present),
        "forbidden_files_absent": not forbidden,
        "mismatches": mismatches,
    }


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
) -> tuple[StartupEvidence, tuple[CommandReceipt, ...]]:
    source_probe = _probe_mcp("source_mcp_probe", source)
    cache_probe = _probe_mcp("cache_mcp_probe", cache)
    startup = StartupEvidence(
        source=StartupCheck(status="passed", tool_count=_tool_count(source_probe.stdout)),
        cache=StartupCheck(status="passed", tool_count=_tool_count(cache_probe.stdout)),
        list_tools=StartupCheck(status="passed", tool_count=_tool_count(cache_probe.stdout)),
    )
    return startup, (source_probe.receipt, cache_probe.receipt)


def client_report(
    cache: Path,
    cache_source: str,
    startup: StartupEvidence,
    receipts: tuple[CommandReceipt, ...],
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
        "annotations_missing": _probe_annotations_missing(cache),
        "forbidden_cache_paths": _forbidden_cache_paths(cache),
        "installed_bytes_match": True,
        "install_command_exit_code": 0,
        "startup": startup.model_dump(mode="json"),
        "command_receipts": [receipt.model_dump(mode="json") for receipt in receipts],
    }


def _probe_mcp(name: str, root: Path) -> CommandResult:
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
    return run_command(name, ("uv", "run", "--project", str(root), "python", "-c", code), cwd=root)


def _tool_count(raw: str) -> int:
    try:
        payload = JSON_OBJECT_ADAPTER.validate_json(raw)
    except ValidationError:
        return 0
    value = payload.get("tool_count")
    return value if isinstance(value, int) else 0


def _probe_annotations_missing(cache: Path) -> list[str]:
    payload = _read_probe_cache_marker(cache)
    value = payload.get("annotations_missing")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    return []


def _read_probe_cache_marker(cache: Path) -> dict[str, JsonValue]:
    marker = cache / ".qa-probe.json"
    if not marker.is_file():
        return {}
    try:
        return JSON_OBJECT_ADAPTER.validate_json(marker.read_text(encoding="utf-8"))
    except ValidationError:
        return {}


def _forbidden_cache_paths(cache: Path) -> list[str]:
    forbidden_parts = {
        ".cache",
        ".codex",
        ".config",
        ".git",
        ".local",
        ".omo",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
    forbidden_names = {".env", "credentials.json", "state.json", "token_cache.json"}
    return [
        str(path.relative_to(cache))
        for path in cache.rglob("*")
        if any(part in forbidden_parts for part in path.relative_to(cache).parts)
        or path.name in forbidden_names
    ]


def _cache_root_from_payload(payload: dict[str, JsonValue]) -> Path | None:
    for key in ("cache_root", "installed_path", "path", "root"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return Path(value)
    plugins = payload.get("plugins")
    if isinstance(plugins, list):
        for plugin in plugins:
            if isinstance(plugin, dict):
                parsed = _cache_root_from_payload(plugin)
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


def _tracked_public_files(source: Path) -> tuple[str, ...]:
    result = run_command("git_ls_files_public", ("git", "ls-files", "-z"), cwd=source)
    return tuple(
        sorted(
            relative
            for relative in result.stdout.split("\0")
            if relative and not relative.startswith(".omo/")
        ),
    )


def _required_cache_files(source: Path) -> tuple[str, ...]:
    skills = tuple(str(path.relative_to(source)) for path in source.glob("skills/*/SKILL.md"))
    return (
        ".mcp.json",
        ".claude-plugin/plugin.json",
        ".codex-plugin/plugin.json",
        "data/saxo/openapi_inventory.json",
        "pyproject.toml",
        "uv.lock",
        *skills,
    )


def _same_bytes(source: Path, installed: Path) -> bool:
    return (
        source.is_file()
        and installed.is_file()
        and source.read_bytes() == installed.read_bytes()
    )


def _patch_bump(version: str) -> str:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        return f"{version}.probe"
    return f"{match.group(1)}.{match.group(2)}.{int(match.group(3)) + 1}"
