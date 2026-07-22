# Operator Guide

## Start Local Server

```bash
uv sync --locked --all-extras --dev
uv run python -m saxo_bank_mcp --transport stdio
uv run saxo-bank-mcp --transport http --host 127.0.0.1 --port 8000
```

Use stdio for local MCP clients and HTTP only on localhost unless a later
deployment plan adds authentication, TLS, and network policy.

## SIM Login And Status

```bash
uv run python -m saxo_bank_mcp.qa auth-status --out .omo/evidence/saxo-bank-mcp/auth-status.json
uv run python -m saxo_bank_mcp.qa sim-auth --out .omo/evidence/saxo-bank-mcp/sim-auth.json
uv run python -m saxo_bank_mcp.qa token-cache --out .omo/evidence/saxo-bank-mcp/token-cache.json
```

SIM actions may be verified without human input after a valid SIM token cache is
available. If no token cache exists, complete PKCE once in a browser, then rerun
the smoke checks.

## Endpoint Registry

```bash
uv run python -m saxo_bank_mcp.inventory validate --out .omo/evidence/saxo-bank-mcp/inventory.json
uv run python -m saxo_bank_mcp.qa read-smoke --groups all --out .omo/evidence/saxo-bank-mcp/read-smoke.json
uv run python -m saxo_bank_mcp.qa registered-endpoint-denied --method GET --path /not-a-registered-saxo-path --out .omo/evidence/saxo-bank-mcp/unregistered-denied.json
```

Every official Saxo operation must be implemented or refused with a reason. Do
not add arbitrary host, URL, or method escape hatches.

## Safety Config

Keep raw audit logs outside git. Keep token caches outside the repository and
common sync folders. Use the safety status and preview probes before any SIM
write test:

```bash
uv run python -m saxo_bank_mcp.qa approval-happy --out .omo/evidence/saxo-bank-mcp/approval-happy.json
uv run python -m saxo_bank_mcp.qa approval-denied --missing preview-token --out .omo/evidence/saxo-bank-mcp/approval-denied.json
```

LIVE writes are disabled by default. Enablement requires a kill switch, account
allowlist, low limits, and one exact-action approval statement sent by the human
in agent chat. It is single-use and bound to the preview fingerprint. No second
person or second factor is required. The kill switch, allowlists, limits, and
environment are checked again immediately before execution.

## LIVE Read-Only Setup

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
to one hour by default. Leave the command running while completing the browser
login. It must still be running when Saxo redirects the browser to
`http://localhost:8080/callback`. A successful callback stores the LIVE token in
the configured owner-only cache.

After login, keep `uv run saxo-bank-live-session-keeper` running for proactive
refresh. LIVE tools refresh expired tokens on demand. If the computer remains
offline beyond Saxo's refresh-token lifetime, rerun the browser login with
`uv run saxo-bank-live-login`.

For refreshes that must continue without an MCP client, run
`uv run saxo-bank-live-session-keeper`. The primary macOS workstation has this
installed at
`~/Library/LaunchAgents/com.saxobank.mcp-live-session.plist` with
`RunAtLoad` and `KeepAlive`. Check or restart it with:

```bash
launchctl print gui/$(id -u)/com.saxobank.mcp-live-session
launchctl kickstart -k gui/$(id -u)/com.saxobank.mcp-live-session
```

The keeper refreshes only valid cached LIVE sessions. It deliberately waits
when the cache is missing or Saxo rejects an expired refresh token; complete one
new browser login and it will resume automatically.

Use this flow for an agent precheck:

1. Call `saxo_list_live_accounts`.
2. Select an active account by its visible `account_id` or process-scoped
   `account_ref` and pass that selector to `saxo_precheck_live_order`.

If exactly one active account exists, `saxo_precheck_live_order` auto-selects it
when both selectors are omitted. Multiple active accounts require one explicit
selector. `saxo_list_live_accounts` intentionally returns Saxo's `account_id`,
plus an opaque process-scoped reference, so agents can identify and target
accounts without guessing. Technical account and client keys remain internal.
Persistent evidence redacts the visible account ID. Access and refresh tokens
are never returned.

`precheck_accepted` means Saxo accepted only the read-only precheck request. It
does not approve an order or assess whether a trade is ready;
`trade_readiness` always remains `not_assessed`. Qualifiers and pre-trade
disclaimers are blocking tool errors. `saxo_precheck_live_order` cannot place,
change, or cancel an order, or respond to a disclaimer.

Agent-run prechecks set `ManualOrder=false` because no person has confirmed an
order for transmission. A LIVE placement may set `ManualOrder=true` only
after the exact order receives the required human approval in agent chat.
Precheck acceptance is never that approval.

For agent-visible request evidence, call `saxo_get_safe_request_ledger` with
`clear=true` before the task and without `clear` after it. The result is scoped
to the current MCP session and contains only safe request metadata, including
allowlisted Saxo query parameter names but never values. Unapproved names are
redacted. If the retention limit evicts an event,
the tool marks the ledger incomplete and refuses negative proof. For account
money-state comparisons, `saxo_call_registered_endpoint` requires
`response_mode=fingerprint_only`; body mode is denied before networking. The
fingerprint scope is `account_money_state_fields`. It covers a modeled,
validated set of money-state fields without exposing identifiers or monetary
values; no-change proof also requires complete transport evidence with no write.

Run the no-purchase LIVE proof through the FastMCP protocol client:

```bash
uv run saxo-bank-live-precheck-proof \
  --allow-live \
  --out .omo/evidence/saxo-bank-mcp/live-precheck/proof.json \
  --uic 30031 --asset-type Stock --amount 1 --buy-sell Buy \
  --account-position 1
```

The command clears and reads the exposed MCP session ledger and separately
captures outbound HTTP through a transport wrapper feeding an out-of-process
collector. The collector inherits no credentials and does not consume ledger
events. The proof aborts unless all three traces match. They must
contain only the exact account-wide state reads, one instrument-tradability
read, at most a token refresh in the outer trace, and one
`/trade/v2/orders/precheck` POST. Orders, positions, and balances use the
account-wide `/me` endpoints, so no technical account or client keys enter the
read calls. The operator-selected strict contract requires
`__count` on orders and positions, and each count must match returned rows.
Missing or inconsistent counts fail closed. Orders and positions must use
Saxo's `Data` envelope. LIVE trade messages are currently returned as a
top-level array; the proof validates that endpoint separately and records each
endpoint's actual shape and count semantics. A shape mismatch aborts with
`state_collection_shape_invalid` before precheck.
The proof supports nonempty portfolios when orders, positions, trade messages,
and the `account_money_state_fields` fingerprint are unchanged before and after.
It binds the accepted precheck to the selected account and exact instrument,
amount, direction, order type, `ManualOrder=false`, and requested field groups.
It requires HTTP 200, an explicit `Ok` root and every returned child result, no
error object, and no disclaimer object, including an empty one. The sanitized
artifact records these structural facts without retaining the raw response. It
rejects duplicate JSON members and non-finite numbers before model validation,
uses strict scalar validation at safety boundaries, rejects acceptance-signal
keys nested in free-form qualifier objects, including every normalized key
containing `error`, `disclaim`, `order`, or `result`, rejects unknown
accepted-precheck fields, and refuses an otherwise valid FastMCP result when
the protocol result is error-flagged. Decoder nesting and integer-size limit
failures are normalized to the same structured `invalid_precheck_response`
result as other malformed upstream responses. Separate-process collector output
passes through duplicate-safe strict JSON parsing before typed event validation.
It also requires unchanged order, position, trade-message, and modeled account
money-state fingerprints plus complete no-write transport evidence, complete
dirty-file provenance including endpoint policy data, and a clean artifact
secret scan. Proof and gate artifacts are scanned in
memory before an atomic publish; no unscanned candidate or backup is written. An
abort records a sanitized stage and reason.

The public secret scanner permits known fixtures only when no character allowed
inside a detected credential touches either side. A fixture marker cannot erase
an attached prefix or suffix. Email-pattern fixtures suppress only their own
span rather than the rest of the line.

Do not record raw account identifiers in evidence. If credentials are absent,
keep the skip artifact and do not treat LIVE reads as verified.

## LIVE Write Validation Plan

SIM proves the MCP implementation against Saxo's demo environment. It does not
prove LIVE readiness by itself because Saxo documents separate LIVE app
credentials, separate auth hosts, real balances, required permission/testing,
and possible SIM/LIVE differences in version, reporting, and market data:
https://www.developer.saxo/openapi/learn/environments

This plan does not authorize a LIVE write. Keep writes disabled until the
operator starts this separate phase and approves the exact real-money test.

The production order and registered Trading-write paths are implemented. SIM
verification does not replace the separate LIVE validation below. Required
disclaimer responses use `saxo_register_disclaimer_response` for the preview and
`saxo_execute_trading_write` after the exact one-chat approval.

Treat the move to LIVE writes as a staged proof:

1. Configure the LIVE app key, redirect URL, and token cache outside the
   repository.
2. Run LIVE read-only auth and account probes with `SAXO_MCP_ENABLE_LIVE_READS=1`.
3. Run the separate `live-read` proof for accounts, balances, positions,
   orders, and prices without writing or placing orders. Streaming is a
   subscription surface, not part of that GET proof.
4. Keep LIVE writes disabled until the read-only evidence is clean.
5. Enable LIVE writes only with all gates present: explicit live-write flag,
   account allowlist, low value and quantity limits, kill switch, server-created
   preview token, and one exact-action human approval statement in agent chat.
6. Run precheck/defaults first. Do not place an order until precheck evidence is
   clean and the exact account, instrument, side, size, and order type are
   approved.
7. Place one minimal real-money test order only after explicit approval, then
   verify order state, events, account/position impact, and redacted audit logs.
   Prefer an accepted-then-cancelled order if that proves the write path without
   an intended fill.
8. Run the tribunal loop again against the LIVE-tested tools. A tool is not
   live-write-ready until tribunal has exercised it with the real tool behavior
   and has no remaining safety or agent-use feedback.

## Tribunal Loop

```bash
uv run python -m saxo_bank_mcp.tribunal_index --out .omo/evidence/saxo-bank-mcp/tribunal-index.json
```

For each completed tool, keep the MCP schema, input, output, audit, tribunal
run, fixed-feedback notes, and empty remaining-actionable-feedback state.

## Final Verification

```bash
uv run python -m saxo_bank_mcp.qa prod-readiness --out .omo/evidence/saxo-bank-mcp/prod-readiness.json
uv run python -m saxo_bank_mcp.final_verify plan --plan .omo/plans/saxo-bank-mcp.md --out .omo/evidence/saxo-bank-mcp/final-plan-compliance.md
uv run python -m saxo_bank_mcp.final_verify code --out .omo/evidence/saxo-bank-mcp/final-code-quality.md
uv run python -m saxo_bank_mcp.final_verify mcp --out .omo/evidence/saxo-bank-mcp/final-manual-qa.md
uv run python -m saxo_bank_mcp.final_verify scope --out .omo/evidence/saxo-bank-mcp/final-scope-fidelity.md
```

`prod-readiness` is a code gate, not live-money approval. Inspect
`live_read_ready`, `live_write_ready`, `does_not_verify`, and `next_action`
before any LIVE operation.
