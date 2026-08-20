from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
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
    "uv.lock",
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
ALLOWED_INSTALLER_METADATA: Final = (
    ".claude-plugin/.install-metadata.json",
    ".codex-plugin/.install-metadata.json",
)
UNSAFE_BASENAMES: Final = frozenset(
    {
        ".env",
        "auth.json",
        "credentials.json",
        "id_rsa",
        "id_rsa.pub",
        "state.json",
        "token-cache.json",
        "token_cache.json",
    },
)
UNSAFE_BASENAMES_LOWER: Final = frozenset(name.lower() for name in UNSAFE_BASENAMES)
UNSAFE_BASENAME_PATTERN: Final = re.compile(
    r"(?i)^("
    r"\.env(\..+)?"
    r"|.*credentials?\.json"
    r"|token[_-]cache\.json"
    r"|auth\.json"
    r"|state\.json"
    r"|id_rsa(\.pub)?"
    r"|.*private[_-]?key.*"
    r"|.*password.*"
    r")$",
)
UNSAFE_SUFFIXES: Final = frozenset({".key", ".p12", ".pem", ".pfx"})
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
UNSAFE_PATH_PARTS_LOWER: Final = frozenset(part.lower() for part in UNSAFE_PATH_PARTS)


def publishable_tracked_files(source: Path) -> tuple[str, ...]:
    raw = git_output(source, "ls-files", "-z") or ""
    selected = [
        relative for relative in raw.split("\0") if relative and _is_publishable_relative(relative)
    ]
    return tuple(sorted(selected))


def export_publishable_tree(
    source: Path,
    destination: Path,
    *,
    publishable: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    destination.chmod(0o700)
    relatives = publishable if publishable is not None else publishable_tracked_files(source)
    for relative in relatives:
        src = source / relative
        _reject_non_regular_source(src, relative)
        if not src.exists():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target, follow_symlinks=False)
        _reject_non_regular_source(target, relative)
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
        installed = cache / relative
        if _node_kind(installed) == "symlink":
            mismatches.append(f"symlink:{relative}")
            continue
        if _node_kind(installed) not in {"file", "missing"}:
            mismatches.append(f"non_regular:{relative}")
            continue
        if not _same_bytes(source / relative, installed):
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
        kind = _node_kind(path)
        if kind == "symlink" or is_unsafe_relative(relative):
            findings.append(relative)
            continue
        if kind not in {"file", "dir"}:
            findings.append(f"{kind}:{relative}")
    return sorted(findings)


def scrub_runtime_artifacts(root: Path) -> None:
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        relative = str(path.relative_to(root))
        if not _is_runtime_artifact(relative):
            continue
        if path.is_dir() and not path.is_symlink():
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
            "codex": [
                f"${{CODEX_GLOBAL_HOME}}/{path.relative_to(codex_global_home)}"
                if path != codex_global_home
                else "${CODEX_GLOBAL_HOME}"
                for path in _codex_fingerprint_targets(codex_global_home)
            ],
            "claude": [
                f"${{CLAUDE_GLOBAL_HOME}}/{path.relative_to(claude_global_home)}"
                if path != claude_global_home
                else "${CLAUDE_GLOBAL_HOME}"
                for path in _claude_fingerprint_targets(claude_global_home)
            ],
            "fields": ["path", "type", "size", "mode", "target", "sha256"],
            "roots": {
                "CODEX_GLOBAL_HOME": "caller_codex_global_home",
                "CLAUDE_GLOBAL_HOME": "caller_claude_global_home",
            },
        },
    }


def codex_global_state_fingerprint(codex_global_home: Path) -> dict[str, JsonValue]:
    """Privacy-safe fingerprint of only the caller's Codex plugin state."""
    targets = _codex_fingerprint_targets(codex_global_home)
    return {
        "codex": _fingerprint_scope(targets),
        "scope": {
            "codex": [
                f"${{CODEX_GLOBAL_HOME}}/{path.relative_to(codex_global_home)}"
                if path != codex_global_home
                else "${CODEX_GLOBAL_HOME}"
                for path in targets
            ],
            "fields": ["path", "type", "size", "mode", "target", "sha256"],
            "roots": {"CODEX_GLOBAL_HOME": "caller_codex_global_home"},
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
    # lexists: include dangling symlinks.
    if not os.path.lexists(target):
        updater(b"missing:")
        updater(str(target).encode())
        return
    kind = _node_kind(target)
    updater(str(target).encode())
    updater(kind.encode())
    if kind == "symlink":
        try:
            mode = oct(target.lstat().st_mode & 0o777)
            link_target = str(target.readlink())
        except OSError:
            mode = "unknown"
            link_target = "unreadable"
        updater(mode.encode())
        updater(link_target.encode())
        return
    if kind == "file":
        stat_result = target.stat()
        updater(str(stat_result.st_size).encode())
        updater(oct(stat_result.st_mode & 0o777).encode())
        updater(hashlib.sha256(target.read_bytes()).digest())
        return
    if kind == "dir":
        stat_result = target.stat()
        updater(oct(stat_result.st_mode & 0o777).encode())
        try:
            entries = sorted(target.iterdir(), key=lambda item: item.name)
        except OSError:
            updater(b"dir_unreadable")
            return
        for entry in entries:
            relative = str(entry.relative_to(target))
            updater(relative.encode())
            _fingerprint_one(digest, entry)
        return
    # fifo/socket/device/other
    try:
        stat_result = target.lstat()
        updater(str(stat_result.st_mode).encode())
        updater(str(stat_result.st_rdev).encode())
    except OSError:
        updater(b"special_unreadable")


def _cache_files(cache: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(path.relative_to(cache))
            for path in cache.rglob("*")
            if path.is_file() or path.is_symlink() or _node_kind(path) not in {"file", "dir"}
        ),
    )


def is_unsafe_relative(relative: str) -> bool:
    path = Path(relative)
    name = path.name
    if name.lower() in UNSAFE_BASENAMES_LOWER or bool(UNSAFE_BASENAME_PATTERN.fullmatch(name)):
        return True
    if path.suffix.lower() in UNSAFE_SUFFIXES:
        return True
    parts = path.parts[:-1] or path.parts
    return any(part.lower() in UNSAFE_PATH_PARTS_LOWER for part in parts)


def _is_publishable_relative(relative: str) -> bool:
    if is_unsafe_relative(relative):
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
    return relative.startswith(("data/saxo/", "data/analytics/"))


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


def reject_non_regular_source(path: Path, relative: str) -> None:
    """Reject symlinks and non-regular filesystem nodes for publishable trees."""
    if not os.path.lexists(path):
        return
    kind = _node_kind(path)
    if kind == "symlink":
        msg = f"symlink_rejected:{relative}"
        raise ValueError(msg)
    if kind != "file":
        msg = f"non_regular_rejected:{kind}:{relative}"
        raise ValueError(msg)


def _reject_non_regular_source(path: Path, relative: str) -> None:
    reject_non_regular_source(path, relative)


def _node_kind(path: Path) -> str:  # noqa: PLR0911
    if not os.path.lexists(path):
        return "missing"
    if path.is_symlink():
        return "symlink"
    try:
        mode = path.lstat().st_mode
    except OSError:
        return "unknown"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode):
        return "char_device"
    if stat.S_ISBLK(mode):
        return "block_device"
    return "other"
