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
