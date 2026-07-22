# Saxo Bank MCP

Python FastMCP server for Saxo Bank OpenAPI. The server is SIM-first, LIVE
read-only only when explicitly configured, and LIVE writes are disabled by
default.

## Local Setup

```bash
uv sync --locked --all-extras --dev
uv run pytest
uv run ruff check src tests
uv run basedpyright src tests
uv run python -c "import saxo_bank_mcp"
```

Run the local MCP server:

```bash
uv run python -m saxo_bank_mcp --transport stdio
uv run saxo-bank-mcp --transport http --host 127.0.0.1 --port 8000
```

## SIM Auth

SIM is the default environment. Keep credentials in the local credential file or
environment; do not copy values into this repo, docs, logs, or evidence.

```bash
uv run python -m saxo_bank_mcp.qa auth-status --out .omo/evidence/saxo-bank-mcp/auth-status.json
uv run python -m saxo_bank_mcp.qa sim-auth --out .omo/evidence/saxo-bank-mcp/sim-auth.json
uv run python -m saxo_bank_mcp.qa health --out .omo/evidence/saxo-bank-mcp/health.json
```

If no token cache exists, complete PKCE in a browser using the redacted
authorization URL flow. After a valid SIM token cache exists, SIM verification
may run without human input.

## Common MCP Smoke

```bash
uv run python -m saxo_bank_mcp.qa read-smoke --groups all --out .omo/evidence/saxo-bank-mcp/read-smoke.json
uv run python -m saxo_bank_mcp.qa trade-precheck --out .omo/evidence/saxo-bank-mcp/trade-precheck.json
uv run python -m saxo_bank_mcp.qa live-write-refusal --out .omo/evidence/saxo-bank-mcp/live-write-refusal.json
```

The registered endpoint tool is registry-backed only. It is not an arbitrary
HTTP proxy.

## LIVE Read-Only

LIVE reads require all live-read settings and a LIVE token cache outside the
repository:

```bash
export SAXO_MCP_ENVIRONMENT=LIVE
export SAXO_MCP_ENABLE_LIVE_READS=1
export SAXO_MCP_LIVE_CREDENTIAL_FILE="$HOME/Desktop/saxo_bank_mcp_LIVE_credentials.txt"
export SAXO_MCP_LIVE_TOKEN_CACHE_PATH
export SAXO_MCP_LIVE_REDIRECT_URI=http://localhost:8080/callback
uv run saxo-bank-live-login
uv run python -m saxo_bank_mcp.qa live-read --out .omo/evidence/saxo-bank-mcp/live-read.json --skip-out .omo/evidence/saxo-bank-mcp/live-read-skipped.json
```

`uv run saxo-bank-live-login` keeps the localhost callback receiver open for up
to one hour by default. The command must still be running when Saxo redirects
the browser to `http://localhost:8080/callback`. After the callback succeeds,
the command stores the LIVE token in the configured owner-only cache.

The external `saxo-bank-live-session-keeper` refreshes the LIVE token before
expiry. LIVE tools refresh an expired access token on demand under the same
cross-process lock. If the computer remains offline beyond Saxo's refresh-token
lifetime, rerun the browser login with `uv run saxo-bank-live-login`.

To keep the session alive independently of an MCP client, run
`uv run saxo-bank-live-session-keeper`. On the primary macOS workstation this
command is installed as the `com.saxobank.mcp-live-session`
LaunchAgent with `RunAtLoad` and `KeepAlive`; it restarts after login or a crash.
It can refresh a valid cached session, but it cannot recover a refresh token
that expired while the keeper was stopped. That case requires one new browser
login.

For an agent precheck, call `saxo_list_live_accounts` first and select an active
account by visible `account_id` or process-scoped `account_ref` in the `order`
passed to `saxo_precheck_live_order`. If exactly one active account exists,
omitting both selectors auto-selects it. The account tool intentionally exposes
Saxo's visible account ID for agent selection. Technical account and client keys
remain internal. Persistent proof artifacts redact the visible account ID.

`precheck_accepted` means Saxo accepted only the read-only precheck request.
`trade_readiness` always remains `not_assessed`. Qualifiers and pre-trade
disclaimers are blocking tool errors. `saxo_precheck_live_order` cannot place,
change, or cancel an order, or respond to a disclaimer.
It verifies instrument tradability and sends `ManualOrder=false`, because an
unattended agent precheck is not a human-confirmed order. A LIVE order
may use `ManualOrder=true` only after the human confirms the exact order once in
the agent chat.

For a task that needs safe request evidence, call
`saxo_get_safe_request_ledger` with `clear=true` before the task and without
`clear` afterward. It returns only HTTP methods, sanitized paths, completion
status, and a summary of non-GET calls for the current MCP session. It never
stores headers, bodies, tokens, account identifiers, instrument identifiers, or
balances. Only allowlisted Saxo query parameter names are visible; other names
and every value are redacted. Ledger overflow disables negative proof. Balance
operations require
`response_mode=fingerprint_only`; body mode is denied before networking, and
the fingerprint covers a modeled set of validated account cash-state fields.

Create a fail-closed LIVE proof of a single read-only precheck with:

```bash
uv run saxo-bank-live-precheck-proof \
  --allow-live \
  --out .omo/evidence/saxo-bank-mcp/live-precheck/proof.json \
  --uic 30031 --asset-type Stock --amount 1 --buy-sell Buy \
  --account-position 1
```

The proof succeeds only when the exposed MCP session ledger and in-process
audit trace match a sanitized transport-boundary trace collected by a separate
process. Account-wide `/me` reads require no technical account or client keys;
orders and positions must include `__count`, and it must match returned rows. Nonempty collections
are supported when their fingerprints and counts are unchanged before and after.
Orders and positions require Saxo's `Data` envelope. LIVE trade messages use
their observed top-level array shape. The artifact records endpoint-specific
shape and declared-count telemetry, and any mismatch aborts before precheck.
The sole POST is precheck. Saxo must report HTTP 200, a tradable instrument, an
explicit `Ok` root and every returned child result, and no error or disclaimer
object. The request uses `ManualOrder=false`. The no-change conclusion combines
an unchanged modeled money-state fingerprint with complete request-ledger and
transport evidence showing no write operation; it does not claim every possible
Saxo balance field was modeled. Duplicate JSON members, non-finite numbers,
coercible safety fields,
nested error/disclaimer/order/result signals, unknown precheck fields, and
error-flagged FastMCP results are rejected before acceptance. JSON decoder
resource-limit failures return the same structured `invalid_precheck_response`
result as other malformed upstream responses. Collector output also passes
duplicate-safe strict JSON parsing before event validation.
Dirty-file provenance must be complete, and the artifact must pass
an in-memory secret scan before atomic publication. Aborted proofs include a
sanitized stage and reason.

If LIVE credentials are absent, the separate `live-read` probe writes a skip
artifact. LIVE evidence must not include raw account identifiers. The
`live-read` probe covers read-only GET tools: accounts, balances, positions,
orders, and prices. Streaming is not claimed by that probe.

## LIVE Writes

LIVE writes are disabled by default and require explicit runtime enablement:

```bash
SAXO_MCP_ENABLE_LIVE_WRITES=I_UNDERSTAND_REAL_MONEY_RISK
```

That flag alone is not enough. LIVE writes also require live credentials, an
account allowlist, low notional and quantity limits, a ready kill switch, and
one exact-action approval statement sent by the human in the agent chat. The
statement is bound to the request fingerprint, expires with the preview, and is
single-use. The kill switch, allowlists, limits, and environment are checked
again immediately before execution. No second person or second approval factor
is required.

Use `saxo_create_order_preview` before placement, then the matching production
order tool. For a required disclaimer, `saxo_register_disclaimer_response`
returns a LIVE preview and exact approval statement; after approval, execute it
once with `saxo_execute_trading_write` before repeating the order precheck.

## Safety And Audit

SIM writes use a server-created preview token and do not require human input.
Raw audit JSONL lives outside git under the local state directory with
owner-only permissions. Evidence under `.omo/evidence` must be redacted.

Useful probes:

```bash
uv run python -m saxo_bank_mcp.qa approval-happy --out .omo/evidence/saxo-bank-mcp/approval-happy.json
uv run python -m saxo_bank_mcp.qa approval-denied --missing preview-token --out .omo/evidence/saxo-bank-mcp/approval-denied.json
uv run python -m saxo_bank_mcp.qa stream-cleanup --simulate-leak --out .omo/evidence/saxo-bank-mcp/stream-cleanup.json
```

If an order state is unknown, do not retry blindly. Use readback evidence from
portfolio orders and trade messages, preserve the redacted audit trail, and
follow [Incident Cleanup](docs/incident-cleanup.md).

## Tribunal Loop

Every completed MCP tool needs real MCP evidence and a tribunal completion or
honest incomplete/refusal artifact.

```bash
uv run python -m saxo_bank_mcp.tribunal_index --out .omo/evidence/saxo-bank-mcp/tribunal-index.json
```

Tribunal feedback is implementation input. Current-stage safety or agent-use
feedback must be fixed before a tool is marked complete.

## Final Verification

F1-F4 must use only these final verification commands:

```bash
uv run python -m saxo_bank_mcp.final_verify plan --plan .omo/plans/saxo-bank-mcp.md --out .omo/evidence/saxo-bank-mcp/final-plan-compliance.md
uv run python -m saxo_bank_mcp.final_verify code --out .omo/evidence/saxo-bank-mcp/final-code-quality.md
uv run python -m saxo_bank_mcp.final_verify mcp --out .omo/evidence/saxo-bank-mcp/final-manual-qa.md
uv run python -m saxo_bank_mcp.final_verify scope --out .omo/evidence/saxo-bank-mcp/final-scope-fidelity.md
```

Before pushing publicly:

```bash
uv run python -m saxo_bank_mcp.qa prod-readiness --out .omo/evidence/saxo-bank-mcp/prod-readiness.json
uv run python -m saxo_bank_mcp.qa secret-scan --paths README.md docs src tests --out .omo/evidence/saxo-bank-mcp/public-secret-scan.json
git status --short
```

Treat `prod-readiness` as a code gate, not live-money approval. Inspect
`live_read_ready`, `live_write_ready`, `does_not_verify`, and `next_action`
before any LIVE operation.
