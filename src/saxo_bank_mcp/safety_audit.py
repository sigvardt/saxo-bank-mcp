from __future__ import annotations

import hashlib
import json
from pathlib import Path

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.audit import AuditPathError, append_audit_event
from saxo_bank_mcp.safety_models import WritePreviewRequest


def request_fingerprint(request: WritePreviewRequest) -> str:
    payload = request.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def try_audit_denial(audit_dir: Path, event: dict[str, JsonValue]) -> Path | None:
    try:
        return append_audit_event(audit_dir, event)
    except (AuditPathError, OSError):
        return None


def is_inside_repo(path: Path) -> bool:
    repo_root = find_repo_root()
    if repo_root is None:
        return False
    return path.resolve(strict=False).is_relative_to(repo_root.resolve(strict=False))


def audit_mode(path: Path) -> str | None:
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return None


def find_repo_root() -> Path | None:
    try:
        start = Path(__file__).resolve()
    except OSError:
        return None
    for parent in start.parents:
        if (parent / ".git").exists() or (parent / "pyproject.toml").exists():
            return parent
    return None
