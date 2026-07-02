# Saxo Bank MCP

FastMCP server for Saxo Bank OpenAPI. Todo 1 is local-only: no Saxo endpoint
calls, no credential reads, and simulation mode comes before any live use.

## Local checks

```bash
uv sync --locked --all-extras --dev
uv run pytest
uv run ruff check .
uv run basedpyright
uv run python -c "import saxo_bank_mcp"
```

## Run the server

After Worker A is integrated:

```bash
uv run python -m saxo_bank_mcp
uv run saxo-bank-mcp --transport http --host 127.0.0.1 --port 8000
```

## QA probes

Available now:

```bash
uv run python -m saxo_bank_mcp.qa gitignore-secret --out .omo/evidence/saxo-bank-mcp/task-1-gitignore.json
```

The health probe is available now:

```bash
uv run python -m saxo_bank_mcp.qa health --out .omo/evidence/saxo-bank-mcp/task-1-health.json
```

`saxo_health` verifies local MCP server liveness/readiness only. It does not
verify Saxo connectivity, credentials/session, account access, trading
readiness/order placement, or live write readiness.

## Safety

This repo is SIM-first and live-gated. Do not commit credentials, tokens, raw
account identifiers, `.env` files, logs, or local evidence. Raw Saxo data must
stay outside git; checked-in docs and evidence must be redacted.
