from __future__ import annotations

import json

from saxo_bank_mcp.qa_live_evidence import (
    OMITTED_LIVE_RESPONSE,
    private_identifier_findings,
    sanitize_live_read_payloads,
)


def test_live_read_evidence_omits_endpoint_response_body() -> None:
    client_key = "Client" + "Id"
    client_value = "client-" + "123456"
    payloads = {
        "account": {
            "status": "passed",
            "tool_name": "saxo_call_registered_endpoint",
            "response": f'{{"{client_key}":"{client_value}","Balance":12.5}}',
        },
    }

    sanitized = sanitize_live_read_payloads(payloads)

    account = sanitized["account"]
    assert account["response"] == OMITTED_LIVE_RESPONSE
    assert account["response_omitted_from_evidence"] is True
    assert private_identifier_findings(sanitized) == []


def test_live_read_evidence_omits_identifier_bearing_routes() -> None:
    marker = "synthetic-client-" + "route-marker"
    unsafe_template = "/hist/v3/positions/{ClientKey}"
    safe_template = "/hist/v4/performance/summary"
    payloads = {
        "concrete_route": {
            "status": "passed",
            "path": f"/hist/v3/positions/{marker}",
            "resolved_path": f"/hist/v3/positions/{marker}",
        },
        "parameterized_template": {"status": "passed", "path": unsafe_template},
        "static_route": {
            "status": "passed",
            "path": safe_template,
            "resolved_path": safe_template,
        },
    }

    sanitized = sanitize_live_read_payloads(payloads)

    assert "path" not in sanitized["concrete_route"]
    assert "resolved_path" not in sanitized["concrete_route"]
    assert "path" not in sanitized["parameterized_template"]
    assert sanitized["static_route"]["path"] == safe_template
    assert "resolved_path" not in sanitized["static_route"]
    assert marker not in json.dumps(sanitized, sort_keys=True)
    assert private_identifier_findings(sanitized) == []


def test_private_identifier_findings_reports_concrete_route_without_value() -> None:
    marker = "synthetic-account-" + "route-marker"

    findings = private_identifier_findings(
        {"resolved_path": f"/port/v1/accounts/{marker}/orders"},
    )

    assert findings == [
        {
            "path": "resolved_path",
            "key": "resolved_path",
            "class": "private_identifier_route",
        },
    ]
    assert marker not in json.dumps(findings, sort_keys=True)


def test_private_identifier_findings_reports_unredacted_fields_without_values() -> None:
    account_group_id = "Account" + "GroupId"
    findings = private_identifier_findings(
        {
            account_group_id: "group-" + "123456",
            "Nested": {"Client" + "Id": "<redacted>"},
        },
    )

    assert findings == [
        {
            "path": account_group_id,
            "key": account_group_id,
            "class": "private_identifier",
        },
    ]
