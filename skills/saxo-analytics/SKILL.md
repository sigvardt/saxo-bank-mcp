---
name: saxo-analytics
description: Guide bounded Saxo analytics for market and instrument research, portfolio and Cost X-ray analysis, position sizing, scenarios, optimization, derivatives, backtests, artifacts, jobs, storage, deletion, and research-to-trade precheck. Use for analytics, portfolio, scenario, options, optimization, or backtest requests that must preserve proof, privacy, quality, and the no-broker-write boundary.
---

# Saxo analytics

Use the existing Saxo analytics MCP tools as a thin workflow layer. Do not reproduce financial
formulas, recompute a tool result, or substitute model mental math for the domain service.

## Start and report

Call `saxo_analytics_capabilities` first when module support, source coverage, proof state,
entitlement, delay, or limits are not already current in this workflow. Use logical tool IDs only;
resolve the registered tool whose name ends with that logical ID.

Use the user's selected environment; an explicit LIVE default persists until they request
demo. Analytics computation and private owner delivery work in LIVE without a special mode.
Refresh the installed capability receipt after a server reload or implementation change.

For every analysis response:

- Cite its `analysis_id`.
- Report the production state exactly as `verified`, `degraded`, or `refused`.
- Preserve assumptions, provenance, units, cutoff, currency, delay, adjustment state, missingness,
  and quality warnings.
- Never turn unavailable or reduced inputs into a complete conclusion.
- Do not perform replacement math. Ask the appropriate analytics tool for a revised analysis.

Read [analytics-workflows.md](references/analytics-workflows.md) for the 21 logical tools and the
bounded workflow for portfolio, Cost X-ray, comparison, scenario, options, optimization,
backtest, artifact, job, and deletion tasks.

Read [correctness-and-interpretation.md](references/correctness-and-interpretation.md) before
interpreting numerical results, warnings, proof state, proxies, models, or degraded/refused output.

## Privacy and local state

Communicate private values only in the authenticated owner context and only when the result's
visibility permits it. Public evidence is value-free. Never expose a raw account ID, raw Saxo ID,
token, secret, broker payload, private path, balance, holding, cost, or money value.

Read [privacy-and-storage.md](references/privacy-and-storage.md) for owner-only artifacts, bounded
jobs, safe handles, expiry, storage listing, and revision-bound local deletion.

## Research-to-precheck boundary

Analytics is read/compute and owner-local state only. It has no approval, execution, purchase,
order, broker-write, or disclaimer-response authority. There is no disclaimer response path.

Use `saxo_propose_trade_from_analysis` only after the user explicitly asks to turn one current
analysis into typed preview input and supplies the trading choice and risk inputs. Stop after its
precheck-only result and before every broker write. A later order workflow is a separate explicit
follow-on through `saxo-trading`; never infer that follow-on from the analysis request.

Read [research-to-trade.md](references/research-to-trade.md) whenever a request mentions a trade,
order, action, implementation, or purchase.

## Recovery

Recover by preserving the domain result rather than improvising:

- ambiguity: return candidates or ask for the missing choice;
- entitlement gap: report the unavailable dimension and exact next tool;
- stale data: refuse the affected calculation or resync on explicit request;
- schema quarantine: report the quarantine and do not reinterpret unknown fields;
- large job: use the bounded in-process job tool and publish no partial conclusion;
- expired handle: reacquire or replay through the named source tool;
- deletion-preview expiry: request a new exact preview before deletion.

Do not add a background collector, arbitrary code, expressions, SQL, caller paths, network
callbacks, or any source other than Saxo for market/account data.
