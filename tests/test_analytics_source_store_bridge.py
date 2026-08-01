from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx2
import pytest
from pydantic import ValidationError

import saxo_bank_mcp.analytics_store as store_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_models import HandleKind, QualityState, new_safe_handle
from saxo_bank_mcp.analytics_provider import SaxoAnalyticsProvider
from saxo_bank_mcp.analytics_source_contracts import (
    SourceCaptureEnvelope,
    SourceJsonValue,
    SourcePage,
    build_source_capture_context,
    build_source_capture_envelope,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    StoreValidationError,
)
from saxo_bank_mcp.endpoint_registry import EndpointOperation

_FIXTURE_ROOT = Path(__file__).parent / "fixtures/analytics/saxo_pages"
_CAPTURED_AT = datetime(2026, 7, 30, 12, tzinfo=UTC)
_EXPECTED_NATIVE_REVISION_COUNT = 2
_EXPECTED_PAGE_COUNT = 3


class _Executor:
    def __init__(self, payloads: list[bytes]) -> None:
        self.payloads = payloads

    async def __call__(
        self,
        operation: EndpointOperation,
        request_target: str,
        params: Mapping[str, str],
    ) -> httpx2.Response:
        _ = operation, request_target, params
        return httpx2.Response(
            200,
            content=self.payloads.pop(0),
            request=httpx2.Request("GET", "https://registered.invalid"),
        )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path) -> AnalyticsConfig:
    return load_analytics_config(
        {
            "XDG_STATE_HOME": str(tmp_path / "state"),
            "SAXO_MCP_ANALYTICS_STORE_QUOTA_GIB": "1",
        },
    )


def test_store_bridge_supports_every_frozen_provider_source_kind() -> None:
    assert {
        contract.source_kind for contract in source_contracts_by_id().values()
    } <= store_module.supported_source_kinds()


async def _provider_capture() -> SourceCaptureEnvelope:
    requests: dict[str, Mapping[str, object]] = {
        "chart_v3": {"AssetType": "Stock", "Count": 2, "Uic": 1001},
        "transactions_v1": {},
    }
    capture = build_source_capture_context(requests, captured_at=_CAPTURED_AT)
    provider = SaxoAnalyticsProvider(
        request_executor=_Executor(
            [
                (_FIXTURE_ROOT / "chart_page_1.json").read_bytes(),
                (_FIXTURE_ROOT / "chart_page_2.json").read_bytes(),
                (_FIXTURE_ROOT / "transactions_out_of_order.json").read_bytes(),
            ],
        ),
    )
    pages: list[SourcePage] = []
    for contract_id, request in requests.items():
        pages.extend(
            [
                page
                async for page in provider.fetch(
                    contract_id,
                    request,
                    capture=capture,
                )
            ],
        )
    return build_source_capture_envelope(capture, pages)


async def _quote_capture(price_type_bid: str) -> SourceCaptureEnvelope:
    request = {"AssetType": "Stock", "Uic": 1001}
    capture = build_source_capture_context(
        {"info_price_v1": request},
        captured_at=_CAPTURED_AT,
    )
    payload = {
        "AssetType": "Stock",
        "PriceTypeAsk": price_type_bid,
        "PriceTypeBid": price_type_bid,
        "Quote": {
            "Ask": 101.2,
            "Bid": 101.0,
            "DelayedByMinutes": 0,
            "Mid": 101.1,
            "PriceType": "Realtime",
        },
        "Uic": 1001,
    }
    provider = SaxoAnalyticsProvider(
        request_executor=_Executor([json.dumps(payload).encode()]),
    )
    pages = [
        page
        async for page in provider.fetch(
            "info_price_v1",
            request,
            capture=capture,
        )
    ]
    return build_source_capture_envelope(capture, pages)


def _direct_info_price_page(
    store: AnalyticsStore,
    *,
    page_key: str,
    price_type: str,
    source_quality: dict[str, object] | None,
    contract_identity: tuple[str, str] | None = None,
) -> store_module.StoredSourcePage:
    contract = source_contracts_by_id()["info_price_v1"]
    contract_name, contract_sha256 = contract_identity or (
        contract.contract_id,
        source_contract_fingerprint(contract),
    )
    payload: dict[str, object] = {
        "rows": [
            {
                "AssetType": "Stock",
                "PriceTypeAsk": price_type,
                "PriceTypeBid": price_type,
                "Quote": {
                    "Ask": 101.2,
                    "Bid": 101.0,
                    "DelayedByMinutes": 0,
                    "Mid": 101.1,
                    "PriceType": "Realtime",
                },
                "Uic": 1001,
            },
        ],
    }
    if source_quality is not None:
        payload["source_quality"] = source_quality
    return store.put_source_page(
        source_kind=contract.source_kind,
        page_key=page_key,
        source_revision="quote-rev-1",
        contract_name=contract_name,
        contract_sha256=contract_sha256,
        payload=payload,
        row_count=1,
        source_timestamp=_CAPTURED_AT,
        account_scope="aggregate",
        instrument_handle=None,
    )


def _complete_dataset_from_direct_quote(
    store: AnalyticsStore,
    page: store_module.StoredSourcePage,
) -> store_module.StoredDataset:
    return store.create_dataset(
        dataset_id=new_safe_handle(HandleKind.DATASET_ID),
        account_scope="aggregate",
        source_scope="saxo_openapi",
        source_revision="quote-rev-1",
        source_page_ids=(page.page_id,),
        created_at=_CAPTURED_AT,
        coverage_start=_CAPTURED_AT,
        coverage_end=_CAPTURED_AT,
        quality_state=QualityState.COMPLETE,
    )


@pytest.mark.anyio
async def test_provider_capture_is_immutable_and_persisted_exactly_with_replay(
    tmp_path: Path,
) -> None:
    envelope = await _provider_capture()
    pages = envelope.pages
    capture_revisions = {page.capture_revision for page in pages}
    native_revisions = {page.source_revision for page in pages}

    assert len(pages) == _EXPECTED_PAGE_COUNT
    assert len(capture_revisions) == 1
    assert len(native_revisions) == _EXPECTED_NATIVE_REVISION_COUNT
    with pytest.raises(ValidationError):
        pages[0].row_count = 99

    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        first = store.ingest_source_capture(envelope)
        replay = store.ingest_source_capture(envelope)
    finally:
        store.close()

    assert first.dataset.dataset_id == replay.dataset.dataset_id
    assert [page.page_id for page in first.pages] == [page.page_id for page in replay.pages]
    assert first.dataset.source_revision == pages[0].capture_revision
    assert {page.source_native_revision for page in first.pages} == native_revisions
    assert {page.source_kind for page in first.pages} == {"chart", "transactions"}

    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        stored_rows = connection.execute(
            """
            SELECT c.contract_name, p.payload_json
            FROM source_pages AS p
            JOIN source_contracts AS c ON c.contract_id = p.contract_id
            ORDER BY c.contract_name, p.page_key
            """,
        ).fetchall()
    finally:
        connection.close()
    expected_rows = {
        (page.contract_id, page.page_number): page.model_dump(mode="json")["rows"] for page in pages
    }
    actual_rows = {
        (str(contract_id), int(json.loads(str(payload))["page_number"])): json.loads(
            str(payload),
        )["rows"]
        for contract_id, payload in stored_rows
    }
    assert actual_rows == expected_rows


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("price_type_bid", "expected_quality_state"),
    [("NoAccess", QualityState.PARTIAL), ("Realtime", QualityState.COMPLETE)],
)
async def test_quote_quality_is_persisted_and_derives_replay_safe_dataset_quality(
    tmp_path: Path,
    price_type_bid: str,
    expected_quality_state: QualityState,
) -> None:
    envelope = await _quote_capture(price_type_bid)
    config = _config(tmp_path)
    store = AnalyticsStore.open(config)
    try:
        first = store.ingest_source_capture(envelope)
        replay = store.ingest_source_capture(envelope)
    finally:
        store.close()

    assert first.dataset.dataset_id == replay.dataset.dataset_id
    assert first.dataset.quality_state is expected_quality_state
    assert replay.dataset.quality_state is expected_quality_state
    connection = duckdb.connect(str(config.paths.store_path), read_only=True)
    try:
        stored_payload = connection.execute(
            "SELECT payload_json FROM source_pages WHERE page_id = ?",
            (first.pages[0].page_id,),
        ).fetchone()
    finally:
        connection.close()
    assert stored_payload is not None
    persisted = json.loads(str(stored_payload[0]))
    assert persisted["source_quality"] == envelope.pages[0].model_dump(mode="json")[
        "source_quality"
    ]


@pytest.mark.anyio
async def test_create_dataset_cannot_upgrade_persisted_limited_quote_quality(
    tmp_path: Path,
) -> None:
    envelope = await _quote_capture("NoAccess")
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        capture = store.ingest_source_capture(envelope)
        upgraded = store.create_dataset(
            dataset_id=new_safe_handle(HandleKind.DATASET_ID),
            account_scope=envelope.capture.account_scope,
            source_scope="saxo_openapi",
            source_revision=envelope.capture.capture_revision,
            source_page_ids=tuple(page.page_id for page in capture.pages),
            created_at=_CAPTURED_AT,
            coverage_start=_CAPTURED_AT,
            coverage_end=_CAPTURED_AT,
            quality_state=QualityState.COMPLETE,
        )
    finally:
        store.close()

    assert upgraded.quality_state is QualityState.PARTIAL


def test_direct_quote_page_without_quality_proof_cannot_create_dataset(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _direct_info_price_page(
            store,
            page_key="direct-info-price-omitted-quality",
            price_type="NoAccess",
            source_quality=None,
        )

        with pytest.raises(StoreValidationError):
            _complete_dataset_from_direct_quote(store, page)
    finally:
        store.close()


def test_info_price_contract_hash_cannot_hide_behind_alias(tmp_path: Path) -> None:
    contract = source_contracts_by_id()["info_price_v1"]
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _direct_info_price_page(
            store,
            page_key="direct-info-price-aliased-contract",
            price_type="NoAccess",
            source_quality=None,
            contract_identity=(
                "info_price_alias_v1",
                source_contract_fingerprint(contract),
            ),
        )

        with pytest.raises(
            StoreValidationError,
            match="persisted source contract metadata is invalid",
        ):
            _complete_dataset_from_direct_quote(store, page)
    finally:
        store.close()


def test_info_price_contract_name_cannot_bind_wrong_hash(tmp_path: Path) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _direct_info_price_page(
            store,
            page_key="direct-info-price-wrong-contract-hash",
            price_type="NoAccess",
            source_quality=None,
            contract_identity=("info_price_v1", "f" * 64),
        )

        with pytest.raises(
            StoreValidationError,
            match="persisted source contract metadata is invalid",
        ):
            _complete_dataset_from_direct_quote(store, page)
    finally:
        store.close()


def test_direct_quote_page_with_forged_complete_proof_cannot_create_dataset(
    tmp_path: Path,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _direct_info_price_page(
            store,
            page_key="direct-info-price-forged-quality",
            price_type="NoAccess",
            source_quality={
                "state": "complete",
                "entitlement_limited_fields": [],
                "delayed_fields": [],
                "missing_fields": [],
            },
        )

        with pytest.raises(StoreValidationError):
            _complete_dataset_from_direct_quote(store, page)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("price_type", "source_quality", "expected_quality"),
    [
        (
            "Realtime",
            {
                "state": "complete",
                "entitlement_limited_fields": [],
                "delayed_fields": [],
                "missing_fields": [],
            },
            QualityState.COMPLETE,
        ),
        (
            "NoAccess",
            {
                "state": "limited",
                "entitlement_limited_fields": ["PriceTypeAsk", "PriceTypeBid"],
                "delayed_fields": [],
                "missing_fields": [],
            },
            QualityState.PARTIAL,
        ),
    ],
)
def test_direct_quote_page_quality_must_match_exact_rows(
    tmp_path: Path,
    price_type: str,
    source_quality: dict[str, object],
    expected_quality: QualityState,
) -> None:
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        page = _direct_info_price_page(
            store,
            page_key=f"direct-info-price-valid-{price_type.casefold()}",
            price_type=price_type,
            source_quality=source_quality,
        )
        dataset = _complete_dataset_from_direct_quote(store, page)
    finally:
        store.close()

    assert dataset.quality_state is expected_quality


@pytest.mark.anyio
@pytest.mark.parametrize(
    "update",
    [
        {"capture_revision": "capture:" + "f" * 32},
        {"account_scope": "selected SIM account"},
        {"instrument_scope_sha256": "f" * 64},
        {"source_kind": "orders"},
        {"page_fingerprint_sha256": "f" * 64},
    ],
)
async def test_store_bridge_refuses_mixed_or_tampered_pages(
    tmp_path: Path,
    update: dict[str, SourceJsonValue],
) -> None:
    envelope = await _provider_capture()
    pages = list(envelope.pages)
    pages[-1] = pages[-1].model_copy(update=update)
    tampered = envelope.model_copy(update={"pages": tuple(pages)})
    store = AnalyticsStore.open(_config(tmp_path))
    try:
        with pytest.raises(StoreValidationError, match="capture envelope"):
            store.ingest_source_capture(tampered)
    finally:
        store.close()
