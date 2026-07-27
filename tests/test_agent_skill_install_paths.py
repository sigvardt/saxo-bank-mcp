from __future__ import annotations

import json
from pathlib import Path

from test_agent_skill_evidence_support import ROOT, run_cli

from saxo_bank_mcp.agent_skill_command_runner import CommandResult
from saxo_bank_mcp.agent_skill_install_cli_driver import (
    CommandDiscoveryError,
    _cache_root_from_payload,
    cache_root_from_results,
)
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_install_paths import (
    export_publishable_tree,
    forbidden_cache_paths,
    global_state_fingerprint,
    publishable_tracked_files,
)
from saxo_bank_mcp.agent_skill_install_qa import write_install_fixture

INSTALL_QA = ROOT / "scripts/qa_dual_plugin_install.py"


def test_publishable_tree_excludes_omo_and_forbidden() -> None:
    # Given: the candidate worktree contains tracked .omo evidence.
    relatives = publishable_tracked_files(ROOT)

    # When / Then: publishable inventory never includes private path classes.
    assert relatives
    assert all(not relative.startswith(".omo/") for relative in relatives)
    assert all(".git" not in relative.split("/") for relative in relatives)


def test_export_publishable_tree_has_no_git_or_omo(tmp_path: Path) -> None:
    dest = tmp_path / "market"
    export_publishable_tree(ROOT, dest)

    assert not (dest / ".git").exists()
    assert not (dest / ".omo").exists()
    assert (dest / "pyproject.toml").is_file()
    assert (dest / "skills/saxo-bank/SKILL.md").is_file()


def test_forbidden_cache_paths_detect_git_and_omo(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    (cache / ".git").mkdir(parents=True)
    (cache / ".omo" / "evidence").mkdir(parents=True)
    (cache / "ok.txt").write_text("x", encoding="utf-8")

    forbidden = forbidden_cache_paths(cache)

    assert any(path.startswith(".git") for path in forbidden)
    assert any(path.startswith(".omo") for path in forbidden)


def test_cache_root_discovery_requires_cli_payload_not_guess(tmp_path: Path) -> None:
    receipt = CommandReceipt(
        name="codex_plugin_add",
        argv=("codex", "plugin", "add"),
        cwd=str(tmp_path),
        pid=1,
        pgid=1,
        exit_code=0,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
    )
    empty = CommandResult(receipt=receipt, stdout="{}", stderr="")
    try:
        cache_root_from_results((empty,), client="codex")
        raised = False
    except CommandDiscoveryError:
        raised = True
    assert raised


def test_cache_root_parses_camel_case_cli_keys(tmp_path: Path) -> None:
    path = tmp_path / "installed"
    path.mkdir()
    assert _cache_root_from_payload({"installedPath": str(path)}) == path
    assert _cache_root_from_payload([{"installPath": str(path)}]) == path


def test_global_fingerprint_is_scoped_and_stable(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    (codex / "plugins").mkdir(parents=True)
    (claude / "plugins").mkdir(parents=True)
    (codex / "config.toml").write_text("x=1\n", encoding="utf-8")
    (claude / "settings.json").write_text("{}\n", encoding="utf-8")

    first = global_state_fingerprint(codex, claude)
    second = global_state_fingerprint(codex, claude)

    assert first == second
    assert len(first["codex"]) == 64  # noqa: PLR2004
    assert len(first["claude"]) == 64  # noqa: PLR2004


def test_self_test_fixtures_expose_only_class_or_field(tmp_path: Path) -> None:
    private_out = tmp_path / "private.json"
    drift_out = tmp_path / "drift.json"

    private = run_cli(INSTALL_QA, "--self-test-fixture", "private-file", "--out", str(private_out))
    drift = run_cli(INSTALL_QA, "--self-test-fixture", "version-drift", "--out", str(drift_out))
    private_payload = json.loads(private_out.read_text(encoding="utf-8"))
    drift_payload = json.loads(drift_out.read_text(encoding="utf-8"))

    assert private.returncode != 0
    assert drift.returncode != 0
    assert private_payload == {
        "fixture": "private-file",
        "forbidden_path_class": ".omo",
        "status": "failed",
    }
    assert drift_payload == {
        "fixture": "version-drift",
        "status": "failed",
        "version_field": "project.version",
    }
    assert "token" not in json.dumps(private_payload).lower()
    assert "secret" not in json.dumps(drift_payload).lower()


def test_write_install_fixture_helpers_match_cli(tmp_path: Path) -> None:
    out = tmp_path / "x.json"
    assert write_install_fixture("private-file", out) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["forbidden_path_class"] == ".omo"
