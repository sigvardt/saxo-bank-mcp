# Saxo MCP Analytics and BI Vision

Status: research and product architecture
Date: 2026-07-29
Scope: market research, portfolio analytics, quantitative modeling, visualization, reporting,
and agent-deliverable artifacts
Implementation status: implemented on `feat/analytics-bi-suite`; 21 analytics tools bring the
catalog to 60. Sealed candidate `bd26296` passed its exact Codex-only installation, signed
2,751-test full suite, static, type, catalog, evaluation-manifest, privacy, and SIM authorization
gates. Its one sealed `codex_native_v1` proof then stopped during native registration preflight.
All 11 cases reported `codex_native_registration_invalid`; zero model events ran, while downstream
MCP, Saxo, and broker safety facts remain unknown. Local candidate `1277f48` corrects the confirmed
disposable plugin-path defect and adds strict privacy-safe failure evidence, but it has not passed
exact-candidate or sealed-proof gates. Offline numerical proof, the fresh SIM matrix, Saxo
reconciliation, account equality, and activation did not run. No retry or activation occurred;
all 54 proof profiles remain quarantined. Exact evidence boundaries are recorded in
`docs/analytics-bi-validation.md`.

## Executive conclusion

The current MCP does not provide everything a trader or portfolio owner could want from market
and portfolio research. It provides a strong brokerage access, safety, evidence, and execution
kernel, but it does not yet provide an analytics engine.

The opportunity is substantially larger than adding a few charts. The product can become a
private, auditable research terminal that:

1. Fetches and normalizes Saxo OpenAPI data without pushing large payloads through the model
   context.
2. Computes deterministic, versioned portfolio and market analytics.
3. Produces static images, interactive dashboards, tables, PDFs, and data exports directly through
   MCP.
4. Explains and reproduces every number through a stable analysis identifier.
5. Converts a researched decision into the existing preview and approval flow without giving
   analytics any direct trading authority.

The differentiator is not "AI with charts." It is a single, traceable chain from source data to
calculation to visual explanation to a separately approved brokerage action.

The complete vision uses two data tiers:

- Saxo OpenAPI data stored locally as the sole source dataset.
- Deterministic local models and artifacts built from that data.

The product deliberately stays within the data Saxo provides. When Saxo does not provide enough
data for a requested analysis, the tool reports that limitation instead of fetching from another
provider or presenting an unsupported result.

Before implementation moves beyond read-only feasibility work, two hard gates must be closed:

- Private financial result delivery must not bypass the current LIVE balance privacy policy.
- Price return, total return, adjusted price, and unadjusted price must be distinguished in every
  calculation and chart.

## Signature experiences

The product identity is:

> From a question, to broker-true analysis, to an exact approvable order in one conversation.

The analytics foundation matters, but these are the experiences that should make the product feel
exceptionally smart:

1. **Portfolio briefing — "What changed while I was away?"**
   Explain profit and loss drivers, portfolio changes, margin headroom, working orders, settlement,
   costs, and material Saxo events in one private dashboard. Support an on-demand "since yesterday"
   mode before scheduled jobs exist.
2. **Pre-trade impact card — "What will this trade actually do to me?"**
   Before approval, show the resulting position weight, currency exposure, concentration, estimated
   open and close costs, buying-power or margin impact, and explicit downside scenarios. The card
   becomes part of the existing exact preview; it never approves or places the order.
3. **Trading mirror — "Am I hurting my own returns?"**
   Combine trade timing, holding-time asymmetry, winner and loser behavior, averaging down,
   turnover, the do-nothing counterfactual, and the thesis ledger into one direct review of the
   user's actual decisions.
4. **Portfolio time machine — "What would have happened if...?"**
   Replay the real portfolio as of any stored snapshot and compare it with doing nothing, omitting a
   trade, changing its size, investing cash on a schedule, or using a clearly identified
   Saxo-tradable accumulating ETF as a benchmark proxy.
5. **Cost X-ray — "Where did my money leak away?"**
   Turn bookings, transactions, trading conditions, and cost illustrations into a plain breakdown
   of commissions, spread, currency conversion, financing, custody, borrowing, taxes, and turnover.
6. **Portfolio X-ray — "Explain my whole portfolio."**
   Identify which positions drive returns and risk, hidden currency and instrument concentration,
   cost drag, liquidity or margin pressure, and the effect of explicit market and FX shocks.
7. **Session cockpit and execution report card — "Help me prepare, then grade my trading."**
   Before the session, show the user's instruments, multi-timeframe state, volatility, position
   sizing, spread quality, working orders, and risk. Afterwards, compare fills with arrival price
   and surrounding bars and summarize timing, slippage, and repeated behavior. Exact arrival,
   midpoint, and spread claims require a quote snapshot captured by the MCP at preview or
   submission time; otherwise the report uses a labeled bar approximation or refuses the metric.
8. **Margin fire drill — "What breaks first in a crash?"**
   Apply explicit historical or custom shocks and show which positions consume headroom, the order
   of likely pressure, and which user-selected changes improve the result.
9. **Options position lab and IV memory — "Show the full shape of my options risk."**
   Combine the entitled chain, actual positions, payoff, Greeks, costs, margin, expiry and assignment
   radar, and locally accumulated smile and term-structure history.

The first six experiences serve retail and long-term investors directly. Active traders gain the
most from the pre-trade impact card, trading mirror, execution report card, and research-to-order
flow. Options traders gain a differentiated product only after enough entitled chain snapshots have
accumulated to make IV history useful.

### What this will not try to win

This MCP is not a replacement for a low-latency order ladder or continuously updating intraday
terminal. Chat and request-response tools are best for preparation, analysis, monitoring, and
review, while Saxo's trading interface remains the hot execution surface.

It will also refuse rather than imitate:

- Company fundamentals, earnings analysis, analyst forecasts, filings, and news.
- Market-wide scanners, breadth, and movers outside stored holdings, orders, and explicit Saxo
  instrument lists.
- Reliable sector, industry, country, factor, or fund-holdings look-through when Saxo does not
  supply the classification.
- Total-return claims when the Saxo series does not contain distributions.
- Corporate-action workflows when the application lacks the required Saxo service access.

## Research method

This exploration combined:

- A repository audit at commit `16687dcad46c07095d0ab657e7bf8aaf3bc06425`.
- The checked-in Saxo OpenAPI inventory retrieved on 2026-07-01.
- Current official Saxo OpenAPI documentation.
- Current official documentation from analytics platforms and technical libraries.
- Two headless Claude CLI runs using `claude-fable-5` at `--effort max`: the original exploration
  and a separate product-ambition review.
- The user's full original prompt, passed verbatim to Fable as the controlling brief.

Both Fable runs had repository read tools, no edit tools, and `--no-chrome`. Web tools were
requested but denied inside the first isolated session, so current external documentation was
verified separately in this exploration. Neither Fable run modified the repository.

## Current MCP baseline

The current repository has:

- 39 MCP tools.
- 294 classified Saxo operations.
- 182 implemented operations.
- 112 deliberately refused operations.
- 143 implemented GET/read operations.
- A registry-gated generic read tool.
- OAuth, session persistence, and headless token refresh.
- SIM and LIVE trading flows with exact previews and one-chat LIVE approval.
- Redaction, secret scanning, safe request ledgers, and evidence generation.
- Price and options streaming lifecycle support.

The current dependency set is intentionally small: FastMCP, HTTP, Pydantic, and WebSockets. It has
no analytical database, dataframe, numerical, optimization, financial modeling, or rendering
dependency.

The audit found no implementation for:

- Portfolio performance calculations.
- Benchmark-relative analytics.
- Risk statistics.
- Exposure aggregation.
- Return or risk attribution.
- Correlation or factor models.
- Scenario analysis or stress testing.
- Monte Carlo simulation.
- Portfolio optimization.
- Backtesting.
- Options payoff modeling.
- Technical indicators.
- Chart or dashboard rendering.
- Persistent market-data storage.
- Reproducible research runs.
- Agent-deliverable reports or data exports.

The generic endpoint tool can retrieve redacted Saxo payloads, but this is not an analytics product.
It pushes interpretation and arithmetic into the language model, loses data after the call, and
makes large or paginated datasets expensive and error-prone.

## Saxo-native opportunity

The checked-in inventory and current Saxo documentation show that the raw ingredients are broader
than the current user experience.

### Native historical and performance data

Saxo exposes:

- OHLC chart samples through `/chart/v3/charts`.
- Client and account performance summaries.
- Performance timeseries including account value, P&L, and accumulated time-weighted returns.
- Historical transactions.
- Historical and closed positions.
- Trades, bookings, aggregated amounts, and client reports.

Saxo explicitly describes the performance timeseries as suitable for rendering performance charts,
and recommends field groups to limit payload size:

- [Performance v4 reference](https://www.developer.saxo/openapi/referencedocs/hist/v4/performance)
- [Performance guide](https://www.developer.saxo/openapi/learn/performance)
- [Chart v3 reference](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts)

### Native portfolio and account data

Saxo exposes:

- Positions, net positions, orders, balances, and account values.
- Currency, FX, and instrument exposure endpoints.
- Margin and collateral information.
- Closed-position and transaction history.
- Unsettled amounts.
- Corporate-action events and holdings where the application is licensed.

Corporate Actions is explicitly subject to special licensing and is not generally available to
every OpenAPI application:

- [Saxo service groups](https://www.developer.saxo/openapi/referencedocs)

### Native instrument and price data

Saxo exposes:

- Instrument search and detailed reference data.
- Options spaces, expiry and strike data.
- Futures spaces.
- Trading schedules.
- Info prices and batched info prices.
- Price field groups including quote state, market depth, and Greeks where entitled.
- Per-exchange market-data entitlements.

Instrument lookup is account and entitlement aware, and Saxo identifies it as the starting point
for navigating the available instrument universe:

- [Instrument reference](https://www.developer.saxo/openapi/referencedocs/ref/v1/instruments)
- [Reference-data guide](https://www.developer.saxo/openapi/learn/reference-data)
- [Pricing guide](https://www.developer.saxo/openapi/learn/pricing)
- [Market-data entitlements](https://www.developer.saxo/openapi/referencedocs/port/v1/users/get__port__me_entitlements)

### Native cost and trading-condition data

Saxo exposes:

- Pre-trade cost illustrations.
- Commission, financing, custody, carrying, borrowing, spread, and margin-condition fields where
  applicable.
- Instrument-specific trading limits and schedules.

This enables a cost engine based on the user's real Saxo setup rather than generic fee assumptions:

- [Pre-trade cost reference](https://www.developer.saxo/openapi/referencedocs/cs/v1/tradingconditions-cost)
- [Trading conditions](https://www.developer.saxo/openapi/referencedocs/cs/v1/tradingconditions)

### Native limitations

Saxo-native analytics cannot safely promise:

- Full company fundamentals and standardized financial statements.
- Analyst estimates and consensus revisions.
- Broad news and transcripts.
- Reliable sector, industry, country, and factor look-through for every fund.
- Commercial benchmark and factor datasets.
- Deep total-return history for every instrument.
- Complete historical options volatility surfaces.
- Macroeconomic calendars and long macroeconomic series.
- Market-wide scanning or constituent-complete breadth.
- Real-time prices without the user's exchange entitlements.

The current LIVE account has already demonstrated `NoAccess` for bid and ask on one Nasdaq
instrument. Every analytics result therefore needs entitlement-aware degradation rather than a
blanket claim of real-time coverage.

## Product thesis

Build an analytics operating system around the existing trust kernel.

The system should keep large data and calculations inside the MCP process, while the agent receives
small structured results and direct visual artifacts. Every result should be:

- Source-linked.
- Timestamped.
- Environment-tagged.
- Entitlement-aware.
- Versioned.
- Reproducible.
- Explicit about assumptions.
- Explicit about what it does and does not prove.

The agent orchestrates. The server calculates. The renderer communicates. The existing safety
kernel controls any later action.

## User jobs

### Portfolio owner

- How am I performing, before and after costs, deposits, withdrawals, and FX?
- How does that compare with a suitable benchmark?
- What drove today's, this month's, or this year's P&L?
- Where am I concentrated by instrument, currency, exchange, asset class, long or short exposure,
  and leverage?
- What is my drawdown, volatility, tail risk, and margin vulnerability?
- What changes if I add, reduce, hedge, or rebalance a position?
- How much am I paying in spread, commissions, FX conversion, financing, borrow, custody, and tax?
- What corporate actions, dividends, expiries, elections, and deadlines affect me?
- What did I believe when I entered a position, and did the result match the thesis?

### Active trader

- Show the current setup, multi-timeframe chart, trend, volatility, support, resistance, and volume.
- Compare several Saxo instruments or a saved list without opening multiple websites.
- Calculate position size from risk, stop distance, volatility, and portfolio constraints.
- Show execution quality against arrival price, midpoint, VWAP, or the surrounding bars.
- Detect behavior patterns across winners, losers, holding times, entries, exits, and retries.
- Build and forward-test a strategy without risking money.
- Turn a completed analysis into a precise order preview.
- Forecast settlement, unsettled cash, buying power, and upcoming income or expiry events.
- Analyze spread, depth, imbalance, and quote quality when market-depth entitlements permit.
- Receive a broker-true pre-trade impact card before the exact approval statement.

For day traders, this product focuses on session preparation, position and order consequence,
bounded monitoring, and post-session review. It does not claim to replace Saxo's low-latency
execution interface.

Execution-quality claims use an MCP-captured quote snapshot when available. Orders placed outside
the MCP degrade to a clearly labeled surrounding-bar approximation or omit the metric.

### Options trader

- Explore an option chain by expiry, strike, delta, IV, spread, and liquidity.
- Build a multi-leg strategy and see payoff, Greeks, breakevens, probability ranges, and margin.
- Stress spot, volatility, time decay, rates, and correlations.
- Compare modeled values with Saxo-provided Greeks.
- Track skew, term structure, and surface changes over time.

### Long-term investor or adviser

- Produce a portfolio X-ray and client-ready report.
- Model contributions, withdrawals, goals, and sequence-of-returns risk.
- Compare current and proposed portfolios.
- Compare actual decisions with doing nothing or using a Saxo-tradable benchmark proxy.
- Run historical and hypothetical stress scenarios.
- Explain the portfolio in plain language with every statement linked to a calculation.
- Aggregate several accounts without exposing raw account identifiers to the agent.
- Analyze fixed-income yield, duration, convexity, carry, roll-down, and curve shocks where the
  available instrument data is sufficient.

## Experience goal

A user should be able to ask:

> Show me how my portfolio is doing, what drove the result, where the biggest risks are, and what
> would happen if Nasdaq fell 12% while USD weakened 8% against DKK.

The agent should return:

- A short answer.
- A performance and attribution dashboard.
- A stress-result waterfall.
- A table of the largest risk contributors.
- Data-quality and entitlement warnings.
- An analysis identifier that reproduces every number.
- Optional follow-up actions, none of which execute without the existing trade approval flow.

The user should not need to visit a separate chart site, rebuild an Excel workbook, or trust mental
math performed by a language model.

## Architectural principles

### 1. Handles, not payloads

Large datasets stay server-side. Tools pass:

- `dataset_id`
- `portfolio_snapshot_id`
- `analysis_id`
- `artifact_id`
- `job_id`

An agent should not receive thousands of candles just to request a chart.

### 2. Deterministic computation

Financial calculations run in versioned code. Stochastic models require explicit seeds. Identical
inputs, parameters, engine version, and seed produce identical structured results.

### 3. Local-first rendering

Saxo market data must not be sent to a third-party charting service. Rendering happens locally and
headlessly.

FastMCP 3.4.2 can return MCP `ImageContent`, `EmbeddedResource`, and `ResourceLink` content blocks,
so charts and reports can be delivered directly alongside structured JSON.

### 4. Read and compute are separate from trade authority

Analytics tools never place, modify, or cancel orders. An optimizer may return a proposed target and
order delta, but that proposal must enter the existing precheck and approval path as a new action.

### 5. Private outputs and public evidence are different

Runtime analytics may need private account values to answer the account owner's question. Public QA
evidence must never contain those values.

The analytics layer needs a result-visibility policy:

- `secret`: tokens, credentials, approval factors. Never returned or logged.
- `internal_identifier`: broker keys and raw IDs. Kept behind safe handles.
- `private_financial`: balances, P&L, holdings, and account values. Returned only in a private user
  result under an explicit policy; evidence stores fingerprints and schemas only.
- `market_data`: returned and rendered according to Saxo entitlements and the user's delivery
  policy.
- `public_reference`: instrument names, symbols, calendars, and model definitions.

For general LIVE reads, the default remains `fingerprint_only`. Authenticated owner-only analytics
may return balances, costs, profit and loss, holdings, and other money values through
`private_user_result`. This mode is enabled only for the trusted local host with a proven LIVE
session. It requires no trading approval because it cannot change the account. Public evidence,
logs, and review bundles still receive only fingerprints, schemas, and redacted summaries.

Host delivery is part of this policy. Local rendering prevents third-party chart services from
receiving data, but an inline chart may still be retained in a cloud chat history. Private
artifacts therefore need one of these explicit delivery modes:

- `inline_private`
- `local_resource_link`
- `redacted_preview`
- `fingerprint_only`

### 6. Data quality is a first-class result

Every analysis must report:

- Coverage start and end.
- Missing observations.
- Staleness.
- Corporate-action adjustment status.
- Quote delay and price type.
- Currency conversion source and timestamp.
- Entitlement limits.
- Model assumptions.
- Cross-check status.

If quality is below the analysis-specific threshold, the tool refuses to calculate instead of
producing a plausible but misleading number.

## Proposed system

```text
Saxo OpenAPI
      |
normalized ingestion
      |
owner-only DuckDB analytics store
      |
immutable datasets and portfolio snapshots
      |
deterministic, versioned computation engines
      |
analysis result and provenance object store
      |
local static and interactive artifact renderer
      |
FastMCP structured result + image/resource content
      |
agent
      |
existing precheck and approval flow, if asked
```

### Agent-facing request contract

The agent sends one analytics request with:

- One instrument or a bounded list of instruments.
- Start and end dates.
- Requested analysis kinds.
- Optional chart interval, comparison currency, benchmark, and output format.

The MCP handles the work behind the scenes:

- Resolve instrument names or symbols to Saxo instruments.
- Select and call the required Saxo endpoints.
- Split large requests into safe batches and follow pagination.
- Respect Saxo rate limits and entitlements.
- Reuse stored data and fetch only missing coverage.
- Validate timestamps, currencies, gaps, and source quality.
- Calculate the requested measures and cross-check results.
- Store the source-linked result.
- Return structured findings, warnings, and requested charts or files.

The agent receives one finished result or one bounded job handle for work that cannot finish within
the MCP call. The agent does not need to understand Saxo endpoint selection, pagination, or data
assembly.

### Resource and request boundaries

The MCP owns batching and rate-limit compliance. Agents should not need to calculate page sizes or
call counts. Initial hard limits are:

- Up to 25 instruments in a synchronous request.
- Up to 100 instruments in one bounded analytics job.
- Up to 50,000 normalized source rows before a synchronous request becomes a job.
- Up to 5,000,000 normalized source rows in one job.
- Up to 500 structured rows returned directly to the agent. Larger results use a dataset, export,
  or artifact handle.
- Up to four concurrent analytics jobs for one local owner session.
- Up to 25 MiB for one returned artifact. Larger exports stay behind an owner-only local resource
  link.
- A configurable owner-only store quota, defaulting to 50 GiB. Reaching the quota refuses new
  ingestion; it never silently deletes retained history.

These are product safety limits, not Saxo claims. They are configuration-backed, validated before
network work begins, included in `saxo_analytics_capabilities`, and covered by boundary tests.
Requests that exceed a synchronous limit should return the exact bounded job option. Requests that
exceed a job or storage limit should return a value-free refusal and the exact reduction or
deletion action available.

## Data layer

### Recommended initial storage

Use a persistent DuckDB database in the existing owner-only state area. Saxo OpenAPI is the only
source dataset. Parquet, CSV, and JSON exports are generated from that local store when the user
requests them.

DuckDB supports persistent files, direct Parquet reads and writes, filter and projection pushdown,
and Python interoperability:

- [DuckDB Python API](https://duckdb.org/docs/stable/clients/python/overview)
- [DuckDB Parquet support](https://duckdb.org/docs/current/data/parquet/overview)

Important implementation constraints:

- Use explicit DuckDB connection objects, not the global connection.
- Use one writer lock and separate read connections.
- Disable extension installation and external access in the agent-facing query path.
- Do not expose unrestricted SQL in the first release.
- Keep the database and artifacts outside the repository with owner-only permissions.
- Encrypt backups and rely on full-disk encryption for the live local store.
- Tag every stored dataset and artifact with its Saxo source, timestamps, entitlement state,
  retention policy, and delivery visibility.

### Core tables

- `instruments`
- `instrument_entitlements`
- `price_bars`
- `quote_snapshots`
- `trade_context_snapshots`
- `option_chain_snapshots`
- `iv_surface_snapshots`
- `portfolio_snapshots`
- `position_snapshots`
- `order_snapshots`
- `closed_positions`
- `transactions`
- `cost_illustrations`
- `corporate_actions`
- `fx_rates`
- `analysis_runs`
- `analysis_inputs`
- `analysis_results`
- `artifacts`

### Provenance classes

- `saxo_account`
- `saxo_market`
- `derived`

Every field or series should retain its source class and source timestamp.

### Revisions and corporate actions

Saxo notes that chart subscriptions can reset after corrections, corporate actions, or a change
between delayed and real-time pricing. The cache must therefore:

- Re-fetch a trailing correction window on every sync.
- Version revised bars rather than silently overwriting evidence.
- Record whether prices are adjusted or unadjusted.
- Recompute dependent analyses when source revisions invalidate them.

## Analysis result contract

All analytics tools should return the same envelope:

```json
{
  "status": "passed",
  "tool_name": "saxo_analyze_portfolio",
  "analysis_id": "an_...",
  "analysis_kind": "performance_and_risk",
  "environment": "LIVE",
  "as_of": "2026-07-29T07:00:00Z",
  "dataset_id": "ds_...",
  "portfolio_snapshot_id": "ps_...",
  "account_scope": "safe_account_alias_or_aggregate",
  "response_visibility": "fingerprint_only",
  "source_scope": "saxo_openapi",
  "source_revision": "rev_...",
  "verification": {
    "state": "verified",
    "proof_profile_id": "vp_...",
    "proof_candidate": "git_commit_or_release_id",
    "checks_passed": [
      "golden",
      "property",
      "independent_reference",
      "saxo_reconciliation",
      "sim_end_to_end"
    ],
    "metric_classes": {},
    "tolerances": {},
    "reconciled_at": "2026-07-29T07:00:00Z",
    "unexplained_differences": []
  },
  "valid_until": "2026-07-29T07:05:00Z",
  "invalidated_by": [],
  "metrics": {},
  "series_summaries": [],
  "artifacts": [],
  "data_quality": {
    "coverage": {},
    "staleness": {},
    "missingness": {},
    "entitlements": {},
    "warnings": []
  },
  "assumptions": [],
  "is_not_advice": true,
  "is_not_forecast": true,
  "model_distribution_only": false,
  "verifies": [],
  "does_not_verify": [],
  "engine": {
    "name": "saxo-analytics",
    "version": "1",
    "code_commit": "..."
  },
  "replayable": true,
  "next_actions": []
}
```

No result may contain a numeric recommendation without the inputs and assumptions needed to
interpret it.

Every `analysis_kind` must have a discriminated input and output model with required parameters and
typed metric fields. The open-ended `metrics` object above illustrates the common envelope only; it
is not the implementation schema. An unsupported kind is refused with an exact next tool or missing
capability.

Every material metric is classified separately:

- `broker_reported`: returned by Saxo with no local reinterpretation beyond units and safe naming.
- `calculated_verified`: calculated locally and covered by an active proof profile.
- `model_output`: produced by an explicitly named model with assumptions and validation bounds.
- `approximation`: uses a disclosed proxy or incomplete source and must state what it cannot prove.
- `unavailable`: cannot be calculated honestly from the available Saxo data.

Production responses may use `verified`, `degraded`, or `refused`. `unverified` is development-only
and must never be returned as a successful production result. A generic tool is not considered
verified as a whole: each `analysis_kind`, output schema version, and material metric must have its
own active proof coverage.

Handles expire or become invalid when their source data is revised, a portfolio-changing event
occurs, entitlement state changes, or a retention deadline passes. Tools must return the
invalidation reason instead of silently replaying stale work.

## Proposed agent-facing tools

Keep the catalog compact. Prefer rich discriminated schemas over a separate MCP tool for every
metric.

### Discovery and data

#### `saxo_analytics_capabilities`

Returns:

- Installed analytics modules.
- Available Saxo data groups.
- Current entitlements and delays.
- Dataset coverage.
- Supported renderers and export formats.
- Explicit capability limitations.
- Verification maturity and proof-profile version for every `analysis_kind`.
- Any quarantined or degraded analytics and the exact reason.

This is the cold-start orientation tool for agents.

#### `saxo_resolve_research_universe`

Resolves user language such as "my holdings plus this saved Saxo instrument list" into safe
instrument handles and records ambiguities. It never silently selects among materially different
instruments or claims complete index constituents that Saxo does not supply.

#### `saxo_manage_research_universe`

Creates, lists, updates, or deletes owner-only saved instrument universes using resolved safe
instrument handles. It is a typed local-state operation, never a broker write. Updates report added,
removed, ambiguous, unavailable, and entitlement-limited instruments; they never silently replace
an existing instrument with a different listing.

#### `saxo_sync_research_data`

Creates or incrementally refreshes a dataset for:

- Instruments.
- Price bars.
- Portfolio snapshots.
- Transactions and closed positions.
- Costs.
- Corporate actions.
- Entitled options chains, quotes, and Greeks snapshots for explicitly selected instruments.

It returns coverage and quality summaries, not raw data.

#### `saxo_get_research_dataset`

Returns dataset metadata, lineage, freshness, and safe table summaries.

### Market and instrument research

#### `saxo_analyze_market`

Analysis kinds:

- Movers and breadth within holdings, orders, or an explicit saved Saxo instrument list.
- Cross-asset dashboard over an explicit Saxo research universe.
- Correlation and regime.
- Volatility and dispersion.
- Event and corporate-action radar.
- Watchlist health.
- Entitled depth, spread, and microstructure snapshots.

The tool never describes a bounded list as the whole market.

#### `saxo_analyze_instruments`

Accepts one instrument or a bounded list, plus a start date, end date, requested analysis kinds,
and output options. One instrument returns a compact dossier. Several instruments return the same
per-instrument analysis plus requested comparisons.

Analysis kinds:

- Price and volume.
- Returns and risk.
- Technical indicators.
- Relative strength.
- Peer or benchmark comparison.
- Trading conditions and costs.
- Corporate actions and event timeline.
- Options and derivatives availability.
- Income, expiry, and settlement timeline.
- Multi-instrument comparison across normalized price performance, volatility, decline from a
  recent high, correlation, risk-adjusted returns, costs, and liquidity measures.

The result contains one or more artifacts. It reports unavailable dimensions instead of filling
them from another provider.

### Portfolio research

#### `saxo_analyze_portfolio`

Analysis kinds:

- `overview`
- `performance`
- `risk`
- `exposure`
- `attribution`
- `income`
- `costs`
- `liquidity`
- `margin`
- `trade_review`
- `tax_lot_export`
- `cash_and_settlement`
- `income_calendar`
- `regulatory_cost_report`
- `multi_account`
- `full_tearsheet`
- `session_cockpit`

This is the central high-level tool. A single call can request several compatible analysis kinds
and one dashboard.

`session_cockpit` is bounded to the current holdings, working orders, and an explicit saved Saxo
instrument list. It combines multi-timeframe state, volatility, sizing inputs, spread or quote
quality, working orders, margin or buying-power state, and portfolio risk. It is a request-response
analysis, not a live ladder.

Tax-lot output is refused or labeled incomplete when Saxo does not provide authoritative lot basis.
Multi-account aggregation uses safe account aliases and never exposes raw account identifiers.

#### `saxo_size_position`

Calculates a proposed position size from user-confirmed numeric inputs:

- Maximum loss.
- Stop or invalidation price.
- Volatility method.
- Portfolio concentration limit.
- Margin and buying-power constraints.
- Estimated cost.

It returns a proposal only. It does not choose the user's risk budget and never creates an order.

#### `saxo_run_scenario`

Scenario types:

- User-defined shocks.
- Historical replay.
- Currency shock.
- Volatility shock.
- Rate shock where Saxo supplies enough instrument sensitivity data.
- Margin stress.
- Combined narrative scenario.

For a narrative scenario, the agent may propose a shock map, but the tool must echo the explicit
numeric shocks and require user confirmation before calculation. The model must never hide how a
phrase such as "major recession" became numbers.

#### `saxo_optimize_portfolio`

This is a later-phase tool, after the signature account-truth workflows are proven.

Objectives:

- Minimum variance.
- Risk parity.

Constraints:

- Long-only or bounded short.
- Position bounds.
- Asset-class bounds.
- Currency bounds.
- Turnover.
- Estimated transaction cost.
- Margin.
- Minimum trade size.
- Excluded instruments.

Output is a mathematical proposal and current-to-target delta. It never trades.

### Derivatives and strategy research

#### `saxo_model_derivatives`

Analysis kinds:

- Option chain.
- Multi-leg payoff.
- Aggregate Greeks.
- IV smile and term structure.
- Spot, volatility, time, and rate scenarios.
- Futures curve and roll.
- FX forward and carry.

Modeled values should be cross-checked with Saxo-provided Greeks where entitlements permit. A
disagreement is surfaced, not averaged away.

#### `saxo_backtest_strategy`

Use a bounded declarative strategy schema, not arbitrary Python.

Historical backtesting follows SIM ghost-portfolio forward testing. The first historical version
supports:

- Entry and exit rules over approved indicators.
- Position sizing rules.
- Rebalance schedules.
- Long, short, and cash constraints.
- Transaction costs and slippage.
- Walk-forward and holdout periods.
- Benchmark comparison.

Every result must report:

- Look-ahead controls.
- Survivorship and universe limitations.
- Parameter count.
- In-sample and out-of-sample split.
- Turnover and cost sensitivity.
- Overfit warnings.

#### `saxo_propose_trade_from_analysis`

Converts a valid, unexpired `analysis_id` and a user-selected proposal into the exact typed input
required by the existing order precheck. It:

- Preserves analysis provenance.
- Re-resolves current broker instrument identity.
- Re-checks quote, cost, account, and order constraints.
- Captures and stores the quote, spread, account, and order context used for the preview so a later
  execution report can compare the fill with the actual MCP-observed decision point.
- Produces a pre-trade impact card showing resulting concentration, currency exposure, estimated
  round-trip cost, buying-power or margin impact, and user-selected downside scenarios.
- Produces a preview only.
- Never carries an old approval into a new action.

### Rendering, export, and explanation

#### `saxo_render_analysis`

Renders a prior `analysis_id` using a validated template and theme.

Outputs:

- Inline PNG through MCP image content.
- Self-contained interactive HTML through an embedded resource in a later phase.

Analysis tools return structured data by default. They attach one default PNG only when
`artifact_delivery` explicitly requests it. `saxo_render_analysis` is idempotent for a given
`analysis_id`, template, theme, visibility mode, and renderer version.

#### `saxo_export_analysis`

Exports:

- CSV.
- Parquet.
- JSON.
- HTML.
- PDF in a later phase.

Exports inherit the source privacy and delivery classification.

#### `saxo_list_analytics_storage`

Lists owner-only retained data by safe account alias, data type, instrument handle, date range,
analysis, artifact, row count, and byte count. It never returns raw broker identifiers, file paths,
credentials, or private financial values.

#### `saxo_preview_analytics_deletion`

Previews the exact local data, dependent analyses, artifacts, and estimated bytes affected by a
deletion scope. It returns a short-lived, single-use deletion token bound to the normalized scope
and current store revision. It does not delete anything and never calls Saxo.

#### `saxo_delete_analytics_data`

Deletes only the local data covered by a valid deletion preview token. Any scope or store-revision
change invalidates the token and requires a new preview. The tool removes dependent private
artifacts, preserves a value-free audit receipt, and never calls Saxo or changes a brokerage
account.

#### `saxo_explain_analysis`

Returns:

- Formulas.
- Inputs.
- Data lineage.
- Parameters.
- Assumptions.
- Warnings.
- Engine version.
- Reproduction instructions.
- Cross-check results.
- Per-metric classification, proof-profile version, and verification state.
- Exact tolerances, observed differences, and named causes for accepted differences.
- Redacted evidence-pack summary.
- Quarantine, expiry, or invalidation reason where applicable.

#### `saxo_manage_analysis_job`

Starts, checks, or cancels bounded long-running analytics jobs. This supports Monte Carlo,
optimization, large backtests, and report generation without blocking the MCP transport.

All analytics jobs belong to the Saxo MCP. Normal collection is on demand: an agent calls the MCP,
the MCP fetches Saxo data, and the MCP keeps that data in the owner-only store for later analysis.
Nothing needs to run between agent sessions.

Background scheduling is outside this implementation plan. On-demand collection is the product
contract: the agent requests instruments and a time range, and the MCP fetches, stores, analyzes,
and renders the required Saxo data inside that call or a bounded MCP-owned job.

## Analytics catalog

### Performance

- Time-weighted return.
- Money-weighted return and XIRR.
- CAGR.
- Daily, weekly, monthly, quarterly, and annual returns.
- Contribution and withdrawal-aware growth.
- Benchmark-relative return.
- Active return.
- Tracking error.
- Information ratio.
- Alpha and beta.
- Upside and downside capture.
- Rolling returns.
- Best and worst periods.
- Recovery time.

Saxo's own performance endpoints should be used as a cross-check where compatible. Differences
must be attributed to cash-flow treatment, FX conversion, cost treatment, time zone, and data
coverage.

Instrument results must label `price_return`, `adjusted_price_return`, or `total_return`. A total
return claim is refused unless the source series explicitly includes distributions and corporate
actions under a documented method.

### Risk

- Volatility.
- Downside deviation.
- Sharpe, Sortino, and Calmar ratios.
- Maximum drawdown and underwater periods.
- Historical VaR and expected shortfall.
- Parametric VaR with explicit distribution assumptions.
- Marginal and component risk contribution.
- Concentration metrics.
- Correlation and covariance.
- Rolling beta and correlation.
- Margin utilization and distance-to-liquidation approximations where Saxo provides enough data.

### Exposure

- Instrument.
- Asset class.
- Currency.
- Exchange.
- Long, short, gross, and net.
- Leveraged and unleveraged.
- Delta-equivalent options exposure.
- Duration and rate exposure where instrument data supports it.
- Settlement currency and upcoming cash obligations.

Sector, industry, country, factor, and fund-holdings look-through are explicitly unavailable unless
a future Saxo response is proven to supply authoritative classifications.

### Attribution

- Position contribution.
- Currency contribution.
- Income contribution.
- Cost drag.
- Trading contribution.
- Allocation and selection attribution when a suitable Saxo instrument can be used as the
  benchmark.
- "What changed since the last snapshot" waterfall.

### Cost intelligence

- Commissions.
- Spread.
- FX conversion.
- Financing and borrow.
- Carry.
- Custody.
- Exchange fees.
- Taxes and stamp duties where available.
- Cost to open, hold, and close.
- Wrapper comparison for equivalent exposures.
- Realized cost drag over time.
- Regulatory ex-ante and ex-post cost packs where the source data and jurisdiction support them.

This is a high-value differentiator because it can use the user's actual Saxo trading conditions.
The flagship `cost_xray` result explains costs in money and return impact, identifies recurring
patterns, and distinguishes unavoidable charges from user-controlled turnover or conversion
behavior.

### Trade review

- Win rate and expectancy.
- Average winner and loser.
- Holding-time asymmetry.
- Disposition-effect indicators.
- Averaging-down behavior.
- Entry and exit timing.
- Overtrading and turnover.
- Concentration after losses or wins.
- TWR versus MWR behavior gap.
- Plan-versus-outcome analysis when the trade thesis is captured at approval time.

These results must describe observed behavior, not diagnose or shame the user.
The flagship `trading_mirror` combines these observations with the do-nothing counterfactual and
the approval-time thesis ledger.

### Execution quality

Execution comparisons use this source order:

1. Quote and spread captured by the MCP at preview or submission time.
2. Nearest available Saxo chart bar as an explicitly labeled approximation.
3. Refusal when neither source is adequate.

Orders placed outside the MCP normally lack an exact decision-point quote. The tool must not call a
bar open, close, midpoint, or VWAP an arrival price.

### Scenario and goal modeling

- Historical crisis replay.
- Custom multi-asset shocks.
- Monte Carlo with seeded bootstrap.
- Block bootstrap for autocorrelation.
- Contributions and withdrawals.
- Goal and ruin probability.
- Sequence-of-returns risk.
- Inflation-adjusted outcomes using an explicit user-provided numeric inflation assumption.
- Sensitivity bands around assumptions.

Scenario and goal outputs always set `is_not_forecast=true`. Distribution-based probabilities set
`model_distribution_only=true`. No built-in inflation series is claimed; without an explicit user
assumption, the inflation-adjusted result is refused.

### Technical analysis

- Moving averages.
- RSI.
- MACD.
- ATR.
- Bollinger bands.
- Realized volatility.
- Volume and volume-weighted measures when data permits.
- Support and resistance as transparent rules.
- Relative strength.
- Trend and regime classifications.

Technical indicators are descriptive transforms, not forecasts.

Support, resistance, trend, and regime labels must state their exact deterministic rule. They are
never returned as buy or sell signals.

### Options and derivatives

- Chain filtering.
- Breakevens.
- Payoff at expiry.
- Theoretical value over time.
- Delta, gamma, theta, vega, and rho.
- Aggregate strategy Greeks.
- Implied volatility inversion.
- Smile, skew, and term structure.
- Probability ranges under stated distributions.
- Early exercise and dividend warnings.
- Margin and pre-trade cost integration.
- Futures basis, carry, term structure, and roll analysis.

Simple European models can be implemented and golden-tested locally. American options, complex
rates, and products with path dependency need either a proven pricing library or an explicit
limited-capability refusal.

Options probability outputs describe a stated mathematical distribution only and set
`model_distribution_only=true`.

## Visualization system

### Delivery formats

1. Static PNG for immediate, universal inline delivery.
2. CSV, Parquet, and JSON for downstream analysis.
3. Interactive self-contained HTML after deterministic PNG delivery is stable.
4. PDF after the HTML reporting path is stable.

### Recommended rendering stack

Start with:

- NumPy for core numerical arrays.
- A dataframe library only if the first slice demonstrates a real need.
- Matplotlib in headless mode for deterministic PNGs.
- DuckDB for storage and aggregation.

Add later:

- One interactive renderer selected after testing the first drill-down workflows.
- A PDF renderer only after HTML reports are stable.

### Core templates

- Price, volume, and indicator chart.
- Relative-performance chart.
- Equity curve and drawdown.
- Monthly return heatmap.
- Allocation and exposure.
- Contribution waterfall.
- Risk-contribution chart.
- Correlation heatmap and cluster map.
- Scenario waterfall.
- Efficient frontier.
- Options payoff and Greeks.
- IV smile and surface.
- Cost waterfall.
- Trade distribution and behavior dashboard.
- Portfolio tearsheet.
- Pre-trade impact card.
- Portfolio briefing.
- Trading mirror.
- Execution report card.
- Session cockpit.

### Artifact rules

Every artifact must visibly stamp:

- Environment.
- Data cutoff.
- Quote delay or price type.
- Analysis identifier.
- Currency.
- Adjustment status.
- Material warnings.
- Visibility and Saxo source scope.

Interactive HTML must be self-contained, use no external data URLs, and be sanitized before being
returned as an MCP resource.

For private LIVE dashboards, inline delivery is refused unless `inline_private` is enabled for the
current host. The safe fallback is an owner-only local resource link or redacted preview.

## Modeling engine strategy

### Build directly

Implement and exhaustively test small, stable formulas:

- Returns.
- TWR and MWR.
- Volatility.
- Drawdown.
- Common risk-adjusted metrics.
- Historical VaR and expected shortfall.
- Correlation and covariance.
- Black-Scholes and Black-76 where appropriate.
- Simple bootstrap simulation.

The value is auditability. Each formula should be reviewable and linked from
`saxo_explain_analysis`.

### Use proven libraries behind adapters

For advanced domains, do not reimplement an entire research ecosystem:

- Portfolio optimization can use a validated solver layer and compare against PyPortfolioOpt or
  Riskfolio-Lib reference results.
- High-volume backtesting can use a proven engine such as vectorbt behind a constrained schema.
- Complex options and fixed-income pricing should use a proven pricing library when product scope
  justifies it.

Relevant current references:

- [PyPortfolioOpt user guide](https://pyportfolioopt.readthedocs.io/en/latest/UserGuide.html)
- [Riskfolio-Lib documentation](https://riskfolio-lib.readthedocs.io/)
- [vectorbt documentation](https://vectorbt.dev/)
- [QuantStats](https://github.com/ranaroussi/quantstats)

Libraries are implementation details. The MCP result contract, provenance, quality gates, and
agent behavior remain stable if an engine changes.

### Do not build as a trusted core

- Black-box price prediction.
- Unbounded model-generated strategy code.
- Open-ended Python execution.
- Arbitrary SQL over the host filesystem.
- A recommendation engine that presents model output as certainty.
- A hosted market-data redistribution service.

## Saxo data architecture

### Core provider

`SaxoProvider` supplies:

- Account and portfolio state.
- Historical account performance.
- Transactions and closed positions.
- Price bars.
- Quotes and entitlements.
- Reference data.
- Trading conditions and costs.
- Corporate actions where licensed.
- Options and derivatives data where entitled.

There is no external provider interface. If `SaxoProvider` cannot supply a required input, the
analysis returns a clear coverage limitation.

## Design benchmark

Bloomberg PORT, FactSet, Morningstar Direct, Koyfin, TradingView, QuantStats, and vectorbt informed
the workflow research. The Saxo MCP does not attempt feature parity or copy their interfaces. It
wins where generic platforms cannot: the user's actual account, orders, fills, costs, margin,
broker rules, accumulated private history, and a direct path into the existing exact approval
kernel.

## Agent ergonomics

### Tool selection

- `saxo_analytics_capabilities` is the first analytics call in an unfamiliar session.
- High-level tools use explicit `analysis_kind` discriminators.
- Tools accept natural instrument queries but return ambiguity lists before calculation.
- Errors identify the missing prerequisite and exact next tool.
- Tools return small results by default and offer artifacts or exports for detail.

### Numeric claim policy

An agent must not invent or mentally calculate a material financial number when a corresponding
analytics tool exists. User-facing material numeric claims should cite an `analysis_id`.

### Suggested follow-ups

Results may include a short list of safe, relevant follow-up analyses. They must not present a
trade as the default next step. `next_actions` is schema-restricted to research and explanation
calls. Trade preparation requires an explicit user request and
`saxo_propose_trade_from_analysis`.

### Analysis profiles

Reusable profiles reduce repeated parameters:

- `portfolio_daily`
- `instrument_dossier`
- `pre_trade_research`

Profiles are transparent parameter bundles, not hidden prompts.

## Safety and epistemic controls

### Required controls

- Formula and assumption disclosure.
- Coverage and staleness gates.
- Entitlement and delayed-data labels.
- Deterministic seeds.
- Environment separation.
- Input and output schema validation.
- No automatic handoff to execution.
- No arbitrary code.
- No external chart service.
- No public evidence containing private financial values.
- No hidden fallback from broker data to a different provider.
- No "best", "safe", or "guaranteed" language from optimizer outputs.
- Machine-readable `is_not_advice`, `is_not_forecast`, and `model_distribution_only` flags.
- A lint gate that rejects advice-shaped structured labels before publication.

### Model risk

Each model should publish:

- Intended use.
- Unsupported uses.
- Input requirements.
- Known weaknesses.
- Validation dataset.
- Independent reference implementation.
- Version history.

### Scenario safety

Scenarios are hypotheses, not forecasts. A narrative scenario must be translated into an explicit
shock table before calculation:

```text
Nasdaq 100: -12%
USD/DKK: -8%
Implied volatility: +15 percentage points
Risk-free curve: +75 basis points
```

The result must show sensitivity when the shock mapping is uncertain.

### Optimization safety

Optimizers amplify bad inputs. Results must therefore include:

- Estimation window.
- Return model.
- Covariance model.
- Constraints.
- Turnover.
- Cost estimate.
- Stability under small input changes.
- Current-to-target delta.
- A warning when weights are unstable or concentrated.

## Caching and reproducibility

### Content-addressed analysis

The analysis identifier should derive from:

- Source dataset fingerprints.
- Parameters.
- Engine name and version.
- Code commit.
- Random seed.
- Time zone and calendar.
- Currency-conversion method.

Repeated identical analyses can return the cached result.

### Cache policy

- Quotes: seconds, according to quote timestamp.
- Instrument details: long-lived but account-scoped.
- Trading conditions: medium TTL and refreshed before pre-trade use.
- Price bars: incremental with a trailing correction window.
- Portfolio snapshots: immutable.
- Corporate actions: deadline-aware refresh.

### Retention

- Saxo market history, account snapshots, option-chain history, analyses, and artifacts are kept
  indefinitely in the owner-only local store until the owner deletes them.
- Users can list stored data and preview deletion scope before deleting by date range, data type,
  account alias, instrument, analysis, or artifact.
- Deletion removes the selected source data and dependent private artifacts. A value-free audit
  record may retain when and what class of data was deleted.
- No automatic age-based deletion runs by default.
- The store, exports, and temporary files use owner-only filesystem permissions.
- Schema migrations are versioned, transactional, backed up before destructive changes, and tested
  against the previous released schema. Migration failure leaves the prior store readable.
- DuckDB external access, extension installation, arbitrary SQL, and caller-supplied file paths are
  disabled. Tools accept typed filters and safe handles only.

## Performance targets

- Cached analysis: under 1 second.
- Warm portfolio dashboard: under 5 seconds.
- Cold single-instrument sync, analysis, and render: under 15 seconds within Saxo rate limits.
- Long-running jobs report progress and estimated remaining work.
- Results stream only safe progress metadata, not partial misleading conclusions.

## Testing and evaluation

### Non-negotiable correctness rule

Every analytics result starts untrusted. It becomes production-eligible only when the exact
combination of:

- Tool.
- `analysis_kind`.
- Output schema version.
- Material metric.
- Engine version.
- Saxo source contract.

has an active, source-bound proof profile.

Passing one analysis kind does not pass the containing MCP tool. Passing a formula does not pass
its data ingestion. Passing structured output does not pass its chart. Passing an agent evaluation
does not prove the arithmetic. Each layer is proved separately and then exercised together.

When proof is missing, stale, contradictory, or outside tolerance, the affected metric or analysis
kind is quarantined. The server returns `degraded` or `refused` with the exact reason. It never asks
the language model to fill the gap, calculate the number mentally, or soften the failure into a
plausible answer.

### Source-controlled metric definitions

Every material metric needs a versioned definition before implementation:

- Plain-language meaning.
- Exact formula and loss or return sign convention.
- Saxo source fields and endpoint contracts.
- Input and output units.
- Account, instrument, and currency scope.
- FX conversion source, direction, and timestamp.
- Time zone, trading calendar, cutoff, and annualization convention.
- Cash-flow, fee, financing, tax, settlement, and corporate-action treatment.
- Missing, duplicate, revised, delayed, stale, or partial data behavior.
- Minimum sample and coverage requirements.
- Absolute and relative numeric tolerances.
- Independent reference method.
- Saxo reconciliation target where one exists.
- Runtime failure and degradation policy.

Definitions are reviewed like code. A formula change, source-field change, unit change, tolerance
change, or output-schema change invalidates the old proof profile and requires a new candidate.

### Exact tolerance policy

There is no global "close enough" threshold.

- Counts, identifiers, order state, dates, and boolean conditions require exact equality.
- Currency totals use decimal arithmetic and a tolerance derived from the currency minor unit and
  Saxo's documented rounding.
- Ratios and floating-point metrics define both absolute and relative tolerances.
- Aggregations define how rounding accumulates across rows.
- Broker-versus-local comparisons separate known timing, scope, FX, and rounding differences.
- Model outputs use deterministic seed checks plus statistical calibration and sensitivity bounds.

Any difference outside tolerance is a failure until it has a named, reproduced cause. Raising a
tolerance merely to make a test pass is forbidden.

### Per-analysis proof profile

Each production `analysis_kind` has a machine-readable proof profile covering:

1. **Source contract proof**: required endpoints, fields, enums, pagination, entitlement states,
   units, signs, timestamp semantics, and null behavior.
2. **Known-answer proof**: hand-checkable and published examples with exact expected results.
3. **Property proof**: mathematical invariants and metamorphic behavior over generated inputs.
4. **Independent calculation proof**: a separate slow reference implementation or trusted library
   that shares frozen inputs but no production calculation helpers.
5. **Accounting proof**: applicable balances, cash flows, costs, exposures, and contributions obey
   defined identities.
6. **Saxo reconciliation proof**: compare with Saxo-reported performance, costs, exposure, margin,
   balances, or Greeks wherever the concepts are genuinely comparable.
7. **Executable SIM proof**: exercise the real MCP tool against Saxo SIM with recorded request
   receipts, output validation, cleanup, and unchanged brokerage state.
8. **Artifact parity proof**: every displayed value and chart series comes from the same verified
   result object without renderer-side recalculation.
9. **Agent-use proof**: tasks executed under the candidate's explicit model policy select the right
   tools, report the right numbers, preserve warnings, and do not invent unsupported conclusions.
   The current `codex_native_v1` policy requires exactly one real Codex harness record and rejects
   every other harness; historical dual-agent evidence remains historical.
10. **Privacy and safety proof**: no secret, raw account identifier, disallowed private value, or
    broker write enters evidence or an unauthorized response.

Independent math libraries may validate formulas but may never supply market or account data. All
runtime source data remains Saxo-only.

### Known-answer and adversarial datasets

Maintain small, reviewable synthetic datasets with exact expected answers for:

- Deposits and withdrawals before, during, and after measurement periods.
- Same-timestamp cash flows and trades.
- Single and multiple currencies with explicit FX paths.
- Long, short, leveraged, cash, and zero-position portfolios.
- Fees, financing, taxes, custody, borrowing, and unsettled cash.
- Partial fills, corrected transactions, duplicate rows, and out-of-order pages.
- Empty, one-observation, sparse, stale, delayed, and missing series.
- Market holidays, daylight-saving changes, leap days, and exchange time-zone boundaries.
- Revised bars, splits, distributions, expiries, assignments, and other supported lifecycle events.
- Options at zero time, near expiry, deep in or out of the money, and extreme but supported
  volatility.
- Extreme values, tiny values, rounding boundaries, and invalid numeric input.
- Entitlement denial and partial field availability.

Generated property cases must use reproducible seeds and retain the smallest failing example.
Private LIVE data is never promoted into a public golden fixture.

### Independent calculation requirement

Material calculated metrics require two implementation paths:

- The production engine optimized for the MCP.
- A deliberately separate reference path optimized for clarity.

The paths may share schema parsing and frozen input serialization, but not formula helpers,
aggregation code, currency conversion code, or rounding helpers. Selected high-risk calculations
also compare with a mature independent numerical library and Saxo's own value where available.

If all implementations share the same assumption, the assumption is tested separately against a
hand-calculated example. Agreement between two copies of the same bug is not proof.

### Accounting and reconciliation identities

Applicable analyses must prove identities such as:

- Position, cash, and unsettled components explain the account scope they claim to explain.
- Position contributions sum to the reported portfolio contribution within defined rounding.
- Cost components sum to the cost total without double counting.
- Currency contributions plus local-asset contributions reconcile to the labeled portfolio return
  under the chosen method.
- TWR chain links match the defined subperiod returns and cash-flow boundaries.
- Scenario contributions sum to the total scenario effect.
- Options leg values and Greeks sum to the strategy totals.
- Current-to-proposed deltas reproduce the proposed holdings.

Some Saxo values use private methods or different cutoffs. Those comparisons may be categorized as
`not_comparable`, but only after the scope and timing difference is reproduced. `not_comparable`
is never used as a generic escape hatch.

### Data-ingestion proof

Before calculating anything, ingestion tests prove:

- Every requested page was consumed exactly once.
- Stable keys deduplicate retries without deleting legitimate revisions.
- Row counts and continuation links are internally consistent.
- Timestamps are normalized without changing the economic trading date.
- Currency, quantity, price, percentage, and money units are explicit.
- Saxo correction and reset events create a new source revision.
- Delayed or denied fields cannot masquerade as zero.
- Missing fields remain missing rather than receiving convenient defaults.
- Dataset fingerprints change when any material input changes.

Recorded SIM fixtures are redacted, schema-checked, and bound to the request template and capture
date. They supplement but never replace executable SIM validation.

### Model and backtest proof

Modeled results need stronger checks than deterministic accounting:

- Fixed-seed golden runs.
- Cross-seed calibration tests.
- Monotonic and limiting-case behavior.
- Perturbation and numerical-stability tests.
- Explicit distribution and parameter recovery tests on synthetic data.
- Holdout and walk-forward checks.
- Timestamp-boundary tests proving no look-ahead.
- Cost, slippage, missing-bar, and survivorship limitation tests.
- Stability warnings when small input changes materially alter the answer.

The tool reports a model result, not a fact or forecast. A statistically plausible output that
fails calibration or stability checks is refused.

### Visualization parity

The renderer consumes the verified serialized result and is forbidden from recalculating financial
metrics. Tests prove:

- Every chart series fingerprint matches the structured result.
- Displayed headline values match typed output after declared formatting.
- Currency, units, date range, quote delay, adjustment state, and warnings are visible.
- Axis transformations cannot change the meaning of negative values, drawdowns, or percentages.
- Missing intervals and approximations are visible rather than interpolated silently.
- Static and interactive renderers agree on sampled points.

### Deterministic tests

- Published formula examples.
- Golden return and cash-flow series.
- Published option-pricing cases.
- Reference optimizer cases.
- Fixed random seeds.
- Time-zone and market-calendar edge cases.
- Corporate-action revisions.
- Missing and stale data.
- Currency-conversion paths.

### Property and metamorphic tests

- Portfolio weights sum to the required total.
- TWR is invariant to external cash-flow timing under the defined method.
- Drawdown is never positive.
- Expected shortfall is at least as severe as VaR under the same loss convention.
- More restrictive constraints cannot improve an optimizer's feasible objective.
- An expiry payoff is piecewise linear for supported vanilla legs.
- Reordering input rows does not change order-independent metrics.

### Cross-check tests

- Local performance versus Saxo performance endpoints.
- Local Greeks versus Saxo Greeks.
- Local costs versus Saxo pre-trade illustration.
- Local exposure versus Saxo exposure endpoints.
- A second independent library for selected golden cases.

Differences must be categorized, not ignored.

### Artifact tests

- Stable dimensions.
- No blank image.
- No clipped text.
- No overlapping labels.
- Privacy footer present.
- Provenance stamp present.
- Delayed-data warning present when required.
- Deterministic image hashes where rendering permits.
- Desktop and mobile readability for HTML artifacts.

### Agent evaluation

Build a fixture portfolio with known answers and evaluate every harness required by the candidate's
explicit model policy. Under `codex_native_v1`, that is exactly one real Codex run on tasks such as:

- Explain YTD performance versus a benchmark.
- Produce a Cost X-ray with the exact expected total and component sum.
- Explain whether the last trades' timing helped under the defined counterfactual.
- Find the largest risk contributor.
- Explain why P&L changed.
- Model a combined equity and currency shock.
- Produce a pre-trade impact card and stop before execution.
- Refuse an execution-quality claim when no decision-point quote or adequate bar exists.
- Build an options payoff.
- Compare current and optimized portfolios.
- Turn a proposed trade into a precheck without executing it.
- Forecast settlement and buying power without revealing raw account identifiers.

Score:

- Tool selection.
- Numeric correctness.
- Citation to `analysis_id`.
- Correct reporting of `verified`, `degraded`, or `refused`.
- No model-performed replacement arithmetic.
- Warning handling.
- No unsupported inference.
- No accidental write.
- Clear user communication.

### Saxo environment validation

Every Saxo-backed `analysis_kind` needs:

- Deterministic unit tests.
- Independent reference and reconciliation tests.
- Generated coverage-matrix registration.
- SIM endpoint validation.
- A separate agent usability review.
- Cleanup and state comparison for any subscription.
- A separately authorized LIVE read-only validation after the complete SIM implementation loop.

Analytics testing must never require a LIVE order.
The current implementation and verification loop uses SIM credentials and SIM endpoints only.
Missing SIM history may be created through controlled SIM activity after proving
`environment=SIM`; it must be cleaned up and reconciled. Missing Saxo entitlements must produce a
proved degradation or refusal, not an invented result and not a false implementation failure.

## Analytics correctness implementation loop

This loop applies to every `analysis_kind`, not merely every MCP tool.

### Stage 1: Definition freeze

Before code:

1. Write or update the metric definitions.
2. Identify every Saxo endpoint and field used.
3. Define units, signs, timing, currency conversion, missing-data behavior, and tolerances.
4. Add known-answer examples and invariants.
5. Name the independent reference path and Saxo reconciliation target.
6. Define what runtime condition causes `verified`, `degraded`, or `refused`.

Implementation does not begin while any material output has an undefined meaning.

### Stage 2: Test-driven development

For each coherent implementation batch:

1. Add failing tests for definitions, known answers, edge cases, and failure behavior.
2. Implement the smallest production path.
3. Add the separate reference calculation.
4. Run focused deterministic, property, type, lint, privacy, and schema tests.
5. Compare every material metric with the reference path.
6. Consolidate related fixes before creating another candidate.

The reference path is not copied from the production implementation. Reviewers inspect that
separation explicitly.

### Stage 3: Data and reconciliation proof

Using redacted fixtures and executable Saxo SIM reads:

1. Capture the actual source contract and pagination behavior.
2. Normalize into DuckDB and prove row counts, keys, timestamps, units, and revisions.
3. Run production and reference calculations over the same immutable dataset.
4. Reconcile applicable results with Saxo values and accounting identities.
5. Explain every difference outside exact equality, including cutoff, currency, scope, or rounding.
6. Refuse the result if the difference remains unexplained or exceeds its metric tolerance.

The proof report includes expected value, actual value, absolute difference, relative difference,
tolerance, result, and named cause for every accepted nonzero difference.

### Stage 4: Adversarial and mutation proof

The test harness deliberately introduces:

- Missing and duplicated rows.
- Reordered pages.
- Stale and delayed quotes.
- Changed currency direction.
- Reversed signs.
- Timestamp and daylight-saving boundary errors.
- Partial entitlements.
- Revised bars and transactions.
- Corrupted cache fingerprints.
- Extreme but valid numeric values.
- NaN, infinity, overflow, underflow, and division-by-zero inputs.

Mutation testing must demonstrate that representative changes to formulas, signs, FX direction,
annualization, cash-flow timing, and rounding cause tests to fail. A test suite that passes common
financial bugs is not accepted.

### Stage 5: Executable MCP SIM matrix

Drive one sealed installed coordinator and one distinct installed MCP child against Saxo SIM:

- Use isolated headless authentication.
- Require no human approval for SIM actions.
- Spawn one exact copied CPython interpreter over direct stdio, establish one MCP session, list
  exactly the six approved tools once, and allow no reconnect or restart.
- Record logical tool calls, source endpoint receipts, dataset fingerprints, analysis IDs, proof
  profiles, warnings, artifacts, and the reduced process-boundary receipt.
- Exercise success, degraded, entitlement-denied, stale, missing-data, ambiguity, and refusal paths.
- Verify outputs against the known-answer or reconciliation oracle.
- Clean up every stream, job, preview, or temporary state.
- Compare before and after brokerage state.
- Require `live_calls=0`, `live_mutation_calls=0`, and `purchase_occurred=false`.

Manifest validation, mocks, fixture replay, or a dry run cannot be reported as executable SIM
proof.

#### Official isolated source-matrix invocation

The analytics source matrix is an installed-only command. A direct source import is a development
surface and cannot claim or publish official evidence. Each candidate contains a copied CPython
base and complete dependency closure. Sealing sets directories to `0500`, regular files to `0400`,
and executables to `0500`. Coordinator and child cache, work, and temporary directories are
separate, owner-only, outside the sealed runtime, empty before launch, and empty again after child
exit.

Prepare a bootstrap candidate, then independently prepare two final runtimes from the rebuilt
wheel. Generate a manifest from each final runtime and require both to be byte-identical to the
checked manifest:

```bash
umask 077
ARTIFACT_ROOT=$(mktemp -d)
UV_BIN=$(command -v uv)

mkdir -m 700 "$ARTIFACT_ROOT/bootstrap-wheel"
"$UV_BIN" build --offline --wheel --out-dir "$ARTIFACT_ROOT/bootstrap-wheel"
"$UV_BIN" run python scripts/prepare_analytics_source_matrix_runtime.py \
  --uv "$UV_BIN" \
  --runtime "$ARTIFACT_ROOT/bootstrap-runtime" \
  --wheel "$ARTIFACT_ROOT/bootstrap-wheel/saxo_bank_mcp-0.1.0-py3-none-any.whl"
"$ARTIFACT_ROOT/bootstrap-runtime/bin/saxo-bank-analytics-source-matrix-generate" \
  --repository-root "$PWD" \
  --wheel "$ARTIFACT_ROOT/bootstrap-wheel/saxo_bank_mcp-0.1.0-py3-none-any.whl" \
  --out data/analytics/source_matrix_candidate.json

mkdir -m 700 "$ARTIFACT_ROOT/final-wheel"
"$UV_BIN" build --offline --wheel --out-dir "$ARTIFACT_ROOT/final-wheel"
for suffix in a b; do
  "$UV_BIN" run python scripts/prepare_analytics_source_matrix_runtime.py \
    --uv "$UV_BIN" \
    --runtime "$ARTIFACT_ROOT/final-runtime-$suffix" \
    --wheel "$ARTIFACT_ROOT/final-wheel/saxo_bank_mcp-0.1.0-py3-none-any.whl"
  "$ARTIFACT_ROOT/final-runtime-$suffix/bin/saxo-bank-analytics-source-matrix-generate" \
    --repository-root "$PWD" \
    --wheel "$ARTIFACT_ROOT/final-wheel/saxo_bank_mcp-0.1.0-py3-none-any.whl" \
    --out "$ARTIFACT_ROOT/final-manifest-$suffix.json"
done
cmp data/analytics/source_matrix_candidate.json "$ARTIFACT_ROOT/final-manifest-a.json"
cmp "$ARTIFACT_ROOT/final-manifest-a.json" "$ARTIFACT_ROOT/final-manifest-b.json"
```

The preparation command refuses an existing target, verifies the offline lock and sealed dependency
seed, installs without dependency resolution, and refuses Python cache artifacts. The installed
launchers establish `-I -B -S`, one exact installed interpreter, and the fixed runtime site
directory. The coordinator holds no-follow descriptors across the child lifetime and repeats the
canonical ancestor, portable tree, entry metadata, owner, and mode checks only after the child is
reaped.

The full receipt records stdio transport, distinct process proof, irreversible process and stdio
identities, one spawn, one session, one initialize, one tool list, exact tool count and digest, zero
reconnects, zero restarts, child exit zero, protocol-only stdout, and unpublished stderr. Failure
precedence is fixed: post-exit static change, child-process failure, matrix failure, then privacy-scan
failure. These local checks require no Saxo credentials:

```bash
"$ARTIFACT_ROOT/final-runtime-a/bin/saxo-bank-analytics-source-matrix" --help
"$ARTIFACT_ROOT/final-runtime-a/bin/saxo-bank-analytics-source-matrix" --identity
"$ARTIFACT_ROOT/final-runtime-a/bin/saxo-bank-analytics-source-matrix" --preflight
```

Fixture replay and the offline process-boundary proof cannot close Task 6. The normal installed
invocation remains blocked until ready real SIM authentication and separate authorization exist.
That future one-shot run must prove a SIM-only GET ledger, unchanged brokerage state, zero mutation
calls, privacy success, the exact process receipt, and a clean post-exit seal.

### Stage 6: Artifact and agent proof

For every user-facing signature experience:

1. Verify the structured result first.
2. Render every supported artifact from that exact result.
3. Prove chart and table parity against sampled values and fingerprints.
4. Run the exact harness set required by the candidate policy from isolated installations with
   exact logical tool grants. `codex_native_v1` requires Codex only.
5. Grade tool choice, numeric claims, verification-state reporting, warning handling, privacy,
   refusal behavior, and absence of broker writes.
6. Give agents hard compound tasks with expected answers, not merely "call this tool" prompts.

Agent success is necessary for usability but never substitutes for deterministic numeric proof.

### Stage 7: Candidate freeze

After all source changes are complete:

1. Create one stable candidate commit.
2. Freeze dependency versions, formula definitions, schemas, tolerances, fixtures, and renderer
   versions.
3. Generate a machine-readable coverage matrix mapping every tool, `analysis_kind`, metric,
   artifact, proof profile, test, reconciliation target, and agent case.
4. Fail the candidate if any production output lacks coverage.
5. Do not regenerate final evidence for documentation-only changes outside the candidate.

### Stage 8: Final proof run

Run once per stable candidate:

1. Full deterministic, property, metamorphic, mutation, numerical, privacy, lint, and type suite.
2. All independent reference comparisons.
3. All accounting identities and Saxo reconciliation cases.
4. The executable Saxo SIM analytics matrix.
5. Isolated installation checks for every harness required by the candidate's selected model
   policy. `codex_native_v1` requires Codex only; historical or alternative `dual_v1` candidates
   require both Codex and Claude.
6. Policy-matched signature-experience evaluations. `codex_native_v1` requires one real Codex
   evaluation; `dual_v1` requires matched Codex and Claude evaluations.
7. Artifact parity and visual integrity checks.
8. Subscription, job, cache, and temporary-state cleanup.
9. Before and after brokerage-state equality.
10. Secret, identifier, private-value, and evidence-publication scans.
11. One separate independent review using a reviewer allowed by the candidate's model policy and
    current operator restrictions, focused only on reproducible correctness, safety, privacy, and
    agent-usability blockers. Under the current native-only restriction, that reviewer is Codex.

After the complete SIM implementation loop, LIVE read-only validation is a separate, explicitly
started phase. It may calculate privately over LIVE reads, but it must not place, change, cancel,
or answer a disclaimer. LIVE validation records a complete request ledger, unchanged account
state, `live_mutation_calls=0`, and `purchase_occurred=false`. No LIVE call is part of the current
implementation loop.

### Failure handling

When a final check finds a real blocker:

1. Quarantine the affected `analysis_kind` at runtime.
2. Keep the failed evidence; do not overwrite or relabel it.
3. Find the root cause and inspect adjacent metrics sharing the source or helper.
4. Consolidate all related fixes.
5. Create one new stable candidate.
6. Repeat final proof once for that candidate.

Do not rerun only the convenient cases, raise tolerances without justification, accept an
unexplained delta, or claim partial convergence. The implementation loop continues through every
in-scope analysis kind. Saxo service outages are recorded with exact evidence while unaffected
work continues. Missing Saxo data or entitlements must pass the defined degradation or refusal
contract.

### Per-analysis evidence pack

Every production `analysis_kind` ships with a proof receipt containing:

- Candidate commit and dependency lock fingerprint.
- Metric-definition and output-schema versions.
- Saxo source templates and schema fingerprints.
- Fixture and immutable dataset fingerprints.
- Production and reference engine versions.
- Test and mutation coverage.
- Per-metric tolerances and comparison results.
- Saxo reconciliation results and named differences.
- SIM execution receipts.
- Artifact parity results.
- Model-evaluation policy ID and the evaluation results for every harness that policy requires.
- Privacy and unchanged-state results.
- Independent review verdict.
- Known limitations, expiration, and invalidation triggers.

The public evidence pack contains schemas, hashes, counts, safe aliases, pass or fail states, and
redacted comparisons. Private account values remain in the owner-only store.

### Runtime correctness guard

Release proof is necessary but not permanent. At runtime:

- Startup registers only analysis kinds whose proof profile matches the installed code, schema, and
  definition versions.
- Unknown Saxo fields, enums, null patterns, pagination behavior, or timestamp semantics trigger a
  schema-drift alarm and quarantine affected analytics.
- Source revisions invalidate dependent datasets, cached analyses, and artifacts.
- High-risk calculations are periodically shadowed through the independent reference path.
- Broker-comparable totals are reconciled after material portfolio changes and on a bounded
  sampling cadence during analytics calls.
- Repeated identical inputs must reproduce identical deterministic outputs.
- A material unexplained difference automatically disables the affected analysis kind until a new
  proof candidate passes.

`saxo_analytics_capabilities` exposes quarantine and proof status. `saxo_explain_analysis` can
return the redacted proof receipt, formulas, tolerances, comparison state, and exact reason for any
degradation.

### Full-suite completion gate

An `analysis_kind` is production-ready only when:

- Every material metric has a versioned definition and active proof profile.
- Known-answer, property, metamorphic, mutation, numerical, and independent-reference tests pass.
- Every applicable accounting identity passes.
- Saxo comparisons have no unexplained difference outside tolerance.
- Executable SIM MCP cases pass all success, degradation, and refusal paths.
- Artifacts contain the same verified values as structured output.
- Every harness required by the candidate's explicit model policy completes hard tasks without
  inventing numbers or causing a broker write.
- Cleanup succeeds and brokerage state is unchanged.
- Privacy and publication scans are clean.
- The final independent review finds no real blocker.
- No LIVE mutation occurred and no purchase occurred.

If one condition fails, that analysis kind remains quarantined and the implementation loop
continues until the blocker is fixed or the Saxo source contract proves that the correct final
behavior is an explicit capability refusal. Phase exits are progress checkpoints, not reasons to
end the loop. The current loop is complete only when every in-scope analysis kind and tool has
passed its applicable proof profile, every source limitation has a tested refusal or degradation
path, and the full suite passes the final candidate validation.

## Roadmap

Every phase exit requires active proof profiles for all newly production-enabled analysis kinds.
Features may be implemented behind development flags, but they remain absent or quarantined from
production capability discovery until their full correctness evidence pack passes.

The roadmap is one continuous full-suite implementation loop. Completing a phase authorizes the
next phase; it does not complete the overall task. There is no time-based or budget-based early
finish. Work continues across all phases below, with related fixes consolidated before each new
candidate.

### Phase 0: Saxo data feasibility

Goal: prove the data foundation before adding dependencies.

- Exercise charts, performance, closed positions, transactions, exposure, costs, instrument details,
  entitlements, and options data in SIM.
- If SIM lacks useful history, create controlled SIM-only activity and clean it up after evidence
  capture. Before any such action, prove `environment=SIM`. Refuse the action if the effective
  environment is LIVE or uncertain. Record before-and-after SIM state and prove cleanup.
- Prove entitlement-aware option-chain, quote, and Greeks snapshot capture for selected instruments
  and record whether each required field is available.
- Record actual schemas, page limits, correction behavior, and entitlement failures.
- Define private-result and public-evidence policy.
- Use authenticated owner-only private LIVE results and indefinite local retention with deletion
  controls.
- Define price-return, adjusted-price, and total-return naming and refusal rules.
- Define host artifact-delivery modes.
- Define the benchmark rule: a suitable Saxo-tradable accumulating ETF price series as an explicit
  proxy, another suitable Saxo instrument proxy, or no benchmark result. The proxy must disclose
  tracking difference, fees, currency, and that it is not the official benchmark series.

Exit criteria:

- A capability matrix based on real SIM responses.
- Versioned source contracts and draft metric definitions for the first slice.
- Recorded reconciliation targets, tolerances, and refusal conditions.
- Approved analysis result schema.
- Approved private-value and host-delivery policy.
- Approved retention and deletion policy.
- Explicit return-series labeling rules.

### Phase 1: Research foundation

Goal: create the reusable data, provenance, and artifact primitives.

- Owner-only DuckDB store.
- Dataset and snapshot handles.
- Instrument resolution.
- Price-bar synchronization.
- Trade-context and option-chain snapshot primitives.
- Analysis result contract.
- Analysis replay and explanation.
- Local artifact store and cleanup.
- Metric-definition and proof-profile registry.
- Independent reference-engine harness.
- Generated analytics coverage matrix.
- Runtime quarantine and schema-drift mechanism.

Exit criteria:

- Incremental sync is reproducible.
- Large series never enter the agent context by default.
- Every result can be replayed.
- The first verified metric passes the complete correctness loop from frozen input through rendered
  artifact.
- Private values, host delivery, and market-data retention follow the approved user policy.

### Phase 2: Account truth and pre-trade intelligence

Goal: answer "what has my trading cost me, did my decisions help, and what will this next trade do?"

- Closed-position and booking synchronization.
- Cost X-ray.
- Trading mirror over a bounded recent trade set.
- Do-nothing counterfactual.
- Pre-trade impact card integrated with the existing preview flow.
- Bounded session cockpit over holdings, working orders, and a saved Saxo instrument list.
- Return and risk metrics.
- Technical indicators.
- Price and volume charts.
- Relative comparison.
- Trading-condition and cost summary.
- Position sizing from explicit user risk inputs.
- Cost to open, hold, and close using Saxo trading conditions.
- Entitlement-aware quote status.
- Instrument dossier.

Exit criteria:

- One agent call can explain the cost and timing of the user's recent trades.
- Every eligible order preview can include a broker-true impact card.
- One agent call can produce a bounded session cockpit without claiming a live execution terminal.
- One agent call can produce a stamped instrument chart and correct risk summary.
- No material number is calculated by the model.
- The cost result cross-checks against Saxo's pre-trade cost illustration.
- Every enabled Phase 2 analysis kind has a complete per-analysis evidence pack.

### Phase 3: Portfolio intelligence

Goal: answer "how am I doing and why?"

- Performance.
- Benchmark comparison.
- Exposure.
- Drawdown and risk.
- P&L attribution.
- Cost drag.
- Trading mirror.
- Cost X-ray.
- Income.
- Portfolio tearsheet.
- On-demand portfolio briefing and "since yesterday" change narrative.
- Portfolio time machine.
- Settlement and income calendar.
- Multi-account aggregation using safe aliases.

Exit criteria:

- Results cross-check against Saxo's performance and exposure endpoints.
- A private portfolio dashboard is directly deliverable through MCP.
- Benchmark-relative metrics use a clearly identified Saxo instrument proxy.
- Portfolio metrics have no unexplained reconciliation difference outside their exact tolerances.

### Phase 4: Decisions, counterfactuals, and stress

Goal: answer "what happens if?"

- Historical and custom scenarios.
- Margin stress.
- Monte Carlo and goal models.
- Current-versus-proposed portfolio comparison.
- Margin fire drill.
- Index and do-nothing counterfactuals.

Exit criteria:

- Assumptions and stability diagnostics are visible.
- Proposed deltas cannot bypass precheck and approval.
- Scenario and model proof includes calibration, limiting cases, perturbation stability, and
  artifact parity.

### Phase 5: Derivatives and strategy lab

Goal: support advanced traders without creating a black box.

- Options chain and payoff lab.
- Greeks and volatility surfaces.
- IV surface memory.
- Futures and FX carry analysis.
- SIM ghost portfolios and forward testing.
- Bounded historical backtesting after ghost-portfolio validation.
- Minimum-variance and risk-parity optimization.
- Execution-quality analysis.
- Entitled spread and depth analytics.
- Fixed-income analytics where source coverage passes quality gates.

Exit criteria:

- Supported product models are explicitly bounded.
- Backtests pass look-ahead, cost, and holdout gates.
- Options and strategy outputs pass independent reference, Saxo comparison where available, and
  mutation tests for sign, timing, and cost errors.

### Phase 6: Complete on-demand Saxo research platform

Goal: make all supported Saxo analytics continuously useful.

- Multi-account or household views.
- Persistent research notebooks and watchlists.
- Saved Saxo research universes.
- On-demand portfolio, risk, cost, and market reports.
- Storage inspection, deletion preview, and deletion controls.
- Complete installed agent skills, examples, and hard-task evaluations for the active model policy.
- Generated source, metric, proof-profile, tool, artifact, and agent-evaluation coverage catalogs.

Exit criteria:

- Every source value is traceable to a Saxo response and timestamp.
- Runtime shadow checks and schema-drift quarantine have been exercised in recovery tests.
- Every in-scope analysis kind has a complete evidence pack.
- The complete suite passes the final stable-candidate SIM validation.

## Foundation vertical slice

The first vertical slice proves the architecture and changes the user experience, but it is not an
end point for the implementation loop:

1. `saxo_analytics_capabilities`
2. `saxo_resolve_research_universe`
3. `saxo_sync_research_data` for instrument details and chart bars
4. `saxo_sync_research_data` for closed positions, transactions, and bookings
5. Owner-only DuckDB store
6. Versioned analysis result envelope
7. `saxo_analyze_portfolio` with:
   - recent-trade cost breakdown
   - realized cost drag
   - entry and exit timing summary
   - holding-time and winner/loser comparison
8. `saxo_analyze_instruments` with:
   - explicitly labeled price return
   - annualized volatility
   - maximum drawdown
   - rolling return
9. `saxo_render_analysis` with:
   - price and volume
   - equity and drawdown
   - cost and trade-review dashboard
10. `saxo_explain_analysis`
11. Full correctness loop for every first-slice metric, including independent reference,
    accounting identities, exact tolerances, mutation tests, Saxo reconciliation, executable SIM,
    artifact parity, privacy, and hard-model tasks for every harness required by the selected
    candidate policy
12. One source-bound evidence pack and runtime proof-profile registration for every enabled
    first-slice `analysis_kind`

Product demonstration:

> What did my last 20 closed trades cost me, and did my entry and exit timing help or hurt?

The response should include a money-denominated cost X-ray, timing and holding-period comparison,
data-quality warnings, a direct chart, and a replayable analysis identifier. It must describe
observed behavior without pretending that a counterfactual proves causation.

This demonstration runs in SIM and may return SIM money values. LIVE money-denominated cost output
is allowed through `private_user_result` for the authenticated owner on the trusted local host.
Public evidence and logs still contain only fingerprints, schemas, and redacted summaries.

Architecture demonstration:

> Chart Novo Nordisk for the last two years and explain its worst drawdown.

The response should contain a small source-linked answer, an inline chart, explicit data rights and
delay state, the exact return-series definition, and a replayable analysis identifier.

## High-leverage differentiators

### Research-to-execution continuity

An analysis can create a proposed trade specification, but the user still receives the exact
existing preview and approval statement. Research context can be attached to the audit without
weakening action controls.

### Broker-verified cost intelligence

Use the user's actual Saxo conditions and cost illustration rather than generic fee tables.

### Explainable numbers

Every number has an `analysis_id`, formulas, lineage, assumptions, and a replay path.

### Behavioral mirror

Use actual trades and closed positions to show timing, holding, and cost patterns that generic
market sites cannot know.

### Privacy-preserving analytics

The engine can calculate over private values while public evidence contains only fingerprints,
schemas, and pass/fail claims.

### Model-to-broker cross-checks

Compare local performance, exposure, costs, and Greeks with Saxo's corresponding calculations.
Disagreement becomes an insight and quality warning.

## Compounding advantages

The signature experiences create immediate value. These capabilities make that value grow as the
private Saxo history becomes deeper:

1. **Ghost portfolio**: forward-test a strategy in SIM using the same broker rules and costs before
   considering historical backtesting.
2. **Wrapper comparator**: compare Saxo-tradable stock, ETF, CFD, future, or option implementations
   of the same user-selected exposure using actual costs and margin.
3. **FX drag decomposition**: split portfolio return into asset movement and currency translation.
4. **Corporate-action command center**: when the Saxo app has service access, show events,
   deadlines, economic choices, and modeled effects; any election remains a separately approved
   write.
5. **IV surface memory**: retain entitled option-chain snapshots and show changes in skew and term
   structure that Saxo does not provide historically.
6. **Portfolio alerts**: notify only when a computed condition changes materially, not on every
   price tick.
7. **Research notebook**: make every chat analysis an immutable, searchable run with inputs,
   artifacts, conclusions, and later outcomes.
8. **Portfolio observability**: treat profit and loss, risk, cost, data quality, and actions as a
   stream of traceable changes.
9. **Goal-to-order bridge**: model a user goal, derive a bounded proposal, then route it into the
   existing approval kernel.
10. **Explain this chart**: return both pixels and structured chart semantics so the agent can
    explain highlighted periods without guessing from an image.
11. **Ask the portfolio**: use a curated analytics query language to answer novel questions without
    arbitrary SQL or Python.
12. **Settlement radar**: forecast buying power, settlement obligations, income, assignments, and
    expiries across the next trading days.
13. **Model disagreement radar**: flag material differences between local calculations and Saxo's
    corresponding values.

Fixed-income analytics remain conditional. They are exposed only for products where Saxo supplies
enough source data to calculate and cross-check yield, duration, convexity, carry, and rate shocks.

## Ideas to avoid

- Hosted public dashboards containing private Saxo account or market data.
- Market-data calls to external chart-rendering services.
- Black-box price targets.
- Autonomous strategy discovery that can trade.
- Unbounded user or agent code execution.
- Arbitrary DuckDB SQL with file or network access.
- A separate web application before MCP-delivered artifacts prove insufficient.
- Hundreds of one-metric tools.
- Reimplementing mature solvers or complex pricing engines without a strong reason.
- Claims that a backtest, optimizer, or scenario predicts future performance.

## Decisions required before implementation

1. Approved: authenticated owner-only LIVE analytics may return private balances, costs, profit and
   loss, holdings, and money values.
2. Approved: keep Saxo history locally until the owner explicitly deletes it. Provide clear list,
   preview, and deletion controls.
3. Approved: no data collection is required between agent sessions. The MCP fetches and saves data
   when an agent uses it. Scheduled collection remains an optional future MCP tool.

DuckDB is the required local store, deterministic PNG is the first renderer, interactive HTML
follows it, and benchmark comparison uses a disclosed Saxo-tradable proxy or refuses the result.

## Recommended immediate next action

The latest sealed evidence is `06fb8e1`. It passed an exact retained-runtime install with 60 tools,
9 skills, 1 MCP server, and 616 exact files; a signed 2,810-test full suite; static, type, catalog,
evaluation-manifest, privacy, install-readback, and SIM safety gates. The source kept the child
result separate from the final publication, so the terminal child failure remained authenticated
and was not overwritten. The historical dual policy and evidence remain unchanged.

The single sealed native proof passed its child SIM preflight and recorded truthful network
provenance. Ten of eleven hard cases passed. The `scenario` case failed its transcript assertion
despite a passed grant check and exact invocation of both required tools. The outer publication
refused with `proof_child_cleanup_failed`; child cleanup was not started, while retained-runtime and
candidate-runner cleanup completed. Aggregate model, MCP, Saxo, broker-write, mutation, purchase,
and disclaimer-response facts remain unknown. A terminal local readback found no exact-candidate
process, proof run root, or retained proof runtime. No retry ran.

Local diagnostic source `bf72179` adds the evidence needed for the next authorized attempt without
changing the assertion, prompt, final-text parser, cleanup behavior, or historical dual policy. It
separately authenticates content-free raw decoded-event and final parsed-text assertion vectors,
evaluation-cleanup status/counts, and an owner-only PID/process-group/birth-identity/terminal-state
cleanup receipt whose public surface is only a digest. Observation failure remains unknown rather
than being treated as process absence. Ten focused tests passed twice, 233 related tests and 238
auth/privacy tests passed, and type, static, catalog, evaluation-manifest, and bounded privacy gates
were clean. This local source has no exact install or proof and does not relabel `06fb8e1`.

The independent native review should verify the diagnostic source before any exact install or
sealed attempt. A future authenticated run can then prove whether the required phrase existed in a
decoded event but was absent from final parsed text, and whether remaining process identities were
executing, zombies, reused, absent, or unknown. The all-54 numerical proof, Saxo reconciliation,
fresh 60-tool SIM matrix, attempt-bound account equality, and activation decision remain unrun. The
historical matrix remains historical and is not relabeled.

Only after all 54 per-analysis profiles pass may the checked-in proof catalog be regenerated as
active. Activation is a source change, not a runtime toggle. Until that happens, structured
refusal is the correct product behavior for valid unavailable handles. Do not report it as a pass,
do not count input-validation errors as proof of that behavior, and do not substitute non-Saxo data
for missing broker results.

In parallel, write the source-controlled definitions, exact tolerances, known-answer datasets,
independent reference methods, and Saxo reconciliation targets for the first-slice metrics. Do not
begin with optimization or backtesting. First prove:

- Instrument resolution.
- Chart synchronization.
- Performance history.
- Closed-position, transaction, booking, and cost coverage.
- Private-result handling.
- Reproducible analysis envelopes.
- Proof-profile registration and quarantine.
- Independent reference calculation and Saxo reconciliation.
- Direct image delivery.

The foundation vertical slice is not complete until its source-bound correctness evidence pack
passes the full loop. Passing it moves implementation directly into the remaining roadmap phases.
The overall task is not complete until the full-suite completion gate passes.

## Fable controlling brief

The following was passed verbatim to the headless `claude-fable-5` max-effort run:

> After you've completed this round i want you to spawn a a deep exploration cycle on the analytics and BI tools of this MCP. Does this MCP provide ALL that users could dream off, when it comes to market and portfolio reserach? Or could we extend the mcp to do modelling/visualsiations and hand them over directly to the agent, making it way more capable? I want you to dream BIG here. As a part of this deepe xploration i require that you use claude cli and run a fable 5 run on max, where you ask it for ideas where - what could a trader want from an MCP like this? What woul dmake it the ultimate tool for them? Instead of having to browse different webpages and look for and build graphs - the goal is to have the agent be able to show all, analytics call and model all they could dream off. It is a requiremnt that fable is also handed my original prompt, so it knows exactly what the goal of this brainstorm is.
>
> Any questions about this plan before you get going with the research/brainstorm?
