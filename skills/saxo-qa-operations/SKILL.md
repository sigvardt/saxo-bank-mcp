---
name: saxo-qa-operations
description: Guide Saxo Bank MCP dual-harness QA, matched Codex and Claude evals, SIM matrix evidence, LIVE read/precheck no-purchase proof, isolated plugin installs, release evidence, and plan-only audit prompts.
---

# Saxo QA operations

Use this skill for Saxo Bank MCP evidence planning, dual-client evaluation, install checks, SIM matrix checks, LIVE no-purchase proof, and release assembly.

## Invocation

For installed Codex, invoke this skill as `$saxo-bank-mcp:saxo-qa-operations`.

For Claude, invoke this skill as `/saxo-bank-mcp:saxo-qa-operations`.

Use logical tool IDs in reports. Do not place harness-qualified MCP names in shared prompts or skills.

## Plan-only boundary

Treat plan, explain, review, dry run, do not execute, and evidence-design requests as PLAN-ONLY.

In PLAN-ONLY mode, do not run model commands, MCP tools, Saxo tools, broker calls, browser calls, or cleanup calls. Answer from this skill, its direct references, generated scenario coverage, and local metadata only.

Switch to execution only after the user names the exact command or case set to run. A model-backed or broker-backed run must write an output artifact and must not present skipped or unsupported work as a pass.

## Load references

- Read [dual-harness-operations.md](references/dual-harness-operations.md) before planning or running Codex or Claude evals, plugin installs, exact permission grants, isolated homes, or global-state fingerprints.
- Read [sim-live-evidence.md](references/sim-live-evidence.md) before planning or reviewing SIM state cleanup, unknown outcomes, LIVE read/precheck proof, request ledgers, no-purchase claims, or release evidence.
- Read [scenario-coverage.md](references/scenario-coverage.md) when checking tool coverage. It is generated. Never edit it by hand.

## Evidence required before agent-ready

Require exact dual-client evidence before claiming the Saxo MCP is agent-ready:

- Matched natural prompts across Codex and Claude for every case.
- All 39 logical tools covered by scenario coverage and eval grants.
- Exact tool grants only. No wildcard MCP grants and no broad Claude grants.
- Every created SIM order, preview, token fixture, disclaimer fixture, and stream has cleanup evidence.
- Before and after global-state fingerprints prove real Codex and Claude homes did not change.
- LIVE cases use reads and `saxo_precheck_live_order` only.
- LIVE proof includes a complete non-evicted request ledger, before/after state equality, `live_mutation_calls=0`, and `purchase_occurred=false`.
- No LIVE mutation tool is granted, required, called, or implied.

## Eval suite rules

Cases live under `evals/saxo-bank/*/case.yaml`. They are JSON-compatible YAML so deterministic tooling can parse them without a YAML dependency.

Every case must contain one natural prompt, identical Codex and Claude prompt fields, expected skill, required logical tools, forbidden logical tools, transcript assertions, max turns, bounded timeout, cleanup flag, deterministic graders, and exact logical tool grants per harness.

Forbidden:

- Wildcard grants such as `mcp__plugin_*`.
- Harness prefixes such as `$saxo-bank-mcp:` or `/saxo-bank-mcp:` inside the natural prompt.
- Secrets, raw account IDs, `DisplayName`, or submitted validation values.
- LIVE write tools in any LIVE case.
- Unbounded timeout or missing cleanup for controlled lifecycle tools.
- Claiming a skipped, unsupported, or dry-run case as model, MCP, SIM, or LIVE proof.

Use `scripts/validate_agent_skill_evals.py --all` before any runner.

## Runner contract

Use `scripts/run_dual_harness_skill_evals.py` for matched cases. It supports `--harness codex|claude|both`, `--case`, `--tag`, `--environment`, separate `--codex-plugin-root` and `--claude-plugin-root`, isolated `--codex-home` and `--claude-home`, `--out`, exact permission resolution, global-state fingerprints, and nonzero-on-skip behavior.

Use `--dry-run` only for manifest validation. Dry-run output must say `execution_mode=manifest_validation`, `no_model_call=true`, `no_mcp_call=true`, and `no_saxo_call=true`.

Use `scripts/qa_dual_plugin_install.py` for install and cache evidence. It must record before/after global-state fingerprints and process cleanup.

Use `scripts/run_mcp_tool_matrix.py` for the 39-tool SIM matrix. A manifest validation report is not SIM execution proof.

Use `scripts/assemble_agent_skill_release.py` only after the static, install, SIM, and LIVE task evidence exists and is bound to the candidate commit.

## Failure handling

If validation fails, fix the case or script before running agents.

If a model run times out or is skipped, the case fails. Do not summarize partial output as pass.

If an unknown or post-boundary outcome appears, freeze new writes and route through `saxo-safety-recovery` before retry.

If LIVE proof lacks a complete ledger or state equality, say proof is incomplete. Do not say no purchase occurred.
