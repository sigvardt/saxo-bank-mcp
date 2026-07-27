from __future__ import annotations

import json
from pathlib import Path

from test_agent_skill_evidence_support import ROOT, build_install_fixture, run_cli

from saxo_bank_mcp.agent_skill_command_runner import CommandResult
from saxo_bank_mcp.agent_skill_install_cli_driver import (
    CommandDiscoveryError,
    _cache_root_from_payload,
    cache_root_from_results,
)
from saxo_bank_mcp.agent_skill_install_models import CommandReceipt
from saxo_bank_mcp.agent_skill_install_paths import (
    OWNER_ONLY_MODE,
    export_publishable_tree,
    forbidden_cache_paths,
    global_state_fingerprint,
    harden_preserved_roots,
    owner_only_from_modes,
    owner_only_mode,
    publishable_tracked_files,
)
from saxo_bank_mcp.agent_skill_install_qa import (
    load_verified_install_report,
    write_install_fixture,
)

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


def test_harden_preserved_roots_records_final_owner_only_modes(tmp_path: Path) -> None:
    # Given: preserved roots that start world-readable.
    roots = {
        "run_root": tmp_path / "run",
        "clone": tmp_path / "run" / "source-clone",
        "codex_cache": tmp_path / "run" / "codex-cache",
        "claude_cache": tmp_path / "run" / "claude-cache",
        "home": tmp_path / "run" / "home",
        "codex_home": tmp_path / "run" / "codex-home",
        "claude_home": tmp_path / "run" / "claude-home",
    }
    for path in roots.values():
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o755)

    # When: permissions are hardened before report construction.
    modes, errors = harden_preserved_roots(roots)

    # Then: reported modes equal final on-disk owner-only modes.
    assert errors == ()
    assert owner_only_from_modes(modes) is True
    for label, path in roots.items():
        assert modes[label] == OWNER_ONLY_MODE
        assert owner_only_mode(path) == modes[label] == OWNER_ONLY_MODE


def test_verify_rejects_0755_preserved_root(tmp_path: Path) -> None:
    # Given: a complete install report whose clone root is left at 0755.
    fixture = build_install_fixture(tmp_path / "fixture")
    report = json.loads(fixture.report.read_text(encoding="utf-8"))
    clone = Path(report["clone"]["path"])
    clone.chmod(0o755)
    report["fixture_cleanup"]["modes"]["clone"] = "0o755"
    report["clone"]["mode"] = "0o755"
    report["fixture_cleanup"]["owner_only"] = True
    fixture.report.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    # When: verification re-checks reported modes against disk.
    verified, errors = load_verified_install_report(fixture.report)

    # Then: a non-owner-only preserved root fails closed.
    assert verified is None
    assert any(
        error in {"preserved_roots_not_owner_only", "preserved_root_not_owner_only:clone"}
        or error.startswith(("preserved_mode_mismatch:", "preserved_root_not_owner_only:"))
        for error in errors
    )


def test_verify_rejects_report_mode_that_does_not_match_disk(tmp_path: Path) -> None:
    # Given: disk is owner-only but the report still claims 0755 for clone.
    fixture = build_install_fixture(tmp_path / "fixture")
    report = json.loads(fixture.report.read_text(encoding="utf-8"))
    clone = Path(report["clone"]["path"])
    clone.chmod(0o700)
    report["fixture_cleanup"]["modes"]["clone"] = "0o755"
    report["clone"]["mode"] = "0o755"
    fixture.report.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    verified, errors = load_verified_install_report(fixture.report)

    assert verified is None
    assert "preserved_mode_mismatch:clone" in errors or "clone_mode_mismatch" in errors
    assert "preserved_roots_not_owner_only" in errors
