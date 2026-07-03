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
common sync folders. Use the safety status and approval probes before any SIM
write test:

```bash
uv run python -m saxo_bank_mcp.qa approval-happy --out .omo/evidence/saxo-bank-mcp/approval-happy.json
uv run python -m saxo_bank_mcp.qa approval-denied --missing approval-factor --out .omo/evidence/saxo-bank-mcp/approval-denied.json
```

LIVE writes are disabled by default. A future live-write plan must provide a
kill switch, account allowlist, low limits, and two independent approval
factors.

## LIVE Read-Only Setup

```bash
export SAXO_MCP_ENVIRONMENT=LIVE
export SAXO_MCP_ENABLE_LIVE_READS=1
export SAXO_MCP_LIVE_CLIENT_ID
export SAXO_MCP_LIVE_CLIENT_SECRET
export SAXO_MCP_LIVE_TOKEN_CACHE_PATH
uv run python -m saxo_bank_mcp.qa live-read --out .omo/evidence/saxo-bank-mcp/live-read.json --skip-out .omo/evidence/saxo-bank-mcp/live-read-skipped.json
```

Do not record raw account identifiers in evidence. If credentials are absent,
keep the skip artifact and do not treat LIVE reads as verified.

## SIM To LIVE Readiness Plan

SIM proves the MCP implementation against Saxo's demo environment. It does not
prove LIVE readiness by itself because Saxo documents separate LIVE app
credentials, separate auth hosts, real balances, required permission/testing,
and possible SIM/LIVE differences in version, reporting, and market data:
https://www.developer.saxo/openapi/learn/environments

Treat the move to LIVE as a staged proof:

1. Configure LIVE app key, app secret, redirect URL, and token cache outside the
   repository.
2. Run LIVE read-only auth and account probes with `SAXO_MCP_ENABLE_LIVE_READS=1`.
3. Verify LIVE reads for accounts, balances, positions, orders, prices, and
   streaming without writing or placing orders.
4. Keep LIVE writes disabled until the read-only evidence is clean.
5. Enable LIVE writes only with all gates present: explicit live-write flag,
   account allowlist, low value and quantity limits, kill switch, server-created
   preview token, and two independent approval factors.
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
uv run python -m saxo_bank_mcp.final_verify plan --plan .omo/plans/saxo-bank-mcp.md --out .omo/evidence/saxo-bank-mcp/final-plan-compliance.md
uv run python -m saxo_bank_mcp.final_verify code --out .omo/evidence/saxo-bank-mcp/final-code-quality.md
uv run python -m saxo_bank_mcp.final_verify mcp --out .omo/evidence/saxo-bank-mcp/final-manual-qa.md
uv run python -m saxo_bank_mcp.final_verify scope --out .omo/evidence/saxo-bank-mcp/final-scope-fidelity.md
```
