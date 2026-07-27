from __future__ import annotations

from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import CommandResult
from saxo_bank_mcp.agent_skill_install_paths import MARKETPLACE_NAME, PLUGIN_NAME, PLUGIN_REF

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])


class CommandDiscoveryError(Exception):
    def __init__(self, reason: str) -> None:  # noqa: D107
        super().__init__(reason)
        self.reason = reason


def discover_codex_cache(
    results: tuple[CommandResult, ...],
    *,
    run_root: Path,
    codex_home: Path,
    expected_version: str,
    receipt_name: str = "codex_plugin_add",
) -> tuple[Path, str]:
    add = _require_named_success(results, receipt_name)
    payload = add.json_stdout()
    if not payload:
        raise CommandDiscoveryError("codex_plugin_add_json_invalid")
    name = payload.get("name")
    version = payload.get("version")
    plugin_id = payload.get("pluginId")
    installed = payload.get("installedPath")
    if name != PLUGIN_NAME or version != expected_version:
        raise CommandDiscoveryError("codex_identity_version_mismatch")
    if plugin_id not in {PLUGIN_REF, f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"}:
        raise CommandDiscoveryError("codex_plugin_id_mismatch")
    if not isinstance(installed, str) or not installed:
        raise CommandDiscoveryError("codex_installed_path_missing")
    cache = Path(installed).resolve()
    expected = (
        codex_home.resolve()
        / "plugins"
        / "cache"
        / MARKETPLACE_NAME
        / PLUGIN_NAME
        / expected_version
    )
    _validate_cache_path(cache, expected=expected, run_root=run_root, client="codex")
    return cache, add.receipt.name


def discover_claude_cache(
    results: tuple[CommandResult, ...],
    *,
    run_root: Path,
    home: Path,
    expected_version: str,
    receipt_name: str = "claude_plugin_list",
) -> tuple[Path, str]:
    listing = _require_named_success(results, receipt_name)
    payload = listing.json_value()
    if not isinstance(payload, list):
        raise CommandDiscoveryError("claude_plugin_list_json_invalid")
    match: dict[str, JsonValue] | None = None
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("id") == PLUGIN_REF and item.get("version") == expected_version:
            match = item
            break
    if match is None:
        raise CommandDiscoveryError("claude_plugin_not_listed")
    installed = match.get("installPath")
    if not isinstance(installed, str) or not installed:
        raise CommandDiscoveryError("claude_install_path_missing")
    cache = Path(installed).resolve()
    expected = (
        home.resolve()
        / ".claude"
        / "plugins"
        / "cache"
        / MARKETPLACE_NAME
        / PLUGIN_NAME
        / expected_version
    )
    _validate_cache_path(cache, expected=expected, run_root=run_root, client="claude")
    return cache, listing.receipt.name


def require_distinct_caches(codex_cache: Path, claude_cache: Path) -> None:
    if codex_cache.resolve() == claude_cache.resolve():
        raise CommandDiscoveryError("cache_roots_not_distinct")


def parse_claude_details_skills(details: CommandResult) -> tuple[int, tuple[str, ...]]:
    text = details.stdout
    skills: list[str] = []
    for line in text.splitlines():
        if "Skills (" in line and ")" in line:
            # e.g. Skills (8)  saxo-auth-session, saxo-bank, ...
            after = line.split(")", 1)[-1].strip()
            if after:
                skills = [item.strip() for item in after.split(",") if item.strip()]
            break
    return len(skills), tuple(skills)


def parse_mcp_server_count(root: Path) -> int:  # noqa: PLR0911
    path = root / ".mcp.json"
    if not path.is_file():
        return 0
    try:
        payload = _JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return -1
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return -1
    if set(servers) != {PLUGIN_NAME}:
        return -1
    server = servers.get(PLUGIN_NAME)
    if not isinstance(server, dict):
        return -1
    if server.get("command") != "uv":
        return -1
    return 1


def skill_inventory(root: Path) -> tuple[str, ...]:
    return tuple(sorted(path.parent.name for path in root.glob("skills/*/SKILL.md")))


def _validate_cache_path(
    cache: Path,
    *,
    expected: Path,
    run_root: Path,
    client: str,
) -> None:
    if not cache.is_dir():
        raise CommandDiscoveryError(f"{client}_cache_not_directory")
    if cache != expected:
        raise CommandDiscoveryError(f"{client}_cache_hierarchy_mismatch")
    if not cache.is_relative_to(run_root.resolve()):
        raise CommandDiscoveryError(f"{client}_cache_outside_run_root")
    if cache.is_symlink() or any(parent.is_symlink() for parent in cache.parents):
        raise CommandDiscoveryError(f"{client}_cache_symlink_rejected")


def _require_named_success(
    results: tuple[CommandResult, ...],
    name: str,
) -> CommandResult:
    matches = [result for result in results if result.receipt.name == name]
    if len(matches) != 1:
        raise CommandDiscoveryError(f"{name}_receipt_count_invalid")
    result = matches[0]
    if result.receipt.exit_code != 0 or result.receipt.timed_out:
        raise CommandDiscoveryError(f"{name}_receipt_failed")
    return result
