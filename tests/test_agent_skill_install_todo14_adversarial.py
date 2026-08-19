from __future__ import annotations

import hashlib
import json
import os
import string
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from saxo_bank_mcp import agent_skill_command_runner as command_runner
from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    CommandResult,
    descendant_pids,
    remaining_live_pgids,
    remaining_live_pids,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_cli_driver import build_client_report
from saxo_bank_mcp.agent_skill_install_env import (
    DisposableCleanupError,
    cleanup_disposable_isolated_state,
    cleanup_verify_scratch_state,
    enumerated_disposable_paths,
    enumerated_verify_scratch_paths,
    require_disposable_cleanup,
)
from saxo_bank_mcp.agent_skill_install_ledger import (
    append_fixture_ledger_event,
    bind_existing_ledger_event,
    expected_preserved_paths,
    ledger_path_digest,
    verify_fixture_ledger_binding,
)
from saxo_bank_mcp.agent_skill_install_models import (
    EXPECTED_TOOLS,
    REQUIRED_FIXTURE_CONSUMERS,
    CommandReceipt,
    FixtureLedgerBinding,
    StartupCheck,
    StartupEvidence,
    VersionCacheProof,
)
from saxo_bank_mcp.agent_skill_install_privacy import (
    PRIVACY_DIGEST_ZERO,
    PrivacyPipelineError,
    canonical_install_text_for_privacy_scan,
    coverage_union_errors,
    derive_existing_privacy_targets,
    produce_privacy_evidence,
    provisional_privacy_binding,
    scan_directory_normalized,
)
from saxo_bank_mcp.agent_skill_install_probe import (
    ProbePayloadError,
    startup_check_from_payload,
    startup_from_probes,
)
from saxo_bank_mcp.agent_skill_install_qa import verify_install_report
from saxo_bank_mcp.agent_skill_install_verify_live import (
    codex_registration_errors,
    global_fingerprint_pair,
    live_verify_errors,
    startup_probe_errors_for_caches,
)
from saxo_bank_mcp.secret_scan import scan_secret_text

COMMIT = "a" * 40
NONZERO = 7
SHA_LEN = 64


def test_codex_registration_rejects_enabled_true_elsewhere(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    cache = codex_home / "plugins" / "cache" / "sigvardt" / "saxo-bank-mcp" / "0.1.0"
    cache.mkdir(parents=True)
    config = codex_home / "config.toml"
    # enabled=true only on an unrelated plugin; target plugin missing/disabled.
    config.write_text(
        (
            '[plugins."other@x"]\n'
            "enabled = true\n"
            "\n"
            '[plugins."saxo-bank-mcp@sigvardt"]\n'
            "enabled = false\n"
        ),
        encoding="utf-8",
    )
    errors = codex_registration_errors(
        codex_home=codex_home,
        cache_root=cache.resolve(),
        version="0.1.0",
    )
    assert "codex_registration_not_enabled" in errors or "codex_registration_missing" in errors


def test_codex_registration_requires_exact_plugin_enabled(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    cache = codex_home / "plugins" / "cache" / "sigvardt" / "saxo-bank-mcp" / "0.1.0"
    cache.mkdir(parents=True)
    config = codex_home / "config.toml"
    config.write_text(
        '[plugins."saxo-bank-mcp@sigvardt"]\nenabled = true\n',
        encoding="utf-8",
    )
    assert (
        codex_registration_errors(
            codex_home=codex_home,
            cache_root=cache.resolve(),
            version="0.1.0",
        )
        == []
    )


def test_ledger_ignores_unrelated_trailing_events(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    for relative in (
        "source-clone",
        "home",
        "codex-home",
        "claude-home",
        "codex-home/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
        "home/.claude/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "external-ledger.jsonl"
    binding = append_fixture_ledger_event(
        ledger,
        candidate_commit=COMMIT,
        run_root=run_root,
        version="0.1.0",
        consumers=REQUIRED_FIXTURE_CONSUMERS,
    )
    # Unrelated trailing event must not be selected.
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + json.dumps(
            {
                "event": "fixture_retained",
                "candidate_commit": "b" * 40,
                "run_root": str(run_root.resolve()),
                "preserved_paths": list(expected_preserved_paths(run_root, version="0.1.0")),
                "consumers": list(REQUIRED_FIXTURE_CONSUMERS),
                "teardown_owner": "post-final-completion-gate",
                "owner_only": True,
                "cleanup_deadline": (datetime.now(tz=UTC) + timedelta(days=10))
                .replace(microsecond=0)
                .isoformat(),
                "registered_at": datetime.now(tz=UTC).replace(microsecond=0).isoformat(),
            },
        )
        + "\n",
        encoding="utf-8",
    )
    assert (
        verify_fixture_ledger_binding(
            binding,
            candidate_commit=COMMIT,
            run_root=run_root,
            version="0.1.0",
            ledger_path=ledger,
        )
        == []
    )
    rebound, errors = bind_existing_ledger_event(
        ledger,
        candidate_commit=COMMIT,
        run_root=run_root,
        version="0.1.0",
    )
    assert errors == []
    assert rebound is not None
    assert rebound.event_sha256 == binding.event_sha256
    assert rebound.ledger_path_sha256 == ledger_path_digest(ledger)


def test_ledger_rejects_relative_path_and_omitted_ledger(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    binding = FixtureLedgerBinding(
        ledger_path_sha256="a" * 64,
        event_sha256="b" * 64,
        candidate_commit=COMMIT,
        consumers=REQUIRED_FIXTURE_CONSUMERS,
        teardown_owner="post-final-completion-gate",
        owner_only=True,
        cleanup_deadline="2099-01-01T00:00:00+00:00",
        run_root="run_root",
    )
    assert verify_fixture_ledger_binding(
        binding,
        candidate_commit=COMMIT,
        run_root=run_root,
        version="0.1.0",
        ledger_path=None,
    ) == ["fixture_ledger_required"]
    with pytest.raises(ValueError, match="ledger_path_must_be_absolute"):
        append_fixture_ledger_event(
            Path("relative.jsonl"),
            candidate_commit=COMMIT,
            run_root=run_root,
            version="0.1.0",
            consumers=REQUIRED_FIXTURE_CONSUMERS,
        )


def test_ledger_rejects_stale_deadline_and_path_substitution(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    for relative in (
        "source-clone",
        "home",
        "codex-home",
        "claude-home",
        "codex-home/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
        "home/.claude/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    ledger = tmp_path / "ledger.jsonl"
    past = (datetime.now(tz=UTC) - timedelta(days=1)).replace(microsecond=0).isoformat()
    registered = (datetime.now(tz=UTC) - timedelta(days=2)).replace(microsecond=0).isoformat()
    event = {
        "event": "fixture_retained",
        "candidate_commit": COMMIT,
        "run_root": str(run_root.resolve()),
        "preserved_paths": list(expected_preserved_paths(run_root, version="0.1.0")),
        "consumers": list(REQUIRED_FIXTURE_CONSUMERS),
        "teardown_owner": "post-final-completion-gate",
        "owner_only": True,
        "cleanup_deadline": past,
        "registered_at": registered,
    }
    line = json.dumps(event) + "\n"
    ledger.write_text(line, encoding="utf-8")
    binding = FixtureLedgerBinding(
        ledger_path_sha256=ledger_path_digest(ledger),
        event_sha256=hashlib.sha256(line.encode()).hexdigest(),
        candidate_commit=COMMIT,
        consumers=REQUIRED_FIXTURE_CONSUMERS,
        teardown_owner="post-final-completion-gate",
        owner_only=True,
        cleanup_deadline=past,
        run_root="run_root",
    )
    errors = verify_fixture_ledger_binding(
        binding,
        candidate_commit=COMMIT,
        run_root=run_root,
        version="0.1.0",
        ledger_path=ledger,
    )
    assert "fixture_ledger_deadline_not_future" in errors

    # Path substitution: wrong sixth path
    bad_paths = list(expected_preserved_paths(run_root, version="0.1.0"))
    bad_paths[-1] = str((run_root / "evil").resolve())
    (run_root / "evil").mkdir()
    event2 = {
        **event,
        "preserved_paths": bad_paths,
        "cleanup_deadline": (datetime.now(tz=UTC) + timedelta(days=5))
        .replace(microsecond=0)
        .isoformat(),
    }
    line2 = json.dumps(event2) + "\n"
    ledger.write_text(line2, encoding="utf-8")
    deadline2 = str(event2["cleanup_deadline"])
    binding2 = FixtureLedgerBinding(
        ledger_path_sha256=ledger_path_digest(ledger),
        event_sha256=hashlib.sha256(line2.encode()).hexdigest(),
        candidate_commit=COMMIT,
        consumers=REQUIRED_FIXTURE_CONSUMERS,
        teardown_owner="post-final-completion-gate",
        owner_only=True,
        cleanup_deadline=deadline2,
        run_root="run_root",
    )
    errors2 = verify_fixture_ledger_binding(
        binding2,
        candidate_commit=COMMIT,
        run_root=run_root,
        version="0.1.0",
        ledger_path=ledger,
    )
    assert "fixture_ledger_preserved_paths_mismatch" in errors2


def test_privacy_canonical_normalization_detects_secret_outside_digests(tmp_path: Path) -> None:
    install = tmp_path / "install.json"
    run_root = tmp_path / "run"
    run_root.mkdir()
    payload: dict[str, object] = {
        "status": "passed",
        "privacy": provisional_privacy_binding(
            candidate_commit=COMMIT,
            clone_commit=COMMIT,
        ),
        "access_token": string.ascii_lowercase,
    }
    install.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    text = canonical_install_text_for_privacy_scan(install, run_root=run_root)
    assert PRIVACY_DIGEST_ZERO in text
    findings, _errors = scan_secret_text(str(install), text)
    assert findings


def test_privacy_pipeline_fails_on_missing_target(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    install = tmp_path / "install.json"
    install.write_text(
        json.dumps(
            {
                "status": "passed",
                "privacy": provisional_privacy_binding(
                    candidate_commit=COMMIT,
                    clone_commit=COMMIT,
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    missing_cache = run_root / "missing-cache"
    with pytest.raises(PrivacyPipelineError):
        produce_privacy_evidence(
            install_report_path=install,
            report_dir=tmp_path,
            run_root=run_root,
            version="0.1.0",
            candidate_commit=COMMIT,
            clone_commit=COMMIT,
            codex_cache=missing_cache,
            claude_cache=missing_cache,
        )


def test_version_cache_proof_rejects_registration_mismatch() -> None:
    with pytest.raises(ValidationError):
        VersionCacheProof.model_validate(
            {
                "cache_root": "cache-root/path",
                "version": "0.1.1",
                "digest": "1" * 64,
                "source_digest": "1" * 64,
                "inventory_exact_match": True,
                "tool_count": 60,
                "annotations_missing": [],
                "probe_stdout_sha256": "2" * 64,
                "list_receipt_name": "codex_plugin_list_bumped",
                "registration_version": "0.1.0",
                "registration_cache_root": "cache-root/path",
            },
        )


def test_failed_command_redirected_sleeper_cleaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_marker = tmp_path / "child-observed"
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "OBSERVED_MARKER": str(observed_marker),
    }
    root_pid: int | None = None
    child_seen = False
    root_checks_after_child = 0
    original_observation = command_runner.read_process_observation

    class PassiveWatcher:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return

        def start(self) -> None:
            return

        def join(self, timeout: float | None = None) -> None:
            _ = timeout

    def observation(pid: int) -> command_runner.ProcessObservation | None:
        nonlocal child_seen, root_checks_after_child, root_pid
        result = original_observation(pid)
        if root_pid is None:
            root_pid = pid
        elif pid != root_pid and result is not None and result.state == "running":
            child_seen = True
        elif pid == root_pid and child_seen and result is not None and result.state == "running":
            root_checks_after_child += 1
            if root_checks_after_child >= 2:  # noqa: PLR2004
                # Release the root only after the post-scope birth-bound check.
                observed_marker.touch(mode=0o600)
        return result

    code = (
        "import os, subprocess, time\n"
        "child = subprocess.Popen(\n"
        "    ['sleep', '60'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL\n"
        ")\n"
        "deadline = time.monotonic() + 5\n"
        "while not os.path.exists(os.environ['OBSERVED_MARKER']):\n"
        "    if time.monotonic() >= deadline:\n"
        "        child.terminate()\n"
        "        child.wait(timeout=2)\n"
        "        raise SystemExit(99)\n"
        "    time.sleep(0.001)\n"
        f"raise SystemExit({NONZERO})\n"
    )
    monkeypatch.setattr(command_runner.threading, "Thread", PassiveWatcher)
    monkeypatch.setattr(command_runner, "read_process_observation", observation)

    with pytest.raises(CommandFailureError) as err:
        run_command(
            "nonzero_redirected_sleeper",
            (sys.executable, "-c", code),
            cwd=tmp_path,
            env=env,
            timeout_seconds=5,
        )
    receipt = err.value.receipt
    assert receipt.exit_code == NONZERO
    assert receipt.cleanup_attempted is True
    assert receipt.pid is not None
    assert receipt.pgid is not None
    time.sleep(0.15)
    assert remaining_live_pgids((receipt.pgid,)) == ()
    assert remaining_live_pids(descendant_pids(receipt.pid)) == ()


def test_detached_new_session_child_cleaned(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
    }
    code = (
        "import os, time, signal\n"
        "os.setsid()\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    with pytest.raises(CommandFailureError) as err:
        run_command(
            "detached_session_child",
            ("/bin/sh", "-c", f"python3 -c {code!r} & sleep 0.35; exit 3"),
            cwd=tmp_path,
            env=env,
            timeout_seconds=5,
        )
    receipt = err.value.receipt
    assert receipt.cleanup_attempted is True
    assert receipt.pid is not None
    time.sleep(0.2)
    assert remaining_live_pids(descendant_pids(receipt.pid)) == ()


def test_coverage_union_binds_basenames_to_report_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Basename labels must resolve under report_dir, never process cwd."""
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / "install.json").write_text("{}\n", encoding="utf-8")
    (report_dir / "privacy-report.json").write_text("{}\n", encoding="utf-8")
    # A same-named file under cwd would make cwd-bound resolution incorrectly succeed
    # or fail depending on which side used cwd; bind only to report_dir.
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    (elsewhere / "install.json").write_text('{"evil": true}\n', encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    report: dict[str, JsonValue] = {
        "scope": {
            "manual_json": ["install.json"],
        },
    }
    self_scan: dict[str, JsonValue] = {
        "scope": {
            "privacy_report": ["privacy-report.json"],
            "install_report_canonical": ["install.json"],
        },
    }
    assert coverage_union_errors(report, self_scan, report_dir=report_dir) == []


def test_derive_includes_arbitrary_extra_manual_json(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    run_root = tmp_path / "run"
    report_dir.mkdir()
    for relative in (
        "home",
        "codex-home/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
        "home/.claude/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    (report_dir / "install.json").write_text("{}\n", encoding="utf-8")
    (report_dir / "extra-manual.json").write_text("{}\n", encoding="utf-8")
    # Privacy outputs excluded at produce-time; verify excluded until verify-only.
    (report_dir / "privacy-report.json").write_text("{}\n", encoding="utf-8")
    (report_dir / "verify.json").write_text("{}\n", encoding="utf-8")
    targets = derive_existing_privacy_targets(
        report_dir=report_dir,
        run_root=run_root,
        version="0.1.0",
        codex_cache=run_root / "codex-home/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
        claude_cache=run_root / "home/.claude/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
        include_privacy_outputs=False,
        include_verify=False,
    )
    names = {Path(path).name for path in targets["manual_json"]}
    assert "install.json" in names
    assert "extra-manual.json" in names
    assert "privacy-report.json" not in names
    assert "verify.json" not in names


def test_derive_rejects_symlink_and_special_manual_json(tmp_path: Path) -> None:
    report_dir = tmp_path / "reports"
    run_root = tmp_path / "run"
    report_dir.mkdir()
    for relative in (
        "home",
        "codex-home/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
        "home/.claude/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    real = report_dir / "install.json"
    real.write_text("{}\n", encoding="utf-8")
    link = report_dir / "linked-manual.json"
    link.symlink_to(real)
    with pytest.raises(PrivacyPipelineError, match="manual_json_symlink_rejected"):
        derive_existing_privacy_targets(
            report_dir=report_dir,
            run_root=run_root,
            version="0.1.0",
            codex_cache=run_root / "codex-home/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
            claude_cache=run_root / "home/.claude/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
            include_privacy_outputs=False,
            include_verify=False,
        )
    link.unlink()
    fifo = report_dir / "fifo-manual.json"
    os.mkfifo(fifo)
    with pytest.raises(PrivacyPipelineError, match="manual_json_special_rejected"):
        derive_existing_privacy_targets(
            report_dir=report_dir,
            run_root=run_root,
            version="0.1.0",
            codex_cache=run_root / "codex-home/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
            claude_cache=run_root / "home/.claude/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0",
            include_privacy_outputs=False,
            include_verify=False,
        )


def test_scan_directory_rejects_special_node(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    cache = run_root / "cache"
    cache.mkdir(parents=True)
    (cache / "ok.txt").write_text("clean\n", encoding="utf-8")
    fifo = cache / "pipe.fifo"
    os.mkfifo(fifo)
    _findings, errors = scan_directory_normalized(cache, run_root=run_root)
    assert any(item.get("error") == "special_node" for item in errors)
    assert any(str(fifo) in str(item.get("path", "")) for item in errors)


def _seed_retained_fixture(run_root: Path) -> tuple[Path, Path]:
    """Registration + both caches under retained homes; return cache paths."""
    codex_cache = run_root / "codex-home/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0"
    claude_cache = run_root / "home/.claude/plugins/cache/sigvardt/saxo-bank-mcp/0.1.0"
    for relative in (
        "source-clone",
        "home",
        "codex-home",
        "claude-home",
        str(codex_cache.relative_to(run_root)),
        str(claude_cache.relative_to(run_root)),
        "home/.claude/plugins",
        "home/.saxo-bank-mcp-auth",
    ):
        (run_root / relative).mkdir(parents=True, exist_ok=True)
    (run_root / "codex-home/config.toml").write_text(
        '[plugins."saxo-bank-mcp@sigvardt"]\nenabled = true\n',
        encoding="utf-8",
    )
    (run_root / "home/.claude/plugins/installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "saxo-bank-mcp@sigvardt": [
                        {
                            "version": "0.1.0",
                            "installPath": str(claude_cache.resolve()),
                        },
                    ],
                },
            },
        )
        + "\n",
        encoding="utf-8",
    )
    (codex_cache / "marker.txt").write_text("codex-cache\n", encoding="utf-8")
    (claude_cache / "marker.txt").write_text("claude-cache\n", encoding="utf-8")
    return codex_cache, claude_cache


def test_disposable_cleanup_removes_ephemeral_preserves_registration(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    codex_cache, claude_cache = _seed_retained_fixture(run_root)
    ephemeral = {
        run_root / "tmp/x",
        run_root / "uv-cache/wheels",
        run_root / "uv-python/cpython",
        run_root / "probe-env/lib",
        run_root / "marketplace-source/pkg",
        run_root / "home/.cache/uv/CACHEDIR.TAG",
        run_root / "home/.config/uv/uv.toml",
        run_root / "home/.local/share/x",
        run_root / "home/Library/Application Support/fastmcp/state",
        run_root / "home/.claude/backups/old",
        run_root / "codex-home/.tmp/work",
        run_root / "codex-home/tmp/work",
        run_root / "verify-home/.cache",
        run_root / "verify-codex-home/tmp",
        run_root / "verify-probe-env/lib",
    }
    for path in ephemeral:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix or path.name in {"CACHEDIR.TAG", "uv.toml", "x", "state", "old", "work"}:
            if path.suffix or path.name.endswith((".TAG", ".toml", "x", "state", "old", "work")):
                path.write_text("ephemeral\n", encoding="utf-8")
            else:
                path.mkdir(parents=True, exist_ok=True)
        else:
            path.mkdir(parents=True, exist_ok=True)
    # Ensure every enumerated disposable root exists as a tree.
    for path in enumerated_disposable_paths(run_root):
        path.mkdir(parents=True, exist_ok=True)
        (path / ".keep").write_text("x\n", encoding="utf-8")

    residual = cleanup_disposable_isolated_state(run_root)
    assert residual == []
    for path in enumerated_disposable_paths(run_root):
        assert not path.exists(), path
    assert (run_root / "codex-home/config.toml").is_file()
    assert (run_root / "home/.claude/plugins/installed_plugins.json").is_file()
    assert (codex_cache / "marker.txt").is_file()
    assert (claude_cache / "marker.txt").is_file()
    assert (run_root / "source-clone").is_dir()


def test_disposable_cleanup_residue_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    _seed_retained_fixture(run_root)
    sticky = run_root / "tmp"
    sticky.mkdir(parents=True)
    (sticky / "held").write_text("x\n", encoding="utf-8")

    def _boom(_path: Path) -> None:
        raise OSError("permission_denied")

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_env._remove_path_tree",
        _boom,
    )
    residual = cleanup_disposable_isolated_state(run_root)
    assert residual
    with pytest.raises(DisposableCleanupError) as err:
        require_disposable_cleanup(run_root)
    assert err.value.residual_paths


def _plant_retained_canaries(run_root: Path) -> dict[Path, str]:
    """Plant retained disposable-looking canaries that verifier cleanup must leave alone."""
    payloads: dict[Path, str] = {
        run_root / "home/.cache/canary.txt": "retained-cache\n",
        run_root / "home/.config/canary.txt": "retained-config\n",
        run_root / "home/.local/canary.txt": "retained-local\n",
        run_root / "home/Library/Application Support/fastmcp/canary.txt": "retained-fastmcp\n",
        run_root / "home/.claude/backups/canary.txt": "retained-backups\n",
        run_root / "codex-home/.tmp/canary.txt": "retained-codex-dot-tmp\n",
        run_root / "codex-home/tmp/canary.txt": "retained-codex-tmp\n",
    }
    for path, body in payloads.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return payloads


def test_verify_startup_uses_throwaway_homes_and_cleans(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    codex_cache, claude_cache = _seed_retained_fixture(run_root)
    clone = run_root / "source-clone"
    retained_home = run_root / "home"
    canaries = _plant_retained_canaries(run_root)
    # Producer leftover probe-env must not be deleted by verifier scratch cleanup.
    producer_probe = run_root / "probe-env" / "kept"
    producer_probe.parent.mkdir(parents=True, exist_ok=True)
    producer_probe.write_text("producer-probe\n", encoding="utf-8")
    marketplace = run_root / "marketplace-source" / "kept"
    marketplace.parent.mkdir(parents=True, exist_ok=True)
    marketplace.write_text("marketplace\n", encoding="utf-8")
    captured: dict[str, Path] = {}

    def _fake_probe(
        name: str,
        root: Path,
        *,
        env: dict[str, str],
        probe_env: Path,
    ) -> CommandResult:
        home = Path(env["HOME"])
        codex_home = Path(env["CODEX_HOME"])
        captured["home"] = home
        captured["codex_home"] = codex_home
        captured["probe_env"] = probe_env
        # Mutate only throwaway trees (would poison retained state if wrong homes used).
        (home / ".cache" / "uv").mkdir(parents=True, exist_ok=True)
        (home / ".cache" / "uv" / "poison").write_text("nope\n", encoding="utf-8")
        (codex_home / "tmp").mkdir(parents=True, exist_ok=True)
        # Shared run-root scratch from build_isolated_env for probes.
        (run_root / "tmp" / "probe-work").mkdir(parents=True, exist_ok=True)
        (run_root / "uv-cache" / "wheels").mkdir(parents=True, exist_ok=True)
        (run_root / "uv-python" / "cpython").mkdir(parents=True, exist_ok=True)
        receipt = CommandReceipt(
            name=name,
            argv=("uv", "run"),
            cwd=str(root),
            exit_code=0,
            timed_out=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            pid=None,
            pgid=None,
            cleanup_attempted=True,
        )
        return CommandResult(
            receipt=receipt,
            stdout=json.dumps({"tool_count": 60, "annotations_missing": []}),
            stderr="",
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live.probe_root_stdio",
        _fake_probe,
    )
    errors = startup_probe_errors_for_caches(
        run_root=run_root,
        clone=clone,
        codex_cache=codex_cache,
        claude_cache=claude_cache,
    )
    assert "verify_scratch_residue" not in errors
    assert captured["home"].resolve() == (run_root / "verify-home").resolve()
    assert captured["codex_home"].resolve() == (run_root / "verify-codex-home").resolve()
    for scratch in enumerated_verify_scratch_paths(run_root):
        assert not scratch.exists(), scratch
    for path, body in canaries.items():
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == body
    assert producer_probe.is_file()
    assert marketplace.is_file()
    assert not (retained_home / ".cache" / "uv" / "poison").exists()


def test_verify_scratch_cleanup_never_touches_retained_home_codex(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    _seed_retained_fixture(run_root)
    canaries = _plant_retained_canaries(run_root)
    for scratch in enumerated_verify_scratch_paths(run_root):
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "gone").write_text("scratch\n", encoding="utf-8")
    residual = cleanup_verify_scratch_state(run_root)
    assert residual == []
    for scratch in enumerated_verify_scratch_paths(run_root):
        assert not scratch.exists(), scratch
    for path, body in canaries.items():
        assert path.read_text(encoding="utf-8") == body


def test_retained_secret_survives_verify_cleanup_for_privacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup verify cleanup must not erase a post-scan injected retained secret."""
    run_root = tmp_path / "run"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    codex_cache, claude_cache = _seed_retained_fixture(run_root)
    clone = run_root / "source-clone"
    secret_path = run_root / "home" / ".cache" / "leaked.json"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_body = json.dumps({"access_token": string.ascii_lowercase}) + "\n"
    secret_path.write_text(secret_body, encoding="utf-8")

    def _fake_probe(
        name: str,
        root: Path,
        *,
        env: dict[str, str],
        probe_env: Path,
    ) -> CommandResult:
        _ = env, probe_env
        receipt = CommandReceipt(
            name=name,
            argv=("uv", "run"),
            cwd=str(root),
            exit_code=0,
            timed_out=False,
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
            pid=None,
            pgid=None,
            cleanup_attempted=True,
        )
        return CommandResult(
            receipt=receipt,
            stdout=json.dumps({"tool_count": 60, "annotations_missing": []}),
            stderr="",
        )

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live.probe_root_stdio",
        _fake_probe,
    )
    errors = startup_probe_errors_for_caches(
        run_root=run_root,
        clone=clone,
        codex_cache=codex_cache,
        claude_cache=claude_cache,
    )
    assert "verify_scratch_residue" not in errors
    # Secret still present for later privacy verification (not erased by startup cleanup).
    assert secret_path.is_file()
    assert secret_path.read_text(encoding="utf-8") == secret_body
    install = report_dir / "install.json"
    install.write_text(
        json.dumps(
            {
                "status": "passed",
                "privacy": provisional_privacy_binding(
                    candidate_commit=COMMIT,
                    clone_commit=COMMIT,
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PrivacyPipelineError, match="privacy_scan_failed"):
        produce_privacy_evidence(
            install_report_path=install,
            report_dir=report_dir,
            run_root=run_root,
            version="0.1.0",
            candidate_commit=COMMIT,
            clone_commit=COMMIT,
            codex_cache=codex_cache,
            claude_cache=claude_cache,
        )


def _minimal_live_report(
    roots: tuple[Path, Path, Path, Path],
    *,
    before: dict[str, str],
    after: dict[str, str],
) -> SimpleNamespace:
    """Lightweight report stand-in for live_verify_errors with work functions stubbed."""
    run_root, clone, codex_cache, claude_cache = roots
    return SimpleNamespace(
        global_state=SimpleNamespace(before=before, after=after),
        fixture_cleanup=SimpleNamespace(run_root=run_root),
        clone=SimpleNamespace(path=clone),
        codex=SimpleNamespace(
            cache_root=codex_cache,
            version="0.1.0",
            cache_root_source="codex_plugin_add_restored",
        ),
        claude=SimpleNamespace(
            cache_root=claude_cache,
            version="0.1.0",
            cache_root_source="claude_plugin_list_restored",
        ),
        update_probe=None,
        update_receipts=(),
    )


def _empty_errors(_report: object = None, **_kwargs: object) -> list[str]:
    return []


def _stub_live_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live._cache_source_errors",
        _empty_errors,
    )
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live._registration_bind_errors",
        _empty_errors,
    )
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live._startup_probe_errors",
        _empty_errors,
    )
    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live._update_proof_errors",
        _empty_errors,
    )


def test_historical_global_before_after_mismatch_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    codex_cache, claude_cache = _seed_retained_fixture(run_root)
    clone = run_root / "source-clone"
    codex_g = tmp_path / "codex-global"
    claude_g = tmp_path / "claude-global"
    codex_g.mkdir()
    claude_g.mkdir()
    _stub_live_work(monkeypatch)
    report = _minimal_live_report(
        (run_root, clone, codex_cache, claude_cache),
        before={"codex": "a" * 64, "claude": "b" * 64},
        after={"codex": "c" * 64, "claude": "b" * 64},
    )
    errors = live_verify_errors(
        report,  # type: ignore[arg-type]
        codex_global_home=codex_g,
        claude_global_home=claude_g,
    )
    assert "global_state_fingerprint_mismatch" in errors


def test_ambient_drift_before_verify_accepted_when_window_stable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical report digests may differ from current; window start==end still passes."""
    run_root = tmp_path / "run"
    codex_cache, claude_cache = _seed_retained_fixture(run_root)
    clone = run_root / "source-clone"
    codex_g = tmp_path / "codex-global"
    claude_g = tmp_path / "claude-global"
    codex_g.mkdir()
    claude_g.mkdir()
    (codex_g / "config.toml").write_text("x=1\n", encoding="utf-8")
    _stub_live_work(monkeypatch)
    live = global_fingerprint_pair(codex_g, claude_g)
    # Historical producer digests deliberately differ from current ambient state.
    report = _minimal_live_report(
        (run_root, clone, codex_cache, claude_cache),
        before={"codex": "d" * 64, "claude": "e" * 64},
        after={"codex": "d" * 64, "claude": "e" * 64},
    )
    assert live["codex"] != "d" * 64
    errors = live_verify_errors(
        report,  # type: ignore[arg-type]
        codex_global_home=codex_g,
        claude_global_home=claude_g,
    )
    assert "global_state_recompute_mismatch" not in errors
    assert "global_state_verify_window_mismatch" not in errors
    assert "global_state_fingerprint_mismatch" not in errors


def test_mutation_during_verify_window_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    codex_cache, claude_cache = _seed_retained_fixture(run_root)
    clone = run_root / "source-clone"
    codex_g = tmp_path / "codex-global"
    claude_g = tmp_path / "claude-global"
    codex_g.mkdir()
    claude_g.mkdir()
    config = codex_g / "config.toml"
    config.write_text("x=1\n", encoding="utf-8")
    _stub_live_work(monkeypatch)

    def _mutate_registration(_report: object = None, **_kwargs: object) -> list[str]:
        config.write_text("x=2\n", encoding="utf-8")
        return []

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live._registration_bind_errors",
        _mutate_registration,
    )
    report = _minimal_live_report(
        (run_root, clone, codex_cache, claude_cache),
        before={"codex": "f" * 64, "claude": "g" * 64},
        after={"codex": "f" * 64, "claude": "g" * 64},
    )
    errors = live_verify_errors(
        report,  # type: ignore[arg-type]
        codex_global_home=codex_g,
        claude_global_home=claude_g,
    )
    assert "global_state_verify_window_mismatch" in errors


def test_fingerprint_exception_during_verify_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_root = tmp_path / "run"
    codex_cache, claude_cache = _seed_retained_fixture(run_root)
    clone = run_root / "source-clone"
    codex_g = tmp_path / "codex-global"
    claude_g = tmp_path / "claude-global"
    codex_g.mkdir()
    claude_g.mkdir()
    _stub_live_work(monkeypatch)

    def _boom(_codex: Path, _claude: Path) -> dict[str, JsonValue]:
        raise OSError("fingerprint_boom")

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_verify_live.global_fingerprint_pair",
        _boom,
    )
    report = _minimal_live_report(
        (run_root, clone, codex_cache, claude_cache),
        before={"codex": "h" * 64, "claude": "i" * 64},
        after={"codex": "h" * 64, "claude": "i" * 64},
    )
    errors = live_verify_errors(
        report,  # type: ignore[arg-type]
        codex_global_home=codex_g,
        claude_global_home=claude_g,
    )
    assert "global_state_fingerprint_failed" in errors


def test_verify_receipt_not_passed_when_window_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = tmp_path / "install.json"
    out = tmp_path / "verify.json"
    install.write_text("{}\n", encoding="utf-8")
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("\n", encoding="utf-8")
    codex_g = tmp_path / "codex-global"
    claude_g = tmp_path / "claude-global"
    codex_g.mkdir()
    claude_g.mkdir()

    def _fail_load(
        _path: Path,
        *,
        codex_global_home: Path,
        claude_global_home: Path,
        fixture_cleanup_ledger: Path,
    ) -> tuple[None, tuple[str, ...]]:
        _ = codex_global_home, claude_global_home, fixture_cleanup_ledger
        return None, ("global_state_verify_window_mismatch",)

    monkeypatch.setattr(
        "saxo_bank_mcp.agent_skill_install_qa.load_verified_install_report",
        _fail_load,
    )
    code = verify_install_report(
        install,
        out,
        codex_global_home=codex_g,
        claude_global_home=claude_g,
        fixture_cleanup_ledger=ledger,
    )
    assert code == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert "global_state_verify_window_mismatch" in payload["errors"]
    assert payload.get("global_state_unchanged") is not True
    assert payload.get("global_state_recomputed") is not True


def _probe_result(name: str, payload: dict[str, JsonValue], *, cwd: Path) -> CommandResult:
    receipt = CommandReceipt(
        name=name,
        argv=("uv", "run"),
        cwd=str(cwd),
        exit_code=0,
        stdout_sha256="a" * 64,
        stderr_sha256="b" * 64,
        cleanup_attempted=True,
    )
    return CommandResult(
        receipt=receipt,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_list_tools_nonempty_annotations_fails_with_60_tools(tmp_path: Path) -> None:
    """Independent list_tools missing annotations fails at the complete tool count."""
    clean: dict[str, JsonValue] = {"tool_count": 60, "annotations_missing": []}
    dirty: dict[str, JsonValue] = {
        "tool_count": 60,
        "annotations_missing": ["saxo_health"],
    }
    source = _probe_result("source", clean, cwd=tmp_path)
    cache = _probe_result("cache", clean, cwd=tmp_path)
    list_tools = _probe_result("list_tools", dirty, cwd=tmp_path)
    # Parse succeeds for the dirty probe alone (typed list preserved).
    dirty_check = startup_check_from_payload(dirty)
    assert dirty_check.tool_count == EXPECTED_TOOLS
    assert dirty_check.annotations_missing == ("saxo_health",)
    # Combined producer path fail-closes on independent list_tools missings.
    with pytest.raises(ProbePayloadError, match="list_tools_annotations_missing"):
        startup_from_probes(source, cache, list_tools)
    # Client aggregate still unions list_tools when constructing evidence by hand.
    startup = StartupEvidence(
        source=startup_check_from_payload(clean),
        cache=startup_check_from_payload(clean),
        list_tools=dirty_check,
    )
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    (cache_root / "pyproject.toml").write_text(
        '[project]\nname = "saxo-bank-mcp"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    report = build_client_report(
        cache=cache_root,
        cache_source="codex_plugin_add_restored",
        startup=startup,
        receipts=(source.receipt,),
        inventory={
            "inventory_exact_match": True,
            "forbidden_cache_paths": [],
            "mismatches": [],
        },
        details_skill_count=None,
    )
    missing = report["annotations_missing"]
    assert isinstance(missing, list)
    assert "saxo_health" in missing
    assert report["list_tools_annotations_missing"] == ["saxo_health"]
    startup_dump = report["startup"]
    assert isinstance(startup_dump, dict)
    list_tools_dump = startup_dump["list_tools"]
    assert isinstance(list_tools_dump, dict)
    assert list_tools_dump["annotations_missing"] == ["saxo_health"]


def test_missing_annotations_missing_key_fails() -> None:
    with pytest.raises(ProbePayloadError, match="annotations_missing_missing"):
        startup_check_from_payload({"tool_count": 60})


def test_annotations_missing_wrong_type_and_mixed_members_fail() -> None:
    with pytest.raises(ProbePayloadError, match="annotations_missing_not_list"):
        startup_check_from_payload(
            {"tool_count": 60, "annotations_missing": "saxo_health"},
        )
    with pytest.raises(ProbePayloadError, match="annotations_missing_null"):
        startup_check_from_payload({"tool_count": 60, "annotations_missing": None})
    with pytest.raises(ProbePayloadError, match="annotations_missing_non_string"):
        startup_check_from_payload(
            {"tool_count": 60, "annotations_missing": ["ok", 1]},
        )
    with pytest.raises(ValidationError):
        StartupCheck.model_validate(
            {"status": "passed", "tool_count": 60, "annotations_missing": None},
        )
    with pytest.raises(ValidationError):
        StartupCheck.model_validate(
            {"status": "passed", "tool_count": 60, "annotations_missing": ["ok", 2]},
        )


def test_clean_three_probe_startup_evidence_preserves_per_probe_fields(
    tmp_path: Path,
) -> None:
    clean: dict[str, JsonValue] = {"tool_count": 60, "annotations_missing": []}
    startup = startup_from_probes(
        _probe_result("source", clean, cwd=tmp_path),
        _probe_result("cache", clean, cwd=tmp_path),
        _probe_result("list_tools", clean, cwd=tmp_path),
    )
    assert isinstance(startup, StartupEvidence)
    for check in (startup.source, startup.cache, startup.list_tools):
        assert check.tool_count == EXPECTED_TOOLS
        assert check.annotations_missing == ()
        assert check.status == "passed"
    dumped = startup.model_dump(mode="json")
    assert dumped["list_tools"]["annotations_missing"] == []
    assert dumped["source"]["annotations_missing"] == []
    assert dumped["cache"]["annotations_missing"] == []


def test_privacy_still_rejects_secret_in_retained_state(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    codex_cache, claude_cache = _seed_retained_fixture(run_root)
    # Genuine retained registration-adjacent state with a secret must still fail.
    secret_path = run_root / "home" / ".claude" / "plugins" / "notes.json"
    secret_path.write_text(
        json.dumps({"access_token": string.ascii_lowercase}) + "\n",
        encoding="utf-8",
    )
    install = report_dir / "install.json"
    install.write_text(
        json.dumps(
            {
                "status": "passed",
                "privacy": provisional_privacy_binding(
                    candidate_commit=COMMIT,
                    clone_commit=COMMIT,
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PrivacyPipelineError, match="privacy_scan_failed"):
        produce_privacy_evidence(
            install_report_path=install,
            report_dir=report_dir,
            run_root=run_root,
            version="0.1.0",
            candidate_commit=COMMIT,
            clone_commit=COMMIT,
            codex_cache=codex_cache,
            claude_cache=claude_cache,
        )
