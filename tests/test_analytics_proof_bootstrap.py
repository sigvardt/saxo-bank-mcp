from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BOOTSTRAP = (
    Path(__file__).resolve().parents[1] / "src/saxo_bank_mcp/qa_analytics_proof_bootstrap.py"
)
CANDIDATE = "1" * 40
CACHE_SHA256 = "2" * 64
CATALOG_SHA256 = "4" * 64
CONTRACT_SHA256 = "5" * 64
CONFIG_FAILURE_EXIT = 78
ARGUMENT_FAILURE_EXIT = 2
CRASH_EXIT = 17
PRODUCER_FAILURE_EXIT = 9
OWNER_FILE_MODE = 0o600
OWNER_DIRECTORY_MODE = 0o700


@dataclass(frozen=True, slots=True)
class _BootstrapRun:
    process: subprocess.CompletedProcess[str]
    envelope_path: Path
    producer_marker: Path


def _run_bootstrap(
    tmp_path: Path,
    producer_source: str,
    *,
    envelope_path: Path | None = None,
) -> _BootstrapRun:
    import_root = tmp_path / "import-root"
    package = import_root / "saxo_bank_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    producer = package / "qa_analytics_proof_producer.py"
    producer.write_text(producer_source, encoding="utf-8")
    producer_sha256 = hashlib.sha256(producer.read_bytes()).hexdigest()
    bootstrap_sha256 = hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest()
    evidence = tmp_path / "private-evidence"
    evidence.mkdir(mode=OWNER_DIRECTORY_MODE)
    target = envelope_path or evidence / "bootstrap.json"
    marker = tmp_path / "producer-imported"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(import_root),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.run(
        (
            sys.executable,
            str(BOOTSTRAP),
            "--envelope-path",
            str(target),
            "--candidate-commit",
            CANDIDATE,
            "--installed-cache-sha256",
            CACHE_SHA256,
            "--bootstrap-module-sha256",
            bootstrap_sha256,
            "--producer-module-sha256",
            producer_sha256,
            "--catalog-sha256",
            CATALOG_SHA256,
            "--contract-sha256",
            CONTRACT_SHA256,
            "--harness-policy",
            "codex_native_v1",
        ),
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    return _BootstrapRun(process=process, envelope_path=target, producer_marker=marker)


def _read_authenticated_envelope(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    digest = payload.pop("envelope_sha256")
    expected = hashlib.sha256(
        json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode(),
    ).hexdigest()
    assert digest == expected
    payload["envelope_sha256"] = digest
    return payload


def test_bootstrap_source_imports_only_stdlib() -> None:
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
    imported_roots = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert imported_roots <= sys.stdlib_module_names | {"__future__"}


def test_bootstrap_import_failure_retains_authenticated_no_execution(
    tmp_path: Path,
) -> None:
    result = _run_bootstrap(
        tmp_path,
        "raise RuntimeError('access_' + 'token=must-not-leak')\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == 1
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["completed_bootstrap_phases"] == ["entry"]
    assert envelope["current_bootstrap_phase"] == "producer_import"
    assert envelope["child_exit_code"] == 1
    assert envelope["execution_performed"] is False
    assert envelope["network_call_made"] is False
    assert envelope["model_event_count"] == 0
    assert envelope["mcp_event_count"] == 0
    assert envelope["saxo_event_count"] == 0
    assert envelope["broker_write_made"] is False
    assert envelope["live_mutation_calls"] == 0
    assert envelope["purchase_occurred"] is False
    assert envelope["disclaimer_response_made"] is False
    assert envelope["reason"] == "proof_bootstrap_import_failed"
    assert "access_token" not in json.dumps(envelope)
    assert stat.S_IMODE(result.envelope_path.parent.stat().st_mode) == OWNER_DIRECTORY_MODE
    assert stat.S_IMODE(result.envelope_path.stat().st_mode) == OWNER_FILE_MODE


def test_bootstrap_catches_producer_argument_system_exit(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "def main(argv):\n    del argv\n    raise SystemExit(2)\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == ARGUMENT_FAILURE_EXIT
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["current_bootstrap_phase"] == "producer_execution"
    assert envelope["child_exit_code"] == ARGUMENT_FAILURE_EXIT
    assert envelope["execution_performed"] is None
    assert envelope["network_call_made"] is None
    assert envelope["reason"] == "proof_bootstrap_invalid_arguments"


def test_bootstrap_catches_pre_tracker_failure_without_raw_error(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "def main(argv):\n    del argv\n    raise RuntimeError('account_' + 'id=must-not-leak')\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == 1
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["child_exit_code"] == 1
    assert envelope["execution_performed"] is None
    assert envelope["broker_write_made"] is None
    assert envelope["reason"] == "proof_bootstrap_producer_exception"
    assert "account_id" not in json.dumps(envelope)


def test_bootstrap_entry_write_failure_stops_before_producer_import(tmp_path: Path) -> None:
    marker = tmp_path / "producer-imported"
    blocking_parent = tmp_path / "not-a-directory"
    blocking_parent.write_text("blocked", encoding="utf-8")
    result = _run_bootstrap(
        tmp_path,
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n",
        envelope_path=blocking_parent / "bootstrap.json",
    )

    assert result.process.returncode == CONFIG_FAILURE_EXIT
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert not marker.exists()
    assert not result.envelope_path.exists()


def test_bootstrap_child_crash_retains_unknown_handoff(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "import os\ndef main(argv):\n    del argv\n    os._exit(17)\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == CRASH_EXIT
    assert envelope["bootstrap_state"] == "producer_started"
    assert envelope["completed_bootstrap_phases"] == [
        "entry",
        "producer_import",
        "producer_handoff",
    ]
    assert envelope["current_bootstrap_phase"] == "producer_execution"
    assert envelope["child_exit_code"] is None
    assert envelope["execution_performed"] is None
    assert envelope["network_call_made"] is None
    assert envelope["broker_write_made"] is None


def test_bootstrap_normal_nonzero_is_sanitized(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "def main(argv):\n    del argv\n    return 9\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == PRODUCER_FAILURE_EXIT
    assert result.process.stdout == ""
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "failed"
    assert envelope["child_exit_code"] == PRODUCER_FAILURE_EXIT
    assert envelope["execution_performed"] is None
    assert envelope["reason"] == "proof_bootstrap_producer_nonzero"


def test_bootstrap_normal_success_retains_completed_receipt(tmp_path: Path) -> None:
    result = _run_bootstrap(
        tmp_path,
        "import sys\n"
        "def main(argv):\n"
        "    del argv\n"
        '    sys.stdout.write(\'{"status":"ok"}\')\n'
        "    return 0\n",
    )

    envelope = _read_authenticated_envelope(result.envelope_path)
    assert result.process.returncode == 0
    assert result.process.stdout == '{"status":"ok"}'
    assert result.process.stderr == ""
    assert envelope["bootstrap_state"] == "complete"
    assert envelope["completed_bootstrap_phases"] == [
        "entry",
        "producer_import",
        "producer_handoff",
        "producer_execution",
    ]
    assert envelope["current_bootstrap_phase"] == "complete"
    assert envelope["child_exit_code"] == 0
    assert envelope["reason"] == "proof_bootstrap_complete"
