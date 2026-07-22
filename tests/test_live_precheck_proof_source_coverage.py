from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from saxo_bank_mcp.live_precheck_proof_audit import source_provenance


def test_source_provenance_hashes_dirty_runtime_sources(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    source = repo / "src/package/module.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname = 'proof'\n", encoding="utf-8")
    commands = (
        ("init",),
        ("config", "user.name", "Proof Test"),
        ("config", "user.email", "proof.invalid"),
        ("add", "."),
        ("commit", "-m", "initial"),
    )
    for command in commands:
        subprocess.run([git, *command], cwd=repo, check=True, capture_output=True)
    source.write_text("VALUE = 2\n", encoding="utf-8")
    untracked = repo / "src/package/new.py"
    untracked.write_text("NEW = True\n", encoding="utf-8")

    provenance = source_provenance(repo)

    assert provenance.complete is True
    assert (
        provenance.git_head
        == subprocess.run(
            [git, "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert provenance.dirty_source_sha256 == {
        "src/package/module.py": hashlib.sha256(source.read_bytes()).hexdigest(),
        "src/package/new.py": hashlib.sha256(untracked.read_bytes()).hexdigest(),
    }


def test_source_provenance_hashes_dirty_tests_and_docs(tmp_path: Path) -> None:
    git = shutil.which("git")
    assert git is not None
    repo = tmp_path / "repo"
    test_file = repo / "tests/test_proof.py"
    doc_file = repo / "docs/proof.md"
    policy_file = repo / "data/saxo/policy.json"
    test_file.parent.mkdir(parents=True)
    doc_file.parent.mkdir(parents=True)
    policy_file.parent.mkdir(parents=True)
    test_file.write_text("def test_initial(): pass\n", encoding="utf-8")
    doc_file.write_text("initial\n", encoding="utf-8")
    policy_file.write_text('{"status":"initial"}\n', encoding="utf-8")
    for command in (
        ("init",),
        ("config", "user.name", "Proof Test"),
        ("config", "user.email", "proof.invalid"),
        ("add", "."),
        ("commit", "-m", "initial"),
    ):
        subprocess.run([git, *command], cwd=repo, check=True, capture_output=True)
    test_file.write_text("def test_changed(): pass\n", encoding="utf-8")
    doc_file.write_text("changed\n", encoding="utf-8")
    policy_file.write_text('{"status":"changed"}\n', encoding="utf-8")

    provenance = source_provenance(repo)

    assert provenance.complete is True
    assert provenance.dirty_source_sha256 == {
        "data/saxo/policy.json": hashlib.sha256(policy_file.read_bytes()).hexdigest(),
        "docs/proof.md": hashlib.sha256(doc_file.read_bytes()).hexdigest(),
        "tests/test_proof.py": hashlib.sha256(test_file.read_bytes()).hexdigest(),
    }
