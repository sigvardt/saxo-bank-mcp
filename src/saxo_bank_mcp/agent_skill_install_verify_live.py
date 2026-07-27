from __future__ import annotations

import json
import tomllib
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_install_env import build_isolated_env
from saxo_bank_mcp.agent_skill_install_models import (
    ALLOWED_PRODUCTION_CACHE_SOURCES,
    EXPECTED_TOOLS,
    InstallEvidenceReport,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    MARKETPLACE_NAME,
    PLUGIN_NAME,
    PLUGIN_REF,
    global_state_fingerprint,
)
from saxo_bank_mcp.agent_skill_install_probe import probe_root_stdio

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_JSON_LIST: TypeAdapter[list[JsonValue]] = TypeAdapter(list[JsonValue])


def live_verify_errors(
    report: InstallEvidenceReport,
    *,
    codex_global_home: Path | None = None,
    claude_global_home: Path | None = None,
    run_startup_probes: bool = True,
) -> list[str]:
    """Independent verify-only checks against retained client state and caches."""
    errors: list[str] = []
    run_root = report.fixture_cleanup.run_root.resolve()
    clone = report.clone.path.resolve()
    errors.extend(_cache_source_errors(report))
    errors.extend(_registration_bind_errors(report, run_root=run_root))
    if codex_global_home is not None and claude_global_home is not None:
        errors.extend(
            _recompute_global_state_errors(
                report,
                codex_global_home=codex_global_home,
                claude_global_home=claude_global_home,
            ),
        )
    if run_startup_probes:
        errors.extend(_startup_probe_errors(report, run_root=run_root, clone=clone))
    return errors


def _cache_source_errors(report: InstallEvidenceReport) -> list[str]:
    errors: list[str] = []
    for name, client in (("codex", report.codex), ("claude", report.claude)):
        if client.cache_root_source not in ALLOWED_PRODUCTION_CACHE_SOURCES:
            errors.append(f"{name}_cache_root_source_rejected")
    return errors


def _registration_bind_errors(report: InstallEvidenceReport, *, run_root: Path) -> list[str]:
    errors: list[str] = []
    codex_home = (run_root / "codex-home").resolve()
    home = (run_root / "home").resolve()
    errors.extend(
        _codex_registration_errors(
            codex_home=codex_home,
            cache_root=report.codex.cache_root.resolve(),
            version=report.codex.version,
        ),
    )
    errors.extend(
        _claude_registration_errors(
            home=home,
            cache_root=report.claude.cache_root.resolve(),
            version=report.claude.version,
        ),
    )
    return errors


def _codex_registration_errors(  # noqa: PLR0911
    *,
    codex_home: Path,
    cache_root: Path,
    version: str,
) -> list[str]:
    config = codex_home / "config.toml"
    if not config.is_file():
        return ["codex_registration_missing"]
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return ["codex_registration_invalid"]
    # Fail-closed textual proof of enabled registration for the known plugin id.
    plugin_header = f'[plugins."{PLUGIN_REF}"]'
    plugin_header_alt = f'[plugins."{PLUGIN_NAME}@{MARKETPLACE_NAME}"]'
    if plugin_header not in text and plugin_header_alt not in text:
        return ["codex_registration_missing"]
    if "enabled = true" not in text and "enabled=true" not in text:
        return ["codex_registration_missing"]
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return ["codex_registration_invalid"]
    expected = (
        codex_home / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME / version
    ).resolve()
    if cache_root != expected or not cache_root.is_dir():
        return ["codex_cache_bind_mismatch"]
    return []


def _claude_registration_errors(  # noqa: PLR0911
    *,
    home: Path,
    cache_root: Path,
    version: str,
) -> list[str]:
    installed = home / ".claude" / "plugins" / "installed_plugins.json"
    if not installed.is_file():
        return ["claude_registration_missing"]
    try:
        payload = _JSON_OBJECT.validate_json(installed.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return ["claude_registration_invalid"]
    plugins = payload.get("plugins")
    if not isinstance(plugins, dict):
        return ["claude_registration_missing"]
    entries = plugins.get(PLUGIN_REF)
    if not isinstance(entries, list) or not entries:
        return ["claude_registration_missing"]
    match: dict[str, JsonValue] | None = None
    for item in entries:
        if isinstance(item, dict) and item.get("version") == version:
            match = item
            break
    if match is None:
        return ["claude_registration_version_mismatch"]
    install_path = match.get("installPath")
    if not isinstance(install_path, str):
        return ["claude_cache_bind_mismatch"]
    bound = Path(install_path).resolve()
    expected = (
        home / ".claude" / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME / version
    ).resolve()
    if bound != cache_root.resolve() or cache_root.resolve() != expected:
        return ["claude_cache_bind_mismatch"]
    return []


def _recompute_global_state_errors(
    report: InstallEvidenceReport,
    *,
    codex_global_home: Path,
    claude_global_home: Path,
) -> list[str]:
    live = global_state_fingerprint(codex_global_home, claude_global_home)
    live_before_after = {"codex": live["codex"], "claude": live["claude"]}
    errors: list[str] = []
    if report.global_state.before != report.global_state.after:
        errors.append("global_state_fingerprint_mismatch")
    if report.global_state.after != live_before_after:
        errors.append("global_state_recompute_mismatch")
    return errors


def _startup_probe_errors(
    report: InstallEvidenceReport,
    *,
    run_root: Path,
    clone: Path,
) -> list[str]:
    home = run_root / "home"
    codex_home = run_root / "codex-home"
    probe_env = run_root / "verify-probe-env"
    try:
        env = build_isolated_env(
            home=home,
            codex_home=codex_home,
            run_root=run_root,
            probe_env=probe_env,
        )
    except Exception:  # noqa: BLE001 - containment failure is a verify error
        return ["verify_env_invalid"]
    errors: list[str] = []
    for label, root in (
        ("source", clone),
        ("codex_cache", report.codex.cache_root.resolve()),
        ("claude_cache", report.claude.cache_root.resolve()),
    ):
        try:
            result = probe_root_stdio(
                f"verify_{label}_stdio",
                root,
                env=env,
                probe_env=probe_env,
            )
        except Exception:  # noqa: BLE001
            errors.append(f"{label}_startup_probe_failed")
            continue
        payload = _probe_payload(result.stdout)
        tool_count = payload.get("tool_count")
        missing = payload.get("annotations_missing")
        if tool_count != EXPECTED_TOOLS:
            errors.append(f"{label}_startup_tool_count_invalid")
        if not isinstance(missing, list) or missing:
            errors.append(f"{label}_startup_annotations_missing")
    # Independent list_tools probes for each cache (not a duplicated claim).
    for name, cache in (
        ("codex", report.codex.cache_root.resolve()),
        ("claude", report.claude.cache_root.resolve()),
    ):
        try:
            result = probe_root_stdio(
                f"verify_{name}_list_tools",
                cache,
                env=env,
                probe_env=probe_env,
            )
        except Exception:  # noqa: BLE001
            errors.append(f"{name}_list_tools_probe_failed")
            continue
        payload = _probe_payload(result.stdout)
        if payload.get("tool_count") != EXPECTED_TOOLS:
            errors.append(f"{name}_list_tools_count_invalid")
        missing = payload.get("annotations_missing")
        if not isinstance(missing, list) or missing:
            errors.append(f"{name}_list_tools_annotations_missing")
    return errors


def _probe_payload(raw: str) -> dict[str, JsonValue]:
    for raw_line in reversed(raw.splitlines()):
        stripped = raw_line.strip()
        if stripped.startswith("{"):
            try:
                return _JSON_OBJECT.validate_json(stripped)
            except ValidationError:
                continue
    try:
        return _JSON_OBJECT.validate_json(raw)
    except ValidationError:
        return {}


def registration_snapshot(run_root: Path) -> dict[str, JsonValue]:
    """Debug helper for retained client registration proof."""
    codex_home = run_root / "codex-home"
    home = run_root / "home"
    snapshot: dict[str, JsonValue] = {}
    config = codex_home / "config.toml"
    if config.is_file():
        snapshot["codex_config"] = config.read_text(encoding="utf-8")
    installed = home / ".claude" / "plugins" / "installed_plugins.json"
    if installed.is_file():
        try:
            snapshot["claude_installed"] = json.loads(installed.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            snapshot["claude_installed"] = "invalid"
    return snapshot
