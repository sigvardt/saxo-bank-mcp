from __future__ import annotations

from collections.abc import Callable

import pytest
from live_precheck_test_support import DECODER_LIMIT_PAYLOADS
from pydantic import ValidationError

from saxo_bank_mcp.live_account_refs import parse_live_accounts
from saxo_bank_mcp.live_instrument_refs import parse_live_instrument
from saxo_bank_mcp.live_precheck_response_models import parse_precheck_response
from saxo_bank_mcp.strict_json import StrictJsonError


@pytest.mark.parametrize(
    "content",
    [
        b'{"PreCheckResult":"Rejected","PreCheckResult":"Ok"}',
        b'{"PreCheckResult":"Ok","ErrorInfo":{"ErrorCode":"Rejected"},"ErrorInfo":null}',
        b'{"PreCheckResult":"Ok","Cost":{"ErrorInfo":{"ErrorCode":"Rejected"}}}',
        b'{"PreCheckResult":"Ok","Cost":{"ErrorCode":"Rejected"}}',
        b'{"PreCheckResult":"Ok","Cost":{"Disclaimer":{}}}',
        b'{"PreCheckResult":"Ok","Cost":{"Order_Identifier":"unexpected"}}',
        b'{"PreCheckResult":"Ok","Cost":{"precheck_result":"Rejected"}}',
        b'{"PreCheckResult":"Ok","Cost":{"order_id":"unexpected"}}',
    ],
)
def test_precheck_parser_rejects_duplicate_or_nested_reserved_signals(
    content: bytes,
) -> None:
    with pytest.raises((StrictJsonError, ValidationError)):
        parse_precheck_response(content)


def test_precheck_parser_allows_non_signal_cost_fields() -> None:
    parsed = parse_precheck_response(
        b'{"PreCheckResult":"Ok","Cost":{"Commission":1.5,"StampDuty":0}}',
    )

    assert parsed.precheck_result == "Ok"


@pytest.mark.parametrize("payload_factory", DECODER_LIMIT_PAYLOADS)
def test_precheck_parser_normalizes_decoder_limit_failures(
    payload_factory: Callable[[], bytes],
) -> None:
    with pytest.raises(StrictJsonError, match="invalid_json"):
        parse_precheck_response(payload_factory())


def test_instrument_parser_rejects_duplicate_and_coercible_fields() -> None:
    with pytest.raises(StrictJsonError):
        parse_live_instrument(
            b'{"Uic":999,"Uic":30031,"AssetType":"Stock","IsTradable":true}',
        )
    with pytest.raises(ValidationError):
        parse_live_instrument(
            b'{"Uic":"30031","AssetType":"Stock","IsTradable":"yes"}',
        )


def test_account_parser_rejects_duplicate_members() -> None:
    with pytest.raises(StrictJsonError):
        parse_live_accounts(
            b'{"Data":[],"__count":1,"__count":0}',
        )
