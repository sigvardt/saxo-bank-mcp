from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.secret_scan import scan_secret_text, secret_scan_pattern_classes

_JSON_OBJECT_ADAPTER: Final[TypeAdapter[dict[str, JsonValue]]] = TypeAdapter(
    dict[str, JsonValue],
)


def write_scanned_artifact(out: Path, payload: dict[str, JsonValue]) -> bool:
    redacted = _JSON_OBJECT_ADAPTER.validate_python(redact_json(payload))
    candidate_text = _json_text(redacted)
    findings, scan_errors = scan_secret_text(out.name, candidate_text)
    clean = not findings and not scan_errors
    scan_summary: dict[str, JsonValue] = {
        "clean": clean,
        "finding_count": len(findings),
        "scan_error_count": len(scan_errors),
        "pattern_classes": list(secret_scan_pattern_classes()),
    }
    final_payload: dict[str, JsonValue] = {
        **(redacted if clean else _scan_abort_payload()),
        "secret_scan": scan_summary,
    }
    final_text = _json_text(final_payload)
    final_findings, final_errors = scan_secret_text(out.name, final_text)
    if final_findings or final_errors:
        clean = False
        final_payload = {
            **_scan_abort_payload(),
            "secret_scan": {
                "clean": False,
                "finding_count": len(final_findings),
                "scan_error_count": len(final_errors),
                "pattern_classes": list(secret_scan_pattern_classes()),
            },
        }
        final_text = _json_text(final_payload)
    _atomic_write(out, final_text)
    return clean


def _scan_abort_payload() -> dict[str, JsonValue]:
    return {
        "status": "aborted",
        "abort_stage": "artifact",
        "abort_reason": "artifact_secret_scan_failed",
    }


def _json_text(payload: dict[str, JsonValue]) -> str:
    return json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"


def _atomic_write(out: Path, text: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        dir=out.parent,
        prefix=f".{out.name}.",
        suffix=".tmp",
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(out)
        directory_fd = os.open(out.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)
