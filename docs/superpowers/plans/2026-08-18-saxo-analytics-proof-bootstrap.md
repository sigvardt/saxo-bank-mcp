# Codex-native proof bootstrap implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Authenticate native proof startup before producer import, authenticate the complete native
publication, and retain exact private full-suite evidence for the next candidate.

**Architecture:** A direct-file stdlib bootstrap owns durable startup evidence, then hands off to
the existing producer phase tracker. The parent validates both layers and publishes one strict
digest-bound native envelope. A separate local runner retains owner-only JUnit and command evidence.

**Tech stack:** Python 3.12 standard library, Pydantic, pytest through `scripts/run-pytest`, Ruff,
BasedPyright.

## Global constraints

- Do not invoke Claude, another model, a browser, Saxo, LIVE, trades, purchases, disclaimers, or a
  subagent during local corrective work.
- Run every pytest command through `scripts/run-pytest` with the required external temp variables.
- Preserve the legacy `dual_v1` path and old evidence without relabeling.
- Never persist raw child output, secrets, identifiers, handles, paths in public output, or broker
  payloads.
- Freeze one clean candidate before one install, one full suite, and one sealed proof attempt.

### Task 1: Add RED bootstrap subprocess tests

**Files:**
- Create: `tests/test_analytics_proof_bootstrap.py`

- [x] Test import failure retains a sanitized authenticated no-execution receipt.
- [x] Test producer argument `SystemExit` and pre-tracker exception retain safe typed receipts.
- [x] Test initial envelope-write failure prevents producer import.
- [x] Test hard child crash retains the last durable unknown-outcome handoff.
- [x] Test normal nonzero and success receipts, owner-only modes, and no raw failure text.
- [x] Run the file once and record RED before implementation.

### Task 2: Implement and integrate the stdlib bootstrap

**Files:**
- Create: `src/saxo_bank_mcp/qa_analytics_proof_bootstrap.py`
- Modify: `src/saxo_bank_mcp/qa_analytics_proof_failure.py`
- Modify: `src/saxo_bank_mcp/qa_analytics_proof_producer.py`
- Modify: `tests/test_analytics_proof_failure_envelope.py`

- [x] Implement durable atomic entry, fixed reason codes, exact byte binding, and state transitions.
- [x] Validate file ownership, mode, digest, policy, candidate, install, module, catalog, and contract.
- [x] Invoke the bootstrap in the native sealed command and require it on success and failure.
- [x] Merge pre-import facts with the existing child phase envelope without false negatives.
- [x] Preserve legacy commands and successful producer result validation.

### Task 3: Authenticate the complete native publication

**Files:**
- Create: `src/saxo_bank_mcp/qa_analytics_proof_publication.py`
- Modify: `scripts/run_analytics_proof_matrix.py`
- Modify: `tests/test_analytics_proof_failure_envelope.py`

- [x] Add RED round-trip, unsigned-extra, nested-binding, and digest-tamper tests.
- [x] Add strict verified-success, verified-failure, and unknown-boundary result wrappers.
- [x] Publish exactly one outer native envelope with counts and digest.
- [x] Keep dual and plan-only outputs unchanged.

### Task 4: Retain exact full-suite evidence

**Files:**
- Create: `src/saxo_bank_mcp/qa_candidate_full_suite.py`
- Create: `scripts/run_candidate_full_suite.py`
- Create: `tests/test_qa_candidate_full_suite.py`

- [x] Add RED tests for candidate or tree mismatch, wrong temp root, failed suite, count parsing,
      owner-only artifacts, and receipt tampering.
- [x] Invoke only `scripts/run-pytest` and retain mode-0600 JUnit plus a strict digest-bound receipt.
- [x] Bind candidate commit, tree, external temp path, command exit, counts, and artifact digests.

### Task 5: Run local corrective gates

- [x] Run bootstrap, publication, and full-suite focused tests twice after GREEN.
- [x] Run the broader proof, command-runner, auth/session, privacy, static, and catalog tests.
- [x] Run Ruff check, changed-file Ruff format, and BasedPyright.
- [x] Review source and tests for raw output, paths, secrets, account identifiers, and broker data.
- [x] Commit the verified corrective batch and record its exact clean candidate.

### Task 6: Validate the frozen candidate once

- [ ] Run one exact-candidate Codex-only clean install and verify 60 tools, 9 skills, one MCP, and
      exact installed inventory.
- [ ] Run one guarded full suite through the receipt runner and retain owner-only JUnit and receipt.
- [ ] Run static, catalog, and privacy readbacks against the exact installed candidate.

### Task 6A: Correct the pre-bootstrap launcher boundary

- [x] Add RED real-subprocess coverage for missing and broken launchers plus isolated `PATH`.
- [x] Invoke the stdlib bootstrap directly with one absolute Python interpreter and `-I -S`.
- [x] Write and sync the owner-only entry envelope before any `uv` or installed-project command.
- [x] Launch the heavy producer only through one absolute offline `uv --project` handoff.
- [x] Suppress raw child stderr and untyped stdout; retain unknown facts after a launch attempt.
- [x] Prove the focused boundary suite twice, broader related and privacy suites, Ruff,
      BasedPyright, plugin/static/catalog/eval gates, and a bounded changed-file scan.

### Task 7: Run one sealed native proof attempt

- [ ] Re-prove SIM-only status and current session capabilities before broker-bound activity.
- [ ] Run exactly one sealed `codex_native_v1` attempt.
- [ ] On failure, retain authenticated startup and phase evidence and stop without retry.
- [ ] On success, finish 54 proof receipts, Saxo reconciliation, fresh 60-tool SIM matrix, controlled
      cleanup, account equality, and activation only if every gate passes.

### Task 8: Close out

- [ ] Update Task 24 report, progress, validation docs, and Knowledge Base rows with redacted facts.
- [ ] Read back repository docs, private evidence permissions, and Knowledge Base rows.
- [ ] Report candidate, commits, tests, proof result, activation count, blockers, and unpushed state.
