from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from saxo_bank_mcp import nontrade_policy, qa, qa_read_probes
from saxo_bank_mcp.server import mcp

EXPECTED_SERVICE_GROUPS = 17
TRADING_OPERATION_COUNT = 45


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_registered_endpoint_denials_are_errors() -> None:
    operation = nontrade_policy.first_nontrade_write_operation("Asset Transfers")
    assert operation is not None
    rejected_path = "/not-a-registered-saxo-path/private-caller-value"

    async with Client(mcp) as client:
        write_result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": operation.method, "path": operation.path_template},
            raise_on_error=False,
        )
        get_result = await client.call_tool(
            "saxo_call_registered_endpoint",
            {"method": "GET", "path": rejected_path},
            raise_on_error=False,
        )

    write_payload = write_result.structured_content
    get_payload = get_result.structured_content
    assert write_result.is_error is True
    assert write_payload is not None
    assert write_payload["status"] == "denied"
    assert write_payload["denied_class"] == "write"
    assert write_payload["network_call_made"] is False
    assert get_result.is_error is True
    assert get_payload is not None
    assert get_payload["status"] == "denied"
    assert get_payload["denial_reason"] == "unregistered_endpoint"
    assert rejected_path not in str(get_payload)
    assert get_payload["path"] == "<redacted-unregistered-path>"


@pytest.mark.anyio
async def test_registered_endpoint_denials_raise_by_default() -> None:
    operation = nontrade_policy.first_nontrade_write_operation("Asset Transfers")
    assert operation is not None

    async with Client(mcp) as client:
        with pytest.raises(ToolError):
            await client.call_tool(
                "saxo_call_registered_endpoint",
                {"method": operation.method, "path": operation.path_template},
            )
        with pytest.raises(ToolError):
            await client.call_tool(
                "saxo_call_registered_endpoint",
                {"method": "GET", "path": "/not-a-registered-saxo-path"},
            )


def test_read_smoke_probe_records_every_group_or_reason(tmp_path: Path) -> None:
    out = tmp_path / "read-smoke.json"

    result = qa_read_probes.handle_read_smoke(out, "all")

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "passed"
    assert report["groups"] == "all"
    assert report["fastmcp_tools"]["saxo_call_registered_endpoint"] is True
    assert len(report["per_group"]) == EXPECTED_SERVICE_GROUPS
    assert report["no_arbitrary_url_call"] is True
    assert report["live_write"] is False
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_list_registry_stdout_probe_calls_fastmcp_without_out(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = qa.main(
        ["list-registry-stdout", "--service-group", "Trading", "--limit", "1"],
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["status"] == "metadata_only_not_ready_for_trading"
    assert payload["matched_count"] == TRADING_OPERATION_COUNT
    assert payload["returned_count"] == 1
    assert payload["registry_only"] is True


def test_registered_endpoint_denied_probe_uses_real_fastmcp_tool(tmp_path: Path) -> None:
    out = tmp_path / "denied.json"

    result = qa_read_probes.handle_registered_endpoint_denied(
        out,
        method="GET",
        path="/not-a-registered-saxo-path",
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["status"] == "denied"
    assert report["tool_name"] == "saxo_call_registered_endpoint"
    assert report["network_call_made"] is False
    assert report["denial_reason"] == "unregistered_endpoint"
    assert report["secret_scan"] == {"findings": [], "scan_errors": []}


def test_registered_endpoint_denied_probe_does_not_persist_arbitrary_method(
    tmp_path: Path,
) -> None:
    out = tmp_path / "denied.json"
    caller_method = "caller-controlled-method-" + ("x" * 256)

    result = qa_read_probes.handle_registered_endpoint_denied(
        out,
        method=caller_method,
        path="/not-a-registered-saxo-path",
    )

    report = json.loads(out.read_text(encoding="utf-8"))
    assert result == 0
    assert report["method"] == "<redacted>"
    assert caller_method not in out.read_text(encoding="utf-8")
