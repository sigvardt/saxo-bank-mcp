from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest
from fastmcp import Client
from fastmcp.tools import FunctionTool
from pydantic import BaseModel

from saxo_bank_mcp.fastmcp_logging_safety import FASTMCP_VALIDATION_SAFETY_TRANSFORM
from saxo_bank_mcp.live_precheck_request import LiveOrderPrecheckRequest
from saxo_bank_mcp.server import mcp

_MARKER = "tribunal-v19-o1-validation-marker"
_GENERIC_ERROR = "Tool input validation failed."
_GENERIC_EXECUTION_ERROR = "Tool execution failed."


class _RecordCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_invalid_function_tool_input_is_absent_from_warning_logs() -> None:
    logger = logging.getLogger("fastmcp.server.server")
    capture = _RecordCapture()
    logger.addHandler(capture)
    try:
        async with Client(mcp) as client:
            await client.call_tool(
                "saxo_call_registered_endpoint",
                {"method": "GET", "path": [_MARKER]},
                raise_on_error=False,
            )
    finally:
        logger.removeHandler(capture)

    assert _MARKER not in "\n".join(record.getMessage() for record in capture.records)


@pytest.mark.anyio
async def test_invalid_function_tool_input_returns_generic_client_error() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": [_MARKER]},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert [getattr(block, "text", "") for block in result.content] == [_GENERIC_ERROR]
    assert result.structured_content == {
        "status": "invalid_arguments",
        "message": _GENERIC_ERROR,
    }
    assert _MARKER not in repr(result.content)
    assert _MARKER not in repr(result.structured_content)


@pytest.mark.anyio
async def test_internal_validation_error_returns_generic_client_error() -> None:
    class _Payload(BaseModel):
        amount: int

    async def raises_validation_error() -> dict[str, str]:
        _Payload.model_validate({"amount": _MARKER})
        return {"status": "unreachable"}

    tools = await FASTMCP_VALIDATION_SAFETY_TRANSFORM.list_tools(
        [FunctionTool.from_function(raises_validation_error)],
    )
    safe_tool = tools[0]

    result = await safe_tool.run({})

    assert result.is_error is True
    assert [getattr(block, "text", "") for block in result.content] == [_GENERIC_ERROR]
    assert _MARKER not in repr(result.content)
    assert _MARKER not in repr(result.structured_content)


@pytest.mark.anyio
async def test_internal_os_error_returns_generic_client_error() -> None:
    async def raises_os_error() -> dict[str, str]:
        raise OSError(f"cache write failed at /private/{_MARKER}")

    tools = await FASTMCP_VALIDATION_SAFETY_TRANSFORM.list_tools(
        [FunctionTool.from_function(raises_os_error)],
    )

    result = await tools[0].run({})

    assert result.is_error is True
    assert [getattr(block, "text", "") for block in result.content] == [
        _GENERIC_EXECUTION_ERROR,
    ]
    assert result.structured_content == {
        "status": "tool_error",
        "message": _GENERIC_EXECUTION_ERROR,
    }
    assert _MARKER not in repr(result.content)
    assert _MARKER not in repr(result.structured_content)


@pytest.mark.anyio
async def test_internal_value_error_returns_generic_client_error() -> None:
    async def raises_value_error() -> dict[str, str]:
        _ = int(_MARKER)
        return {"status": "unreachable"}

    tools = await FASTMCP_VALIDATION_SAFETY_TRANSFORM.list_tools(
        [FunctionTool.from_function(raises_value_error)],
    )

    result = await tools[0].run({})

    assert result.is_error is True
    assert [getattr(block, "text", "") for block in result.content] == [
        _GENERIC_EXECUTION_ERROR,
    ]
    assert _MARKER not in repr(result.content)
    assert _MARKER not in repr(result.structured_content)


@pytest.mark.anyio
async def test_live_precheck_os_error_is_absent_from_result_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def raises_os_error(_order: LiveOrderPrecheckRequest) -> dict[str, str]:
        raise OSError(f"cache write failed at /private/{_MARKER}")

    monkeypatch.setattr(
        "saxo_bank_mcp.live_precheck_tool.saxo_precheck_live_order",
        raises_os_error,
    )
    arguments = {
        "order": {
            "uic": 30031,
            "asset_type": "Stock",
            "amount": 1,
            "buy_sell": "Buy",
        },
    }

    with caplog.at_level(logging.WARNING):
        async with Client(mcp) as client:
            result = await client.call_tool(
                "saxo_precheck_live_order",
                arguments,
                raise_on_error=False,
            )

    assert result.is_error is True
    assert _MARKER not in repr(result.content)
    assert _MARKER not in repr(result.structured_content)
    assert _MARKER not in caplog.text


def test_fastmcp_strict_validation_environment_cannot_bypass_safe_tool() -> None:
    environment = {**os.environ, "FASTMCP_STRICT_INPUT_VALIDATION": "true"}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from saxo_bank_mcp.server import mcp; print(mcp.strict_input_validation)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.stdout.strip() == "False"


@pytest.mark.anyio
async def test_unknown_tool_name_returns_generic_client_error() -> None:
    unknown_name = f"unknown-{_MARKER}"
    async with Client(mcp) as client:
        result = await client.call_tool(
            unknown_name,
            {"unsafe_marker": _MARKER},
            raise_on_error=False,
        )

    assert result.is_error is True
    assert [getattr(block, "text", "") for block in result.content] == ["Unknown tool."]
    assert result.structured_content == {
        "status": "unknown_tool",
        "message": "Unknown tool.",
    }
    assert _MARKER not in repr(result.content)
    assert _MARKER not in repr(result.structured_content)


@pytest.mark.anyio
async def test_validation_safety_covers_every_registered_function_tool() -> None:
    tools = await mcp.list_tools()
    function_tools = [tool for tool in tools if isinstance(tool, FunctionTool)]

    assert function_tools
    async with Client(mcp) as client:
        for tool in function_tools:
            result = await client.call_tool(
                tool.name,
                {"unexpected_argument": [_MARKER]},
                raise_on_error=False,
            )

            assert result.is_error is True
            assert [getattr(block, "text", "") for block in result.content] == [
                _GENERIC_ERROR,
            ]
            assert _MARKER not in repr(result.structured_content)


@pytest.mark.anyio
async def test_valid_function_tool_call_keeps_structured_result() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool("saxo_health", {})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "passed"
    assert result.structured_content["service"] == "saxo-bank-mcp"


@pytest.mark.anyio
async def test_valid_function_tool_call_keeps_structured_project_error() -> None:
    async with Client(mcp) as client:
        result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": "/not-a-registered-saxo-path"},
            raise_on_error=False,
        )

    assert result.structured_content is not None
    assert result.structured_content["status"] == "denied"
    assert result.structured_content["denial_reason"] == "unregistered_endpoint"
