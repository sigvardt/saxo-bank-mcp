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
export SAXO_MCP_LIVE_CLIENT_ID
export SAXO_MCP_LIVE_CLIENT_SECRET
export SAXO_MCP_LIVE_TOKEN_CACHE_PATH
uv run python -m saxo_bank_mcp.qa live-read --out .omo/evidence/saxo-bank-mcp/live-read.json --skip-out .omo/evidence/saxo-bank-mcp/live-read-skipped.json
```

If LIVE credentials are absent, the probe writes a skip artifact. LIVE evidence
must not include raw account identifiers. The probe covers read-only GET tools:
accounts, balances, positions, orders, and prices. Streaming is not claimed by
this read-only proof.

## LIVE Writes

LIVE writes remain disabled until a later explicit live-write enablement plan.
The current refusal checklist intentionally includes:

```bash
SAXO_MCP_ENABLE_LIVE_WRITES=I_UNDERSTAND_REAL_MONEY_RISK
```

That flag alone is not enough. LIVE writes also require live credentials, an
account allowlist, low notional and quantity limits, a ready kill switch, and
two independent approval factors.

## Safety And Audit

Risky SIM writes use a server-created preview token plus a separate approval
factor. Raw audit JSONL lives outside git under the local state directory with
owner-only permissions. Evidence under `.omo/evidence` must be redacted.

Useful probes:

```bash
uv run python -m saxo_bank_mcp.qa approval-happy --out .omo/evidence/saxo-bank-mcp/approval-happy.json
uv run python -m saxo_bank_mcp.qa approval-denied --missing approval-factor --out .omo/evidence/saxo-bank-mcp/approval-denied.json
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
