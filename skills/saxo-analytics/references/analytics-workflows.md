# Analytics workflows

Use one typed domain route at a time. Start with `saxo_analytics_capabilities` when current
capability, entitlement, delay, proof, or limit state is unknown. Saxo is the only named source for
market and account data.

## Logical tool catalog

This table is generated and checked by `scripts/generate_agent_skill_catalogs.py` against the
current 60-tool FastMCP catalog.

| Logical tool | Purpose |
| --- | --- |
| `saxo_analytics_capabilities` | Read installed modules, limits, formats, source coverage, and proof quarantine. |
| `saxo_resolve_research_universe` | Resolve bounded user language to safe instrument handles and ambiguities. |
| `saxo_manage_research_universe` | Create, list, revision-update, or delete one owner-local universe. |
| `saxo_sync_research_data` | Perform one on-demand bounded Saxo read into normalized owner-local data. |
| `saxo_get_research_dataset` | Read bounded source lineage, quality, and safe dataset pages. |
| `saxo_analyze_market` | Analyze an explicit bounded universe, market state, depth, or saved condition. |
| `saxo_analyze_instruments` | Analyze or compare safe instruments with source-bound inputs. |
| `saxo_analyze_portfolio` | Run accounting, attribution, exposure, income, cost, liquidity, or trade review. |
| `saxo_size_position` | Size only from a caller-supplied confirmed risk budget. |
| `saxo_run_scenario` | Run explicit accepted numeric shock maps or return a proposal for confirmation. |
| `saxo_optimize_portfolio` | Return constrained mathematical deltas and stability diagnostics only. |
| `saxo_model_derivatives` | Run supported bounded derivatives models and exact capability refusals. |
| `saxo_backtest_strategy` | Run declarative bounded research with costs, splits, and limitations. |
| `saxo_propose_trade_from_analysis` | Produce current-analysis-bound typed preview input only. |
| `saxo_render_analysis` | Replay a stored verified analysis and render an approved template. |
| `saxo_export_analysis` | Replay and export exact bound values in an approved format. |
| `saxo_explain_analysis` | Return definitions, lineage, assumptions, warnings, and proof binding. |
| `saxo_manage_analysis_job` | Start, inspect, or cancel one allowlisted in-process bounded job. |
| `saxo_list_analytics_storage` | List value-free owner-local storage metadata. |
| `saxo_preview_analytics_deletion` | Preview exact revision-bound dependency closure and issue an expiring token. |
| `saxo_delete_analytics_data` | Consume that token once to delete only the exact owner-local closure. |

## Bounded task routes

- Portfolio briefing: capabilities, resolve ambiguity, sync only if requested, then
  `saxo_analyze_portfolio`; explain or render the same stored `analysis_id` when needed.
- Cost X-ray: use the portfolio cost analysis. Preserve currencies, booking corrections, partial
  fills, quote basis, approximation labels, and Saxo reconciliation differences.
- Market comparison: resolve an explicit universe, obtain source-bound data, then use
  `saxo_analyze_instruments` or `saxo_analyze_market`. Never call a bounded list the whole market.
- Scenario: numeric shocks must be explicit. Narrative text may propose numbers, but do not call
  `saxo_run_scenario` until the user echoes and accepts those numbers.
- Options: use `saxo_model_derivatives`; report exact unsupported-product or entitlement refusal.
- Optimization: require the caller's objective, bounds, and risk tolerance. Report deltas and
  stability diagnostics, not a recommendation.
- Backtest: accept declarative strategy input only. State survivorship, source, split, cost, fill,
  and SIM-verification limitations; never call a backtest a forecast.
- Artifact delivery: replay the same verified stored result. Use direct delivery through the
  configured limit and an owner-only resource link above it; never accept caller trust evidence.
- Deletion: list, exact preview, then consume the current single-use revision-bound token. Preview
  expiry requires a new preview.
- Research-to-precheck: analyze, then use `saxo_propose_trade_from_analysis` only on an explicit
  request. Stop before broker write and do not call an order or disclaimer tool.

For Saxo chart semantics and source limitations, use the reviewed official
[chart documentation](https://www.developer.saxo/openapi/learn/chart) and
[reference-data documentation](https://www.developer.saxo/openapi/learn/reference-data). The MCP
contracts remain authoritative for which source coverage is actually implemented.
