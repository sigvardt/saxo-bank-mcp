from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_evidence_io import git_output

PLUGIN_NAME: Final = "saxo-bank-mcp"
MARKETPLACE_NAME: Final = "sig" + "vardt"
PLUGIN_REF: Final = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
REQUIRED_CACHE_FILES: Final = (
    ".mcp.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "data/saxo/openapi_inventory.json",
    "pyproject.toml",
    "uv.lock",
)
FORBIDDEN_PATH_PARTS: Final = frozenset(
    {
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
    },
)
FORBIDDEN_FILE_NAMES: Final = frozenset(
    {
        ".env",
        "credentials.json",
        "state.json",
        "token_cache.json",
        "token-cache.json",
        ".saxo-token-cache",
    },
)
PRIVATE_TRACKED_PREFIXES: Final = (".omo/",)
VERSION_RELATIVES: Final = (
    "pyproject.toml",
    ".codex-plugin/plugin.json",
    ".claude-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
)


def publishable_tracked_files(source: Path) -> tuple[str, ...]:
    raw = git_output(source, "ls-files", "-z") or ""
    return tuple(
        sorted(
            relative
            for relative in raw.split("\0")
            if relative and _is_publishable_relative(relative)
        ),
    )


def export_publishable_tree(source: Path, destination: Path) -> tuple[str, ...]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    relatives = publishable_tracked_files(source)
    for relative in relatives:
        src = source / relative
        if not src.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return relatives


def installed_byte_check(source: Path, cache: Path) -> dict[str, JsonValue]:
    publishable = publishable_tracked_files(source)
    mismatches: list[str] = []
    compared = 0
    for relative in publishable:
        compared += 1
        if not _same_bytes(source / relative, cache / relative):
            mismatches.append(relative)
    required = required_cache_files(source)
    required_present = tuple(relative for relative in required if (cache / relative).is_file())
    if len(required_present) != len(required):
        mismatches.append("required_cache_file_missing")
    forbidden = forbidden_cache_paths(cache)
    extras = _cache_extra_files(cache, publishable)
    metadata = tuple(sorted(path for path in extras if path not in set(forbidden)))
    forbidden_only = tuple(sorted(set(forbidden)))
    return {
        "compared_files": compared,
        "metadata_exceptions": list(metadata),
        "required_files_present": list(required_present),
        "forbidden_files_absent": not forbidden_only,
        "mismatches": mismatches if not forbidden_only else [*mismatches, "forbidden"],
        "forbidden_cache_paths": list(forbidden_only),
    }


def required_cache_files(source: Path) -> tuple[str, ...]:
    skills = tuple(
        sorted(str(path.relative_to(source)) for path in source.glob("skills/*/SKILL.md")),
    )
    return (*REQUIRED_CACHE_FILES, *skills)


def forbidden_cache_paths(cache: Path) -> list[str]:
    if not cache.is_dir():
        return ["cache_root_missing"]
    return sorted(
        str(path.relative_to(cache))
        for path in cache.rglob("*")
        if _is_forbidden(str(path.relative_to(cache)))
    )


def scrub_runtime_artifacts(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        relative = str(path.relative_to(root))
        if not _is_runtime_artifact(relative):
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file():
            path.unlink(missing_ok=True)


def global_state_fingerprint(codex_global_home: Path, claude_global_home: Path) -> dict[str, str]:
    return {
        "codex": _fingerprint_targets(_codex_fingerprint_targets(codex_global_home)),
        "claude": _fingerprint_targets(_claude_fingerprint_targets(claude_global_home)),
    }


OWNER_ONLY_MODE: Final = "0o700"
PRESERVED_ROOT_LABELS: Final = (
    "run_root",
    "clone",
    "codex_cache",
    "claude_cache",
    "home",
    "codex_home",
    "claude_home",
)


def owner_only_mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


def harden_preserved_roots(roots: dict[str, Path]) -> tuple[dict[str, str], tuple[str, ...]]:
    """Chmod each required preserved root to 0700 and return actual modes plus errors."""
    modes: dict[str, str] = {}
    errors: list[str] = []
    for label in PRESERVED_ROOT_LABELS:
        path = roots.get(label)
        if path is None or not path.exists():
            errors.append(f"preserved_root_missing:{label}")
            continue
        path.chmod(0o700)
        mode = owner_only_mode(path)
        modes[label] = mode
        if mode != OWNER_ONLY_MODE:
            errors.append(f"preserved_root_not_owner_only:{label}")
    missing_labels = [label for label in PRESERVED_ROOT_LABELS if label not in modes]
    for label in missing_labels:
        if f"preserved_root_missing:{label}" not in errors:
            errors.append(f"preserved_root_missing:{label}")
    return modes, tuple(errors)


def owner_only_from_modes(modes: dict[str, str]) -> bool:
    return set(modes) == set(PRESERVED_ROOT_LABELS) and all(
        mode == OWNER_ONLY_MODE for mode in modes.values()
    )


def _codex_fingerprint_targets(home: Path) -> tuple[Path, ...]:
    return (home / "config.toml", home / "plugins")


def _claude_fingerprint_targets(home: Path) -> tuple[Path, ...]:
    return (
        home / "settings.json",
        home / "plugins",
        home / "plugins" / "known_marketplaces.json",
        home / "plugins" / "installed_plugins.json",
        home / "plugins" / "marketplaces",
    )


def _fingerprint_targets(targets: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for target in targets:
        if not target.exists():
            digest.update(b"missing:")
            digest.update(str(target).encode())
            continue
        if target.is_file():
            _update_file_fingerprint(digest, target, target.name)
            continue
        for file_path in sorted(path for path in target.rglob("*") if path.is_file()):
            relative = str(file_path.relative_to(target))
            _update_file_fingerprint(digest, file_path, relative)
    return digest.hexdigest()


def _update_file_fingerprint(digest: object, path: Path, relative: str) -> None:
    updater = getattr(digest, "update", None)
    if updater is None:
        msg = "digest missing update"
        raise TypeError(msg)
    stat = path.stat()
    updater(relative.encode())
    updater(str(stat.st_size).encode())
    updater(hashlib.sha256(path.read_bytes()).digest())


def _cache_extra_files(cache: Path, publishable: tuple[str, ...]) -> tuple[str, ...]:
    published = set(publishable)
    extras: list[str] = []
    for path in cache.rglob("*"):
        if not path.is_file():
            continue
        relative = str(path.relative_to(cache))
        if relative not in published:
            extras.append(relative)
    return tuple(sorted(extras))


def _is_publishable_relative(relative: str) -> bool:
    for prefix in PRIVATE_TRACKED_PREFIXES:
        if relative == prefix.rstrip("/") or relative.startswith(prefix):
            return False
    return not _is_forbidden(relative)


def _is_forbidden(relative: str) -> bool:
    path = Path(relative)
    if path.name in FORBIDDEN_FILE_NAMES:
        return True
    return any(part in FORBIDDEN_PATH_PARTS for part in path.parts)


def _is_runtime_artifact(relative: str) -> bool:
    parts = set(Path(relative).parts)
    return bool(parts & {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})


def _same_bytes(source: Path, installed: Path) -> bool:
    return (
        source.is_file() and installed.is_file() and source.read_bytes() == installed.read_bytes()
    )
