from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypedDict

from pydantic import TypeAdapter, ValidationError

from saxo_bank_mcp.auth import SaxoPendingAuthorization, SaxoTokenSet

_STATE_SUBPATH: Final = Path(".local/state/saxo-bank-mcp/token-cache.json")
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
_TOKEN_ADAPTER: Final = TypeAdapter(SaxoTokenSet)
_PENDING_ADAPTER: Final = TypeAdapter(SaxoPendingAuthorization)


@dataclass(frozen=True, slots=True)
class TokenCachePathError(Exception):
    path: Path
    reason: str

    def __str__(self) -> str:
        """Return a safe cache-path refusal message."""
        return f"refusing token cache path: {self.reason}"


class TokenCacheInspection(TypedDict):
    present: bool
    readable: bool
    token: SaxoTokenSet | None


class PendingAuthorizationInspection(TypedDict):
    present: bool
    readable: bool
    pending: SaxoPendingAuthorization | None


def default_token_cache_path() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured is not None and configured.strip():
        return Path(configured) / "saxo-bank-mcp" / "token-cache.json"
    return Path.home() / _STATE_SUBPATH


def token_cache_path(path: Path | None = None, *, repo_root: Path | None = None) -> Path:
    candidate = (default_token_cache_path() if path is None else path).expanduser()
    resolved = candidate.resolve(strict=False)
    actual_repo_root = repo_root if repo_root is not None else _find_repo_root()
    if actual_repo_root is not None and resolved.is_relative_to(
        actual_repo_root.resolve(strict=False),
    ):
        raise TokenCachePathError(candidate, "inside repository")
    for root in _common_sync_roots():
        if resolved.is_relative_to(root.resolve(strict=False)):
            raise TokenCachePathError(candidate, "inside common synced folder")
    return resolved


def save_token_cache(path: Path, token: SaxoTokenSet) -> None:
    _write_owner_only(path, token.model_dump_json())


def load_token_cache(path: Path) -> SaxoTokenSet | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return None
    try:
        return _TOKEN_ADAPTER.validate_json(text)
    except ValidationError:
        return None


def inspect_token_cache(path: Path) -> TokenCacheInspection:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"present": False, "readable": False, "token": None}
    except (OSError, UnicodeDecodeError):
        return {"present": True, "readable": False, "token": None}
    try:
        token = _TOKEN_ADAPTER.validate_json(text)
    except ValidationError:
        return {"present": True, "readable": False, "token": None}
    return {"present": True, "readable": True, "token": token}


def pending_authorization_path(cache_path: Path) -> Path:
    return cache_path.with_name(f"{cache_path.name}.pending-pkce.json")


def save_pending_authorization(path: Path, pending: SaxoPendingAuthorization) -> None:
    _write_owner_only(path, pending.model_dump_json())


def load_pending_authorization(path: Path) -> SaxoPendingAuthorization | None:
    return inspect_pending_authorization(path)["pending"]


def inspect_pending_authorization(path: Path) -> PendingAuthorizationInspection:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"present": False, "readable": False, "pending": None}
    except (OSError, UnicodeDecodeError):
        return {"present": True, "readable": False, "pending": None}
    try:
        pending = _PENDING_ADAPTER.validate_json(text)
    except ValidationError:
        return {"present": True, "readable": False, "pending": None}
    return {"present": True, "readable": True, "pending": pending}


def delete_pending_authorization(path: Path) -> None:
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return


def _write_owner_only(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as file:
            tmp_path = Path(file.name)
            tmp_path.chmod(0o600)
            file.write(text)
            file.write("\n")
        tmp_path.replace(path)
    except OSError:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise
    path.chmod(0o600)


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
