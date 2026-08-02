# Correctness and interpretation

## Proof-first narration

Treat the tool output as the calculation authority. Cite `analysis_id`, state
`verified`/`degraded`/`refused`, and retain every assumption, provenance item, unit, cutoff, delay,
currency, adjustment stamp, quality warning, and limitation. Do not perform replacement math or
silently repair a missing value.

- `verified`: the exact analysis kind, schema, material metric, and current proof binding passed.
- `degraded`: the result is intentionally reduced or proxied; say what it does and does not prove.
- `refused`: no numeric conclusion is available; give the exact capability limitation or next tool.

Never describe development-only `unverified` output as successful. Model probabilities are model
distributions, not forecasts or predictions. Optimization output is a mathematical proposal, not
the user's selected objective or risk tolerance. Position sizing never chooses a risk budget.

## Quality and source checks

- Ambiguity: do not select among materially different instruments, accounts, benchmarks, or units.
- Entitlement gap: preserve `NoAccess` and unavailable fields; never upgrade them to complete.
- Stale data: stale, invalid, or gapped quotes cannot match conditions or support exact claims.
- Schema quarantine: do not infer a meaning for an unknown enum, field, or changed schema.
- Partial history: disclose coverage and refuse identities that need missing boundary observations.
- Approximation: label bar-based arrival/midpoint/spread estimates; exact claims require the bound
  decision-point quote.
- Reconciliation: name Saxo and local differences without averaging disagreement away.

When a current source refresh is explicitly requested, follow the official Saxo
[rate-limit guidance](https://www.developer.saxo/openapi/learn/rate-limiting). Do not add a
collector, retry loop, or alternate data provider.

## Recovery vocabulary

Use these exact recovery classes in user-facing guidance: ambiguity, entitlement gap, stale data,
schema quarantine, large job, expired handle, and deletion-preview expiry. A large job remains
in-process and bounded; progress never contains a partial conclusion. An expired handle requires
replay or reacquisition, not caller reconstruction.
