# Task 6 official cancellation correction report

## Status

The offline stable replacement candidate is complete. The failed candidate
`cdb5abd68e41045631ef029dbba7876eab39288b1e912f72228935ee25eebed3` remains
immutable incident history. Its guard and evidence were not inspected, deleted, replaced, reused,
or retried.

No authentication, Saxo endpoint, browser, LIVE or SIM endpoint, disclaimer, official guard,
official evidence, or official source-matrix run was used during this correction.

## Root cause and correction

The real full-child path completed its matrix calls, cleanup, process reaping, post-exit seal
validation, and minimal failure publication. Cancellation was then deliberately re-raised so that
direct async callers could observe it. The synchronous CLI boundary only caught `Exception`, while
`asyncio.CancelledError` is a `BaseException`, so the installed command leaked a traceback after
the correctly completed cancellation path.

The correction adds one CLI-only async wrapper that catches AnyIO's active cancellation class and
returns a nonzero status. The direct async coordinator is unchanged, still publishes exactly one
minimal failure after claim, and still propagates cancellation to its caller. One child, one MCP
session, one initialize, one tool list, zero reconnects, zero restarts, bounded cleanup, child
reaping, post-exit seal validation, failure precedence, privacy, and one-shot guard behavior remain
unchanged.

## TDD evidence

- Strict RED: the new full raw-MCP fixture-child regression failed once with
  `asyncio.exceptions.CancelledError` escaping `main([])` and a traceback reaching the CLI.
- Focused GREEN: the exact regression passed after the CLI-only wrapper was added.
- The direct async half still raises `asyncio.CancelledError` after publishing exactly
  `{"reason":"child_process_failed","status":"failed"}`.
- The CLI half returns nonzero with empty stdout and no traceback or private runtime path.

## Replacement candidate

- schema: `5`
- candidate identity:
  `f346a26ea2b98f6371599e262f27b70101b28692ae80db1b0c03d7acc0b1af0a`
- portable runtime tree SHA-256:
  `e410556eefcdeaad17045d484fd0ac4f1f0b3f0b528132bdbf9ed3810b8bffa0`
- portable runtime entries: `12500`
- manifest SHA-256:
  `c2d6cd8497ff6e08a899072f51f1b1dad2ff67217666a9505cc5388273826bf4`
- final wheel SHA-256:
  `50085823366924bf5e9e7f1513e4cc5a74683b7dc40a849fb1ae3a5feb8da4fa`
- checked manifest, final manifest A, and final manifest B: byte-identical
- final runtime A and B candidate identities: identical
- final runtime A and B roots: owner-only and sealed with mode `0500`

Retained local-only artifacts, all relative to this report directory:

- `task-6-official-cancellation-artifacts/bootstrap-wheel`
- `task-6-official-cancellation-artifacts/bootstrap-runtime`
- `task-6-official-cancellation-artifacts/final-wheel`
- `task-6-official-cancellation-artifacts/final-runtime-a`
- `task-6-official-cancellation-artifacts/final-runtime-b`
- `task-6-official-cancellation-artifacts/final-manifest-a.json`
- `task-6-official-cancellation-artifacts/final-manifest-b.json`

## Verification

- focused process-boundary and Task 6 suite: `153 passed`
- deterministic installed fixture proof: `2 passed`
- installed `--help`: passed from an unrelated owner-only working directory
- installed `--identity`: passed in both final runtimes with the exact candidate identity
- installed `--preflight`: passed in both final runtimes using a fresh owner-only temporary token
  path outside the repository, without authentication or network access
- exact `uv run basedpyright`: `0 errors, 0 warnings, 0 notes`
- `uv lock --check --offline`: `103` packages resolved
- changed-scope Ruff: passed
- forbidden live-object transport and retired projection-name search: passed
- changed-file and report privacy scan: `0` findings, `0` errors
- `git diff --check`: passed

The replacement remains an offline candidate. No official attempt is part of this correction.
