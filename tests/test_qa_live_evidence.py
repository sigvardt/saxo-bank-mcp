from __future__ import annotations

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
