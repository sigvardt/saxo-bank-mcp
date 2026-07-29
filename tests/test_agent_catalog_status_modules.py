from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.agent_catalog_statuses import status_discovery

ROOT: Final = Path(__file__).resolve().parents[1]
STATUS_SOURCE: Final = ROOT / "data/saxo/agent_status_routes.json"
JSON_ADAPTER: Final[TypeAdapter[JsonValue]] = TypeAdapter(JsonValue)
STREAMING_PAYLOAD_MODULE: Final = "src/saxo_bank_mcp/streaming_tool_payloads.py"
STREAMING_PAYLOAD_STATUSES: Final = frozenset({"auth_required", "denied"})


def test_streaming_tool_payload_status_module_is_classified_and_routed() -> None:
    # Given: registered streaming tools route through streaming_tool_payloads helpers.
    discovery = status_discovery(ROOT)
    included = {module.module: frozenset(module.statuses) for module in discovery.included}
    status_rows = _status_rows()

    # When: the catalog status-module coverage is checked.
    module_statuses = included.get(STREAMING_PAYLOAD_MODULE, frozenset())
    missing_routes = STREAMING_PAYLOAD_STATUSES - frozenset(status_rows)

    # Then: the reachable helper module is classified and its statuses are non-success routes.
    assert STREAMING_PAYLOAD_MODULE in included
    assert module_statuses >= STREAMING_PAYLOAD_STATUSES
    assert missing_routes == frozenset()
    for status in STREAMING_PAYLOAD_STATUSES:
        assert status_rows[status].get("unqualified_mutation_success") is False


def _status_rows() -> dict[str, dict[str, JsonValue]]:
    data = JSON_ADAPTER.validate_python(json.loads(STATUS_SOURCE.read_text(encoding="utf-8")))
    assert isinstance(data, dict)
    rows = data.get("status_routes")
    assert isinstance(rows, list)
    parsed: dict[str, dict[str, JsonValue]] = {}
    for row in rows:
        assert isinstance(row, dict)
        status = row.get("status")
        assert isinstance(status, str)
        parsed[status] = row
    return parsed
