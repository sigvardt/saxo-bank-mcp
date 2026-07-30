# Saxo Analytics and BI Suite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this
> plan task by task. Use in-session Codex agents for implementation support and independent review.
> Do not use Oracle or attempt Grok authentication.

**Goal:** Build the complete on-demand Saxo analytics and BI suite described in
`docs/analytics-bi-vision.md`, expose it through FastMCP for Codex and Claude, and prove every
numeric result, artifact, privacy boundary, and agent workflow against Saxo SIM before any separate
LIVE read validation.

**Architecture:** Saxo OpenAPI remains the only source dataset. Typed provider adapters normalize
SIM responses into an owner-only DuckDB store. Immutable dataset handles feed deterministic,
versioned analytics engines, which produce structured results and local artifacts with provenance.
Analytics have no broker-write authority. A separately requested trade proposal may enter the
existing precheck and approval kernel, but no analytics tool can place, modify, or cancel an order.

**Tech Stack:** Python 3.12, FastMCP 3.4.2, Pydantic 2, HTTPX 2, DuckDB, NumPy, SciPy, Matplotlib,
Plotly, pytest, Hypothesis, Ruff, BasedPyright, the existing Saxo SIM QA harness, Codex and Claude
skill harnesses, and in-session Codex implementation and review agents.

## Global Constraints

- Start from `origin/main` in an isolated worktree. The current local `main` has unrelated source
  edits and must remain untouched.
- Copy this plan and `docs/analytics-bi-vision.md` into the worktree before implementation.
- Use Saxo SIM credentials and SIM endpoints for all network activity in this plan.
- Prove `environment=SIM` immediately before controlled activity. Refuse if the environment is LIVE
  or uncertain.
- SIM actions require no human approval. They still require deterministic cleanup and before and
  after state comparison.
- Never call a LIVE endpoint, place a LIVE order, answer a Saxo disclaimer, or open or focus a
  visible browser during this plan.
- Authentication uses the isolated headless login and cached-session flow.
- Saxo is the only source of market, instrument, options, account, transaction, and cost data.
- Missing Saxo data or entitlements produce a tested `degraded` or `refused` result. They are not
  replaced with external data.
- Collection is on demand inside the MCP call or a bounded MCP-owned job. No background collector
  or scheduler is in scope.
- Private owner-only results may contain balances, holdings, costs, P&L, and money values. Public
  evidence, logs, Codex-agent reports, tests, and agent-evaluation transcripts may contain only safe
  aliases, schemas, hashes, counts, and redacted summaries.
- Retain source history, analyses, and artifacts indefinitely until explicit owner deletion. Never
  auto-delete data to satisfy a quota.
- Disable arbitrary SQL, caller-supplied paths, DuckDB external access, and extension installation.
- Add exactly 21 analytics tools. The complete MCP catalog grows from 39 to 60 logical tools.
- Phase gates are progress checkpoints. They do not complete the task. The implementation loop
  continues through all tasks and all in-scope analysis kinds.
- There is no time-based, token-based, or phase-based early finish. A source limitation is complete
  only when its honest degradation or refusal contract is implemented and proved.
- During development, run only the focused tests named by the current task, Ruff on changed files,
  and BasedPyright on changed modules.
- Use the implementation Codex agent serially for implementation tasks. Do not perform parallel
  work while it runs. At the end of each main area, use a separate Codex review agent with
  the complete area diff and test evidence. Wait directly for each run; do not poll continuously.
- Fix only reproducible safety, correctness, privacy, or agent-usability problems. Record style and
  future-scope suggestions without interrupting the loop.
- Consolidate related fixes before freezing a candidate.
- Final installation, full tests, full SIM matrices, dual-agent evaluations, artifact QA, privacy
  scans, and final Codex review run once per stable candidate. If they find a real blocker,
  consolidate all fixes, create one new candidate, and repeat the final validation once.
- Short progress updates state what is running, what passed or failed, whether source changed, and
  the single next step.

## Fixed Product Contracts

### Request limits

- Synchronous request: at most 25 instruments and 50,000 normalized source rows.
- Bounded job: at most 100 instruments and 5,000,000 normalized source rows.
- Direct structured response: at most 500 rows.
- Concurrent local analytics jobs: at most four.
- Returned artifact: at most 25 MiB; larger exports use an owner-only resource link.
- Local store: configurable, 50 GiB default; refuse new ingestion at the limit without deleting.

### Result states

Production analytics return exactly one of:

- `verified`: every material metric has an active proof profile for the current source and engine.
- `degraded`: the result is useful but has an explicit source, entitlement, approximation, or
  quality limitation.
- `refused`: required data or proof is absent, stale, ambiguous, unsupported, or quarantined.

`unverified` is development-only and cannot be returned as a successful production result.

### Core handles

- `instrument_handle`
- `universe_id`
- `dataset_id`
- `portfolio_snapshot_id`
- `analysis_id`
- `artifact_id`
- `job_id`
- `deletion_preview_token`

Handles are random opaque identifiers. They never encode broker IDs, account keys, file paths, or
private values.

### Analytics tool catalog

1. `saxo_analytics_capabilities`
2. `saxo_resolve_research_universe`
3. `saxo_manage_research_universe`
4. `saxo_sync_research_data`
5. `saxo_get_research_dataset`
6. `saxo_analyze_market`
7. `saxo_analyze_instruments`
8. `saxo_analyze_portfolio`
9. `saxo_size_position`
10. `saxo_run_scenario`
11. `saxo_optimize_portfolio`
12. `saxo_model_derivatives`
13. `saxo_backtest_strategy`
14. `saxo_propose_trade_from_analysis`
15. `saxo_render_analysis`
16. `saxo_export_analysis`
17. `saxo_explain_analysis`
18. `saxo_manage_analysis_job`
19. `saxo_list_analytics_storage`
20. `saxo_preview_analytics_deletion`
21. `saxo_delete_analytics_data`

## Area A: Isolated Baseline and Contracts

### Task 1: Create the isolated implementation worktree

**Files:**

- Copy: `docs/analytics-bi-vision.md`
- Copy: `docs/superpowers/plans/2026-07-30-saxo-analytics-bi-suite.md`
- Verify: `pyproject.toml`
- Verify: `uv.lock`

- [ ] Fetch without changing the current dirty worktree:

  ```bash
  git fetch origin
  git worktree add ../saxo-bank-mcp-analytics -b feat/analytics-bi-suite origin/main
  ```

  Expected: the new worktree starts at `origin/main`; the original worktree remains unchanged.

- [ ] Copy the two planning documents into the new worktree and verify their hashes.

- [ ] Install the locked baseline and run deterministic baseline gates:

  ```bash
  uv sync --locked --all-extras --dev
  uv run pytest
  uv run ruff check .
  uv run basedpyright
  uv run python scripts/validators/validate_plugin.py .
  claude plugin validate --strict .
  ```

  Expected: all baseline gates pass before analytics dependencies or source files are changed.

- [ ] Use one in-session Codex agent for implementation support and a separate in-session Codex
  agent for independent review. Record only task IDs and redacted verdicts.

- [ ] Commit the approved plan and vision:

  ```bash
  git add docs/analytics-bi-vision.md docs/superpowers/plans/2026-07-30-saxo-analytics-bi-suite.md
  git commit -m "docs: plan complete Saxo analytics suite"
  ```

### Task 2: Lock dependencies and resource configuration

**Files:**

- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `src/saxo_bank_mcp/analytics_config.py`
- Create: `tests/test_analytics_config.py`

**Interfaces:**

- `AnalyticsLimits`
- `AnalyticsPaths`
- `load_analytics_config(env: Mapping[str, str]) -> AnalyticsConfig`
- `prepare_owner_only_path(path: Path) -> Path`

- [ ] Write failing tests for every fixed request limit, owner-only `0700` directories, owner-only
  `0600` files, path containment, quota refusal, and invalid environment overrides.

- [ ] Run:

  ```bash
  uv run pytest tests/test_analytics_config.py
  ```

  Expected: fail because the configuration module does not exist.

- [ ] Add bounded dependency ranges for DuckDB, NumPy, SciPy, Matplotlib, and Plotly; add Hypothesis
  to the dev group; resolve and freeze exact versions in `uv.lock`.

- [ ] Implement immutable Pydantic configuration. State paths must resolve under the existing
  owner-only Saxo state directory. Reject symlinks escaping that root.

- [ ] Run the focused tests, Ruff on `analytics_config.py`, and BasedPyright on the new module.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright. Fix only reproducible boundary, permission, or dependency
  problems.

- [ ] Commit:

  ```bash
  git add pyproject.toml uv.lock src/saxo_bank_mcp/analytics_config.py tests/test_analytics_config.py
  git commit -m "feat: define bounded analytics runtime"
  ```

### Task 3: Define the typed analytics contracts

**Files:**

- Create: `src/saxo_bank_mcp/analytics_models.py`
- Create: `src/saxo_bank_mcp/analytics_errors.py`
- Create: `tests/test_analytics_models.py`
- Create: `data/analytics/output_schema_v1.json`

**Interfaces:**

- Enums: `AnalysisStatus`, `MetricClass`, `VisibilityMode`, `QualityState`, `HandleKind`
- Models: `DataCoverage`, `DataQuality`, `MetricValue`, `AnalysisWarning`, `AnalysisProvenance`
- Models: `AnalysisResult`, `DatasetSummary`, `ArtifactSummary`, `JobSummary`
- Models: `AnalyticsRefusal`, `AnalyticsDegradation`
- Functions: `new_safe_handle(kind)`, `validate_public_evidence(result)`

- [ ] Write failing round-trip and schema tests for all enums, discriminated analysis requests,
  value-free refusals, handle opacity, UTC timestamps, explicit currency/unit fields, and
  `verified` proof requirements.

- [ ] Write failing privacy tests proving raw account IDs, client keys, order IDs, URLs, local paths,
  tokens, and `DisplayName` cannot enter public evidence models.

- [ ] Run:

  ```bash
  uv run pytest tests/test_analytics_models.py
  ```

  Expected: fail because the models and frozen schema do not exist.

- [ ] Implement strict Pydantic models with `extra="forbid"`. Material numeric values require unit,
  currency where applicable, metric class, source timestamp, and proof-profile ID.

- [ ] Generate and check in `output_schema_v1.json`; test exact schema stability.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright.

- [ ] Run the Area A review through the separate Codex review agent with the complete contracts and
  configuration diff. Fix reproducible blockers before committing the area.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_models.py src/saxo_bank_mcp/analytics_errors.py \
    tests/test_analytics_models.py data/analytics/output_schema_v1.json
  git commit -m "feat: add typed analytics result contracts"
  ```

## Area B: Owner-Only Data Foundation

### Task 4: Implement the DuckDB store and migrations

**Files:**

- Create: `src/saxo_bank_mcp/analytics_store.py`
- Create: `src/saxo_bank_mcp/analytics_migrations.py`
- Create: `data/analytics/migrations/0001_initial.sql`
- Create: `tests/test_analytics_store.py`
- Create: `tests/fixtures/analytics/store_v0.duckdb`

**Interfaces:**

- `AnalyticsStore.open(config) -> AnalyticsStore`
- `AnalyticsStore.transaction()`
- `AnalyticsStore.put_source_page(...)`
- `AnalyticsStore.create_dataset(...)`
- `AnalyticsStore.create_snapshot(...)`
- `AnalyticsStore.put_analysis(...)`
- `AnalyticsStore.put_artifact(...)`
- `AnalyticsStore.list_storage(scope)`
- `AnalyticsStore.preview_delete(scope)`
- `AnalyticsStore.delete_previewed(token)`
- `migrate_store(path, target_version)`

- [ ] Write failing tests for schema creation, transactional migration, rollback, migration backup,
  one-writer locking, separate read connections, deterministic fingerprints, duplicate-page
  idempotency, revisions, quota refusal, cascade previews, single-use deletion tokens, token expiry,
  and owner-only permissions.

- [ ] Add tests proving DuckDB cannot load extensions, access the network, execute caller SQL, or
  read/write an arbitrary path through the public store API.

- [ ] Run:

  ```bash
  uv run pytest tests/test_analytics_store.py
  ```

  Expected: fail because the store is absent.

- [ ] Implement normalized tables for safe instruments, universes, source pages, source contracts,
  price bars, quotes, option snapshots, account snapshots, transactions, bookings, closed
  positions, costs, datasets, analyses, metrics, artifacts, jobs, proof receipts, and value-free
  deletion receipts.

- [ ] Verify failed migration leaves the previous fixture readable and its fingerprint unchanged.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_store.py src/saxo_bank_mcp/analytics_migrations.py \
    data/analytics/migrations tests/test_analytics_store.py tests/fixtures/analytics/store_v0.duckdb
  git commit -m "feat: add owner-only analytics store"
  ```

### Task 5: Freeze Saxo source contracts and provider behavior

**Files:**

- Create: `src/saxo_bank_mcp/analytics_source_contracts.py`
- Create: `src/saxo_bank_mcp/analytics_provider.py`
- Create: `src/saxo_bank_mcp/analytics_pagination.py`
- Create: `data/analytics/source_contracts.json`
- Create: `tests/test_analytics_source_contracts.py`
- Create: `tests/test_analytics_provider.py`
- Create: `tests/fixtures/analytics/saxo_pages/`

**Interfaces:**

- `SourceContract`
- `SourceField`
- `SourcePage`
- `SaxoAnalyticsProvider.fetch(contract_id, request) -> AsyncIterator[SourcePage]`
- `follow_registered_pagination(first_page, fetch_next)`
- `compare_source_schema(contract, response) -> SchemaComparison`

- [ ] Write failing tests for registered-read-only endpoint selection, absent fields, nulls, unknown
  enums, duplicate pages, repeated next links, page limits, out-of-order rows, revised rows,
  entitlement failures, rate-limit responses, transport ambiguity, and schema drift.

- [ ] Prove pagination treats Saxo `Data` and `__next` structurally and never follows a raw
  caller-supplied URL.

- [ ] Run the two focused test files and confirm failure.

- [ ] Implement provider adapters over the existing registry-gated HTTP client. Source contracts
  cover chart v3, reference instruments, prices/info-prices, options chains, performance,
  balances, positions, orders, transactions, bookings, closed positions, exposure, costs, and
  entitled corporate actions.

- [ ] Add bounded retry only for requests known not to have changed broker state. Preserve unknown
  outcomes and rate-limit reset details without exposing raw payloads.

- [ ] Quarantine dependent analysis kinds on source drift.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_source_contracts.py \
    src/saxo_bank_mcp/analytics_provider.py src/saxo_bank_mcp/analytics_pagination.py \
    data/analytics/source_contracts.json tests/test_analytics_source_contracts.py \
    tests/test_analytics_provider.py tests/fixtures/analytics/saxo_pages
  git commit -m "feat: add Saxo analytics source contracts"
  ```

### Task 6: Execute the Phase 0 SIM data feasibility matrix

**Files:**

- Create: `src/saxo_bank_mcp/qa_analytics_source_matrix.py`
- Create: `scripts/run_analytics_source_matrix.py`
- Create: `tests/test_qa_analytics_source_matrix.py`
- Generate: `.omo/evidence/saxo-bank-mcp/analytics-source-matrix/`

- [ ] Write failing harness tests for environment proof, registered operation lookup, source
  receipts, pagination receipts, entitlement/refusal receipts, controlled-SIM activity, cleanup,
  unchanged-state comparison, `live_events=0`, and secret scanning.

- [ ] Implement a source matrix that calls every source contract through the actual MCP in SIM.
  Capture schemas, counts, timestamps, entitlement state, page behavior, and fingerprints only.

- [ ] If history is absent, prove SIM, create the minimum controlled SIM activity needed, capture
  the source behavior, clean it up, and reconcile account state. Never answer a disclaimer.

- [ ] Run the source matrix once for this source-contract candidate. Record
  `disclaimer_context_unavailable` once if Saxo supplies no text; do not rerun to erase it.

- [ ] Freeze observed source contracts and fixtures. Any contract change after this task requires
  focused source tests, not another full source matrix until the next stable candidate.

- [ ] Run the Area B review through the separate Codex review agent on the complete data-foundation diff
  and redacted source matrix. Its hard task is to identify a field or entitlement condition that
  could make an agent state an incorrect market or portfolio fact.

- [ ] Commit source-contract corrections and harness code, excluding credentials and private
  evidence values.

## Area C: On-Demand Ingestion and Provenance

### Task 7: Implement instrument resolution and saved universes

**Files:**

- Create: `src/saxo_bank_mcp/analytics_resolver.py`
- Create: `src/saxo_bank_mcp/analytics_universes.py`
- Create: `tests/test_analytics_resolver.py`
- Create: `tests/test_analytics_universes.py`

**Interfaces:**

- `resolve_instruments(query, asset_types, exchanges) -> ResolutionResult`
- `create_universe(name, handles) -> UniverseSummary`
- `update_universe(universe_id, additions, removals, expected_revision)`
- `list_universes()`
- `delete_universe(universe_id, expected_revision)`

- [ ] Write failing tests for ambiguous tickers, multiple listings, asset-type collisions, delisted
  instruments, renamed instruments, missing exchange, safe display labels, optimistic concurrency,
  and no silent replacement.

- [ ] Implement resolution through Saxo reference endpoints and safe local handles. Saved universe
  operations are local-only and revision guarded.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_resolver.py \
    src/saxo_bank_mcp/analytics_universes.py tests/test_analytics_resolver.py \
    tests/test_analytics_universes.py
  git commit -m "feat: add safe research universes"
  ```

### Task 8: Implement market, quote, chart, and option ingestion

**Files:**

- Create: `src/saxo_bank_mcp/analytics_sync.py`
- Create: `src/saxo_bank_mcp/analytics_market_data.py`
- Create: `tests/test_analytics_sync.py`
- Create: `tests/test_analytics_market_data.py`

**Interfaces:**

- `sync_research_data(request) -> SyncResult`
- `sync_price_bars(handle, interval, start, end)`
- `capture_quote(handle)`
- `capture_option_chain(handle, expiries)`
- `get_dataset(dataset_id, page, limit) -> DatasetPage`

- [ ] Write failing tests for incremental chart windows, trailing correction refresh, UTC and
  exchange-time conversion, interval boundaries, duplicate bars, gaps, stale/delayed quotes,
  missing volume, price-return labeling, option-chain entitlement, and request limits.

- [ ] Implement on-demand batching with Saxo rate-limit budgeting. Do not send thousands of candles
  to the agent; persist them and return coverage plus a dataset handle.

- [ ] Fingerprint raw pages, normalized rows, source contract, entitlements, and correction state.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_sync.py src/saxo_bank_mcp/analytics_market_data.py \
    tests/test_analytics_sync.py tests/test_analytics_market_data.py
  git commit -m "feat: add on-demand Saxo market ingestion"
  ```

### Task 9: Implement account, transaction, cost, and portfolio ingestion

**Files:**

- Create: `src/saxo_bank_mcp/analytics_account_data.py`
- Create: `src/saxo_bank_mcp/analytics_portfolio_snapshots.py`
- Create: `tests/test_analytics_account_data.py`
- Create: `tests/test_analytics_portfolio_snapshots.py`

**Interfaces:**

- `sync_account_history(scope, start, end) -> SyncResult`
- `capture_portfolio_snapshot(scope) -> PortfolioSnapshot`
- `sync_transactions(scope, start, end)`
- `sync_bookings(scope, start, end)`
- `sync_closed_positions(scope, start, end)`
- `sync_cost_sources(scope, instruments)`

- [ ] Write failing tests for deposits, withdrawals, fees, financing, taxes, dividends, partial
  fills, corrected transactions, duplicates, unsettled cash, account aliases, multi-account
  boundaries, currency conversion timestamps, and absent tax-lot basis.

- [ ] Implement owner-only private-value storage. Public return objects contain handles, coverage,
  safe aliases, row counts, and fingerprints only unless the trusted host requests
  `private_user_result`.

- [ ] Add immutable portfolio snapshots and invalidate dependent analyses after material source
  revisions.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_account_data.py \
    src/saxo_bank_mcp/analytics_portfolio_snapshots.py tests/test_analytics_account_data.py \
    tests/test_analytics_portfolio_snapshots.py
  git commit -m "feat: ingest private Saxo portfolio history"
  ```

### Task 10: Implement definitions, proof profiles, replay, and quarantine

**Files:**

- Create: `src/saxo_bank_mcp/analytics_metric_definitions.py`
- Create: `src/saxo_bank_mcp/analytics_proof_profiles.py`
- Create: `src/saxo_bank_mcp/analytics_provenance.py`
- Create: `data/analytics/metric_definitions.json`
- Create: `data/analytics/proof_profiles.json`
- Create: `tests/test_analytics_proof_profiles.py`
- Create: `tests/test_analytics_provenance.py`

**Interfaces:**

- `MetricDefinition`
- `ProofProfile`
- `ProofRegistry.status(analysis_kind, schema_version, source_contracts)`
- `build_analysis_id(inputs, engine_versions, seed) -> str`
- `replay_analysis(analysis_id) -> AnalysisResult`
- `quarantine_analysis_kind(kind, reason)`

- [ ] Write failing tests for definition versioning, source binding, deterministic IDs, random
  seeds, invalidation, stale proofs, source revisions, engine changes, replay equality, quarantine,
  and refusal when proof is absent.

- [ ] Define units, signs, timing, cash-flow treatment, currency conversion, missing-data behavior,
  exact tolerances, independent reference, and broker reconciliation for every metric named in the
  vision.

- [ ] Generate a coverage matrix that fails when a production metric, analysis kind, artifact
  template, or source field has no active proof profile.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, the generated-catalog check, Ruff, and BasedPyright.

- [ ] Run the Area C review through the separate Codex review agent with the complete ingestion,
  provenance, replay, and quarantine diff. Fix reproducible blockers before committing the area.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_metric_definitions.py \
    src/saxo_bank_mcp/analytics_proof_profiles.py src/saxo_bank_mcp/analytics_provenance.py \
    data/analytics/metric_definitions.json data/analytics/proof_profiles.json \
    tests/test_analytics_proof_profiles.py tests/test_analytics_provenance.py
  git commit -m "feat: add source-bound analytics proof profiles"
  ```

## Area D: Deterministic Analytics Engines

### Task 11: Build the independent numerical foundation

**Files:**

- Create: `src/saxo_bank_mcp/analytics_metrics.py`
- Create: `src/saxo_bank_mcp/analytics_reference_metrics.py`
- Create: `src/saxo_bank_mcp/analytics_cashflows.py`
- Create: `src/saxo_bank_mcp/analytics_fx.py`
- Create: `tests/test_analytics_metrics.py`
- Create: `tests/test_analytics_properties.py`
- Create: `tests/fixtures/analytics/golden_metrics.json`

**Interfaces:**

- Return series: simple, log, cumulative, annualized
- Performance: TWR, MWR/XIRR, CAGR, active return, tracking error, capture ratios
- Risk: volatility, downside deviation, drawdown, Sharpe, Sortino, Calmar, VaR, expected shortfall
- Dependence: covariance, correlation, beta, alpha
- Cash-flow and FX normalization primitives

- [ ] Write known-answer tests before implementation for every formula and edge case.

- [ ] Write Hypothesis invariants for translation, scale, permutation, compounding, monotonicity,
  cash-flow neutrality, drawdown bounds, covariance symmetry, and deterministic seed behavior.

- [ ] Implement a vectorized production path and a structurally independent small-loop reference
  path. Do not share formula helpers between them.

- [ ] Add mutation checks for sign, denominator, annualization, date order, fee omission, FX
  direction, and off-by-one errors. Each seeded mutation must be killed by a named test.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused unit, property, and mutation tests, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_metrics.py \
    src/saxo_bank_mcp/analytics_reference_metrics.py src/saxo_bank_mcp/analytics_cashflows.py \
    src/saxo_bank_mcp/analytics_fx.py tests/test_analytics_metrics.py \
    tests/test_analytics_properties.py tests/fixtures/analytics/golden_metrics.json
  git commit -m "feat: add independently checked financial metrics"
  ```

### Task 12: Implement instrument and bounded-market research

**Files:**

- Create: `src/saxo_bank_mcp/analytics_instruments.py`
- Create: `src/saxo_bank_mcp/analytics_market.py`
- Create: `src/saxo_bank_mcp/analytics_indicators.py`
- Create: `src/saxo_bank_mcp/analytics_fixed_income.py`
- Create: `tests/test_analytics_instruments.py`
- Create: `tests/test_analytics_market.py`
- Create: `tests/test_analytics_indicators.py`
- Create: `tests/test_analytics_fixed_income.py`

**Analysis kinds:**

- Instrument dossier, price return, risk, drawdown, rolling return, trend, momentum, technical
  indicators, liquidity/quote quality, relative comparison, correlation, fixed-income measures.
- Holdings/order/saved-universe movers, breadth, correlation regime, volatility regime, session
  preparation, entitled spread/depth analysis, wrapper comparison, and on-demand saved-condition
  checks.

- [ ] Write failing golden, property, missing-data, stale-quote, delayed-data, unadjusted-price,
  total-return-refusal, bounded-universe, and entitlement tests.

- [ ] Implement analysis over safe handles and datasets. Never describe a bounded universe as the
  whole market. Refuse yield, duration, convexity, carry, or roll-down when Saxo fields are
  insufficient.

- [ ] Cross-check price and indicator outputs against independent fixture calculations.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, proof-coverage generation, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_instruments.py src/saxo_bank_mcp/analytics_market.py \
    src/saxo_bank_mcp/analytics_indicators.py src/saxo_bank_mcp/analytics_fixed_income.py \
    tests/test_analytics_instruments.py tests/test_analytics_market.py \
    tests/test_analytics_indicators.py tests/test_analytics_fixed_income.py
  git commit -m "feat: add bounded Saxo market research"
  ```

### Task 13: Implement portfolio truth and attribution

**Files:**

- Create: `src/saxo_bank_mcp/analytics_portfolio.py`
- Create: `src/saxo_bank_mcp/analytics_attribution.py`
- Create: `src/saxo_bank_mcp/analytics_exposure.py`
- Create: `src/saxo_bank_mcp/analytics_income.py`
- Create: `src/saxo_bank_mcp/analytics_liquidity.py`
- Create: `src/saxo_bank_mcp/analytics_query.py`
- Create: `tests/test_analytics_portfolio.py`
- Create: `tests/test_analytics_attribution.py`
- Create: `tests/test_analytics_exposure.py`
- Create: `tests/test_analytics_income.py`
- Create: `tests/test_analytics_liquidity.py`
- Create: `tests/test_analytics_query.py`

**Analysis kinds:**

- Overview, performance, risk, exposure, attribution, income, liquidity, margin,
  cash-and-settlement, income calendar, tax-lot export, regulatory-cost report, multi-account, full
  tearsheet, portfolio briefing, portfolio time machine, settlement radar, FX drag decomposition,
  entitled corporate-action center, model-disagreement radar, and curated "ask the portfolio"
  intents.

- [ ] Write failing accounting-identity tests: opening value plus flows plus P&L equals closing
  value; contribution sums to total return; allocation sums to total exposure; currency components
  reconcile; component costs sum to total.

- [ ] Add tests for missing benchmark, proxy benchmark disclosure, deposits, withdrawals,
  dividends, corrected bookings, short positions, derivatives, multi-currency accounts, partial
  history, and account alias isolation.

- [ ] Implement private owner-only money outputs and redacted public evidence.

- [ ] Reconcile compatible totals to Saxo performance and exposure fixtures. Every difference must
  have a named reason or refuse verification.

- [ ] Implement a typed portfolio-query intent catalog over approved metrics and filters. Reject
  arbitrary SQL, Python, caller expressions, paths, and network requests.

- [ ] Refuse authoritative tax-lot or corporate-action claims when Saxo does not supply the
  required basis or entitlement.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, proof coverage, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_portfolio.py src/saxo_bank_mcp/analytics_attribution.py \
    src/saxo_bank_mcp/analytics_exposure.py src/saxo_bank_mcp/analytics_income.py \
    src/saxo_bank_mcp/analytics_liquidity.py src/saxo_bank_mcp/analytics_query.py \
    tests/test_analytics_portfolio.py tests/test_analytics_attribution.py \
    tests/test_analytics_exposure.py tests/test_analytics_income.py \
    tests/test_analytics_liquidity.py tests/test_analytics_query.py
  git commit -m "feat: add broker-reconciled portfolio analytics"
  ```

### Task 14: Implement Cost X-ray, trading mirror, and pre-trade impact

**Files:**

- Create: `src/saxo_bank_mcp/analytics_costs.py`
- Create: `src/saxo_bank_mcp/analytics_trade_review.py`
- Create: `src/saxo_bank_mcp/analytics_pretrade.py`
- Modify: `src/saxo_bank_mcp/trade_preview.py`
- Create: `tests/test_analytics_costs.py`
- Create: `tests/test_analytics_trade_review.py`
- Create: `tests/test_analytics_pretrade.py`

**Interfaces:**

- Cost components: commission, spread, FX conversion, financing, borrow, custody, tax, turnover
- Trade review: holding time, winner/loser asymmetry, averaging, do-nothing counterfactual,
  execution-quality evidence class
- Session cockpit and post-session execution report card
- `build_pretrade_impact(analysis_id, proposal) -> PreTradeImpact`

- [ ] Write failing component-sum, fee-sign, currency, partial-fill, corrected-booking, quote
  availability, and bar-approximation tests.

- [ ] Require an MCP-captured decision-point quote for exact arrival, midpoint, or spread claims.
  Otherwise label a bar approximation or refuse the metric.

- [ ] Integrate impact cards into preview without changing approval or execution authority. The
  analytics layer may produce typed preview input only.

- [ ] Cross-check eligible cost estimates with Saxo pre-trade cost illustration.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests including existing trade preview regression tests, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_costs.py src/saxo_bank_mcp/analytics_trade_review.py \
    src/saxo_bank_mcp/analytics_pretrade.py src/saxo_bank_mcp/trade_preview.py \
    tests/test_analytics_costs.py tests/test_analytics_trade_review.py \
    tests/test_analytics_pretrade.py
  git commit -m "feat: add cost and pretrade intelligence"
  ```

### Task 15: Implement sizing, scenarios, and goal models

**Files:**

- Create: `src/saxo_bank_mcp/analytics_position_sizing.py`
- Create: `src/saxo_bank_mcp/analytics_scenarios.py`
- Create: `src/saxo_bank_mcp/analytics_monte_carlo.py`
- Create: `tests/test_analytics_position_sizing.py`
- Create: `tests/test_analytics_scenarios.py`
- Create: `tests/test_analytics_monte_carlo.py`

**Interfaces:**

- Explicit-risk position sizing
- Historical, equity, currency, volatility, rate, margin, and combined shock maps
- Seeded bootstrap Monte Carlo and sequence-of-returns goal models

- [ ] Write failing limiting-case, scale, monotonicity, zero-shock, combined-shock, margin-headroom,
  seed reproducibility, and distribution-label tests.

- [ ] Require explicit numeric shocks. A narrative can be converted to proposed numbers but cannot
  run until the numbers are echoed and accepted by the caller.

- [ ] Label probability results as model distributions, not predictions. Position sizing requires
  user-supplied risk budget and does not choose one.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, proof coverage, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_position_sizing.py \
    src/saxo_bank_mcp/analytics_scenarios.py src/saxo_bank_mcp/analytics_monte_carlo.py \
    tests/test_analytics_position_sizing.py tests/test_analytics_scenarios.py \
    tests/test_analytics_monte_carlo.py
  git commit -m "feat: add bounded decision models"
  ```

### Task 16: Implement portfolio optimization

**Files:**

- Create: `src/saxo_bank_mcp/analytics_optimization.py`
- Create: `src/saxo_bank_mcp/analytics_optimizer_reference.py`
- Create: `tests/test_analytics_optimization.py`
- Create: `tests/fixtures/analytics/golden_optimizations.json`

**Interfaces:**

- Minimum variance
- Risk parity
- Long-only or bounded-short constraints
- Position, asset-class, currency, turnover, cost, margin, minimum-trade, and exclusion constraints

- [ ] Write failing known-answer, feasibility, KKT/residual, constraint, perturbation, concentrated
  covariance, singular covariance, and infeasible-problem tests.

- [ ] Implement with SciPy's constrained solvers behind a typed adapter. Implement an independent
  reference formulation for small golden cases.

- [ ] Return current-to-target deltas and stability diagnostics only. Never create an order or
  imply that the optimizer selected the user's objective or risk tolerance.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, proof coverage, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_optimization.py \
    src/saxo_bank_mcp/analytics_optimizer_reference.py \
    tests/test_analytics_optimization.py tests/fixtures/analytics/golden_optimizations.json
  git commit -m "feat: add constrained portfolio optimization"
  ```

### Task 17: Implement derivatives, options, futures, and FX models

**Files:**

- Create: `src/saxo_bank_mcp/analytics_derivatives.py`
- Create: `src/saxo_bank_mcp/analytics_options.py`
- Create: `src/saxo_bank_mcp/analytics_derivatives_reference.py`
- Create: `tests/test_analytics_derivatives.py`
- Create: `tests/test_analytics_options.py`
- Create: `tests/fixtures/analytics/golden_options.json`

**Interfaces:**

- European Black-Scholes and Black-76 values and Greeks
- Implied-volatility inversion
- Multi-leg payoff and aggregate Greeks
- Smile, skew, term structure, expiry and assignment radar
- Futures basis, carry, term structure, and roll
- FX forward and carry

- [ ] Write failing put-call parity, Greek finite-difference, expiry, zero-volatility, deep
  in/out-of-money, IV inversion, payoff sum, limiting-case, and cross-library golden tests.

- [ ] Compare entitled outputs with Saxo-provided Greeks without averaging disagreements.

- [ ] Refuse unsupported American, path-dependent, rate-complex, or insufficient-source products
  with the exact capability limitation.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, proof coverage, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_derivatives.py src/saxo_bank_mcp/analytics_options.py \
    src/saxo_bank_mcp/analytics_derivatives_reference.py tests/test_analytics_derivatives.py \
    tests/test_analytics_options.py tests/fixtures/analytics/golden_options.json
  git commit -m "feat: add verified derivatives analytics"
  ```

### Task 18: Implement bounded backtests and SIM ghost portfolios

**Files:**

- Create: `src/saxo_bank_mcp/analytics_strategy_schema.py`
- Create: `src/saxo_bank_mcp/analytics_backtest.py`
- Create: `src/saxo_bank_mcp/analytics_ghost_portfolio.py`
- Create: `src/saxo_bank_mcp/analytics_backtest_reference.py`
- Create: `tests/test_analytics_backtest.py`
- Create: `tests/test_analytics_ghost_portfolio.py`

**Interfaces:**

- Declarative entries, exits, indicators, sizing, rebalancing, long/short/cash constraints
- Transaction-cost and slippage models
- Walk-forward and holdout splits
- SIM ghost-portfolio lifecycle and readback

- [ ] Write failing look-ahead, survivorship disclosure, parameter count, cost sensitivity, warm-up,
  rebalance timing, split, delisting, missing-bar, and order-fill-model tests.

- [ ] Implement a bounded vectorized engine plus independent event-loop reference for small fixtures.
  Reject arbitrary Python, expressions, SQL, filesystem paths, and network callbacks.

- [ ] Run ghost portfolios in SIM only. Reconcile every controlled order lifecycle and clean up.
  Backtesting becomes `verified` only after the equivalent ghost workflow passes.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, proof coverage, Ruff, and BasedPyright.

- [ ] Run the Area D review through the separate Codex review agent on all deterministic engines. Hard
  tasks must include: compare 25 instruments over five years without making a market-wide claim;
  explain multi-account P&L and margin without identifiers; diagnose 20 SIM trades; model a
  combined equity/FX shock; detect unstable optimization; detect an option sign error; and find
  look-ahead leakage.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_strategy_schema.py \
    src/saxo_bank_mcp/analytics_backtest.py src/saxo_bank_mcp/analytics_ghost_portfolio.py \
    src/saxo_bank_mcp/analytics_backtest_reference.py tests/test_analytics_backtest.py \
    tests/test_analytics_ghost_portfolio.py
  git commit -m "feat: add bounded strategy research"
  ```

## Area E: Artifacts, Jobs, and Local Controls

### Task 19: Implement deterministic charts and exports

**Files:**

- Create: `src/saxo_bank_mcp/analytics_render.py`
- Create: `src/saxo_bank_mcp/analytics_chart_semantics.py`
- Create: `src/saxo_bank_mcp/analytics_export.py`
- Create: `src/saxo_bank_mcp/analytics_reports.py`
- Create: `tests/test_analytics_render.py`
- Create: `tests/test_analytics_export.py`
- Create: `tests/fixtures/analytics/golden_artifacts/`

**Interfaces:**

- PNG templates for every core chart in the vision
- Self-contained sanitized Plotly HTML
- CSV, Parquet, JSON, HTML, and PDF exports
- Structured chart semantics for agent explanation

- [ ] Write failing tests for data parity, dimensions, blank pixels, clipping, overlapping labels,
  long labels, privacy footer, environment, cutoff, delay, currency, adjustment, warning,
  provenance, and visibility stamps.

- [ ] Render Matplotlib through a forced headless backend. Interactive HTML must contain no external
  URLs, scripts, data fetches, secrets, raw IDs, or private paths.

- [ ] Enforce the 25 MiB return limit and owner-only resource link fallback.

- [ ] Run visual QA at desktop and mobile widths for HTML, pixel checks for PNG, and exact value
  parity against structured results.

- [ ] Use the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, BasedPyright, and visual checks with actual rendered artifacts.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_render.py \
    src/saxo_bank_mcp/analytics_chart_semantics.py src/saxo_bank_mcp/analytics_export.py \
    src/saxo_bank_mcp/analytics_reports.py tests/test_analytics_render.py \
    tests/test_analytics_export.py tests/fixtures/analytics/golden_artifacts
  git commit -m "feat: render source-linked analytics artifacts"
  ```

### Task 20: Implement bounded jobs and explicit deletion controls

**Files:**

- Create: `src/saxo_bank_mcp/analytics_jobs.py`
- Create: `src/saxo_bank_mcp/analytics_storage_tools.py`
- Create: `tests/test_analytics_jobs.py`
- Create: `tests/test_analytics_storage_tools.py`

**Interfaces:**

- `start_job(request)`, `get_job(job_id)`, `cancel_job(job_id)`
- `list_storage(scope)`
- `preview_deletion(scope) -> DeletionPreview`
- `delete_analytics_data(token) -> DeletionReceipt`

- [ ] Write failing tests for four-job concurrency, progress without partial conclusions, restart
  recovery, cancellation, expired jobs, duplicate starts, request fingerprint idempotency, and
  temporary-file cleanup.

- [ ] Write failing deletion tests for scope normalization, dependency closure, bytes/rows preview,
  token expiry, single use, revision mismatch, race conditions, local-only request ledger, and
  value-free audit receipts.

- [ ] Implement in-process bounded jobs that exist only while the MCP runs. Persist job state and
  deterministic inputs so an interrupted job can be safely restarted by explicit request. No
  background daemon is created.

- [ ] Prove all storage tools make zero Saxo network calls and zero broker writes.

- [ ] Resume the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright.

- [ ] Run the Area E review through the separate Codex review agent with actual artifacts, job recovery,
  and deletion evidence. Fix reproducible blockers before committing the area.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/analytics_jobs.py \
    src/saxo_bank_mcp/analytics_storage_tools.py tests/test_analytics_jobs.py \
    tests/test_analytics_storage_tools.py
  git commit -m "feat: add bounded analytics jobs and deletion"
  ```

## Area F: FastMCP and Agent Experience

### Task 21: Expose all 21 analytics tools through FastMCP

**Files:**

- Create: `src/saxo_bank_mcp/mcp_analytics_tools.py`
- Create: `src/saxo_bank_mcp/analytics_tool_descriptions.py`
- Modify: `src/saxo_bank_mcp/server_tool_ids.py`
- Modify: `src/saxo_bank_mcp/server_tool_registration.py`
- Modify: `src/saxo_bank_mcp/tool_annotations.py`
- Modify: `src/saxo_bank_mcp/tool_metadata.py`
- Create: `tests/test_mcp_analytics_tools.py`
- Modify: `tests/test_tool_annotations.py`
- Create: `tests/test_analytics_tool_metadata.py`

- [ ] Write failing registration tests for all 21 exact IDs, unique registration, schema generation,
  annotations, metadata, and `EXPECTED_TOOL_COUNT == 60`.

- [ ] Write agent-usability tests for useful value-free validation errors, ambiguity recovery,
  exact next-tool hints, job transitions, private-result delivery modes, quality warnings, and
  refusal language.

- [ ] Implement thin FastMCP adapters. Adapters validate typed input, call domain services, and map
  known failures to structured results. They must not contain financial formulas.

- [ ] Mark analytics tools read-only with respect to Saxo. Mark universe, job, artifact, and storage
  changes as local-state writes in metadata. Mark
  `saxo_propose_trade_from_analysis` as precheck-only and never broker-executing.

- [ ] Generate the 60-tool catalog and fail CI if IDs, annotations, metadata, descriptions, and
  registrations diverge.

- [ ] Resume the implementation Codex agent for the task and wait. Inspect its changes, then run
  focused tests, Ruff, and BasedPyright.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/mcp_analytics_tools.py \
    src/saxo_bank_mcp/analytics_tool_descriptions.py src/saxo_bank_mcp/server_tool_ids.py \
    src/saxo_bank_mcp/server_tool_registration.py src/saxo_bank_mcp/tool_annotations.py \
    src/saxo_bank_mcp/tool_metadata.py tests/test_mcp_analytics_tools.py \
    tests/test_tool_annotations.py tests/test_analytics_tool_metadata.py
  git commit -m "feat: expose complete analytics MCP catalog"
  ```

### Task 22: Build matched Codex and Claude analytics skills

**Files:**

- Create: `skills/saxo-analytics/SKILL.md`
- Create: `skills/saxo-analytics/agents/openai.yaml`
- Create: `skills/saxo-analytics/references/analytics-workflows.md`
- Create: `skills/saxo-analytics/references/correctness-and-interpretation.md`
- Create: `skills/saxo-analytics/references/privacy-and-storage.md`
- Create: `skills/saxo-analytics/references/research-to-trade.md`
- Modify: `skills/saxo-bank/SKILL.md`
- Modify: `skills/saxo-bank/references/router-contract.md`
- Modify: `scripts/generate_agent_skill_catalogs.py`
- Modify: `scripts/run_dual_harness_skill_evals.py`
- Create: `evals/saxo-analytics/`
- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_saxo_analytics_skill.py`

- [ ] Write failing static and isolated-install tests for both Codex and Claude packaging,
  frontmatter, nested references, official links, tool IDs, permissions, and version parity.

- [ ] Write matched hard-task scenarios for portfolio briefing, Cost X-ray, market comparison,
  scenario, options, optimization, backtest limitations, artifact delivery, deletion, and
  research-to-precheck.

- [ ] Require agents to cite `analysis_id`, report `verified/degraded/refused`, avoid replacement
  math, communicate private values only in owner context, and stop before a broker write.

- [ ] Teach recovery for ambiguity, entitlement gaps, stale data, schema quarantine, large jobs,
  expired handles, and deletion-preview expiry.

- [ ] Generate catalogs and run focused static validators plus one focused local fixture-based dual
  evaluation.

- [ ] Resume the implementation Codex agent for the task and wait. Then run the Area F review
  through the separate Codex review agent on the installed skills and complete FastMCP diff. The reviewer
  must start from an ambiguous user question, discover capabilities, fetch data, calculate,
  render, explain, stop before trade execution, and identify any misleading instruction or missing
  recovery step.

- [ ] Commit:

  ```bash
  git add skills/saxo-analytics skills/saxo-bank scripts/generate_agent_skill_catalogs.py \
    scripts/run_dual_harness_skill_evals.py evals/saxo-analytics .github/workflows/ci.yml \
    tests/test_saxo_analytics_skill.py
  git commit -m "feat: teach Codex and Claude Saxo analytics"
  ```

## Area G: Executable SIM Proof and Stable Candidate

### Task 23: Build the complete analytics SIM and correctness evidence harness

**Files:**

- Create: `src/saxo_bank_mcp/qa_analytics_sim.py`
- Create: `src/saxo_bank_mcp/qa_analytics_evidence.py`
- Create: `src/saxo_bank_mcp/qa_analytics_artifacts.py`
- Modify: `src/saxo_bank_mcp/qa_sim_tool_matrix.py`
- Modify: `src/saxo_bank_mcp/qa_sim_tool_matrix_models.py`
- Modify: `scripts/run_mcp_tool_matrix.py`
- Create: `scripts/run_analytics_proof_matrix.py`
- Create: `tests/test_qa_analytics_sim.py`
- Create: `tests/test_qa_analytics_evidence.py`
- Create: `data/analytics/analysis_kind_catalog.json`

- [ ] Write failing generated-coverage tests proving every source contract, metric definition,
  proof profile, analysis kind, tool, artifact template, skill scenario, and evidence receipt is
  represented exactly once.

- [ ] Expand the actual MCP SIM matrix from 39 to 60 tool receipts. Every analytics tool must have
  success plus applicable degradation, refusal, privacy, timeout, and recovery cases.

- [ ] Build per-analysis proof execution for known answers, properties, metamorphic cases,
  independent reference, mutation kills, numerical tolerances, accounting identities, Saxo
  reconciliation, and artifact parity.

- [ ] Add controlled SIM lifecycle cases for transaction history, execution context, ghost
  portfolios, options where entitled, and cleanup. Capture before and after balances, positions,
  orders, messages, and subscription state as fingerprints and counts.

- [ ] Require `environment=SIM`, `live_events=0`, `live_mutation_calls=0`, complete cleanup, unchanged
  post-cleanup brokerage state, and no secrets or identifiers in publishable evidence.

- [ ] Add an explicit post-send timeout and reconciliation case proving no blind retry.

- [ ] Run only focused harness tests during development. Do not run the full matrix until Task 24
  freezes the candidate.

- [ ] Resume the implementation Codex agent for the task and wait. Then run the Area G harness
  review through the separate Codex review agent over the complete proof design and seeded-fault cases.

- [ ] Commit:

  ```bash
  git add src/saxo_bank_mcp/qa_analytics_sim.py \
    src/saxo_bank_mcp/qa_analytics_evidence.py \
    src/saxo_bank_mcp/qa_analytics_artifacts.py \
    src/saxo_bank_mcp/qa_sim_tool_matrix.py \
    src/saxo_bank_mcp/qa_sim_tool_matrix_models.py scripts/run_mcp_tool_matrix.py \
    scripts/run_analytics_proof_matrix.py tests/test_qa_analytics_sim.py \
    tests/test_qa_analytics_evidence.py data/analytics/analysis_kind_catalog.json
  git commit -m "test: add full-suite Saxo analytics proof"
  ```

### Task 24: Freeze and validate the full stable candidate

**Files:**

- Update only if a real blocker is found: source, tests, skills, catalogs, and proof data above
- Generate: `.omo/evidence/saxo-bank-mcp/analytics-final/`
- Update: `docs/analytics-bi-vision.md`
- Create: `docs/analytics-bi-validation.md`

- [ ] Consolidate all known reproducible blockers from focused tests and both Codex agents before
  creating the candidate.

- [ ] Run focused tests, Ruff, and BasedPyright after the last source change.

- [ ] Commit the stable candidate and record its exact commit SHA. Do not change source while final
  evidence is running.

- [ ] Run the isolated Codex and Claude skill installation check once for that exact commit.

- [ ] Run the complete deterministic suite once:

  ```bash
  uv run pytest
  uv run ruff check .
  uv run basedpyright
  uv run python scripts/validators/validate_plugin.py .
  claude plugin validate --strict .
  uv run python scripts/run_agent_skill_static_gates.py --check
  uv run python scripts/generate_agent_skill_catalogs.py --check
  uv run python scripts/validate_agent_skill_evals.py --all
  ```

- [ ] Run the complete 60-tool MCP SIM matrix once.

- [ ] Run the full per-analysis proof matrix once, including independent references, mutations,
  accounting identities, Saxo reconciliation, artifact value parity, visual integrity, and schema
  drift recovery.

- [ ] Run the matched Codex and Claude hard-task evaluation once against the isolated installed
  skills and actual MCP.

- [ ] Run the post-send timeout and reconciliation test once.

- [ ] Run cleanup once and prove unchanged SIM balances, positions, orders, messages,
  subscriptions, jobs, caches, and temporary files after controlled activity.

- [ ] Run privacy, secret, identifier, private-value, path, URL, raw-payload, and publication scans
  once.

- [ ] Use the separate Codex review agent once for a final review of the exact candidate and
  redacted final evidence. Ask only for reproducible correctness, safety, privacy, or
  agent-usability blockers.

- [ ] If a real blocker is found, preserve failed evidence, consolidate all fixes, create one new
  candidate, and repeat this task once for the new candidate. Do not reopen the loop for style,
  file-size, old-public-content, future LIVE-write, or optional background-scheduling suggestions.

- [ ] Create `docs/analytics-bi-validation.md` with candidate SHA, dependency-lock fingerprint,
  proof-profile coverage, 60-tool SIM result, per-analysis result, artifact QA result, dual-agent
  result, cleanup and unchanged-state proof, privacy result, final Codex verdict, external Saxo
  limitations, `live_events=0`, `live_mutation_calls=0`, and `purchase_occurred=false`.

- [ ] Commit only the final validation document and safe evidence indexes. Push the feature branch
  and open a reviewable pull request. Do not merge without the user's explicit instruction.

## Full-Suite Completion

The implementation loop is complete only when all 24 tasks are checked and:

- Every one of the 60 MCP tools passes the actual SIM matrix.
- Every in-scope `analysis_kind` has a current source-bound proof profile.
- All material metrics pass known-answer, property, metamorphic, mutation, numerical, independent
  reference, and applicable Saxo reconciliation checks.
- All accounting identities pass.
- All artifacts contain the same verified values as structured output and pass visual QA.
- Codex and Claude complete the hard workflows correctly with the installed skills and actual MCP.
- Missing Saxo data and entitlements produce the exact proved degradation or refusal.
- Cleanup succeeds and SIM account state is unchanged after controlled activity.
- Public evidence contains no credentials, identifiers, private values, paths, raw URLs, or raw
  broker payloads.
- The final independent Codex review finds no reproducible blocker.
- No LIVE endpoint was called, no LIVE mutation occurred, and no purchase occurred.

After this complete SIM result, LIVE read-only validation is a separate user-started phase. It is
not part of this plan and cannot be initiated implicitly.
