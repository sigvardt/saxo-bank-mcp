from __future__ import annotations

import json
import sys
from typing import Final

SOURCE_MATRIX_CHILD_TOOLS_LITERAL: Final[tuple[str, ...]] = (
    "saxo_auth_status",
    "saxo_list_registered_endpoints",
    "saxo_get_session_capabilities",
    "saxo_get_entitlements",
    "saxo_get_safe_request_ledger",
    "saxo_call_registered_endpoint",
)
_SCENARIOS: Final[frozenset[str]] = frozenset(
    {"normal", "early_exit", "malformed", "truncated", "nonzero"},
)
_EXPECTED_ARG_COUNT: Final = 2


def _write_json(value: object) -> None:
    encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _result_for(message: dict[str, object]) -> dict[str, object] | None:
    method = message.get("method")
    if method == "notifications/initialized":
        return None
    request_id = message.get("id")
    params = message.get("params")
    if method == "initialize" and isinstance(params, dict):
        result: dict[str, object] = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "sealed-source-matrix-fixture", "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {
                    "name": name,
                    "description": "sealed fixture",
                    "inputSchema": {"type": "object", "additionalProperties": True},
                }
                for name in SOURCE_MATRIX_CHILD_TOOLS_LITERAL
            ],
        }
    elif method == "tools/call":
        result = {
            "content": [],
            "structuredContent": {"status": "passed"},
            "isError": False,
        }
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "method not found"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    if len(sys.argv) != _EXPECTED_ARG_COUNT or sys.argv[1] not in _SCENARIOS:
        return 64
    scenario = sys.argv[1]
    if scenario == "early_exit":
        return 0
    for line in sys.stdin.buffer:
        message = json.loads(line.decode("utf-8", errors="strict"))
        if not isinstance(message, dict):
            return 65
        if scenario == "malformed":
            sys.stdout.buffer.write(b"{malformed}\n")
            sys.stdout.buffer.flush()
            return 0
        if scenario == "truncated":
            sys.stdout.buffer.write(b'{"jsonrpc":"2.0"')
            sys.stdout.buffer.flush()
            return 0
        response = _result_for(message)
        if response is not None:
            _write_json(response)
    return 17 if scenario == "nonzero" else 0


if __name__ == "__main__":
    raise SystemExit(main())
