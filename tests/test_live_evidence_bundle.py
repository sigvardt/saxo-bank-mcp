"""Release evidence publisher regression matrix. # noqa: SIZE_OK."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

import pytest
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.live_evidence_bundle import publish_live_evidence_bundle
from saxo_bank_mcp.live_evidence_gate_validation import (
    FOCUSED_RELEASE_TEST_PATHS,
    raw_gate_manifest_passed,
)
from saxo_bank_mcp.live_evidence_release_validation import release_payloads_passed

EXPECTED_RELEASE_PAYLOADS = 4
EXPECTED_RELEASE_GATES = 9
JSON_OBJECT_ADAPTER = TypeAdapter(dict[str, JsonValue])


def test_publish_live_evidence_bundle_binds_release_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    receipt = tmp_path / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 0
    raw_manifest = json.loads(
        (payload / "raw-gates" / "manifest.json").read_text(encoding="utf-8"),
    )
    assert "release_payloads" not in raw_manifest
    bundle_path = payload / "bundle-manifest.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert len(bundle["release_payloads"]) == EXPECTED_RELEASE_PAYLOADS
    paths = {item["path"] for item in bundle["artifacts"]}
    assert {
        "proof-production.json",
        "live-read.json",
        "prod-readiness.json",
        "manual-qa.json",
        "raw-gates/manifest.json",
    } <= paths
    published_receipt = json.loads(receipt.read_text(encoding="utf-8"))
    assert published_receipt["status"] == "passed"
    assert published_receipt["bundle_manifest_sha256"] == hashlib.sha256(
        bundle_path.read_bytes(),
    ).hexdigest()


def test_publish_live_evidence_bundle_rejects_stale_source_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "src" / "sample.py").write_text("changed = True\n", encoding="utf-8")
    receipt = tmp_path / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "reason": "raw_gate_integrity_failed",
        "status": "failed",
    }


def test_publish_live_evidence_bundle_rejects_failed_release_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    (payload / "live-read.json").write_text('{"status":"failed"}\n', encoding="utf-8")
    receipt = tmp_path / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "reason": "release_payload_status_failed",
        "status": "failed",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "failed_check",
        "empty_checks",
        "changed_command",
        "missing_source",
        "missing_documentation",
        "missing_artifacts",
    ],
)
def test_publish_live_evidence_bundle_rejects_incomplete_gate_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Literal[
        "failed_check",
        "empty_checks",
        "changed_command",
        "missing_source",
        "missing_documentation",
        "missing_artifacts",
    ],
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    manifest_path = payload / "raw-gates" / "manifest.json"
    manifest = JSON_OBJECT_ADAPTER.validate_json(manifest_path.read_bytes())
    _mutate_manifest(manifest, mutation)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    receipt = tmp_path / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "reason": "raw_gate_integrity_failed",
        "status": "failed",
    }


def _mutate_manifest(
    manifest: dict[str, JsonValue],
    mutation: Literal[
        "failed_check",
        "empty_checks",
        "changed_command",
        "missing_source",
        "missing_documentation",
        "missing_artifacts",
    ],
) -> None:
    if mutation in {"failed_check", "changed_command"}:
        checks = manifest["checks"]
        assert isinstance(checks, list)
        first = checks[0]
        assert isinstance(first, dict)
        first["exit_code" if mutation == "failed_check" else "command"] = (
            1 if mutation == "failed_check" else ["true"]
        )
    elif mutation == "empty_checks":
        manifest["checks"] = []
    elif mutation in {"missing_source", "missing_documentation"}:
        key = (
            "python_source_test_sha256"
            if mutation == "missing_source"
            else "documentation_sha256"
        )
        entries = manifest[key]
        assert isinstance(entries, dict)
        entries.pop(
            "tests/test_sample.py" if mutation == "missing_source" else "README.md",
        )
    elif mutation == "missing_artifacts":
        artifacts = manifest["produced_artifacts"]
        assert isinstance(artifacts, list)
        manifest["produced_artifacts"] = artifacts[:1]


def test_publish_live_evidence_bundle_rejects_status_only_release_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    (payload / "manual-qa.json").write_text('{"status":"passed"}\n', encoding="utf-8")
    receipt = tmp_path / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "reason": "release_payload_status_failed",
        "status": "failed",
    }


@pytest.mark.parametrize(
    "mutation",
    ["missing_proof_source", "nested_live_write", "replay_command_tail"],
)
def test_publish_live_evidence_bundle_rejects_semantic_payload_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Literal[
        "missing_proof_source",
        "nested_live_write",
        "replay_command_tail",
    ],
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    if mutation == "missing_proof_source":
        target = payload / "proof-production.json"
        report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
        report.pop("source")
    elif mutation == "nested_live_write":
        target = payload / "live-read.json"
        report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
        tool_results = report["tool_results"]
        assert isinstance(tool_results, dict)
        first = tool_results["saxo_get_session_capabilities"]
        assert isinstance(first, dict)
        first["live_write_called"] = True
    else:
        target = payload / "manual-qa.json"
        report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
        replay = report["replay_command"]
        assert isinstance(replay, list)
        replay.append("--unexpected-tail")
    target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    receipt = tmp_path / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "reason": "release_payload_status_failed",
        "status": "failed",
    }


@pytest.mark.parametrize(
    "mutation",
    ["amount", "side", "uic", "field_groups", "counts"],
)
def test_publish_live_evidence_bundle_rejects_changed_precheck_request_terms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Literal["amount", "side", "uic", "field_groups", "counts"],
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = payload / "proof-production.json"
    report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
    precheck = report["precheck"]
    assert isinstance(precheck, dict)
    request_summary = precheck["request_summary"]
    assert isinstance(request_summary, dict)
    if mutation == "amount":
        request_summary["amount"] = 2.0
    elif mutation == "side":
        request_summary["buy_sell"] = "Sell"
    elif mutation == "uic":
        request_summary["uic"] = 42
    elif mutation == "field_groups":
        request_summary["field_groups"] = ["Costs"]
    else:
        after_counts = report["after_counts"]
        assert isinstance(after_counts, dict)
        after_counts["orders"] = 1
    target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_rejects_failed_nested_live_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = payload / "live-read.json"
    report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
    tool_results = report["tool_results"]
    assert isinstance(tool_results, dict)
    balances = tool_results["saxo_call_registered_endpoint_balances"]
    assert isinstance(balances, dict)
    balances["status"] = "failed"
    balances["http_status"] = 500
    target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_rejects_manual_scenario_identity_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = payload / "manual-qa.json"
    report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
    scenarios = report["scenarios"]
    assert isinstance(scenarios, list)
    first = scenarios[0]
    assert isinstance(first, dict)
    first["tool_name"] = "unknown_tool"
    first["result_is_error"] = False
    target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_rejects_nested_private_identifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = payload / "live-read.json"
    report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
    tool_results = report["tool_results"]
    assert isinstance(tool_results, dict)
    balances = tool_results["saxo_call_registered_endpoint_balances"]
    assert isinstance(balances, dict)
    balances["nested"] = {"account_id": "unsafe"}
    target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_rejects_raw_balance_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = payload / "proof-production.json"
    report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
    scope = report.setdefault("account_money_state_scope", {})
    assert isinstance(scope, dict)
    scope["CashBalance"] = 123.0
    target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


@pytest.mark.parametrize("mutation", ["placement_trace", "omitted_traces"])
def test_publish_live_evidence_bundle_rejects_underbound_proof_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Literal["placement_trace", "omitted_traces"],
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = payload / "proof-production.json"
    report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
    ledger = report["mcp_request_ledger"]
    transport = report["transport_boundary_capture"]
    assert isinstance(ledger, dict)
    assert isinstance(transport, dict)
    if mutation == "omitted_traces":
        report.pop("request_ledger")
        ledger.pop("events")
        transport.pop("events")
    else:
        for events in (report["request_ledger"], ledger["events"], transport["events"]):
            assert isinstance(events, list)
            first = events[0]
            assert isinstance(first, dict)
            first["method"] = "POST"
            first["path"] = "/openapi/trade/v2/orders"
    target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_rejects_reduced_manual_qa_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    target = payload / "manual-qa.json"
    report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
    scenarios = report["scenarios"]
    assert isinstance(scenarios, list)
    report["scenarios"] = [
        {"scenario_id": item["scenario_id"], "status": "passed"}
        for item in scenarios
        if isinstance(item, dict)
    ]
    for field in (
        "checked_at",
        "generator",
        "generator_source_sha256",
        "rejected_input",
        "warning_log_transcript",
        "warning_log_transcript_sha256",
    ):
        report.pop(field)
    readme = tmp_path / "README.md"
    report["source_hashes"] = [
        {"path": "README.md", "bytes": readme.stat().st_size, "sha256": _sha256(readme)},
    ]
    target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_fails_when_payload_is_missing(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    receipt = tmp_path / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "reason": "required_payload_missing",
        "status": "failed",
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "reduced_focused_tests",
        "alternate_policy_file_set",
        "fabricated_git_head",
        "deep_live_write_flag",
        "raw_financial_value",
        "partial_proof_source",
        "contradictory_tradability",
        "fabricated_test_count",
        "unexpected_gate_field",
    ],
)
def test_publish_live_evidence_bundle_rejects_v23_tribunal_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: Literal[
        "reduced_focused_tests",
        "alternate_policy_file_set",
        "fabricated_git_head",
        "deep_live_write_flag",
        "raw_financial_value",
        "partial_proof_source",
        "contradictory_tradability",
        "fabricated_test_count",
        "unexpected_gate_field",
    ],
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    if mutation in {
        "reduced_focused_tests",
        "alternate_policy_file_set",
        "fabricated_test_count",
        "unexpected_gate_field",
    }:
        manifest_path = payload / "raw-gates" / "manifest.json"
        manifest = JSON_OBJECT_ADAPTER.validate_json(manifest_path.read_bytes())
        gate_name = (
            "05-no-excuse"
            if mutation == "alternate_policy_file_set"
            else "01-focused-pytest"
        )
        check = _manifest_check(manifest, gate_name)
        command = check["command"]
        assert isinstance(command, list)
        if mutation == "reduced_focused_tests":
            command[3:] = [FOCUSED_RELEASE_TEST_PATHS[0]]
        elif mutation == "alternate_policy_file_set":
            command[-1] = FOCUSED_RELEASE_TEST_PATHS[0]
        elif mutation == "fabricated_test_count":
            check["test_counts"] = {
                "tests": 999,
                "errors": 0,
                "failures": 0,
                "skipped": 0,
            }
        else:
            check["unexpected"] = True
        _rewrite_gate_record(payload, check)
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    else:
        target = payload / (
            "live-read.json" if mutation == "deep_live_write_flag" else "proof-production.json"
        )
        report = JSON_OBJECT_ADAPTER.validate_json(target.read_bytes())
        if mutation == "fabricated_git_head":
            source = report["source"]
            assert isinstance(source, dict)
            source["git_head"] = "b" * 40
        elif mutation == "partial_proof_source":
            source = report["source"]
            assert isinstance(source, dict)
            hashes = source["dirty_source_sha256"]
            assert isinstance(hashes, dict)
            source["dirty_source_sha256"] = dict(list(hashes.items())[:1])
        elif mutation == "contradictory_tradability":
            precheck = report["precheck"]
            assert isinstance(precheck, dict)
            precheck["instrument_tradable"] = False
        elif mutation == "deep_live_write_flag":
            tool_results = report["tool_results"]
            assert isinstance(tool_results, dict)
            first = tool_results["saxo_get_session_capabilities"]
            assert isinstance(first, dict)
            first["nested"] = {"live_write_called": True}
        else:
            precheck = report["precheck"]
            assert isinstance(precheck, dict)
            precheck["estimated_cash_required"] = 1
        target.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    receipt = tmp_path / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1


def test_publish_live_evidence_bundle_rejects_receipt_bundle_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    collision = payload / "bundle-manifest.json"

    result = publish_live_evidence_bundle(payload, collision)

    assert result == 1
    assert not collision.exists()


def test_publish_live_evidence_bundle_preserves_existing_passing_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    receipt = tmp_path / "publication-receipt.json"
    assert publish_live_evidence_bundle(payload, receipt) == 0
    original = receipt.read_bytes()

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert receipt.read_bytes() == original


def test_publish_live_evidence_bundle_rejects_receipt_inside_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    receipt = payload / "publication-receipt.json"

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert not receipt.exists()


def test_publish_live_evidence_bundle_fails_when_publication_lock_is_busy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    receipt = tmp_path / "publication-receipt.json"

    def busy_lock(_fd: int, _operation: int) -> None:
        raise BlockingIOError

    monkeypatch.setattr(fcntl, "flock", busy_lock)

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert not receipt.exists()


def test_publish_live_evidence_bundle_locks_the_receipt_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    receipt = tmp_path / "publication-receipt.json"
    observed: list[Path] = []

    def capture_lock(target: Path) -> int:
        observed.append(target)
        return os.open(tmp_path, os.O_RDONLY | os.O_CLOEXEC)

    monkeypatch.setattr("saxo_bank_mcp.live_evidence_bundle._publication_lock", capture_lock)

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert observed == [receipt.resolve()]


def test_publish_live_evidence_bundle_rechecks_receipt_after_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    receipt = tmp_path / "publication-receipt.json"
    original = b'{"status":"passed"}\n'

    def create_receipt_during_lock(_target: Path) -> int:
        receipt.write_bytes(original)
        return os.open(tmp_path, os.O_RDONLY | os.O_CLOEXEC)

    def unexpected_publish(_payload: Path, _receipt: Path) -> int:
        pytest.fail("publisher must not run after a receipt appears under the lock")

    monkeypatch.setattr(
        "saxo_bank_mcp.live_evidence_bundle._publication_lock",
        create_receipt_during_lock,
    )
    monkeypatch.setattr(
        "saxo_bank_mcp.live_evidence_bundle._publish_locked",
        unexpected_publish,
    )

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert receipt.read_bytes() == original


def test_raw_gate_manifest_relative_path_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    relative = (payload / "raw-gates" / "manifest.json").relative_to(tmp_path)

    assert raw_gate_manifest_passed(relative, Path()) is False


def test_release_payload_validation_rejects_empty_path_list() -> None:
    assert release_payloads_passed([]) is False


def test_publish_live_evidence_bundle_rejects_swapped_static_gate_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    manifest_path = payload / "raw-gates" / "manifest.json"
    manifest = JSON_OBJECT_ADAPTER.validate_json(manifest_path.read_bytes())
    ruff = _manifest_check(manifest, "03-ruff")
    basedpyright = _manifest_check(manifest, "04-basedpyright")
    fields = ("stdout_path", "stdout_bytes", "stdout_sha256")
    for field in fields:
        ruff[field], basedpyright[field] = basedpyright[field], ruff[field]
    _rewrite_gate_record(payload, ruff)
    _rewrite_gate_record(payload, basedpyright)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_rejects_nonzero_skipped_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    manifest_path = payload / "raw-gates" / "manifest.json"
    manifest = JSON_OBJECT_ADAPTER.validate_json(manifest_path.read_bytes())
    check = _manifest_check(manifest, "01-focused-pytest")
    counts = check["test_counts"]
    assert isinstance(counts, dict)
    counts["skipped"] = 1
    stdout_path = tmp_path / str(check["stdout_path"])
    stdout_path.write_text(
        stdout_path.read_text(encoding="utf-8").replace("skipped=0", "skipped=1"),
        encoding="utf-8",
    )
    check["stdout_bytes"] = stdout_path.stat().st_size
    check["stdout_sha256"] = _sha256(stdout_path)
    _rewrite_gate_record(payload, check)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_rejects_bad_no_excuse_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    manifest_path = payload / "raw-gates" / "manifest.json"
    manifest = JSON_OBJECT_ADAPTER.validate_json(manifest_path.read_bytes())
    replay = manifest["replay_inputs"]
    assert isinstance(replay, dict)
    checker = replay["no_excuse_checker"]
    assert isinstance(checker, dict)
    checker["execution_result_path"] = "README.md"
    checker["command_and_input_paths_recorded_in"] = "README.md"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

    result = publish_live_evidence_bundle(payload, tmp_path / "receipt.json")

    assert result == 1


def test_publish_live_evidence_bundle_rejects_payload_changed_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _release_tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    receipt = tmp_path / "publication-receipt.json"
    def mutate_after_validation(paths: list[Path]) -> bool:
        passed = release_payloads_passed(paths)
        paths[0].write_text(paths[0].read_text(encoding="utf-8") + " ", encoding="utf-8")
        return passed

    monkeypatch.setattr(
        "saxo_bank_mcp.live_evidence_bundle.release_payloads_passed",
        mutate_after_validation,
    )

    result = publish_live_evidence_bundle(payload, receipt)

    assert result == 1
    assert json.loads(receipt.read_text(encoding="utf-8")) == {
        "reason": "release_payload_changed_during_validation",
        "status": "failed",
    }


def _release_tree(root: Path) -> Path:
    payload = root / "payload"
    raw_gates = payload / "raw-gates"
    raw_gates.mkdir(parents=True)
    source = _write(root / "src" / "sample.py", "VALUE = 1\n")
    test_sources = [
        _write(root / relative_path, "def test_placeholder():\n    pass\n")
        for relative_path in FOCUSED_RELEASE_TEST_PATHS
    ]
    test_sources.append(
        _write(root / "tests" / "test_sample.py", "def test_sample():\n    pass\n"),
    )
    readme = _write(root / "README.md", "release evidence\n")
    operator = _write(root / "docs" / "operator-guide.md", "operator guide\n")
    incident = _write(root / "docs" / "incident-cleanup.md", "incident cleanup\n")
    pyproject = _write(root / "pyproject.toml", "[project]\nname='fixture'\n")
    lock = _write(root / "uv.lock", "version = 1\n")
    checker = _write(raw_gates / "retained-tools" / "check-no-excuse-rules.py", "pass\n")
    produced = [
        _write(raw_gates / "secret-scan.json", '{"status":"passed"}\n'),
        _write(raw_gates / "live-write-refusal.json", '{"status":"refused"}\n'),
        _write(raw_gates / "live-read-refusal.json", '{"status":"refused"}\n'),
        _write(raw_gates / "retained-evidence-scan.json", '{"status":"passed"}\n'),
    ]
    git_head = _commit_release_sources(root)
    source.write_text("VALUE = 1\nDIRTY = True\n", encoding="utf-8")
    test_sources[-1].write_text(
        "def test_sample():\n    assert True\n",
        encoding="utf-8",
    )
    checks = _gate_records(root, raw_gates, checker, produced)
    source_files = [source, *test_sources]
    source_hashes = {
        path.relative_to(root).as_posix(): _sha256(path) for path in source_files
    }
    manifest = {
        "schema_version": "saxo-live-precheck-release-gates-v6",
        "created_at": "2026-07-22T00:00:00Z",
        "environment": "LIVE_SAFETY_CERTIFICATION",
        "cwd": ".",
        "repository_root": ".",
        "path_base": "repository_root",
        "status": "passed",
        "all_checks_exit_zero": True,
        "checks": checks,
        "check_count": EXPECTED_RELEASE_GATES,
        "git_head": git_head,
        "source_hash_algorithm": "sha256",
        "python_source_test_scope": "all .py files under src/ and tests/",
        "python_source_test_file_count": len(source_hashes),
        "policy_python_scope": "git-dirty and untracked .py files under src/ and tests/",
        "policy_python_file_count": 2,
        "python_source_test_sha256": source_hashes,
        "documentation_sha256": {
            "README.md": _sha256(readme),
            "docs/operator-guide.md": _sha256(operator),
            "docs/incident-cleanup.md": _sha256(incident),
        },
        "documentation_scope": "README.md and operator/incident guides",
        "junit_retained": False,
        "temporary_junit_removed_on_exit": True,
        "produced_artifacts": [
            {"producer_gate": gate, **_entry(path, root)}
            for gate, path in zip(
                (
                    "06-secret-scan",
                    "07-live-write-refusal",
                    "08-live-read-refusal",
                    "09-retained-evidence-scan",
                ),
                produced,
                strict=True,
            )
        ],
        "replay_inputs": {
            "pyproject_toml_sha256": _sha256(pyproject),
            "uv_lock_sha256": _sha256(lock),
            "no_excuse_checker": {
                **_entry(checker, root),
                "provenance": "retained exact copy executed by gate 05-no-excuse",
                "execution_result_path": (
                    raw_gates / "05-no-excuse.result.json"
                ).relative_to(root).as_posix(),
                "command_and_input_paths_recorded_in": (
                    raw_gates / "05-no-excuse.result.json"
                ).relative_to(root).as_posix(),
            },
        },
    }
    _write(raw_gates / "manifest.json", json.dumps(manifest, sort_keys=True) + "\n")
    _write_release_payloads(payload, git_head)
    return payload


def _manifest_check(manifest: dict[str, JsonValue], name: str) -> dict[str, JsonValue]:
    checks = manifest["checks"]
    assert isinstance(checks, list)
    return next(item for item in checks if isinstance(item, dict) and item.get("name") == name)


def _rewrite_gate_record(payload: Path, check: dict[str, JsonValue]) -> None:
    name = check["name"]
    assert isinstance(name, str)
    (payload / "raw-gates" / f"{name}.result.json").write_text(
        json.dumps(check, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _gate_records(
    root: Path,
    raw_gates: Path,
    checker: Path,
    produced: list[Path],
) -> list[dict[str, JsonValue]]:
    output = raw_gates.relative_to(root).as_posix()
    commands = {
        "01-focused-pytest": [
            ".venv/bin/pytest",
            "-q",
            "--junitxml=<temporary-junit>",
            *FOCUSED_RELEASE_TEST_PATHS,
        ],
        "02-full-pytest": [".venv/bin/pytest", "-q", "--junitxml=<temporary-junit>"],
        "03-ruff": [".venv/bin/ruff", "check", "--no-cache", "."],
        "04-basedpyright": [".venv/bin/basedpyright"],
        "05-no-excuse": [
            ".venv/bin/python",
            checker.relative_to(root).as_posix(),
            "src/sample.py",
            "tests/test_sample.py",
        ],
        "06-secret-scan": [
            ".venv/bin/python", "-m", "saxo_bank_mcp.qa", "secret-scan", "--out",
            f"{output}/secret-scan.json", "--paths", "README.md", "docs", "src", "tests",
            "data", "pyproject.toml", "uv.lock", ".github", ".gitignore",
        ],
        "07-live-write-refusal": [
            ".venv/bin/python", "-m", "saxo_bank_mcp.qa", "live-write-refusal", "--out",
            f"{output}/live-write-refusal.json",
        ],
        "08-live-read-refusal": [
            ".venv/bin/python", "-m", "saxo_bank_mcp.qa", "live-read-refusal", "--out",
            f"{output}/live-read-refusal.json",
        ],
        "09-retained-evidence-scan": [
            ".venv/bin/python", "-m", "saxo_bank_mcp.qa", "secret-scan", "--out",
            f"{output}/retained-evidence-scan.json", "--paths", ".omo/evidence", ".omo/tmp",
        ],
    }
    records: list[dict[str, JsonValue]] = []
    for name, command in commands.items():
        is_pytest = name in {"01-focused-pytest", "02-full-pytest"}
        stdout_text = (
            "release_test_counts tests=1 errors=0 failures=0 skipped=0\n"
            if is_pytest
            else "no violations in 2 file(s)\n"
            if name == "05-no-excuse"
            else "All checks passed!\n"
            if name == "03-ruff"
            else "0 errors, 0 warnings, 0 notes\n"
            if name == "04-basedpyright"
            else ""
        )
        stdout = _write(raw_gates / f"{name}.stdout.txt", stdout_text)
        stderr = _write(raw_gates / f"{name}.stderr.txt", "")
        record: dict[str, JsonValue] = {
            "name": name,
            "command": command,
            "cwd": ".",
            "repository_root": ".",
            "path_base": "repository_root",
            "exit_code": 0,
            "started_at": "2026-07-22T00:00:00Z",
            "duration_seconds": 0,
            "stdout_path": stdout.relative_to(root).as_posix(),
            "stdout_bytes": stdout.stat().st_size,
            "stdout_sha256": _sha256(stdout),
            "stderr_path": stderr.relative_to(root).as_posix(),
            "stderr_bytes": 0,
            "stderr_sha256": _sha256(stderr),
        }
        if is_pytest:
            record["test_counts"] = {
                "tests": 1,
                "errors": 0,
                "failures": 0,
                "skipped": 0,
            }
        _write(raw_gates / f"{name}.result.json", json.dumps(record, sort_keys=True) + "\n")
        records.append(record)
    assert len(produced) == EXPECTED_RELEASE_PAYLOADS
    return records


def _write_release_payloads(payload: Path, git_head: str) -> None:
    proof_events = _proof_events()
    proof: dict[str, JsonValue] = {
        "status": "completed",
        "trade_readiness": "precheck_only_not_order_ready",
        "source": {
            "git_head": git_head,
            "dirty_source_sha256": {
                "src/sample.py": _sha256(payload.parent / "src/sample.py"),
                "tests/test_sample.py": _sha256(payload.parent / "tests/test_sample.py"),
            },
        },
        "account_binding": {"account_id": "<redacted>"},
        "account_counts": {"active": 1, "total": 1},
        "before_counts": {"orders": 0, "positions": 0, "trade_messages": 0},
        "after_counts": {"orders": 0, "positions": 0, "trade_messages": 0},
        "instrument": {
            "amount": 1.0,
            "asset_type": "Stock",
            "buy_sell": "Buy",
            "uic": 30031,
            "verified_tradable_before_precheck": True,
        },
        "precheck": {
            "status": "precheck_accepted", "http_status": 200,
            "precheck_result": "Ok", "precheck_request_accepted": True,
            "root_result_explicitly_ok": True, "all_returned_results_explicitly_ok": True,
            "account_lookup_endpoint_called": True,
            "instrument_lookup_endpoint_called": True,
            "instrument_tradable": True,
            "precheck_endpoint_called": True,
            "live_write_called": False, "order_or_subscription_created": False,
            "order_placement_endpoint_called": False, "order_change_endpoint_called": False,
            "order_cancel_endpoint_called": False,
            "disclaimer_response_endpoint_called": False, "order_identifier_present": False,
            "requires_order_readback": False,
            "estimated_cash_required_value_present": True,
            "estimated_cash_required_currency_present": True,
            "estimated_total_cost_in_account_currency_value_present": True,
            "disclaimer_count": 0,
            "requires_disclaimer_review": False,
            "child_result_count": 1,
            "disclaimer_object_present": False,
            "error_object_present": False,
            "request_summary": {
                "amount": 1.0,
                "asset_type": "Stock",
                "buy_sell": "Buy",
                "duration_type": "DayOrder",
                "field_groups": ["Costs", "MarginImpactBuySell"],
                "manual_order": False,
                "order_type": "Market",
                "uic": 30031,
            },
        },
        "mcp_request_ledger": {
            "status": "passed", "ledger_complete": True, "safe_fields_only": True,
            "only_precheck_gateway_non_get": True,
            "unsafe_gateway_request_detected": False,
            "order_placement_endpoint_called": False,
            "events": proof_events, "events_evicted": 0,
            "negative_proof_available": True, "scope": "current_mcp_session",
        },
        "transport_boundary_capture": {
            "collector_complete": True, "safe_fields_only": True, "collector_exit_code": 0,
            "collector_credentials_inherited": False,
            "collector_process": "separate_process", "events": proof_events,
            "transport_layer": "httpx_async_base_transport",
        },
        "unchanged": {
            "account_money_state_fields": True, "orders": True, "orders_count": True,
            "positions": True, "positions_count": True, "trade_messages": True,
            "trade_messages_count": True,
        },
        "request_ledger_parity": True, "transport_boundary_parity": True,
        "request_ledger": proof_events,
        "secret_scan": {"clean": True, "finding_count": 0, "scan_error_count": 0},
    }
    scenarios: list[str] = [
        "generic_fastmcp_validation", "live_precheck_validation",
        "live_write_refusal", "disabled_live_read_refusal",
    ]
    reports: dict[str, dict[str, JsonValue]] = {
        "proof-production.json": proof,
        "live-read.json": {
            "status": "passed", "requested_environment": "LIVE", "network_read_count": 8,
            "read_scenarios_exercised": list(_live_read_statuses()),
            "tool_statuses": _live_read_statuses(),
            "tool_results": _live_read_tool_results(), "live_write_called": False,
            "order_or_subscription_created": False, "private_identifiers_redacted": True,
            "private_financial_data_omitted": True, "private_identifier_findings": [],
            "secret_scan": {"findings": [], "scan_errors": []},
        },
        "prod-readiness.json": {
            "status": "passed", "status_scope": "code_safety_checks_only",
            "code_safety_checks_passed": True, "production_ready": False,
            "network_call_made": False, "transport_constructed": False,
            "live_write_called": False, "order_or_subscription_created": False,
            "secret_scan": {"findings": [], "scan_errors": []},
        },
        "manual-qa.json": _manual_qa_report(payload, scenarios),
    }
    for name, report in reports.items():
        _write(payload / name, json.dumps(report, sort_keys=True) + "\n")


def _manual_qa_report(payload: Path, scenarios: list[str]) -> dict[str, JsonValue]:
    source = payload.parent / "src/sample.py"
    warning_transcript: list[JsonValue] = [
        {"logger": "fastmcp.server.server", "level": "WARNING", "message": "canary"},
    ]
    return {
            "schema_version": "saxo-manual-live-boundary-v1", "status": "passed",
            "checked_at": "2026-07-22T00:00:00Z", "generator": "fixture",
            "generator_source_sha256": _sha256(source),
            "scope": "local_fastmcp_live_safety_boundaries", "scenario_count": 4,
            "scenarios": [_manual_scenario(item) for item in scenarios],
            "replay_command": ["uv", "run", "python", "-m", "saxo_bank_mcp.qa",
                               "manual-live-boundary", "--out", "payload/manual-qa.json"],
            "source_hash_algorithm": "sha256",
            "source_hashes": [{
                "path": "src/sample.py",
                "bytes": source.stat().st_size,
                "sha256": _sha256(source),
            }],
            "rejected_input": {
                "generated_for_this_run": True, "persisted": False, "sha256": "a" * 64,
            },
            "warning_log_transcript": warning_transcript,
            "warning_log_transcript_sha256": hashlib.sha256(
                json.dumps(warning_transcript, sort_keys=True).encode(),
            ).hexdigest(),
            "warning_capture_verified": True, "network_call_made": False,
            "live_write_called": False, "order_or_subscription_created": False,
    }


def _manual_scenario(scenario_id: str) -> dict[str, JsonValue]:
    expected = {
        "generic_fastmcp_validation": (
            "saxo_call_registered_endpoint",
            "invalid_arguments",
        ),
        "live_precheck_validation": ("saxo_precheck_live_order", "invalid_request"),
        "live_write_refusal": ("saxo_place_sim_order", "refused"),
        "disabled_live_read_refusal": (
            "saxo_call_registered_endpoint",
            "live_not_called",
        ),
    }
    tool_name, status = expected[scenario_id]
    return {
        "scenario_id": scenario_id,
        "status": "passed",
        "actual_status": status,
        "argument_shape": {"field": "str"},
        "exception_type": "",
        "expected_status": status,
        "live_write_called": False,
        "network_call_made": False,
        "order_or_subscription_created": False,
        "rejected_input_absent_from_mcp_result": True,
        "rejected_input_absent_from_warning_logs": True,
        "result_content": [],
        "result_is_error": True,
        "safety_fields_passed": True,
        "structured_result_keys": ["status"],
        "tool_name": tool_name,
        "transport_constructed": False,
        "warning_records": [],
    }


def _proof_events() -> list[dict[str, JsonValue]]:
    requests: tuple[tuple[str, str, list[str]], ...] = (
        ("GET", "/openapi/port/v1/accounts/me", []),
        ("GET", "/openapi/port/v1/accounts/me", []),
        ("GET", "/openapi/ref/v1/instruments/details/{redacted}/{redacted}", []),
        ("GET", "/openapi/port/v1/orders/me", []),
        ("GET", "/openapi/port/v1/orders/me", []),
        ("GET", "/openapi/port/v1/positions/me", []),
        ("GET", "/openapi/port/v1/positions/me", []),
        ("GET", "/openapi/port/v1/balances/me", []),
        ("GET", "/openapi/port/v1/balances/me", []),
        ("GET", "/openapi/trade/v1/messages", []),
        ("GET", "/openapi/trade/v1/messages", []),
        ("POST", "/openapi/trade/v2/orders/precheck", []),
    )
    return [
        {
            "timestamp": "2026-07-22T00:00:00Z",
            "phase": phase,
            "host_role": "gateway",
            "method": method,
            "path": path,
            "query_names": query_names,
            "query_present": bool(query_names),
            "status": None if phase == "attempted" else 200,
        }
        for method, path, query_names in requests
        for phase in ("attempted", "completed")
    ]


def _live_read_statuses() -> dict[str, str]:
    return {
        "saxo_get_session_capabilities": "passed",
        "saxo_get_entitlements": "passed",
        "saxo_list_registered_endpoints": "metadata_only_not_ready_for_trading",
        "saxo_call_registered_endpoint_public_diagnostics": "passed",
        "saxo_call_registered_endpoint_authenticated_account": "passed",
        "saxo_call_registered_endpoint_balances": "passed",
        "saxo_call_registered_endpoint_positions": "passed",
        "saxo_call_registered_endpoint_orders": "passed",
        "saxo_call_registered_endpoint_prices": "passed",
    }


def _live_read_tool_results() -> dict[str, dict[str, JsonValue]]:
    statuses = _live_read_statuses()
    results: dict[str, dict[str, JsonValue]] = {
        name: {
            "status": status,
            "tool_name": name,
            "network_call_made": True,
            "live_write_called": False,
            "order_or_subscription_created": False,
        }
        for name, status in statuses.items()
    }
    results["saxo_list_registered_endpoints"]["network_call_made"] = False
    results["saxo_call_registered_endpoint_public_diagnostics"]["auth_exercised"] = False
    identities = {
        "saxo_call_registered_endpoint_public_diagnostics": (
            "get.root.v1.diagnostics.get", "/root/v1/diagnostics/get", "raw_response_body",
        ),
        "saxo_call_registered_endpoint_authenticated_account": (
            "get.port.v1.accounts.me", "/port/v1/accounts/me", "raw_response_body",
        ),
        "saxo_call_registered_endpoint_balances": (
            "get.port.v1.balances.me", "/port/v1/balances/me", "account_money_state_fields",
        ),
        "saxo_call_registered_endpoint_positions": (
            "get.port.v1.positions.me", "/port/v1/positions/me", "raw_response_body",
        ),
        "saxo_call_registered_endpoint_orders": (
            "get.port.v1.orders.me", "/port/v1/orders/me", "raw_response_body",
        ),
        "saxo_call_registered_endpoint_prices": (
            "get.trade.v1.infoprices", "/trade/v1/infoprices", "raw_response_body",
        ),
    }
    for name, (operation_id, path, scope) in identities.items():
        results[name].update(
            {
                "tool_name": "saxo_call_registered_endpoint",
                "operation_id": operation_id,
                "method": "GET",
                "path": path,
                "response_fingerprint_scope": scope,
                "http_status": 200,
            },
        )
    for name in identities:
        if name != "saxo_call_registered_endpoint_public_diagnostics":
            results[name]["auth_exercised"] = True
    return results


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _commit_release_sources(root: Path) -> str:
    git = shutil.which("git")
    assert git is not None
    commands = (
        (git, "init", "-q"),
        (git, "add", "src", "tests", "README.md", "docs", "pyproject.toml", "uv.lock"),
        (
            git,
            "-c",
            "user.name=Release Fixture",
            "-c",
            "user.email=release-fixture",
            "commit",
            "-qm",
            "release fixture",
        ),
    )
    for command in commands:
        subprocess.run(command, cwd=root, check=True, capture_output=True, timeout=10)
    result = subprocess.run(
        (git, "rev-parse", "HEAD"),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _entry(path: Path, root: Path) -> dict[str, str | int]:
    return {
        "path": path.relative_to(root).as_posix(),
        "path_base": "repository_root",
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
