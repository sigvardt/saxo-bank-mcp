# Incident Cleanup

Use this guide when a SIM or future LIVE operation has unknown state, partial
success, cleanup failure, or unexpected subscription/order residue.

## Immediate Freeze

```bash
uv run python -m saxo_bank_mcp.qa auth-status --out .omo/evidence/saxo-bank-mcp/incident-auth-status.json
uv run python -m saxo_bank_mcp.qa live-write-refusal --out .omo/evidence/saxo-bank-mcp/incident-live-write-refusal.json
```

Do not retry an order write when state is unknown. Preserve the audit record and
move to readback.

## Order Readback

Check the redacted audit event for request id, operation id, write class, and
preview fingerprint. Then read back through the registered portfolio and trade
message paths used by the MCP tool evidence. Treat missing readback as
incomplete, not as safe failure.

## Streaming Cleanup

```bash
uv run python -m saxo_bank_mcp.qa stream-cleanup --simulate-leak --out .omo/evidence/saxo-bank-mcp/incident-stream-cleanup.json
```

If remote cleanup cannot be verified because auth material is missing, record
that fact and ensure the local subscription registry is empty.

## Evidence Hygiene

```bash
uv run python -m saxo_bank_mcp.qa secret-scan --paths README.md docs src tests .omo/evidence/saxo-bank-mcp --out .omo/evidence/saxo-bank-mcp/incident-secret-scan.json
git status --short
```

Evidence may contain only redacted data. Never paste token values, raw account
keys, raw account numbers, or private financial details into the repository.

## Escalation

For SIM, keep verifying and cleaning up without human approval when credentials
and endpoints are SIM-only. For LIVE reads, stop if redaction is uncertain. For
LIVE writes, do not proceed; current tooling must refuse and a later
human-approved live-write plan is required.
