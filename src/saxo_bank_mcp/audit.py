from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from saxo_bank_mcp._evidence import JsonValue, now_utc

_AUDIT_SUBPATH: Final = Path(".local/state/saxo-bank-mcp/audit")
_AUDIT_FILE_NAME: Final = "events.jsonl"
_SYNC_DIR_NAMES: Final = (
    "Desktop",
    "Documents",
    "Downloads",
    "Dropbox",
    "Google Drive",
    "OneDrive",
    "iCloud Drive",
)
_APPLE_ICLOUD_DIR: Final = Path("Library/Mobile Documents")


class AuditPathError(Exception):
    def __init__(self, path: Path, reason: str) -> None:  # noqa: D107
        self.path = path
        self.reason = reason
        super().__init__(f"refusing audit path {path}: {reason}")


def default_audit_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured is not None and configured.strip():
        return Path(configured) / "saxo-bank-mcp" / "audit"
    return Path.home() / _AUDIT_SUBPATH


def audit_dir_path(path: Path | None = None, *, repo_root: Path | None = None) -> Path:
    candidate = (default_audit_dir() if path is None else path).expanduser()
    resolved = candidate.resolve(strict=False)
    actual_repo_root = repo_root if repo_root is not None else _find_repo_root()
    if actual_repo_root is not None and resolved.is_relative_to(
        actual_repo_root.resolve(strict=False),
    ):
        raise AuditPathError(candidate, "inside repository")
    for root in _common_sync_roots():
        if resolved.is_relative_to(root.resolve(strict=False)):
            raise AuditPathError(candidate, "inside common synced folder")
    return resolved


def audit_log_path(path: Path | None = None, *, repo_root: Path | None = None) -> Path:
    return audit_dir_path(path, repo_root=repo_root) / _AUDIT_FILE_NAME


def append_audit_event(
    audit_dir: Path,
    event: dict[str, JsonValue],
    *,
    repo_root: Path | None = None,
) -> Path:
    log_path = audit_log_path(audit_dir, repo_root=repo_root)
    payload = {"checked_at": now_utc(), **event}
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_path.parent.chmod(0o700)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as file:
        file.write(json.dumps(payload, sort_keys=True))
        file.write("\n")
    log_path.chmod(0o600)
    return log_path


def audit_file_mode(path: Path) -> str | None:
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return None


def _common_sync_roots() -> tuple[Path, ...]:
    home = Path.home()
    return (*(home / name for name in _SYNC_DIR_NAMES), home / _APPLE_ICLOUD_DIR)


def _find_repo_root() -> Path | None:
    try:
        start = Path(__file__).resolve()
    except OSError:
        return None
    for parent in start.parents:
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            return parent
    return None
