from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest
from fastmcp import Client
from live_precheck_test_support import (
    JSON_OBJECT_ADAPTER,
    LIVE_PRECHECK_TOOL,
    accounts_body,
    capture_fastmcp_debug,
    configure_live,
    install_transport,
    invalid_arguments,
)
from pydantic import TypeAdapter

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.server import mcp


@pytest.mark.parametrize(
    ("case", "expected_errors", "blocked_markers"),
    [
        (
            "nested_order",
            [
                {
                    "location": ["order", "uic"],
                    "type": "int_type",
                    "message": "Use an integer.",
                },
                {
                    "location": ["order", "amount"],
                    "type": "float_type",
                    "message": "Use a finite number.",
                },
                {
                    "location": ["order", "buy_sell"],
                    "type": "literal_error",
                    "message": "Use one of the values allowed by the schema.",
                },
                {
                    "location": ["order", "<extra>"],
                    "type": "extra_forbidden",
                    "message": "Remove this unrecognized field.",
                },
            ],
            ["Bearer ", "UNKNOWN-FIELD-", "ACCOUNT-KEY-"],
        ),
        (
            "scalar_order",
            [
                {
                    "location": ["order"],
                    "type": "model_type",
                    "message": "Use an object matching the advertised order schema.",
                },
            ],
            ["Bearer "],
        ),
        (
            "unexpected_top_level",
            [
                {
                    "location": ["order"],
                    "type": "missing",
                    "message": "Add this required field.",
                },
                {
                    "location": ["<extra>"],
                    "type": "extra_forbidden",
                    "message": "Remove this unrecognized field.",
                },
            ],
            ["Bearer ", "unexpected"],
        ),
    ],
)
@pytest.mark.anyio
async def test_precheck_redacts_rejected_arguments_before_fastmcp_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_errors: list[dict[str, JsonValue]],
    blocked_markers: list[str],
) -> None:
    configure_live(tmp_path, monkeypatch)
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=accounts_body(), request=request)

    install_transport(monkeypatch, handler)
    sensitive_marker = "Bearer " + ("x" * 48)
    account_key = "ACCOUNT-KEY-" + ("q" * 48)
    unknown_marker = "UNKNOWN-FIELD-" + ("z" * 48)
    arguments = invalid_arguments(case, sensitive_marker)
    if case == "nested_order":
        order = TypeAdapter(dict[str, JsonValue]).validate_python(arguments["order"])
        order["uic"] = True
        order["buy_sell"] = unknown_marker
        order["account_key"] = account_key
        arguments["order"] = order
    with capture_fastmcp_debug() as captured:
        async with Client(mcp) as client:
            result = await client.call_tool(LIVE_PRECHECK_TOOL, arguments, raise_on_error=False)

    payload = JSON_OBJECT_ADAPTER.validate_python(result.structured_content)
    rendered = f"{captured()}\n{result.content!r}\n{json.dumps(payload)}"
    assert result.is_error is True
    assert payload["status"] == "invalid_request"
    assert payload["reason"] == "request_schema_invalid"
    assert payload["validation_errors"] == expected_errors
    assert payload["network_call_made"] is False
    assert payload["precheck_request_accepted"] is False
    assert payload["live_write_called"] is False
    for blocked_marker in [sensitive_marker, "https://errors.pydantic.dev", *blocked_markers]:
        assert blocked_marker not in rendered
    assert requests == []
