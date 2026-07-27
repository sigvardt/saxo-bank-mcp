from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_evidence_io import git_output
from saxo_bank_mcp.agent_skill_static_gate_constants import PUBLIC_SECRET_SCAN_PATHS

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
VERSION_RELATIVES: Final = (
    "pyproject.toml",
    ".codex-plugin/plugin.json",
    ".claude-plugin/plugin.json",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
)
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
# Installer may add only these extra relative paths beyond the publishable tree.
ALLOWED_INSTALLER_METADATA: Final = (
    ".claude-plugin/.install-metadata.json",
    ".codex-plugin/.install-metadata.json",
)
UNSAFE_NAME_PATTERN: Final = re.compile(
    r"(?i)(credential|credentials|token_cache|token-cache|\.env$|\.pem$|\.key$|"
    r"secret|password|private[_-]?key|id_rsa|auth\.json|state\.json)",
)
UNSAFE_PATH_PARTS: Final = frozenset(
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
        "evidence",
        "credentials",
        "tokens",
    },
)


def publishable_tracked_files(source: Path) -> tuple[str, ...]:
    raw = git_output(source, "ls-files", "-z") or ""
    selected = [
        relative
        for relative in raw.split("\0")
        if relative and _is_publishable_relative(relative)
    ]
    return tuple(sorted(selected))


def export_publishable_tree(source: Path, destination: Path) -> tuple[str, ...]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    relatives = publishable_tracked_files(source)
    for relative in relatives:
        src = source / relative
        if src.is_symlink():
            msg = f"symlink_rejected:{relative}"
            raise ValueError(msg)
        if not src.is_file():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target, follow_symlinks=False)
    return relatives


def installed_inventory_check(
    source: Path,
    cache: Path,
    *,
    publishable: tuple[str, ...] | None = None,
) -> dict[str, JsonValue]:
    publishable = publishable if publishable is not None else publishable_tracked_files(source)
    mismatches: list[str] = []
    compared = 0
    for relative in publishable:
        compared += 1
        if (cache / relative).is_symlink():
            mismatches.append(f"symlink:{relative}")
            continue
        if not _same_bytes(source / relative, cache / relative):
            mismatches.append(relative)
    required = required_cache_files(source)
    required_present = tuple(relative for relative in required if (cache / relative).is_file())
    if len(required_present) != len(required):
        mismatches.append("required_cache_file_missing")
    cache_files = _cache_files(cache)
    publishable_set = set(publishable)
    extras = tuple(sorted(path for path in cache_files if path not in publishable_set))
    allowed_meta = tuple(path for path in extras if path in ALLOWED_INSTALLER_METADATA)
    unexpected = tuple(path for path in extras if path not in ALLOWED_INSTALLER_METADATA)
    forbidden = forbidden_cache_paths(cache)
    if unexpected:
        mismatches.append("unexpected_cache_files")
    if forbidden:
        mismatches.append("forbidden")
    return {
        "compared_files": compared,
        "metadata_exceptions": list(allowed_meta),
        "required_files_present": list(required_present),
        "forbidden_files_absent": not forbidden,
        "mismatches": mismatches,
        "forbidden_cache_paths": forbidden,
        "publishable_count": len(publishable),
        "cache_file_count": len(cache_files),
        "inventory_exact_match": not unexpected and not mismatches and not forbidden,
    }


def required_cache_files(source: Path) -> tuple[str, ...]:
    skills = tuple(
        sorted(str(path.relative_to(source)) for path in source.glob("skills/*/SKILL.md")),
    )
    return (*REQUIRED_CACHE_FILES, *skills)


def forbidden_cache_paths(cache: Path) -> list[str]:
    if not cache.is_dir():
        return ["cache_root_missing"]
    findings: list[str] = []
    for path in cache.rglob("*"):
        relative = str(path.relative_to(cache))
        if path.is_symlink() or _is_unsafe_relative(relative):
            findings.append(relative)
    return sorted(findings)


def scrub_runtime_artifacts(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        relative = str(path.relative_to(root))
        if not _is_runtime_artifact(relative):
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)


def global_state_fingerprint(
    codex_global_home: Path,
    claude_global_home: Path,
) -> dict[str, JsonValue]:
    return {
        "codex": _fingerprint_scope(_codex_fingerprint_targets(codex_global_home)),
        "claude": _fingerprint_scope(_claude_fingerprint_targets(claude_global_home)),
        "scope": {
            "codex": [str(path) for path in _codex_fingerprint_targets(codex_global_home)],
            "claude": [str(path) for path in _claude_fingerprint_targets(claude_global_home)],
            "fields": ["path", "type", "size", "mode", "sha256"],
        },
    }


def owner_only_mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


def ensure_owner_only(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    mode = owner_only_mode(path)
    if mode != OWNER_ONLY_MODE:
        msg = f"path_not_owner_only:{path}"
        raise PermissionError(msg)
    return mode


def assert_preserved_modes(roots: dict[str, Path]) -> dict[str, str]:
    modes: dict[str, str] = {}
    for label in PRESERVED_ROOT_LABELS:
        path = roots.get(label)
        if path is None or not path.exists():
            msg = f"preserved_root_missing:{label}"
            raise FileNotFoundError(msg)
        mode = owner_only_mode(path)
        if mode != OWNER_ONLY_MODE:
            msg = f"preserved_root_not_owner_only:{label}:{mode}"
            raise PermissionError(msg)
        modes[label] = mode
    return modes


def owner_only_from_modes(modes: dict[str, str]) -> bool:
    return set(modes) == set(PRESERVED_ROOT_LABELS) and all(
        mode == OWNER_ONLY_MODE for mode in modes.values()
    )


def tree_digest(root: Path, relatives: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in relatives:
        path = root / relative
        digest.update(relative.encode())
        if path.is_file() and not path.is_symlink():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            digest.update(b"missing")
    return digest.hexdigest()


def _codex_fingerprint_targets(home: Path) -> tuple[Path, ...]:
    return (
        home / "config.toml",
        home / "plugins",
        home / "plugins" / "cache",
        home / "plugins" / "index.json",
    )


def _claude_fingerprint_targets(home: Path) -> tuple[Path, ...]:
    return (
        home / "settings.json",
        home / "plugins",
        home / "plugins" / "cache",
        home / "plugins" / "known_marketplaces.json",
        home / "plugins" / "installed_plugins.json",
        home / "plugins" / "marketplaces",
    )


def _fingerprint_scope(targets: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for target in targets:
        _fingerprint_one(digest, target)
    return digest.hexdigest()


def _fingerprint_one(digest: object, target: Path) -> None:
    updater = getattr(digest, "update", None)
    if updater is None:
        msg = "digest missing update"
        raise TypeError(msg)
    if not target.exists():
        updater(b"missing:")
        updater(str(target).encode())
        return
    kind = "dir" if target.is_dir() else "file"
    if target.is_file():
        stat = target.stat()
        updater(str(target).encode())
        updater(kind.encode())
        updater(str(stat.st_size).encode())
        updater(oct(stat.st_mode & 0o777).encode())
        updater(hashlib.sha256(target.read_bytes()).digest())
        return
    for file_path in sorted(path for path in target.rglob("*") if path.is_file()):
        stat = file_path.stat()
        relative = str(file_path.relative_to(target))
        updater(relative.encode())
        updater(b"file")
        updater(str(stat.st_size).encode())
        updater(oct(stat.st_mode & 0o777).encode())
        updater(hashlib.sha256(file_path.read_bytes()).digest())


def _cache_files(cache: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(path.relative_to(cache))
            for path in cache.rglob("*")
            if path.is_file() or path.is_symlink()
        ),
    )


def _is_publishable_relative(relative: str) -> bool:
    if _is_unsafe_relative(relative):
        return False
    public_dirs = tuple(
        path for path in PUBLIC_SECRET_SCAN_PATHS if not Path(path).suffix and "/" not in path
    )
    public_files = frozenset(path for path in PUBLIC_SECRET_SCAN_PATHS if Path(path).suffix)
    if relative in public_files or relative in PUBLIC_SECRET_SCAN_PATHS:
        return True
    if "/" not in relative:
        return relative in public_files or relative in {
            "README.md",
            "pyproject.toml",
            "uv.lock",
            ".gitignore",
            ".mcp.json",
        }
    if any(relative.startswith(f"{directory}/") for directory in public_dirs):
        return True
    return relative.startswith("data/saxo/")


def _is_unsafe_relative(relative: str) -> bool:
    path = Path(relative)
    if path.name in {
        ".env",
        "credentials.json",
        "state.json",
        "token_cache.json",
        "token-cache.json",
    }:
        return True
    if any(part in UNSAFE_PATH_PARTS for part in path.parts):
        return True
    return bool(UNSAFE_NAME_PATTERN.search(relative))


def _is_runtime_artifact(relative: str) -> bool:
    parts = set(Path(relative).parts)
    return bool(parts & {".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"})


def _same_bytes(source: Path, installed: Path) -> bool:
    return (
        source.is_file()
        and installed.is_file()
        and not source.is_symlink()
        and not installed.is_symlink()
        and source.read_bytes() == installed.read_bytes()
    )
