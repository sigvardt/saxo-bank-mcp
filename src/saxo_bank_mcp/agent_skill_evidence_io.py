from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path


def git_output(repo: Path, *args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            (git, "-C", str(repo), *args), capture_output=True, check=False, text=True
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_commit(repo: Path, commit: str) -> str | None:
    return git_output(repo, "rev-parse", "--verify", f"{commit}^{{commit}}")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
