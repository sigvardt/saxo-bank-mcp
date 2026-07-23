# Dual-harness operations

## Shared prompts

Each eval case uses the same natural prompt for Codex and Claude. Do not embed `$saxo-bank-mcp:` or `/saxo-bank-mcp:` in the natural prompt. Harness-specific invocation belongs to the runner command, not the case prompt.

## Exact grants

Grant only the logical tools named by the case. The runner resolves them to harness-specific names and records the resolved list in the output artifact.

Never use wildcard grants. Reject `*`, `?`, `mcp__plugin_*`, broad Claude grants, and any grant not found in generated scenario coverage.

## Isolated homes

Install and eval commands must use task-owned homes:

- `--codex-home` for Codex.
- `--claude-home` or isolated `HOME` for Claude.
- Separate Codex and Claude plugin roots.
- Explicit `--out` path for every command.

Capture before and after fingerprints for real global Codex and Claude state when an install check touches plugin setup. Equality is required unless the command explicitly works only inside isolated roots.

## Runner modes

`manifest_validation` checks cases, grants, coverage, and command shape. It is not model evidence.

`model_execution` runs the selected client command and applies deterministic transcript assertions. It fails on timeout, nonzero exit, skipped case, forbidden transcript text, or missing required text.

## Native command shapes

Codex uses `codex exec --json --cd <plugin-root> <prompt>`.

Claude uses `claude --plugin-dir <plugin-root> --permission-mode plan --allowedTools <exact-list> --output-format json --print <prompt>` for plan-only cases.

The full model-backed suite must not run in Todo 12. Todo 12 records only schema, dry-run, fixture, and plan-only QA evidence.
