from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Final

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json, scan_secret_paths
from saxo_bank_mcp.evidence_publication import write_scanned_json
from saxo_bank_mcp.qa_auth_probes import handle_auth_status
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.qa_probes import handle_health

README_DOC_PATHS: Final[tuple[Path, ...]] = (
    Path("README.md"),
    Path("docs/operator-guide.md"),
    Path("docs/incident-cleanup.md"),
)
README_REQUIRED_MARKERS: Final[tuple[str, ...]] = (
    "uv run python -m saxo_bank_mcp.qa health --out",
    "uv run python -m saxo_bank_mcp.qa auth-status --out",
    "uv run python -m saxo_bank_mcp.qa sim-auth --out",
    "uv run python -m saxo_bank_mcp.qa read-smoke --groups all --out",
    "uv run python -m saxo_bank_mcp.qa live-read --out",
    "uv run python -m saxo_bank_mcp.qa live-write-refusal --out",
    "uv run python -m saxo_bank_mcp.qa stream-cleanup --simulate-leak --out",
    "uv run python -m saxo_bank_mcp.tribunal_index --out",
    "uv run python -m saxo_bank_mcp.final_verify plan",
    "uv run python -m saxo_bank_mcp.final_verify code",
    "uv run python -m saxo_bank_mcp.final_verify mcp",
    "uv run python -m saxo_bank_mcp.final_verify scope",
    "uv run saxo-bank-mcp --transport http --host 127.0.0.1 --port 8000",
    "SAXO_MCP_ENABLE_LIVE_READS=1",
    "SAXO_MCP_ENABLE_LIVE_WRITES=I_UNDERSTAND_REAL_MONEY_RISK",
)
FINAL_VERIFY_HELP_COMMANDS: Final[tuple[str, ...]] = ("plan", "code", "mcp", "scope")
JSON_OBJECT_ADAPTER: Final = TypeAdapter(dict[str, JsonValue])


class ReadmeProbePublicationError(TypeError):
    pass


def handle_readme_smoke(out: Path) -> int:
    combined_text = "\n".join(
        path.read_text(encoding="utf-8") if path.exists() else "" for path in README_DOC_PATHS
    )
    missing_paths = [str(path) for path in README_DOC_PATHS if not path.exists()]
    missing_markers = [
        marker for marker in README_REQUIRED_MARKERS if marker not in combined_text
    ]
    with TemporaryDirectory(prefix="saxo-readme-smoke-") as tmp:
        tmp_dir = Path(tmp)
        health_out = tmp_dir / "health.json"
        auth_status_out = tmp_dir / "auth-status.json"
        health_exit = handle_health(health_out)
        auth_status_exit = handle_auth_status(auth_status_out)
        health_payload = JSON_OBJECT_ADAPTER.validate_json(
            health_out.read_text(encoding="utf-8"),
        )
        auth_status_payload = JSON_OBJECT_ADAPTER.validate_json(
            auth_status_out.read_text(encoding="utf-8"),
        )

    help_exit_codes = {
        name: _final_verify_help_exit_code(name) for name in FINAL_VERIFY_HELP_COMMANDS
    }
    doc_findings, doc_scan_errors = scan_secret_paths([str(path) for path in README_DOC_PATHS])
    passed = (
        not missing_paths
        and not missing_markers
        and health_exit == 0
        and auth_status_exit == 0
        and all(code == 0 for code in help_exit_codes.values())
        and not doc_findings
        and not doc_scan_errors
    )
    event: dict[str, JsonValue] = {
        **base_event(
            "readme-smoke",
            "passed" if passed else "failed",
            "README and operator docs command smoke checked",
        ),
        "docs_checked": [str(path) for path in README_DOC_PATHS],
        "missing_paths": missing_paths,
        "required_command_markers_present": not missing_markers,
        "missing_command_markers": missing_markers,
        "health_exit_code": health_exit,
        "health_status": health_payload.get("status"),
        "auth_status_exit_code": auth_status_exit,
        "auth_status_status": auth_status_payload.get("status"),
        "final_verify_help_exit_codes": help_exit_codes,
        "executed_commands": [
            "python -m saxo_bank_mcp.qa health --out <temp>",
            "python -m saxo_bank_mcp.qa auth-status --out <temp>",
            *[
                f"python -m saxo_bank_mcp.final_verify {name} --help"
                for name in FINAL_VERIFY_HELP_COMMANDS
            ],
        ],
        "copied_secret_values_detected": bool(doc_findings),
        "prompted_user": False,
        "secret_scan": {"findings": doc_findings, "scan_errors": doc_scan_errors},
    }
    redacted = redact_json(event)
    if not isinstance(redacted, dict):
        raise ReadmeProbePublicationError("readme smoke event redaction returned non-object")
    published = write_scanned_json(out, redacted)
    return 0 if passed and published else 1


def _final_verify_help_exit_code(command: str) -> int:
    result = subprocess.run(
        [sys.executable, "-m", "saxo_bank_mcp.final_verify", command, "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode
