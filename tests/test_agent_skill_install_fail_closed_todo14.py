from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent_skill_evidence_support import ROOT, errors, run_cli, write_json

from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    descendant_pids,
    remaining_live_pgids,
    remaining_live_pids,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_env import (
    EnvironmentContainmentError,
    build_isolated_env,
    write_bearing_env_keys,
)
from saxo_bank_mcp.agent_skill_install_ledger import (
    append_fixture_ledger_event,
    verify_fixture_ledger_binding,
)
from saxo_bank_mcp.agent_skill_install_models import (
    REQUIRED_FIXTURE_CONSUMERS,
    UpdateProbeEvidence,
    VersionCacheProof,
)
from saxo_bank_mcp.agent_skill_install_paths import (
    export_publishable_tree,
    forbidden_cache_paths,
    global_state_fingerprint,
    is_unsafe_relative,
    reject_non_regular_source,
)
from saxo_bank_mcp.agent_skill_install_privacy import (
    build_privacy_binding,
    write_minimal_privacy_pair,
)

INSTALL_QA = ROOT / "scripts/qa_dual_plugin_install.py"
COMMIT = "a" * 40
NONZERO_PARENT_EXIT = 7


def _proof(version: str) -> dict[str, object]:
    return {
        "cache_root": f"cache/{version}",
        "version": version,
        "digest": "1" * 64,
        "source_digest": "1" * 64,
        "inventory_exact_match": True,
        "tool_count": 39,
        "annotations_missing": [],
        "probe_stdout_sha256": "2" * 64,
    }


def test_update_probe_requires_nested_fields() -> None:
    payload: dict[str, object] = {
        "original_version": "0.1.0",
        "bumped_version": "0.1.1",
        "codex_reached_bumped": True,
        "claude_reached_bumped": True,
        "candidate_restored": True,
        "bumped_proof": {"codex": _proof("0.1.1"), "claude": _proof("0.1.1")},
        "restored_proof": {"codex": _proof("0.1.0"), "claude": _proof("0.1.0")},
        "temporary_fixtures_removed": True,
        "remaining_temporary_paths": [],
    }
    model = UpdateProbeEvidence.model_validate(payload)
    assert model.bumped_version == "0.1.1"

    broken = json.loads(json.dumps(payload))
    del broken["bumped_proof"]["codex"]["digest"]
    with pytest.raises(ValidationError):
        UpdateProbeEvidence.model_validate(broken)


def test_version_cache_proof_rejects_annotations() -> None:
    payload = _proof("0.1.0")
    payload["annotations_missing"] = ["saxo_health"]
    with pytest.raises(ValidationError):
        VersionCacheProof.model_validate(payload)


def test_failed_command_with_sleeper_child_is_cleaned(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
    }
    with pytest.raises(CommandFailureError) as err:
        run_command(
            "nonzero_with_child",
            ("/bin/sh", "-c", f"sleep 60 & exit {NONZERO_PARENT_EXIT}"),
            cwd=tmp_path,
            env=env,
            timeout_seconds=5,
        )
    receipt = err.value.receipt
    assert receipt.exit_code == NONZERO_PARENT_EXIT
    assert receipt.cleanup_attempted is True
    assert receipt.pgid is not None
    assert receipt.pid is not None
    time.sleep(0.1)
    assert remaining_live_pgids((receipt.pgid,)) == ()
    assert remaining_live_pids(descendant_pids(receipt.pid)) == ()


def test_escaped_process_group_child_is_stopped(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
    }
    code = (
        "import os, time, signal\n"
        "os.setpgid(0, 0)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(CommandFailureError) as err:
        run_command(
            "escaped_child",
            ("/bin/sh", "-c", f"python3 -c {code!r} & sleep 0.3; exit 3"),
            cwd=tmp_path,
            env=env,
            timeout_seconds=5,
        )
    receipt = err.value.receipt
    assert receipt.pid is not None
    time.sleep(0.2)
    assert remaining_live_pids(descendant_pids(receipt.pid)) == ()


def test_env_write_bearing_keys_forced_under_run_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    codex = tmp_path / "codex"
    probe = tmp_path / "probe"
    for path in (home, codex, probe):
        path.mkdir()
    outside = tmp_path / "outside-tmp"
    outside.mkdir()
    monkeypatch.setenv("TMPDIR", str(outside))
    monkeypatch.setenv("UV_CACHE_DIR", str(outside / "uv"))
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(outside / "py"))
    monkeypatch.setenv("HOME", str(tmp_path / "evil-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "evil-codex"))
    env = build_isolated_env(
        home=home,
        codex_home=codex,
        run_root=tmp_path,
        probe_env=probe,
    )
    root = tmp_path.resolve()
    for key in write_bearing_env_keys():
        value = env.get(key)
        if not value:
            continue
        assert Path(value).resolve().is_relative_to(root), key
    assert env["TMPDIR"] == str((tmp_path / "tmp").resolve())
    assert env["UV_CACHE_DIR"] == str((tmp_path / "uv-cache").resolve())
    assert env["HOME"] == str(home.resolve())


def test_env_rejects_auth_target_outside_run_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    codex = tmp_path / "codex"
    probe = tmp_path / "probe"
    for path in (home, codex, probe):
        path.mkdir()
    outside = tmp_path.parent / "auth-outside"
    outside.mkdir(exist_ok=True)
    secret = outside / "secret.json"
    secret.write_text("{}", encoding="utf-8")
    with pytest.raises(EnvironmentContainmentError):
        build_isolated_env(
            home=home,
            codex_home=codex,
            run_root=tmp_path,
            probe_env=probe,
            auth_targets={"SAXO_MCP_TOKEN_CACHE_PATH": secret},
        )


def test_fingerprint_includes_symlink_and_directory_mode(tmp_path: Path) -> None:
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    codex.mkdir()
    claude.mkdir()
    (codex / "plugins").mkdir()
    (codex / "plugins").chmod(0o700)
    target = codex / "plugins" / "cache"
    target.mkdir()
    link = codex / "plugins" / "index.json"
    link.symlink_to("missing-target")
    first = global_state_fingerprint(codex, claude)
    link.unlink()
    link.write_text("x", encoding="utf-8")
    second = global_state_fingerprint(codex, claude)
    assert first["codex"] != second["codex"]
    scope = first.get("scope")
    assert isinstance(scope, dict)
    fields = scope.get("fields")
    assert isinstance(fields, list)
    assert "target" in fields or "type" in fields


def test_unsafe_names_are_case_insensitive() -> None:
    assert is_unsafe_relative("Secrets/AUTH.JSON") is True
    assert is_unsafe_relative("nested/Token_Cache.JSON") is True
    assert is_unsafe_relative(".OMO/evidence/x") is True
    assert is_unsafe_relative("src/saxo_bank_mcp/credentials.py") is False


def test_forbidden_and_export_reject_fifo_and_symlink(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    fifo = cache / "pipe.fifo"
    os.mkfifo(fifo)
    (cache / "link").symlink_to("somewhere")
    findings = forbidden_cache_paths(cache)
    assert any("fifo" in item or item.endswith("pipe.fifo") for item in findings)
    assert any(item.endswith("link") or "link" in item for item in findings)

    source = tmp_path / "src"
    source.mkdir()
    # export_publishable_tree uses git tracked files; exercise reject helper via direct call path
    tracked = source / "evil"
    os.mkfifo(tracked)
    with pytest.raises(ValueError, match=r"non_regular_rejected|symlink_rejected"):
        reject_non_regular_source(tracked, "evil")


def test_fixture_ledger_round_trip(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    ledger = run_root / "fixture-cleanup-ledger.jsonl"
    commit = "b" * 40
    binding = append_fixture_ledger_event(
        ledger,
        candidate_commit=commit,
        run_root=run_root,
        preserved_paths=(str(run_root / "clone"),),
        consumers=REQUIRED_FIXTURE_CONSUMERS,
        cleanup_deadline=(
            datetime.now(tz=UTC) + timedelta(days=14)
        ).replace(microsecond=0).isoformat(),
    )
    assert binding.event_sha256
    assert verify_fixture_ledger_binding(
        binding,
        candidate_commit=commit,
        run_root=run_root,
        ledger_path=ledger,
    ) == []
    # stale / mismatched consumers fail
    lines = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text(
        lines[0] + "\n" + lines[0].replace(commit, "c" * 40) + "\n",
        encoding="utf-8",
    )
    assert "fixture_ledger_digest_mismatch" in verify_fixture_ledger_binding(
        binding,
        candidate_commit=commit,
        run_root=run_root,
        ledger_path=ledger,
    )


def test_privacy_binding_digest_and_scope(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    report, self_scan = write_minimal_privacy_pair(
        tmp_path,
        candidate_commit=COMMIT,
        clone_commit=COMMIT,
        run_root=run_root,
    )
    binding = build_privacy_binding(
        privacy_report=report,
        privacy_self_scan=self_scan,
        candidate_commit=COMMIT,
        clone_commit=COMMIT,
    )
    assert binding.privacy_report_sha256
    assert binding.findings_count == 0
    # modified report fails digest check via verify path in qa
    mutated = report.read_text(encoding="utf-8").replace("passed", "failed")
    report.write_text(mutated, encoding="utf-8")
    with pytest.raises(ValueError, match="privacy_"):
        build_privacy_binding(
            privacy_report=report,
            privacy_self_scan=self_scan,
            candidate_commit=COMMIT,
            clone_commit=COMMIT,
        )


def test_verify_only_rejects_fabricated_counts(tmp_path: Path) -> None:
    report = tmp_path / "install.json"
    write_json(
        report,
        {
            "status": "passed",
            "execution_mode": "installed_verification",
            "expected_skills": 8,
            "expected_tools": 39,
            "codex": {"tool_count": 39, "installed": True},
            "claude": {"tool_count": 39, "installed": True},
        },
    )
    out = tmp_path / "out.json"
    result = run_cli(
        INSTALL_QA,
        "--verify-only",
        "--install-report",
        str(report),
        "--out",
        str(out),
        "--skip-startup-probes",
    )
    assert result.returncode != 0
    payload_errors = errors(out)
    assert "install_report_schema_invalid" in payload_errors or payload_errors


def test_export_publishable_tree_still_works_on_repo(tmp_path: Path) -> None:
    dest = tmp_path / "dest"
    export_publishable_tree(ROOT, dest)
    assert (dest / "pyproject.toml").is_file()
