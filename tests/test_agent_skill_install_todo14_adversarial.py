from __future__ import annotations

import hashlib
import json
import os
import string
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from saxo_bank_mcp.agent_skill_command_runner import (
    CommandFailureError,
    descendant_pids,
    remaining_live_pgids,
    remaining_live_pids,
    run_command,
)
from saxo_bank_mcp.agent_skill_install_ledger import (
    append_fixture_ledger_event,
    bind_existing_ledger_event,
    expected_preserved_paths,
    ledger_path_digest,
    verify_fixture_ledger_binding,
)
from saxo_bank_mcp.agent_skill_install_models import (
    REQUIRED_FIXTURE_CONSUMERS,
    FixtureLedgerBinding,
    VersionCacheProof,
)
from saxo_bank_mcp.agent_skill_install_privacy import (
    PRIVACY_DIGEST_ZERO,
    PrivacyPipelineError,
    canonical_install_text_for_privacy_scan,
    produce_privacy_evidence,
    provisional_privacy_binding,
)
from saxo_bank_mcp.agent_skill_install_verify_live import codex_registration_errors
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
                "cleanup_deadline": (
                    datetime.now(tz=UTC) + timedelta(days=10)
                ).replace(microsecond=0).isoformat(),
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
    assert (
        verify_fixture_ledger_binding(
            binding,
            candidate_commit=COMMIT,
            run_root=run_root,
            version="0.1.0",
            ledger_path=None,
        )
        == ["fixture_ledger_required"]
    )
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
    event2 = {**event, "preserved_paths": bad_paths, "cleanup_deadline": (
        datetime.now(tz=UTC) + timedelta(days=5)
    ).replace(microsecond=0).isoformat()}
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
                "tool_count": 39,
                "annotations_missing": [],
                "probe_stdout_sha256": "2" * 64,
                "list_receipt_name": "codex_plugin_list_bumped",
                "registration_version": "0.1.0",
                "registration_cache_root": "cache-root/path",
            },
        )


def test_failed_command_redirected_sleeper_cleaned(tmp_path: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
    }
    with pytest.raises(CommandFailureError) as err:
        run_command(
            "nonzero_redirected_sleeper",
            ("/bin/sh", "-c", f"sleep 60 >/dev/null 2>&1 & exit {NONZERO}"),
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
