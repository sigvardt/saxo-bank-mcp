from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from saxo_bank_mcp._evidence import JsonValue, write_text
from saxo_bank_mcp.secret_scan import scan_secret_text


def write_scanned_json(path: Path, payload: Mapping[str, JsonValue]) -> bool:
    text = json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n"
    return write_scanned_text(path, text, json_output=True)


def write_scanned_text(path: Path, text: str, *, json_output: bool = False) -> bool:
    findings, errors = scan_secret_text(path.name, text)
    if findings or errors:
        safe_text = (
            '{\n  "status": "failed",\n  "reason": "evidence_secret_scan_failed"\n}\n'
            if json_output
            else "# Evidence Publication Failed\n\n- status: `failed`\n"
            "- reason: `evidence_secret_scan_failed`\n"
        )
        write_text(path, safe_text)
        return False
    write_text(path, text)
    return True
