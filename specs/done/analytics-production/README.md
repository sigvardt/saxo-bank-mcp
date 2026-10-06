# Usable Saxo analytics

Closed 2026-10-05. The working implementation supports 58 analysis kinds: stock and market
research, portfolio accounting, decision models, options, optimisation and historical backtests.
An installed capability means its implementation passed the release checks; a calculation also
needs adequate authenticated source data. These are separate questions.

## Why the release has its own proof

The earlier inventory advertised a planned suite while ordinary requests still reached refusal
paths. Treating an inventory entry or a successful refusal as a completed calculation left that
gap invisible. Production activation therefore binds exact installed code, dependency versions,
source contracts and metric definitions to successful calculation checks. Historical test receipts
keep their original meaning instead of becoming evidence for changed code.

Captures made at different times cannot share one artificial revision. Combined analyses retain
the original datasets and prior analyses as authenticated dependencies. A combined dataset must
not hide invalidation of an input or substitute an older model's account snapshot for current data.

Financial dimensions forced several departures from the initial plan. Native contract amounts
and monetary Greeks retain their contract currency even when the account is in another
currency. Price-dependent decision models check quote freshness at the calculation cutoff,
not merely at capture. Other quote routes retain their capture quality. Option roots
come from authenticated related-root metadata; a root ID is not an underlying instrument ID.
These distinctions prevent apparently valid results that cannot be replayed or applied correctly.

## Principles that must survive future changes

- Source facts come from authenticated Saxo captures. Typed caller choices remain assumptions.
- Missing or inaccessible facts remain unavailable. Account growth cannot stand in for a
  cash-flow-neutral return, and missing depth or settlement data cannot become zero.
- Declared metric and table-cell units and currencies survive storage, replay and export. Source
  quality and model limitations remain bound to the result. Some ancillary table cells have no
  declared unit. Native monetary metrics do not become reporting-currency amounts without FX.
- Explanation, rendering and export authenticate and replay the saved result. They do not
  calculate replacement values or accept caller trust stamps.
- Cancellation and job result commitment share an atomic boundary. Cancelled work publishes no
  partial conclusion; completed work retains its exact result.
- Owner calculations and private delivery work in LIVE. Analytics has no broker-write authority.
  Numerical backtesting does not establish broker execution or future performance.

## Where the enforceable contracts live

- [Release activation](../../../src/saxo_bank_mcp/analytics_release.py),
  [receipt](../../../data/analytics/production_release.json), and
  [runtime dispatch](../../../src/saxo_bank_mcp/analytics_runtime.py).
- [Authenticated inputs](../../../src/saxo_bank_mcp/analytics_runtime_inputs.py),
  [result dimensions](../../../src/saxo_bank_mcp/analytics_models.py), and
  [dependency replay](../../../src/saxo_bank_mcp/analytics_provenance.py).
- [Option ingestion](../../../src/saxo_bank_mcp/analytics_sync.py) and
  [job commitment](../../../src/saxo_bank_mcp/analytics_jobs.py).
- Normal calculation and replay checks:
  [market](../../../tests/test_analytics_market_runtime.py),
  [portfolio](../../../tests/test_analytics_production_portfolio.py),
  [advanced models](../../../tests/test_analytics_advanced_runtime.py), and
  [jobs](../../../tests/test_analytics_production_jobs.py).
- [Stored artifact checks](../../../tests/test_analytics_stored_artifacts.py) and the
  [workflow](../../../skills/saxo-analytics/SKILL.md).

The source contracts deliberately ignore unconsumed additional fields on partial response
schemas while still checking consumed fields and types. Chart schemas remain strict because
unknown revision or adjustment semantics can change a historical calculation. Rejecting every
unmodeled account-response field prevented ordinary LIVE captures; ignoring consumed-field drift
would defeat source proof. The bounded policy preserves both requirements.

## Verification and practical limits

All 58 kinds have successful authenticated-fixture calculations and exact stored replay. The
regression run covered 1,569 tests; nine failures found during the run were repaired and those
exact nine cases passed again. The separate proof and source-boundary checks passed 296 tests.
Lint, type checks and skill/catalog checks passed. A fresh stdio connection using the saved LIVE
configuration reported 58 active profiles, calculated the portfolio overview, replayed it exactly,
and exported an instrument-condition table with exact saved cells. Its complete request ledger
contained eight LIVE gateway GET requests and no order placement.

Saxo denied stock chart history during that check, because the account had not yet accepted
Saxo's market-data terms, and omitted `FundsReservedForSettlement` from the balance response. Those source limitations remain visible; the release does not claim those
specific calculations succeeded. The already-running Codex connection retains the old server
until it reconnects. Fresh connections use the updated saved configuration.

## Visual provenance

These synthetic fixture images document the comparison standard, not a user-uploaded design.
The earlier table layout is preserved at
[desktop](assets/table_baseline_1280_top.png) and
[mobile](assets/table_baseline_375_top.png). It supplied the reference for readable stored
dimensions and exact values. The revised
[mobile table](assets/table_candidate_375_top.png),
[mobile composite](assets/selected_composite_375_top.png), and
[heatmap](assets/selected_heatmap.png) preserve the accepted outcomes for mobile wrapping,
separate monetary axes, and visible heatmap values and units. A fresh independent reviewer
accepted all 61 captures, including six PDF pages, with no P1/P2 findings. Remaining layout
limits are recorded in [visual-review.md](visual-review.md).
