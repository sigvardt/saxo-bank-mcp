from __future__ import annotations

import json
from pathlib import Path

from saxo_bank_mcp._evidence import JsonValue, write_json
from saxo_bank_mcp._redaction import redact_json
from saxo_bank_mcp.qa_events import base_event
from saxo_bank_mcp.qa_live_evidence import private_identifier_findings
from saxo_bank_mcp.secret_scan import scan_secret_text


class QaLiveProbeSerializationError(TypeError):
    pass


def write_secret_scanned_event(
    out: Path,
    payload: dict[str, JsonValue],
    *,
    mirrors: tuple[Path, ...] = (),
) -> int:
    redacted = redact_json(payload)
    if not isinstance(redacted, dict):
        raise QaLiveProbeSerializationError
    identifier_findings = private_identifier_findings(redacted)
    if "private_identifiers_redacted" in redacted:
        redacted["private_identifiers_redacted"] = not identifier_findings
    redacted["private_identifier_findings"] = identifier_findings
    redacted["evidence_redaction_status"] = "passed" if not identifier_findings else "failed"
    candidate = json.dumps(redacted, indent=2, sort_keys=True) + "\n"
    findings, scan_errors = scan_secret_text(out.name, candidate)
    scanned: dict[str, JsonValue] = {
        **redacted,
        "secret_scan": {"findings": findings, "scan_errors": scan_errors},
    }
    final_text = json.dumps(scanned, indent=2, sort_keys=True) + "\n"
    final_findings, final_errors = scan_secret_text(out.name, final_text)
    if final_findings or final_errors:
        scanned = {
            **base_event(
                "evidence-publication",
                "failed",
                "secret scan rejected evidence before publication",
            ),
            "secret_scan": {
                "findings": final_findings,
                "scan_errors": final_errors,
            },
        }
    write_json(out, scanned)
    for mirror in mirrors:
        write_json(mirror, scanned)
    clean = not any(
        (findings, scan_errors, final_findings, final_errors, identifier_findings),
    )
    return 0 if clean else 1
