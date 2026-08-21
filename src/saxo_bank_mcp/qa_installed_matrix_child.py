"""Isolated authority-bearing child for the installed Task 23 SIM matrix."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast

import anyio

import saxo_bank_mcp.mcp_analytics_tools as tools_module
from saxo_bank_mcp.analytics_config import AnalyticsConfig, load_analytics_config
from saxo_bank_mcp.analytics_execution import (
    BacktestExecutionParameters,
    StoredAnalysisExecutionError,
    StoredBacktestExecutionContext,
    _execute_sim_verified_backtest,  # pyright: ignore[reportPrivateUsage]
)
from saxo_bank_mcp.analytics_ghost_portfolio import (
    GhostLifecycleEvidence,
    GhostPortfolioVerification,
    GhostWorkflowRequest,
    _validated_ghost_lifecycle,  # pyright: ignore[reportPrivateUsage]
)
from saxo_bank_mcp.analytics_metric_definitions import (
    MetricDefinitionBinding,
    MetricDefinitionCatalog,
    load_metric_definition_catalog,
)
from saxo_bank_mcp.analytics_models import AnalysisResult, VisibilityMode
from saxo_bank_mcp.analytics_proof_profiles import (
    EngineProofBinding,
    ProfileActivationState,
    ProofProfile,
    ProofProfileCatalog,
    ProofProfileError,
    ProofRegistry,
    SourceContractProofBinding,
    load_proof_profile_catalog,
)
from saxo_bank_mcp.analytics_source_contracts import (
    source_contract_catalog_sha256,
    source_contract_fingerprint,
    source_contracts_by_id,
)
from saxo_bank_mcp.analytics_store import AnalyticsStore
from saxo_bank_mcp.analytics_strategy_schema import strategy_definition_fingerprint
from saxo_bank_mcp.fastmcp_logging_safety import (
    FASTMCP_VALIDATION_SAFETY_TRANSFORM,
    SafeFastMCP,
    install_fastmcp_argument_log_filter,
)
from saxo_bank_mcp.mcp_request_ledger_tools import SAFE_REQUEST_LEDGER_MIDDLEWARE
from saxo_bank_mcp.qa_installed_matrix_envelope import (
    InstalledMatrixEnvelope,
    InstalledMatrixFailureCategory,
    InstalledMatrixFailurePhase,
    build_installed_matrix_failure_envelope,
    matrix_receipt_sha256,
)
from saxo_bank_mcp.qa_sim_tool_matrix import (
    _run_matrix,  # pyright: ignore[reportPrivateUsage]
)
from saxo_bank_mcp.qa_sim_tool_matrix_models import (
    FIXTURE_INSTRUMENT,
    FIXTURE_LIMIT_PRICE,
    FIXTURE_MODIFIED_LIMIT_PRICE,
    FIXTURE_ORDER_AMOUNT,
    FIXTURE_STREAM_UIC,
    MULTILEG_FIXTURE_UICS,
    MatrixFixtures,
    SimToolMatrixReceipt,
)
from saxo_bank_mcp.server_core_tools import SERVICE_NAME
from saxo_bank_mcp.server_tool_registration import register_saxo_tools

_COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}$")
_ANALYSIS_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


async def _cleanup_child_runtime(
    clear_session: Callable[[], None],
    record_phase: Callable[[InstalledMatrixFailurePhase], None],
) -> None:
    """Classify either child runtime cleanup attempt before re-raising safely."""
    try:
        clear_session()
        await tools_module.shutdown_analytics_runtime()
    except Exception:
        record_phase("cleanup")
        raise


def _analytics_config() -> AnalyticsConfig:
    return load_analytics_config(os.environ)


def _process_active_catalog(
    catalog: ProofProfileCatalog,
    *,
    definitions: MetricDefinitionCatalog,
    candidate_commit: str,
    allowed_kinds: frozenset[str],
    source_revisions: dict[str, str],
) -> ProofProfileCatalog:
    profiles = list(catalog.profiles)
    if "bounded_backtest" in allowed_kinds and not any(
        profile.analysis_kind == "bounded_backtest" for profile in profiles
    ):
        metric = definitions.by_id()["total_return"]
        fields: dict[str, set[str]] = {}
        for binding in metric.input_bindings:
            if binding.source_contract_id is not None:
                fields.setdefault(binding.source_contract_id, set()).update(binding.field_paths)
        profiles.append(
            ProofProfile(
                proof_profile_id="vp_bounded_backtest_runtime_v1",
                profile_version="1",
                activation_state=ProfileActivationState.QUARANTINED,
                quarantine_reason="authenticated_ghost_required",
                analysis_kind="bounded_backtest",
                schema_version="1",
                metric_definitions=(
                    MetricDefinitionBinding(
                        metric_id=metric.metric_id,
                        definition_version=metric.definition_version,
                    ),
                ),
                source_contracts=tuple(
                    SourceContractProofBinding(
                        contract_id=contract_id,
                        contract_sha256=source_contract_fingerprint(
                            source_contracts_by_id()[contract_id]
                        ),
                        field_paths=tuple(sorted(field_paths)),
                    )
                    for contract_id, field_paths in sorted(fields.items())
                ),
                source_revision=None,
                engines=(),
                artifact_template_ids=(),
                definition_catalog_sha256=definitions.fingerprint_sha256,
                source_catalog_sha256=source_contract_catalog_sha256(),
                valid_until=None,
            ),
        )
    active_profiles: list[ProofProfile] = []
    for profile in profiles:
        revision = source_revisions.get(profile.analysis_kind)
        if profile.analysis_kind not in allowed_kinds or revision is None:
            active_profiles.append(profile)
            continue
        active_profiles.append(
            profile.model_copy(
                update={
                    "activation_state": ProfileActivationState.ACTIVE,
                    "quarantine_reason": None,
                    "source_revision": revision,
                    "engines": (
                        EngineProofBinding(
                            engine_name="saxo_analytics",
                            engine_version="task23-installed-proof",
                            code_commit=candidate_commit,
                        ),
                    ),
                    "valid_until": datetime.now(UTC) + timedelta(hours=1),
                },
            ),
        )
    return catalog.model_copy(
        update={
            "production_analysis_kinds": tuple(
                dict.fromkeys(
                    (*catalog.production_analysis_kinds, *(p.analysis_kind for p in profiles)),
                ),
            ),
            "production_metric_ids": tuple(
                dict.fromkeys(
                    (
                        *catalog.production_metric_ids,
                        *(
                            binding.metric_id
                            for profile in profiles
                            for binding in profile.metric_definitions
                        ),
                    ),
                ),
            ),
            "profiles": tuple(active_profiles),
        },
    )


async def _run_child_matrix(  # noqa: C901
    candidate_commit: str,
    analysis_kinds: Sequence[str],
    record_phase: Callable[[InstalledMatrixFailurePhase], None],
) -> SimToolMatrixReceipt:
    """Own all live ghost authority inside this process and return only a strict receipt."""
    record_phase("input_validation")
    if _COMMIT_PATTERN.fullmatch(candidate_commit) is None:
        raise ValueError("process proof candidate is invalid")
    if (
        not analysis_kinds
        or any(_ANALYSIS_KIND_PATTERN.fullmatch(kind) is None for kind in analysis_kinds)
        or len(analysis_kinds) != len(set(analysis_kinds))
    ):
        raise ValueError("process proof analysis kinds are invalid")
    if os.environ.get("SAXO_MCP_ENVIRONMENT", "").strip().upper() != "SIM":
        raise ValueError("installed matrix child requires SIM")
    if os.environ.get("SAXO_MCP_LIVE_TOKEN_CACHE_PATH", "").strip():
        raise ValueError("installed matrix child refuses LIVE authority")
    record_phase("runtime_setup")
    kinds = frozenset((*analysis_kinds, "bounded_backtest"))
    session_seal = object()

    class InstalledMatrixSession:
        """Child-local capability destroyed before the serialized receipt leaves."""

        def __init__(self, seal: object) -> None:
            if seal is not session_seal:
                raise ValueError("process proof session construction refused")
            self._active = True
            self._source_revisions: dict[str, str] = {}
            self._backtest_lifecycles: dict[
                tuple[str, str],
                tuple[GhostLifecycleEvidence, str],
            ] = {}

        def clear(self) -> None:
            self._active = False
            self._source_revisions.clear()
            self._backtest_lifecycles.clear()

        def _require_active(self) -> None:
            if not self._active:
                raise ValueError("process proof session is unavailable")

        def candidate_commit(self) -> str:
            self._require_active()
            return candidate_commit

        def proof_registry(
            self,
            config: AnalyticsConfig,
            *,
            analysis_kind: str | None,
            source_revision: str | None,
        ) -> ProofRegistry:
            self._require_active()
            definitions = load_metric_definition_catalog()
            catalog = load_proof_profile_catalog(definitions=definitions)
            if analysis_kind in kinds and source_revision is not None:
                prior = self._source_revisions.setdefault(analysis_kind, source_revision)
                if prior != source_revision:
                    raise ProofProfileError("process proof source revision changed")
            active_catalog = _process_active_catalog(
                catalog,
                definitions=definitions,
                candidate_commit=candidate_commit,
                allowed_kinds=kinds,
                source_revisions=dict(self._source_revisions),
            )
            return ProofRegistry(definitions=definitions, catalog=active_catalog, config=config)

        def controlled_backtest_source_binding(
            self,
            dataset_id: str,
            instrument_handle: str,
            *,
            expected_uic: int,
            expected_asset_type: str,
        ) -> str:
            self.candidate_commit()
            store = AnalyticsStore.open(_analytics_config())
            try:
                material = store.get_authenticated_dataset_material(dataset_id)
                snapshot = store.get_authenticated_snapshot_material(dataset_id, "backtest_input")
                context = StoredBacktestExecutionContext.model_validate(
                    snapshot.payload,
                    strict=False,
                )
            finally:
                store.close()
            identities: set[tuple[object | None, object | None]] = set()
            for page in material.pages:
                if page.contract_name != "reference_instruments_v1":
                    continue
                rows = page.payload.get("rows")
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, Mapping):
                        identities.add(
                            (row.get("AssetType"), row.get("Identifier", row.get("Uic"))),
                        )
            if context.instrument_handle != instrument_handle or identities != {
                (expected_asset_type, expected_uic)
            }:
                raise ValueError("controlled ghost fixture source binding mismatch")
            return material.account_scope

        def record_observed_ghost_lifecycle(
            self,
            evidence: GhostLifecycleEvidence,
            *,
            ledger_provenance_sha256: str,
        ) -> None:
            if self.candidate_commit() != evidence.candidate_commit:
                raise ValueError("process ghost candidate is unavailable")
            store = AnalyticsStore.open(_analytics_config())
            try:
                material = store.get_authenticated_dataset_material(evidence.dataset_id)
                snapshot = store.get_authenticated_snapshot_material(
                    evidence.dataset_id,
                    "backtest_input",
                )
                context = StoredBacktestExecutionContext.model_validate(
                    snapshot.payload,
                    strict=False,
                )
            finally:
                store.close()
            if (
                material.account_scope != evidence.account_alias
                or context.account_alias != evidence.account_alias
                or context.instrument_handle != evidence.instrument_handle
            ):
                raise ValueError("process ghost stored source binding mismatch")
            request = GhostWorkflowRequest(
                candidate_commit=evidence.candidate_commit,
                dataset_id=evidence.dataset_id,
                account_alias=evidence.account_alias,
                instrument_handle=evidence.instrument_handle,
                strategy_fingerprint_sha256=evidence.strategy_fingerprint_sha256,
                fill_model=evidence.fill_model,
                controlled_fixture="task_18_controlled_stock",
            )
            validated = _validated_ghost_lifecycle(request, evidence)
            if not isinstance(validated, GhostPortfolioVerification):
                raise TypeError(validated.reason_code)
            if _SHA256_PATTERN.fullmatch(ledger_provenance_sha256) is None:
                raise ValueError("controlled ghost request ledger is invalid")
            key = (evidence.dataset_id, evidence.strategy_fingerprint_sha256)
            self._backtest_lifecycles[key] = (evidence, ledger_provenance_sha256)

        def execute_backtest(  # noqa: PLR0913
            self,
            *,
            tool_name: str,
            dataset_id: str,
            visibility: VisibilityMode,
            parameters: BacktestExecutionParameters,
            config: AnalyticsConfig,
            store: AnalyticsStore,
            registry: ProofRegistry,
            profile: ProofProfile,
        ) -> AnalysisResult:
            self._require_active()
            strategy_fingerprint = strategy_definition_fingerprint(parameters.strategy)
            observed = self._backtest_lifecycles.get((dataset_id, strategy_fingerprint))
            if observed is None:
                raise StoredAnalysisExecutionError("backtest_sim_proof_unavailable")
            evidence, ledger_provenance_sha256 = observed
            snapshot = store.get_authenticated_snapshot_material(dataset_id, "backtest_input")
            context = StoredBacktestExecutionContext.model_validate(snapshot.payload, strict=False)
            if (
                evidence.candidate_commit != candidate_commit
                or evidence.dataset_id != dataset_id
                or evidence.account_alias != context.account_alias
                or evidence.instrument_handle != parameters.instrument_handle
                or evidence.instrument_handle != context.instrument_handle
                or evidence.strategy_fingerprint_sha256 != strategy_fingerprint
                or evidence.fill_model != parameters.strategy.rebalancing.fill_timing
                or evidence.environment != "SIM"
                or evidence.before != evidence.after
                or not evidence.request_ledger_complete
                or not evidence.request_ledger_read_last
                or evidence.disclaimer_present
                or evidence.purchase_occurred
                or evidence.live_event_count != 0
                or evidence.live_mutation_count != 0
                or _SHA256_PATTERN.fullmatch(ledger_provenance_sha256) is None
            ):
                raise StoredAnalysisExecutionError("backtest_sim_proof_mismatch")
            return _execute_sim_verified_backtest(
                tool_name,
                dataset_id,
                visibility,
                parameters,
                config,
                store,
                registry,
                profile,
            )

    session = InstalledMatrixSession(session_seal)
    tools_state = cast("dict[str, object]", vars(tools_module))
    prior_registry = tools_state["_current_process_proof_registry"]
    prior_backtest = tools_state["_execute_current_proof_backtest"]
    try:
        tools_state["_current_process_proof_registry"] = session.proof_registry
        tools_state["_execute_current_proof_backtest"] = session.execute_backtest

        @asynccontextmanager
        async def installed_lifespan(
            _server: object,
        ) -> AsyncGenerator[dict[str, object]]:
            try:
                yield {"analytics_runtime_owned": True}
            finally:
                await _cleanup_child_runtime(session.clear, record_phase)

        record_phase("server_setup")
        install_fastmcp_argument_log_filter()
        proof_server = SafeFastMCP(
            SERVICE_NAME,
            strict_input_validation=False,
            lifespan=installed_lifespan,
        )
        proof_server.add_transform(FASTMCP_VALIDATION_SAFETY_TRANSFORM)
        proof_server.add_middleware(SAFE_REQUEST_LEDGER_MIDDLEWARE)
        register_saxo_tools(proof_server)
        fixtures = MatrixFixtures(
            stock_uic=FIXTURE_INSTRUMENT,
            amount=float(FIXTURE_ORDER_AMOUNT),
            limit_price=float(FIXTURE_LIMIT_PRICE),
            modified_limit_price=float(FIXTURE_MODIFIED_LIMIT_PRICE),
            option_uics=MULTILEG_FIXTURE_UICS,
            stream_uic=FIXTURE_STREAM_UIC,
        )
        record_phase("matrix_execution")
        return await _run_matrix(  # pyright: ignore[reportPrivateUsage]
            fixtures,
            proof_recorder=session,
            matrix_server=proof_server,
        )
    finally:
        try:
            tools_state["_current_process_proof_registry"] = prior_registry
            tools_state["_execute_current_proof_backtest"] = prior_backtest
            await _cleanup_child_runtime(session.clear, record_phase)
        except Exception:
            record_phase("cleanup")
            raise


def _failure_category(error: Exception) -> InstalledMatrixFailureCategory:
    if isinstance(error, OSError):
        return "io_error"
    if isinstance(error, RuntimeError):
        return "runtime_error"
    if isinstance(error, TypeError):
        return "type_error"
    if isinstance(error, ValueError):
        return "validation_error"
    return "unexpected_error"


def main(argv: Sequence[str] | None = None) -> int:
    """Emit one strict redacted matrix receipt and no authority-bearing object."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--analysis-kind", action="append", default=[])
    failure_phase: InstalledMatrixFailurePhase = "matrix_execution"
    candidate_commit = ""
    analysis_kinds: tuple[str, ...] = ()

    def record_phase(phase: InstalledMatrixFailurePhase) -> None:
        nonlocal failure_phase
        failure_phase = phase

    try:
        arguments = parser.parse_args(argv)
        candidate_commit = str(arguments.candidate)
        analysis_kinds = tuple(str(kind) for kind in arguments.analysis_kind)
        receipt = anyio.run(
            _run_child_matrix,
            candidate_commit,
            analysis_kinds,
            record_phase,
        )
        record_phase("envelope_validation")
        envelope = InstalledMatrixEnvelope(
            candidate_commit=candidate_commit,
            analysis_kinds=analysis_kinds,
            matrix_sha256=matrix_receipt_sha256(receipt),
            matrix=receipt,
        )
    except Exception as error:  # noqa: BLE001 - terminal child boundary normalizes all failures
        if (
            _COMMIT_PATTERN.fullmatch(candidate_commit) is not None
            and analysis_kinds
            and all(_ANALYSIS_KIND_PATTERN.fullmatch(kind) is not None for kind in analysis_kinds)
            and len(analysis_kinds) == len(set(analysis_kinds))
        ):
            failure = build_installed_matrix_failure_envelope(
                candidate_commit=candidate_commit,
                analysis_kinds=analysis_kinds,
                failure_phase=failure_phase,
                failure_category=_failure_category(error),
            )
            sys.stdout.write(failure.model_dump_json())
        return 2
    sys.stdout.write(envelope.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
else:
    del _run_child_matrix
    del _process_active_catalog
