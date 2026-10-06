"""Shared authenticated inputs and typed outputs for production analysis recipes."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel

from saxo_bank_mcp.analytics_config import AnalyticsConfig
from saxo_bank_mcp.analytics_instrument_identity import instrument_handle_for_saxo_identity
from saxo_bank_mcp.analytics_instruments import PriceSeriesDataset, QuoteResearchDataset
from saxo_bank_mcp.analytics_models import (
    AnalysisDependency,
    AnalysisResult,
    AnalysisTable,
    MetricClass,
    NamedModelAssumption,
    QualityState,
)
from saxo_bank_mcp.analytics_portfolio import SaxoSourceBinding
from saxo_bank_mcp.analytics_proof_profiles import ProofRegistry
from saxo_bank_mcp.analytics_provenance import replay_analysis
from saxo_bank_mcp.analytics_resolver import InstrumentState, ResolvedInstrument
from saxo_bank_mcp.analytics_source_contracts import (
    SourceJsonValue,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import (
    AnalyticsStore,
    AuthenticatedDatasetMaterial,
    AuthenticatedSourceMaterial,
)
from saxo_bank_mcp.analytics_sync import (
    DatasetRow,
    PriceBarDatasetRow,
    QuoteDatasetRow,
    get_dataset,
)


class AnalyticsExecutionError(RuntimeError):
    """A value-free, specific source or request limitation."""

    def __init__(self, reason_code: str, missing_fields: Sequence[str] = ()) -> None:
        """Retain only stable error codes and missing field labels."""
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.missing_fields = tuple(missing_fields)


@dataclass(frozen=True, slots=True)
class RecipeRequest:
    """Internal projection of a validated public MCP request; never accepts source facts."""

    analysis_kind: str
    dataset_ids: tuple[str, ...]
    instrument_handles: tuple[str, ...] = ()
    arguments: Mapping[str, object] = field(default_factory=dict[str, object])

    @classmethod
    def from_model(cls, request: BaseModel) -> RecipeRequest:
        arguments = request.model_dump(mode="python")
        dataset_ids = arguments.get("dataset_ids")
        if dataset_ids is None:
            dataset_ids = (arguments["dataset_id"],)
        return cls(
            analysis_kind=cast("str", arguments["analysis_kind"]),
            dataset_ids=(
                *tuple(cast("Sequence[str]", dataset_ids)),
                *tuple(cast("Sequence[str]", arguments.get("supporting_dataset_ids", ()))),
            ),
            instrument_handles=tuple(
                cast("Sequence[str]", arguments.get("instrument_handles", ())),
            ),
            arguments=arguments,
        )


@dataclass(frozen=True, slots=True)
class RecipeMetric:
    metric_id: str
    value: float
    currency: str | None = None
    metric_class: MetricClass | None = None


@dataclass(frozen=True, slots=True)
class RecipePayload:
    """Domain results; the runtime alone adds canonical proof and storage."""

    metrics: tuple[RecipeMetric, ...] = ()
    tables: tuple[AnalysisTable, ...] = ()
    warnings: tuple[str, ...] = ()
    unavailable_fields: tuple[str, ...] = ()
    assumptions: tuple[NamedModelAssumption, ...] = ()
    verifies: tuple[str, ...] = ()
    does_not_verify: tuple[str, ...] = ("Future returns.",)
    model_distribution_only: bool = False
    is_not_forecast: bool = True


@dataclass(slots=True)
class ResearchInputs:
    """Already authenticated immutable datasets; calculations have no network client."""

    config: AnalyticsConfig
    store: AnalyticsStore
    materials: tuple[AuthenticatedDatasetMaterial, ...]
    _rows: dict[str, tuple[DatasetRow, ...]] = field(default_factory=dict)
    _supporting_pages: dict[str, AuthenticatedSourceMaterial] = field(default_factory=dict)
    _dependency_pages: dict[str, AuthenticatedSourceMaterial] = field(default_factory=dict)
    _analysis_dependencies: dict[str, AnalysisDependency] = field(default_factory=dict)
    cancellation_check: Callable[[], None] | None = None
    progress: Callable[[int, int], None] | None = None
    proof_registry: ProofRegistry | None = None

    def check_cancellation(self) -> None:
        """Allow a process-owned job to stop at bounded calculation boundaries."""
        if self.cancellation_check is not None:
            self.cancellation_check()

    def report_work(self, completed: int, total: int) -> None:
        """Report safe work counters and check cancellation at bounded loop boundaries."""
        self.check_cancellation()
        if self.progress is not None:
            self.progress(completed, total)

    def analysis(self, analysis_id: str) -> AnalysisResult:
        """Authenticate an exact prior model through the same proof-aware replay owner."""
        if self.proof_registry is None:
            raise AnalyticsExecutionError("analysis_proof_context_required")
        result = replay_analysis(analysis_id, config=self.config, registry=self.proof_registry)
        material = self.store.get_authenticated_dataset_material(result.provenance.dataset_id)
        if material.account_scope not in {"aggregate", self.account_scope}:
            raise AnalyticsExecutionError("analysis_input_account_scope_mismatch")
        for page in material.pages:
            self._dependency_pages[page.page_id] = page
        self._analysis_dependencies[analysis_id] = AnalysisDependency(
            analysis_id=analysis_id,
            result_sha256=hashlib.sha256(
                json.dumps(
                    result.model_dump(mode="json"),
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
            ).hexdigest(),
        )
        return result

    @property
    def analysis_dependencies(self) -> tuple[AnalysisDependency, ...]:
        """Expose exact prior-result fingerprints after all source resolution."""
        return tuple(
            self._analysis_dependencies[key] for key in sorted(self._analysis_dependencies)
        )

    @property
    def pages(self) -> tuple[AuthenticatedSourceMaterial, ...]:
        """All pages pinned for replay, including exact prior-model inputs."""
        by_id = {page.page_id: page for page in self.calculation_pages}
        by_id.update(self._dependency_pages)
        return tuple(by_id.values())

    @property
    def calculation_pages(self) -> tuple[AuthenticatedSourceMaterial, ...]:
        """Current inputs and resolved metadata, without importing a prior model's facts."""
        by_id = {page.page_id: page for material in self.materials for page in material.pages}
        by_id.update(self._supporting_pages)
        return tuple(by_id.values())

    @property
    def as_of(self) -> datetime:
        return max(material.dataset.created_at for material in self.materials)

    @property
    def account_scope(self) -> str:
        scopes = {material.account_scope for material in self.materials} - {"aggregate"}
        if len(scopes) > 1:
            raise AnalyticsExecutionError("analysis_input_account_scope_mismatch")
        return next(iter(scopes), "aggregate")

    def source_rows(self, contract_id: str) -> tuple[Mapping[str, SourceJsonValue], ...]:
        rows: list[Mapping[str, SourceJsonValue]] = []
        for page in self.calculation_pages:
            if page.contract_name != contract_id:
                continue
            values = page.payload.get("rows")
            if isinstance(values, (list, tuple)):
                rows.extend(value for value in values if isinstance(value, Mapping))
        return tuple(rows)

    @property
    def has_supporting_pages(self) -> bool:
        """Whether a recipe resolved additional authenticated metadata."""
        return bool(self._supporting_pages or self._dependency_pages)

    def bindings(self) -> tuple[SaxoSourceBinding, ...]:
        """Bind every immutable capture, including independent capture revisions."""
        return source_bindings_from_pages(self.calculation_pages)

    def metadata(self, material: AuthenticatedDatasetMaterial) -> Mapping[str, SourceJsonValue]:
        for page in material.pages:
            if page.source_revision == material.dataset.source_revision:
                metadata = page.payload.get("sync_metadata")
                if isinstance(metadata, Mapping):
                    return metadata
        return {}

    def dataset_rows(self, material: AuthenticatedDatasetMaterial) -> tuple[DatasetRow, ...]:
        dataset_id = material.dataset.dataset_id
        if dataset_id in self._rows:
            return self._rows[dataset_id]
        if self.metadata(material).get("data_kind") not in {"price_bars", "quote", "option_chain"}:
            self._rows[dataset_id] = ()
            return ()
        rows: list[DatasetRow] = []
        page_number = 1
        while True:
            page = get_dataset(
                dataset_id, page_number, self.config.limits.response_rows, config=self.config
            )
            if page.total_rows > self.config.limits.sync_rows:
                raise AnalyticsExecutionError("analysis_row_limit_exceeded")
            rows.extend(page.rows)
            if page.next_page is None:
                break
            page_number = page.next_page
        self._rows[dataset_id] = tuple(rows)
        return self._rows[dataset_id]

    def all_rows(self) -> tuple[DatasetRow, ...]:
        return tuple(row for material in self.materials for row in self.dataset_rows(material))

    def series(self, handle: str) -> PriceSeriesDataset:
        matches: list[tuple[AuthenticatedDatasetMaterial, tuple[PriceBarDatasetRow, ...]]] = []
        for material in self.materials:
            rows = tuple(
                row
                for row in self.dataset_rows(material)
                if isinstance(row, PriceBarDatasetRow) and row.instrument_handle == handle
            )
            if rows:
                matches.append((material, rows))
        if len(matches) != 1:
            raise AnalyticsExecutionError("price_series_missing_or_ambiguous")
        material, rows = matches[0]
        metadata = self.metadata(material)
        missing = metadata.get("missing_interval_count", 0)
        warnings = metadata.get("warnings", ())
        return PriceSeriesDataset(
            dataset_id=material.dataset.dataset_id,
            instrument_handle=handle,
            bars=rows,
            quality_state=material.dataset.quality_state,
            missing_interval_count=cast("int", missing),
            return_series_label="price_return",
            adjustment_status="unadjusted",
            warnings=tuple(cast("Sequence[str]", warnings)),
        )

    def quote(self, handle: str) -> QuoteResearchDataset:
        matches: list[tuple[AuthenticatedDatasetMaterial, QuoteDatasetRow]] = []
        for material in self.materials:
            matches.extend(
                (material, row)
                for row in self.dataset_rows(material)
                if isinstance(row, QuoteDatasetRow) and row.instrument_handle == handle
            )
        if len(matches) != 1:
            raise AnalyticsExecutionError("quote_missing_or_ambiguous")
        material, row = matches[0]
        metadata = self.metadata(material)
        prices = tuple(
            row
            for page in material.pages
            if page.contract_name == "info_price_v1"
            for row in cast("Sequence[SourceJsonValue]", page.payload.get("rows", ()))
            if isinstance(row, Mapping)
        )
        price_type: str | None = None
        for price in prices:
            identity = price.get("Uic")
            asset_type = price.get("AssetType")
            if not isinstance(identity, int) or not isinstance(asset_type, str):
                continue
            if instrument_handle_for_saxo_identity(asset_type, identity) != handle:
                continue
            quote = price.get("Quote")
            candidate = price.get("PriceTypeBid")
            if candidate is None and isinstance(quote, Mapping):
                candidate = quote.get("PriceType") or quote.get("PriceTypeBid")
            if isinstance(candidate, str):
                price_type = candidate
                break
        entitlement = metadata.get("entitlement_state")
        delay = metadata.get("delayed_by_minutes")
        return QuoteResearchDataset(
            dataset_id=material.dataset.dataset_id,
            instrument_handle=handle,
            quote=row,
            quality_state=material.dataset.quality_state,
            entitlement_state="available" if entitlement in {"available", "delayed"} else "denied",
            delayed_by_minutes=cast("int | None", delay),
            price_type=price_type,
            warnings=tuple(cast("Sequence[str]", metadata.get("warnings", ()))),
        )

    def reference(self, handle: str) -> ResolvedInstrument:
        pages = self.store.find_authenticated_source_materials(
            contract_name="reference_instruments_v1",
            instrument_handle=handle,
        )
        for page in sorted(pages, key=lambda item: item.source_timestamp, reverse=True):
            rows = page.payload.get("rows", ())
            if not isinstance(rows, (list, tuple)):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                identifier = row.get("Identifier")
                asset_type = row.get("AssetType")
                if not isinstance(identifier, int) or not isinstance(asset_type, str):
                    continue
                if instrument_handle_for_saxo_identity(asset_type, identifier) != handle:
                    continue
                self._supporting_pages[page.page_id] = page
                return ResolvedInstrument(
                    instrument_handle=handle,
                    display_label=cast("str", row.get("Description") or "Saxo instrument"),
                    symbol=cast("str | None", row.get("Symbol")),
                    asset_type=asset_type,
                    exchange=cast("str | None", row.get("ExchangeId")),
                    state=InstrumentState.CURRENT,
                )
        raise AnalyticsExecutionError("instrument_reference_unavailable")

    def currency(self) -> str:
        balances = self.source_rows("balances_v1")
        if len(balances) == 1 and isinstance(balances[0].get("Currency"), str):
            return cast("str", balances[0]["Currency"])
        currencies = {
            cast("str", row["CurrencyCode"])
            for contract in ("reference_instruments_v1", "reference_instrument_details_v1")
            for row in self.source_rows(contract)
            if isinstance(row.get("CurrencyCode"), str)
        }
        if len(currencies) == 1:
            return next(iter(currencies))
        return "XXX"


def number(value: object, *, field_name: str) -> float:
    """Require a supplied finite number, without replacing missing source data."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise AnalyticsExecutionError("source_numeric_field_unavailable", (field_name,))
    return float(value)


def utc_timestamp(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise AnalyticsExecutionError("source_timestamp_unavailable", (field_name,))
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AnalyticsExecutionError("source_timestamp_unavailable", (field_name,)) from error
    if parsed.tzinfo is None:
        raise AnalyticsExecutionError("source_timestamp_unavailable", (field_name,))
    return parsed.astimezone(UTC)


def source_bindings(
    materials: Sequence[AuthenticatedDatasetMaterial],
) -> tuple[SaxoSourceBinding, ...]:
    """One canonical binding per contract, pinning all underlying authenticated captures."""
    return source_bindings_from_pages(
        tuple(page for material in materials for page in material.pages)
    )


def source_bindings_from_pages(
    pages: Sequence[AuthenticatedSourceMaterial],
) -> tuple[SaxoSourceBinding, ...]:
    """Canonical contract bindings for an authenticated page set."""
    grouped: dict[str, dict[str, AuthenticatedSourceMaterial]] = {}
    for page in pages:
        grouped.setdefault(page.contract_name, {})[page.page_id] = page
    contracts = source_contracts_by_id()
    bindings: list[SaxoSourceBinding] = []
    for name, contract_pages in sorted(grouped.items()):
        contract = contracts.get(name)
        if contract is None:
            raise AnalyticsExecutionError("source_binding_invalid")
        sha = source_contract_fingerprint(contract)
        if any(page.contract_sha256 != sha for page in contract_pages.values()):
            raise AnalyticsExecutionError("source_contract_changed")
        fingerprints = sorted(page.fingerprint_sha256 for page in contract_pages.values())
        digest = hashlib.sha256(
            json.dumps(fingerprints, separators=(",", ":")).encode()
        ).hexdigest()
        revisions = {page.source_revision for page in contract_pages.values()}
        revision = next(iter(revisions)) if len(revisions) == 1 else f"composite:{digest}"
        quality = QualityState.COMPLETE
        entitlement = "available"
        for page in contract_pages.values():
            source_quality = page.payload.get("source_quality")
            if isinstance(source_quality, Mapping) and source_quality.get("state") == "limited":
                quality = QualityState.PARTIAL
                if source_quality.get("entitlement_limited_fields"):
                    entitlement = "partial"
            metadata = page.payload.get("sync_metadata")
            if isinstance(metadata, Mapping) and metadata.get("entitlement_state") == "denied":
                entitlement = "denied"
        bindings.append(
            SaxoSourceBinding(
                contract_id=name,
                contract_sha256=sha,
                source_revision=revision,
                capture_fingerprint_sha256=fingerprints[0] if len(fingerprints) == 1 else digest,
                quality_state=quality,
                entitlement_state=entitlement,
            )
        )
    return tuple(bindings)
