from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_install_env import (
    build_isolated_env,
    cleanup_verify_scratch_state,
    verify_throwaway_roots,
)
from saxo_bank_mcp.agent_skill_install_models import (
    ALLOWED_PRODUCTION_CACHE_SOURCES,
    EXPECTED_TOOLS,
    InstallEvidenceReport,
    UpdateProbeEvidence,
    VersionCacheProof,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    MARKETPLACE_NAME,
    PLUGIN_NAME,
    PLUGIN_REF,
    VERSION_RELATIVES,
    ensure_owner_only,
    global_state_fingerprint,
    installed_inventory_check,
    publishable_tracked_files,
    tree_digest,
)
from saxo_bank_mcp.agent_skill_install_probe import probe_root_stdio

_JSON_OBJECT: TypeAdapter[dict[str, JsonValue]] = TypeAdapter(dict[str, JsonValue])
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def live_verify_errors(
    report: InstallEvidenceReport,
    *,
    codex_global_home: Path,
    claude_global_home: Path,
) -> list[str]:
    """Independent production verify checks (registration, startup, update, window).

    Producer historical before==after is checked from the report only.
    Live global fingerprints are taken immediately before and after live work;
    start==end is required (detects verifier mutation). Ambient drift between
    producer and verify is allowed: current hashes need not match report history.
    """
    errors: list[str] = []
    run_root = report.fixture_cleanup.run_root.resolve()
    clone = report.clone.path.resolve()
    errors.extend(_historical_global_state_errors(report))

    start_pair: dict[str, JsonValue] | None = None
    try:
        start_pair = global_fingerprint_pair(codex_global_home, claude_global_home)
    except OSError:
        errors.append("global_state_fingerprint_failed")

    # Live work that may touch processes/scratch under the retained run_root only.
    errors.extend(_cache_source_errors(report))
    errors.extend(_registration_bind_errors(report, run_root=run_root))
    errors.extend(_startup_probe_errors(report, run_root=run_root, clone=clone))
    errors.extend(_update_proof_errors(report, run_root=run_root, clone=clone))

    try:
        end_pair = global_fingerprint_pair(codex_global_home, claude_global_home)
    except OSError:
        errors.append("global_state_fingerprint_failed")
        return errors
    if start_pair is None:
        # Start fingerprint already failed; do not claim a successful window.
        if "global_state_fingerprint_failed" not in errors:
            errors.append("global_state_fingerprint_failed")
        return errors
    if start_pair != end_pair:
        errors.append("global_state_verify_window_mismatch")
    return errors


def global_fingerprint_pair(
    codex_global_home: Path,
    claude_global_home: Path,
) -> dict[str, JsonValue]:
    """Privacy-safe codex/claude fingerprint digests for the provided global homes."""
    live = global_state_fingerprint(codex_global_home, claude_global_home)
    return {"codex": live["codex"], "claude": live["claude"]}


def _historical_global_state_errors(report: InstallEvidenceReport) -> list[str]:
    """Reject producer reports where before and after fingerprints differ."""
    if report.global_state.before != report.global_state.after:
        return ["global_state_fingerprint_mismatch"]
    return []


def consumer_live_errors(report: InstallEvidenceReport) -> list[str]:
    """Fail-closed consumer checks without caller global homes."""
    errors: list[str] = []
    run_root = report.fixture_cleanup.run_root.resolve()
    clone = report.clone.path.resolve()
    errors.extend(_cache_source_errors(report))
    errors.extend(_registration_bind_errors(report, run_root=run_root))
    errors.extend(_live_inventory_errors(report, clone=clone))
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


def codex_registration_errors(
    *,
    codex_home: Path,
    cache_root: Path,
    version: str,
) -> list[str]:
    """Parse config.toml and require the exact plugin entry enabled."""
    return _codex_registration_errors(
        codex_home=codex_home,
        cache_root=cache_root,
        version=version,
    )


def _codex_registration_errors(  # noqa: C901, PLR0911
    *,
    codex_home: Path,
    cache_root: Path,
    version: str,
) -> list[str]:
    config = codex_home / "config.toml"
    if not config.is_file():
        return ["codex_registration_missing"]
    try:
        payload = cast("dict[str, object]", tomllib.loads(config.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError):
        return ["codex_registration_invalid"]
    plugins_obj = payload.get("plugins")
    if not isinstance(plugins_obj, dict):
        return ["codex_registration_missing"]
    entry = _toml_plugin_entry(cast("dict[str, object]", plugins_obj), PLUGIN_REF)
    if entry is None:
        return ["codex_registration_missing"]
    enabled = entry.get("enabled")
    if enabled is not True:
        return ["codex_registration_not_enabled"]
    expected = (
        codex_home / "plugins" / "cache" / MARKETPLACE_NAME / PLUGIN_NAME / version
    ).resolve()
    if cache_root != expected or not cache_root.is_dir():
        return ["codex_cache_bind_mismatch"]
    for key in ("version", "plugin_version"):
        value = entry.get(key)
        if isinstance(value, str) and value and value != version:
            return ["codex_registration_version_mismatch"]
    for key in ("path", "install_path", "installedPath", "cache_path"):
        value = entry.get(key)
        if isinstance(value, str) and value and Path(value).expanduser().resolve() != expected:
            return ["codex_registration_path_mismatch"]
    return []


def _toml_plugin_entry(plugins: dict[str, object], plugin_ref: str) -> dict[str, object] | None:
    raw = plugins.get(plugin_ref)
    if isinstance(raw, dict):
        return cast("dict[str, object]", raw)
    return None


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


def _live_inventory_errors(report: InstallEvidenceReport, *, clone: Path) -> list[str]:
    errors: list[str] = []
    for name, client in (("codex", report.codex), ("claude", report.claude)):
        inventory = installed_inventory_check(clone, client.cache_root.resolve())
        if inventory.get("inventory_exact_match") is not True:
            errors.append(f"{name}_live_inventory_mismatch")
    return errors


def _startup_probe_errors(
    report: InstallEvidenceReport,
    *,
    run_root: Path,
    clone: Path,
) -> list[str]:
    return startup_probe_errors_for_caches(
        run_root=run_root,
        clone=clone,
        codex_cache=report.codex.cache_root.resolve(),
        claude_cache=report.claude.cache_root.resolve(),
    )


def startup_probe_errors_for_caches(
    *,
    run_root: Path,
    clone: Path,
    codex_cache: Path,
    claude_cache: Path,
) -> list[str]:
    """Startup probes use throwaway HOME/CODEX_HOME; registration stays on retained paths."""
    verify_home, verify_codex_home, probe_env = verify_throwaway_roots(run_root)
    errors: list[str] = []
    try:
        for path in (verify_home, verify_codex_home, probe_env):
            ensure_owner_only(path)
        env = build_isolated_env(
            home=verify_home,
            codex_home=verify_codex_home,
            run_root=run_root,
            probe_env=probe_env,
        )
        for label, root in (
            ("source", clone),
            ("codex_cache", codex_cache),
            ("claude_cache", claude_cache),
        ):
            errors.extend(_one_startup(label, root, env=env, probe_env=probe_env))
        for name, cache in (
            ("codex", codex_cache),
            ("claude", claude_cache),
        ):
            errors.extend(
                _one_startup(f"{name}_list_tools", cache, env=env, probe_env=probe_env),
            )
    except Exception:  # noqa: BLE001
        errors.append("verify_env_invalid")
    finally:
        # run_command already reaps probe children; delete verifier scratch only
        # (never retained home/ or codex-home/ after privacy production).
        residual = cleanup_verify_scratch_state(run_root)
        if residual:
            errors.append("verify_scratch_residue")
    return errors


def _one_startup(
    label: str,
    root: Path,
    *,
    env: dict[str, str],
    probe_env: Path,
) -> list[str]:
    try:
        result = probe_root_stdio(f"verify_{label}_stdio", root, env=env, probe_env=probe_env)
    except Exception:  # noqa: BLE001
        return [f"{label}_startup_probe_failed"]
    payload = _probe_payload(result.stdout)
    errors: list[str] = []
    if payload.get("tool_count") != EXPECTED_TOOLS:
        errors.append(f"{label}_startup_tool_count_invalid")
    missing = payload.get("annotations_missing")
    if not isinstance(missing, list) or missing:
        errors.append(f"{label}_startup_annotations_missing")
    return errors


def _update_proof_errors(
    report: InstallEvidenceReport,
    *,
    run_root: Path,
    clone: Path,
) -> list[str]:
    probe = report.update_probe
    publishable = publishable_tracked_files(clone)
    errors: list[str] = []
    errors.extend(
        _restored_proof_errors(
            probe,
            clone=clone,
            run_root=run_root,
            publishable=publishable,
            version=probe.original_version,
        ),
    )
    errors.extend(
        _bumped_proof_errors(
            probe,
            report,
            clone=clone,
            run_root=run_root,
            publishable=publishable,
        ),
    )
    errors.extend(_update_receipt_errors(report))
    return errors


def _restored_proof_errors(
    probe: UpdateProbeEvidence,
    *,
    clone: Path,
    run_root: Path,
    publishable: tuple[str, ...],
    version: str,
) -> list[str]:
    errors: list[str] = []
    expected_source = tree_digest(clone, publishable)
    for client_name, proof in (
        ("codex", probe.restored_proof.codex),
        ("claude", probe.restored_proof.claude),
    ):
        expected_cache = _expected_cache_root(run_root, client=client_name, version=version)
        errors.extend(
            _proof_against_disk(
                client_name,
                proof,
                label="restored",
                expected_cache=expected_cache,
                expected_source_digest=expected_source,
                publishable=publishable,
                source=clone,
                require_live_startup=True,
                run_root=run_root,
            ),
        )
    return errors


def _bumped_proof_errors(  # noqa: C901
    probe: UpdateProbeEvidence,
    report: InstallEvidenceReport,
    *,
    clone: Path,
    run_root: Path,
    publishable: tuple[str, ...],
) -> list[str]:
    errors: list[str] = []
    expected_source = _bumped_source_digest(
        clone,
        publishable=publishable,
        original=probe.original_version,
        bumped=probe.bumped_version,
    )
    receipt_names = [receipt.name for receipt in report.update_receipts]
    for client_name, proof, list_name, update_name in (
        (
            "codex",
            probe.bumped_proof.codex,
            "codex_plugin_list_bumped",
            "codex_plugin_add_bumped",
        ),
        (
            "claude",
            probe.bumped_proof.claude,
            "claude_plugin_list_bumped",
            "claude_plugin_install_bumped",
        ),
    ):
        expected_cache = _expected_cache_root(
            run_root,
            client=client_name,
            version=probe.bumped_version,
        )
        if Path(proof.cache_root).expanduser().resolve() != expected_cache:
            errors.append(f"{client_name}_bumped_cache_path_invalid")
        if proof.source_digest != expected_source or proof.digest != expected_source:
            errors.append(f"{client_name}_bumped_digest_mismatch")
        if proof.version != probe.bumped_version:
            errors.append(f"{client_name}_bumped_version_mismatch")
        if proof.tool_count != EXPECTED_TOOLS or proof.annotations_missing:
            errors.append(f"{client_name}_bumped_startup_invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", proof.probe_stdout_sha256):
            errors.append(f"{client_name}_bumped_probe_digest_invalid")
        if proof.list_receipt_name != list_name:
            errors.append(f"{client_name}_bumped_list_receipt_invalid")
        if receipt_names.count(list_name) != 1 or receipt_names.count(update_name) != 1:
            errors.append(f"{client_name}_bumped_receipt_linkage_invalid")
        if (
            proof.registration_version != probe.bumped_version
            or Path(proof.registration_cache_root).expanduser().resolve() != expected_cache
        ):
            errors.append(f"{client_name}_bumped_registration_fields_invalid")
        if proof.inventory_exact_match is not True:
            errors.append(f"{client_name}_bumped_inventory_invalid")
    return errors


def _proof_against_disk(  # noqa: C901, PLR0912, PLR0913
    client_name: str,
    proof: VersionCacheProof,
    *,
    label: str,
    expected_cache: Path,
    expected_source_digest: str,
    publishable: tuple[str, ...],
    source: Path,
    require_live_startup: bool,
    run_root: Path,
) -> list[str]:
    errors: list[str] = []
    if Path(proof.cache_root).expanduser().resolve() != expected_cache:
        errors.append(f"{client_name}_{label}_cache_path_invalid")
    if not expected_cache.is_dir():
        errors.append(f"{client_name}_{label}_cache_missing")
        return errors
    live_source = tree_digest(source, publishable)
    live_cache = tree_digest(expected_cache, publishable)
    if live_source != expected_source_digest:
        errors.append(f"{client_name}_{label}_source_digest_stale")
    if proof.source_digest != live_source or proof.digest != live_cache:
        errors.append(f"{client_name}_{label}_digest_mismatch")
    inventory = installed_inventory_check(source, expected_cache, publishable=publishable)
    if inventory.get("inventory_exact_match") is not True:
        errors.append(f"{client_name}_{label}_inventory_mismatch")
    if (
        proof.registration_version != proof.version
        or Path(proof.registration_cache_root).expanduser().resolve() != expected_cache
    ):
        errors.append(f"{client_name}_{label}_registration_fields_invalid")
    if require_live_startup:
        verify_home, verify_codex_home, probe_env = verify_throwaway_roots(run_root)
        try:
            for path in (verify_home, verify_codex_home, probe_env):
                ensure_owner_only(path)
            env = build_isolated_env(
                home=verify_home,
                codex_home=verify_codex_home,
                run_root=run_root,
                probe_env=probe_env,
            )
            result = probe_root_stdio(
                f"verify_{client_name}_{label}_stdio",
                expected_cache,
                env=env,
                probe_env=probe_env,
            )
            payload = _probe_payload(result.stdout)
            if payload.get("tool_count") != EXPECTED_TOOLS:
                errors.append(f"{client_name}_{label}_startup_tool_count_invalid")
            missing = payload.get("annotations_missing")
            if not isinstance(missing, list) or missing:
                errors.append(f"{client_name}_{label}_startup_annotations_missing")
            live_sha = hashlib.sha256(result.stdout.encode()).hexdigest()
            if proof.probe_stdout_sha256 != live_sha:
                errors.append(f"{client_name}_{label}_probe_digest_mismatch")
        except Exception:  # noqa: BLE001
            errors.append(f"{client_name}_{label}_startup_probe_failed")
        finally:
            residual = cleanup_verify_scratch_state(run_root)
            if residual:
                errors.append(f"{client_name}_{label}_verify_scratch_residue")
    return errors


def _update_receipt_errors(report: InstallEvidenceReport) -> list[str]:
    names = [receipt.name for receipt in report.update_receipts]
    required = {
        "codex_plugin_add_bumped",
        "codex_plugin_add_restored",
        "claude_plugin_install_bumped",
        "claude_plugin_install_restored",
        "codex_plugin_list_bumped",
        "claude_plugin_list_bumped",
        "codex_plugin_list_restored",
        "claude_plugin_list_restored",
    }
    present = set(names)
    missing = sorted(required - present)
    if missing:
        return ["update_receipts_incomplete"]
    # Each required bumped/restored list receipt must be unique.
    for name in required:
        if names.count(name) != 1:
            return [f"update_receipt_not_unique:{name}"]
    return []


def _expected_cache_root(run_root: Path, *, client: str, version: str) -> Path:
    if client == "codex":
        return (
            run_root.resolve()
            / "codex-home"
            / "plugins"
            / "cache"
            / MARKETPLACE_NAME
            / PLUGIN_NAME
            / version
        ).resolve()
    return (
        run_root.resolve()
        / "home"
        / ".claude"
        / "plugins"
        / "cache"
        / MARKETPLACE_NAME
        / PLUGIN_NAME
        / version
    ).resolve()


def _bumped_source_digest(
    clone: Path,
    *,
    publishable: tuple[str, ...],
    original: str,
    bumped: str,
) -> str:
    """Recompute expected bumped source digest from candidate clone version transform."""
    digest = hashlib.sha256()
    for relative in publishable:
        path = clone / relative
        digest.update(relative.encode())
        if not path.is_file() or path.is_symlink():
            digest.update(b"missing")
            continue
        data = path.read_bytes()
        if relative in VERSION_RELATIVES:
            text = data.decode("utf-8")
            data = text.replace(original, bumped).encode("utf-8")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


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
