from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from analytics_source_matrix_support import (
    ScriptedMatrixSession,
    canonical_digest,
    matrix_payloads,
)

from saxo_bank_mcp._evidence import JsonValue
from saxo_bank_mcp.analytics_source_contracts import source_contract_catalog_sha256
from saxo_bank_mcp.qa_analytics_source_matrix import (
    ClaimedMatrixDraft,
    PreclaimReason,
    PreclaimRefusal,
    PreparedMatrix,
    SourceMatrixCandidateIdentity,
    SourceMatrixFixtures,
    prepare_analytics_source_matrix,
    prove_sim_environment,
    run_analytics_source_matrix,
)
from saxo_bank_mcp.secret_scan import scan_secret_text

EXPECTED_SOURCE_COUNT = 18
CAPTURED_AT = datetime(2026, 7, 30, 12, tzinfo=UTC)
PRIVATE_ACCOUNT_MARKER = "private-account-marker"
PRIVATE_CLIENT_MARKER = "private-client-marker"


def _identity() -> SourceMatrixCandidateIdentity:
    return SourceMatrixCandidateIdentity(
        source_contract_catalog_sha256=source_contract_catalog_sha256(),
        harness_build_sha256="b" * 64,
        candidate_identity_sha256="c" * 64,
    )


def _fixtures() -> SourceMatrixFixtures:
    return SourceMatrixFixtures(
        account_key=PRIVATE_ACCOUNT_MARKER,
        client_key=PRIVATE_CLIENT_MARKER,
        instrument_uic=211,
        asset_type="Stock",
        option_root_id=120,
    )


def _sim_env(tmp_path: Path | None = None) -> dict[str, str]:
    env = {
        "SAXO_MCP_ENVIRONMENT": "SIM",
        "SAXO_MCP_ENABLE_LIVE_READS": "0",
        "SAXO_MCP_ENABLE_LIVE_WRITES": "",
    }
    if tmp_path is not None:
        env["XDG_STATE_HOME"] = str(tmp_path)
    return env


def _prepared(fixtures: SourceMatrixFixtures | None = None) -> PreparedMatrix:
    result = prepare_analytics_source_matrix(
        env=_sim_env(),
        fixtures=fixtures or _fixtures(),
        candidate_identity=_identity(),
        captured_at=CAPTURED_AT,
    )
    assert isinstance(result, PreparedMatrix)
    return result


def _session(prepared: PreparedMatrix) -> ScriptedMatrixSession:
    return ScriptedMatrixSession(payloads=matrix_payloads(prepared))


async def _run(
    prepared: PreparedMatrix,
    session: ScriptedMatrixSession,
    *,
    claim: Callable[[], bool] | None = None,
) -> PreclaimRefusal | ClaimedMatrixDraft:
    selected_claim = claim or (lambda: True)
    return await run_analytics_source_matrix(
        session,
        prepared=prepared,
        claim_source_execution=selected_claim,
    )


def _payload_for_contract(
    session: ScriptedMatrixSession,
    contract_id: str,
) -> dict[str, JsonValue]:
    return next(
        payload
        for payload in session.payloads["saxo_call_registered_endpoint"]
        if payload.get("analytics_contract_id") == contract_id
    )


def _set_history_rows(session: ScriptedMatrixSession, rows: int) -> None:
    for contract_id in (
        "transactions_v1",
        "bookings_v1",
        "closed_positions_history_v1",
    ):
        payload = _payload_for_contract(session, contract_id)
        pages = payload["page_receipts"]
        assert isinstance(pages, list)
        first = pages[0]
        assert isinstance(first, dict)
        first["row_count"] = rows
        first["timestamp_value_count"] = rows
        payload["row_count"] = rows
        payload["timestamp_value_count"] = rows
        payload["response_fingerprint"] = canonical_digest(pages)


def _set_source_quality(
    session: ScriptedMatrixSession,
    contract_id: str,
    quality: dict[str, JsonValue],
) -> None:
    payload = _payload_for_contract(session, contract_id)
    pages = payload["page_receipts"]
    assert isinstance(pages, list)
    first = pages[0]
    assert isinstance(first, dict)
    first["source_quality"] = quality
    payload["source_quality"] = quality
    payload["response_fingerprint"] = canonical_digest(pages)


def _ledger_readback(session: ScriptedMatrixSession) -> dict[str, JsonValue]:
    return session.payloads["saxo_get_safe_request_ledger"][-1]


def test_environment_proof_requires_exact_sim_and_disabled_live_gates() -> None:
    assert prove_sim_environment(_sim_env()).status == "passed"
    for unsafe in (
        {"SAXO_MCP_ENVIRONMENT": "LIVE"},
        {"SAXO_MCP_ENVIRONMENT": "SIM", "SAXO_MCP_ENABLE_LIVE_READS": "1"},
        {"SAXO_MCP_ENVIRONMENT": "SIM", "SAXO_MCP_ENABLE_LIVE_WRITES": "1"},
        {},
    ):
        assert prove_sim_environment(unsafe).status == "refused"


@pytest.mark.anyio
async def test_matrix_claims_after_readiness_and_uses_safe_receipts_for_all_sources() -> None:
    prepared = _prepared()
    session = _session(prepared)

    def claim() -> bool:
        session.events.append(("claim", {}))
        return True

    receipt = await _run(prepared, session, claim=claim)

    assert isinstance(receipt, ClaimedMatrixDraft)
    assert receipt.status == "passed"
    assert len(receipt.source_receipts) == EXPECTED_SOURCE_COUNT
    assert {item.response_visibility for item in receipt.source_receipts} == {
        "analytics_contract_receipt",
    }
    claim_index = session.events.index(("claim", {}))
    first_source_index = next(
        index
        for index, (tool, arguments) in enumerate(session.events)
        if tool == "saxo_call_registered_endpoint"
        and arguments.get("response_mode") == "analytics_contract_receipt"
    )
    assert claim_index < first_source_index
    assert all(
        arguments["response_mode"] != "redacted_body"
        for tool, arguments in session.events
        if tool == "saxo_call_registered_endpoint"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    (
        "quality",
        "expected_reason",
        "expected_entitlement",
    ),
    [
        (
            {
                "state": "limited",
                "entitlement_limited_fields": ["PriceTypeAsk", "PriceTypeBid"],
                "delayed_fields": [],
                "missing_fields": [],
            },
            "source_field_entitlement_limited",
            "denied",
        ),
        (
            {
                "state": "limited",
                "entitlement_limited_fields": [],
                "delayed_fields": ["PriceTypeAsk", "PriceTypeBid"],
                "missing_fields": [],
            },
            "source_quote_delayed",
            "observed",
        ),
    ],
)
async def test_matrix_reduces_value_free_quote_quality_limitations(
    quality: dict[str, JsonValue],
    expected_reason: str,
    expected_entitlement: str,
) -> None:
    prepared = _prepared()
    session = _session(prepared)
    _set_source_quality(session, "info_price_v1", quality)

    receipt = await _run(prepared, session)

    assert isinstance(receipt, ClaimedMatrixDraft)
    assert receipt.status == "reduced"
    source = next(
        item for item in receipt.source_receipts if item.contract_id == "info_price_v1"
    )
    assert source.source_status == "reduced"
    assert source.outcome_reason == expected_reason
    assert source.entitlement_state == expected_entitlement
    assert source.source_quality is not None
    assert source.source_quality.model_dump(mode="json") == quality


@pytest.mark.anyio
async def test_matrix_refuses_internally_inconsistent_quote_quality_metadata() -> None:
    prepared = _prepared()
    session = _session(prepared)
    _set_source_quality(
        session,
        "info_price_v1",
        {
            "state": "limited",
            "entitlement_limited_fields": [],
            "delayed_fields": [],
            "missing_fields": [],
        },
    )

    receipt = await _run(prepared, session)

    assert isinstance(receipt, ClaimedMatrixDraft)
    source = next(
        item for item in receipt.source_receipts if item.contract_id == "info_price_v1"
    )
    assert source.source_status == "refused"
    assert source.outcome_reason == "analytics_contract_receipt_invalid"
    assert source.entitlement_state == "unverified"
    assert source.source_quality is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("stage", "reason"),
    [
        ("auth", "sim_auth_unavailable"),
        ("session", "sim_session_unavailable"),
        ("entitlements", "sim_entitlements_unavailable"),
    ],
)
async def test_readiness_refusal_never_claims_or_calls_sources(
    stage: str,
    reason: PreclaimReason,
) -> None:
    prepared = _prepared()
    session = _session(prepared)
    if stage == "auth":
        payload = session.payloads["saxo_auth_status"][0]
        payload["token_cache_expired"] = True
        payload["blocking_reasons"] = ["token_cache_expired"]
    else:
        tool = (
            "saxo_get_session_capabilities"
            if stage == "session"
            else "saxo_get_entitlements"
        )
        payload = session.payloads[tool][0]
        payload["status"] = "auth_required"
        payload["network_call_made"] = False
    claims = 0

    def claim() -> bool:
        nonlocal claims
        claims += 1
        return True

    receipt = await _run(prepared, session, claim=claim)

    assert receipt == PreclaimRefusal(reason=reason)
    assert claims == 0
    assert not any(
        tool == "saxo_call_registered_endpoint" for tool, _arguments in session.events
    )


@pytest.mark.anyio
@pytest.mark.parametrize(("rows", "expected"), [(1, "present"), (0, "absent")])
async def test_history_state_is_tri_state(rows: int, expected: str) -> None:
    prepared = _prepared()
    session = _session(prepared)
    _set_history_rows(session, rows)

    receipt = await _run(prepared, session)

    assert isinstance(receipt, ClaimedMatrixDraft)
    assert receipt.history_state == expected
    assert "controlled_activity" not in receipt.model_dump()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("changed_state", "state_fingerprint_mismatch"),
        ("unsafe_method", "unsafe_request_ledger"),
        ("live_environment", "unsafe_request_ledger"),
        ("missing_environment", "unsafe_request_ledger"),
    ],
)
async def test_matrix_fails_closed_on_state_or_ledger_proof(
    failure: str,
    expected_error: str,
) -> None:
    prepared = _prepared()
    session = _session(prepared)
    if failure == "changed_state":
        session.payloads["saxo_call_registered_endpoint"][-3][
            "response_fingerprint"
        ] = "b" * 64
    else:
        ledger = _ledger_readback(session)
        events = ledger["events"]
        assert isinstance(events, list)
        event = events[0]
        assert isinstance(event, dict)
        if failure == "unsafe_method":
            event["method"] = "POST"
            ledger["non_get_request_count"] = 1
            ledger["unsafe_gateway_request_detected"] = True
            ledger["order_placement_endpoint_called"] = True
        elif failure == "live_environment":
            event["environment"] = "LIVE"
        else:
            event.pop("environment")

    receipt = await _run(prepared, session)

    assert isinstance(receipt, ClaimedMatrixDraft)
    assert receipt.status == "failed"
    assert expected_error in receipt.errors


@pytest.mark.anyio
async def test_live_oauth_auth_event_is_not_a_live_gateway_event() -> None:
    prepared = _prepared()
    session = _session(prepared)
    ledger = _ledger_readback(session)
    events = ledger["events"]
    assert isinstance(events, list)
    events.append(
        {
            "timestamp": "2026-07-30T12:00:00+00:00",
            "phase": "attempted",
            "host_role": "oauth",
            "environment": "LIVE",
            "method": "POST",
            "path": "/token",
            "query_names": [],
            "query_present": False,
            "status": None,
        },
    )
    ledger["request_count"] = len(events)
    ledger["non_get_request_count"] = 1

    receipt = await _run(prepared, session)

    assert isinstance(receipt, ClaimedMatrixDraft)
    assert receipt.status == "passed"
    assert receipt.ledger.sim_only is True
    assert receipt.ledger.live_events == 0
    assert receipt.ledger.gateway_environments == ("SIM",)


@pytest.mark.anyio
async def test_matrix_receipt_contains_no_private_fixture_values() -> None:
    prepared = _prepared()
    receipt = await _run(prepared, _session(prepared))

    assert isinstance(receipt, ClaimedMatrixDraft)
    serialized = receipt.model_dump_json()
    assert PRIVATE_ACCOUNT_MARKER not in serialized
    assert PRIVATE_CLIENT_MARKER not in serialized
    findings, errors = scan_secret_text("source-matrix.json", serialized)
    assert findings == []
    assert errors == []
