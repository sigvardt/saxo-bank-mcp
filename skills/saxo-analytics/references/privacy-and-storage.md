# Privacy and storage

## Visibility

Private values may be communicated only in the authenticated owner context and only when the
server-derived result visibility permits it. Public, log, test, CI, catalog, and review evidence is
value-free. Never expose a raw account ID, raw Saxo or broker identifier, secret, token, raw broker
payload, private path, balance, holding, cost, transaction, or money value.

Use safe account aliases, safe instrument handles, opaque dataset handles, and `analysis_id`.
Handles are references, not proof: replay the stored object and its current source/proof binding.
An expired handle is refused and reacquired through the exact named workflow.

## Artifacts and jobs

Rendering and export must bind to a stored replayable verified or degraded analysis. Preserve
every warning and unavailable dimension of a degraded result. Do not accept caller-provided
provenance, visibility, trust, or proof. Direct delivery is bounded to 25 MiB; larger valid output
uses the server-owned owner-only local resource mode. Never accept a caller path.

Replay checks original captures and every prior analysis dependency as well as the combined
dataset. A saved chart or report becomes unavailable if any required source, result, code proof
or artifact changes; obtain a fresh calculation instead of overriding that check.

Jobs live only while the MCP process runs. Use `saxo_manage_analysis_job` for allowlisted bounded
work, at most the configured concurrency, explicit cancellation, safe restart refusal, and no
partial conclusion. There is no daemon, scheduler, subprocess worker, network service, or
background collector.

## Local deletion

Deletion is owner-local only. First call `saxo_preview_analytics_deletion` with a normalized scope.
Report rows and bytes only in the private owner context. The preview binds dependency closure,
revision, and an expiring single-use token. Call `saxo_delete_analytics_data` only with that exact
current token. On deletion-preview expiry, revision change, ambiguity, or race, refuse and obtain a
new preview. Never construct a path and never imply Saxo or broker data was deleted remotely.
