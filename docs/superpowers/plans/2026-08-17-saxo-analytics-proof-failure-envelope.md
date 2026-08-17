# Codex-native analytics proof failure envelope implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve authenticated, privacy-safe partial execution provenance when the native proof
child exits nonzero, while leaving successful native execution and legacy dual execution unchanged.

**Architecture:** A strict child progress tracker emits a candidate-bound failure envelope. The
command runner transports failed output in memory, the native wrapper validates and rebinds it, and
the proof-matrix CLI publishes only verified fields. Missing or untrusted evidence becomes unknown.

**Tech stack:** Python 3.12, Pydantic, subprocess process groups, pytest through
`scripts/run-pytest`, Ruff, BasedPyright.

## Global constraints

- Never invoke Claude, `claude-power`, `codex-power`, a visible browser, or a subagent.
- Never call LIVE endpoints, trade, purchase, or answer a disclaimer.
- Do not run model or Saxo proof work until the local corrective gates pass.
- Run every pytest invocation with the required external `TMPDIR`, `TMP`, and `TEMP`.
- Never persist raw child stdout, stderr, credentials, account identifiers, handles, or payloads.
- Preserve the legacy dual-agent path without relabeling its evidence.
- Freeze one clean candidate before the exact install, full suite, and one sealed proof attempt.

---

### Task 1: Define the strict failure schema and tracker

**Files:**
- Create: `src/saxo_bank_mcp/qa_analytics_proof_failure.py`
- Test: `tests/test_analytics_proof_failure_envelope.py`

- [x] Add RED tests for every required phase and tri-state transition.
- [x] Add strict child and parent receipt models with candidate and install bindings.
- [x] Add deterministic digest validation and safe reason normalization.
- [x] Prove extra fields and sensitive field names or values are rejected.

### Task 2: Preserve failed command output in memory

**Files:**
- Modify: `src/saxo_bank_mcp/agent_skill_command_runner.py`
- Test: `tests/test_agent_skill_evidence_fail_closed.py`
- Test: `tests/test_analytics_proof_failure_envelope.py`

- [x] Add a RED subprocess test that prints a typed envelope and exits nonzero.
- [x] Extend `CommandFailureError` with ephemeral output and process counts, using defaults for
      existing callers.
- [x] Populate those fields on nonzero exit, timeout, and launch failure without changing receipts.
- [x] Prove raw output does not appear in serialized command evidence.

### Task 3: Emit phase-aware child failures

**Files:**
- Modify: `src/saxo_bank_mcp/qa_analytics_proof_producer.py`
- Test: `tests/test_qa_analytics_evidence.py`
- Test: `tests/test_analytics_proof_failure_envelope.py`

- [x] Create progress before input validation and SIM preflight.
- [x] Advance progress around preflight, model evaluation, offline proof, SIM matrix, bundle
      validation, and cleanup.
- [x] Preserve typed event counts and tri-state facts only after validation proves them.
- [x] On nonzero child exit, print one strict envelope and no arbitrary exception text.
- [x] Preserve normal auth-preflight refusal as the existing successful blocked result.

### Task 4: Verify and publish the failure receipt

**Files:**
- Modify: `src/saxo_bank_mcp/qa_analytics_proof_producer.py`
- Modify: `scripts/run_analytics_proof_matrix.py`
- Test: `tests/test_analytics_proof_failure_envelope.py`

- [x] Validate the failed command receipt, envelope digest, candidate, install, policy, and exit.
- [x] Add unknown receipts for missing, malformed, tampered, crashed, or cleanup-failed cases.
- [x] Raise a typed native exception carrying the verified receipt.
- [x] Publish that receipt in the proof-matrix CLI and remove native false or zero fallbacks.
- [x] Prove successful native and legacy dual behavior stays unchanged.

### Task 5: Run the local corrective gates

- [x] Run the focused RED file and record the expected failures.
- [x] Run the focused file twice after GREEN.
- [x] Run the broader related proof, auth/session, command-runner, static, and privacy tests.
- [x] Run Ruff format/check and BasedPyright.
- [x] Review the diff for raw output, secret, account, handle, and payload fields.
- [ ] Commit source, tests, design, plan, and corrected documentation.

### Task 6: Freeze and validate one candidate

- [ ] Confirm the worktree is clean and record the exact commit.
- [ ] Run one exact-candidate Codex-only install and verify 60 tools and 9 skills.
- [ ] Run one guarded exact-candidate full suite with the disk guard.
- [ ] Re-run required static, catalog, and privacy readbacks against that candidate.

### Task 7: Run one sealed native proof attempt

- [ ] Start exactly one `codex_native_v1` proof attempt only after local gates pass.
- [ ] If it fails, retain the typed receipt and stop without retry.
- [ ] If it passes, continue the 54 numerical receipts, Saxo reconciliation, current 60-tool SIM
      matrix, controlled cleanup, unchanged account proof, and activation gate.
- [ ] Activate profiles only if every required receipt and safety gate passes.

### Task 8: Close out evidence and documentation

- [ ] Correct Task 24 report, progress, validation docs, and evidence claims.
- [ ] Update the existing Saxo Project and proof-failure Learning in the Knowledge Base.
- [ ] Read back repository documentation, private evidence permissions, and Knowledge Base rows.
- [ ] Report the exact candidate, commits, tests, proof result, activation count, blockers, and
      unpushed state.
