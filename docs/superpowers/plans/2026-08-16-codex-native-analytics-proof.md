# Codex-native analytics proof implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new `codex_native_v1` install and proof path while preserving all old dual-agent types and evidence unchanged.

**Architecture:** A new typed Codex-only install report proves the exact candidate cache. A parallel proof entry point consumes that report, prepares only Codex and SIM state, runs the existing numerical and SIM proof work, and accepts only real Codex model records. Existing dual-agent commands remain historical and keep their current behavior.

**Tech stack:** Python 3.12, Pydantic, FastMCP, Codex CLI, pytest through `scripts/run-pytest`, Ruff, BasedPyright.

## Global constraints

- Never invoke Claude CLI, models, evaluations, launchers, or processes.
- Never call LIVE endpoints or enable LIVE reads or writes.
- Prove `environment=SIM` before controlled Saxo activity.
- Never answer a disclaimer, purchase, or substitute non-Saxo market or account data.
- Use `TMPDIR`, `TMP`, and `TEMP` set to `/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics` for every pytest run.
- Keep private evidence owner-only and redact every public file.
- Preserve old dual-agent types and receipts without relabeling.
- Freeze one stable source candidate before expensive gates.

---

### Task 1: Define the native harness policy and runtime

**Files:**
- Create: `src/saxo_bank_mcp/qa_codex_native_policy.py`
- Modify: `src/saxo_bank_mcp/agent_skill_matrix_env.py`
- Test: `tests/test_qa_codex_native_policy.py`
- Test: `tests/test_agent_skill_matrix_env.py`

**Interfaces:**
- Produces: `CODEX_NATIVE_POLICY`, a frozen `CodexNativeProofPolicy` with policy ID `codex_native_v1`, required harnesses `("codex",)`, and model quorum `1`.
- Produces: `prepare_eval_isolated_runtime(..., harness_policy="codex_native_v1")`, which copies Codex auth and skips all Claude state.
- Preserves: the default dual runtime behavior for historical callers.

- [ ] **Step 1: Write failing policy and runtime tests**

```python
def test_codex_native_policy_requires_only_real_codex_runs() -> None:
    policy = CODEX_NATIVE_POLICY
    assert policy.policy_id == "codex_native_v1"
    assert policy.required_harnesses == ("codex",)
    assert policy.model_run_quorum == 1

def test_codex_native_runtime_never_reads_claude_auth(...) -> None:
    runtime = prepare_eval_isolated_runtime(..., harness_policy="codex_native_v1")
    assert (runtime.codex_home / "auth.json").is_file()
    assert not (runtime.home / ".claude").exists()
    assert runtime.claude_auth_source is None
```

- [ ] **Step 2: Run the focused tests and confirm RED**

Run:

```bash
TMPDIR=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
TMP=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
TEMP=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
scripts/run-pytest tests/test_qa_codex_native_policy.py tests/test_agent_skill_matrix_env.py -q
```

Expected: collection or assertion failure because the policy and runtime mode do not exist.

- [ ] **Step 3: Add the minimal policy and conditional auth copy**

```python
class CodexNativeProofPolicy(_StrictModel):
    policy_id: Literal["codex_native_v1"] = "codex_native_v1"
    required_harnesses: tuple[Literal["codex"], ...] = ("codex",)
    model_run_quorum: Literal[1] = 1

def prepare_eval_isolated_runtime(..., harness_policy: HarnessPolicy = "dual_v1"):
    ...
```

In native mode, copy `auth.json` only. Do not resolve a Claude home, create `.claude`, copy Claude files, or set promotion markers.

- [ ] **Step 4: Run the focused tests twice and confirm GREEN**

- [ ] **Step 5: Commit the policy and runtime change**

```bash
git add src/saxo_bank_mcp/qa_codex_native_policy.py \
  src/saxo_bank_mcp/agent_skill_matrix_env.py \
  tests/test_qa_codex_native_policy.py tests/test_agent_skill_matrix_env.py
git commit -m "feat: add Codex-native proof policy"
```

### Task 2: Add exact Codex-only installation evidence

**Files:**
- Create: `src/saxo_bank_mcp/agent_skill_codex_install.py`
- Create: `scripts/qa_codex_plugin_install.py`
- Create: `tests/test_agent_skill_codex_install.py`

**Interfaces:**
- Produces: `CodexInstallEvidenceReport`, which has no Claude runtime-state property, command,
  account, process, or receipt. Its exact installed-byte inventory may contain shared packaging
  metadata such as `.claude-plugin/plugin.json`; that is not Claude execution.
- Produces: `produce_codex_install_report(options) -> int`.
- Produces: `load_verified_codex_install_report(path, codex_global_home) -> tuple[CodexInstallEvidenceReport | None, tuple[str, ...]]`.

- [ ] **Step 1: Write failing tests for exact install evidence**

```python
def test_codex_install_report_has_no_claude_runtime_state_surface(...) -> None:
    report = run_fake_codex_install(...)
    assert report.harness_policy == "codex_native_v1"
    assert report.codex.tool_count == 60
    assert "claude" not in set(report.model_dump(mode="json"))

def test_codex_install_rejects_changed_cache_bytes(...) -> None:
    ...
    assert load_verified_codex_install_report(...)[1] == ("installed_inventory_mismatch",)
```

The fake CLI must exercise the real producer boundary and emit a complete Codex plugin registration and cache tree.

- [ ] **Step 2: Run the new test file and confirm RED**

- [ ] **Step 3: Implement the smallest Codex-only producer**

Reuse `clone_candidate`, `export_publishable_tree`, `run_codex_install`, `discover_codex_cache`, `probe_root_stdio`, `installed_inventory_check`, `codex_registration_errors`, and process cleanup helpers. Record only:

```python
class CodexInstallEvidenceReport(_StrictModel):
    status: Literal["passed"]
    execution_mode: Literal["codex_installed_verification"]
    harness_policy: Literal["codex_native_v1"]
    candidate_commit: str
    clone: CloneEvidence
    codex: ClientInstallEvidence
    installed_byte_checks: InstalledByteChecks
    global_codex_state_unchanged: Literal[True]
    process_cleanup: ProcessCleanup
    owner_only: Literal[True]
    privacy_scan_passed: Literal[True]
    errors: tuple[str, ...]
```

The verifier recomputes commit, cleanliness, full inventory, cache digest, 9 skills, one MCP server, 60 tools, registration, owner-only modes, privacy, and the caller Codex state window.

- [ ] **Step 4: Run the install tests twice and confirm GREEN**

- [ ] **Step 5: Commit the install path**

```bash
git add src/saxo_bank_mcp/agent_skill_codex_install.py \
  scripts/qa_codex_plugin_install.py tests/test_agent_skill_codex_install.py
git commit -m "feat: verify Codex-only plugin installs"
```

### Task 3: Add the parallel native proof producer

**Files:**
- Modify: `src/saxo_bank_mcp/qa_analytics_proof_producer.py`
- Modify: `scripts/run_analytics_proof_matrix.py`
- Modify: `scripts/run_dual_harness_skill_evals.py`
- Test: `tests/test_qa_analytics_evidence.py`
- Test: `tests/test_agent_skill_eval_runtime.py`

**Interfaces:**
- Produces: `CodexNativeInstalledProofProducerResult` and `CodexNativeVerifiedInstalledProofValidation`.
- Produces: `run_verified_codex_native_producer(install, candidate_commit) -> CodexNativeVerifiedInstalledProofValidation`.
- Preserves: `InstalledProofProducerResult`, `VerifiedInstalledProofValidation`, and `run_verified_installed_producer` unchanged.

- [ ] **Step 1: Write failing native producer tests**

```python
def test_native_agent_report_requires_only_codex_records(...) -> None:
    report = passed_report(harness="codex", records=(codex_record,))
    receipts = consume_native_agent_report(report)
    assert receipts

def test_native_agent_report_rejects_non_codex_record(...) -> None:
    report = passed_report(harness="codex", records=(codex_record, claude_record))
    with pytest.raises(ProofProducerError, match="native_agent_harness_forbidden"):
        consume_native_agent_report(report)

def test_native_command_has_no_claude_inputs(...) -> None:
    command = native_agent_evaluation_command(...)
    assert "--harness" in command
    assert command[command.index("--harness") + 1] == "codex"
    assert all("claude" not in value.lower() for value in command)
```

- [ ] **Step 2: Run the exact new tests and confirm RED**

- [ ] **Step 3: Implement the native result and command branch**

The native child command includes `--harness-policy codex_native_v1`. It receives only the Codex cache, Codex home, and clean source repo. The hard-task command uses `--harness codex`. Validation requires `report.harness == "codex"`, real model execution, no skipped cases, exact tool coverage, complete cleanup, unchanged state, and every record `record.harness == "codex"`.

Each disposable native `CODEX_HOME` must register the exact retained plugin with the already-proved
marketplace-add/plugin-add flow. Before every non-router model case, run a local-only preflight that
requires the plugin to be enabled and the offline Saxo MCP `list_tools` set to equal that case's
exact server-side grants. Registration, startup, or visibility failures stop before model work with
typed provenance. Missing or unknown Codex MCP event identities fail closed; captured Codex 0.147
JSON event shapes define the parser contract. Native execution prompts bind the exact case ID.

Reuse the existing offline proof suite, installed SIM matrix, numerical receipts, Saxo reconciliation, artifact receipts, timeout reconciliation, and safety checks without changing their acceptance rules.

All exact-workflow subprocess cleanup uses the shared birth-bound cleanup primitive. The command
runner admits new identities only while the original `Popen` root is unreaped, `poll()` reports it
active, and bracketing observations match the captured root birth identity and group. That
admission gate closes permanently on completion, reaping, absence, reuse, unknown observation, or
birth mismatch. Candidate numeric PIDs are discovered first and their birth identities are
observed; a second root-bound PID and process-group scope snapshot, bracketed by the same root
checks, must still contain both the observed PID and its observed group before admission. A
post-scope re-observation must also match the candidate's original PID, birth identity, and group
before the final root check. A replacement, group change, disappearance, unknown observation, or
incomplete re-observation remains detection-only and makes coverage unknown if still present. The
resulting set is frozen as the only possible signal targets before cleanup. The command runner and
nested native evaluation call the same root-handle-bound, two-snapshot admission gate; neither may
capture signal targets from a numeric PID/PGID scope alone. Exact install consumes the same cleanup
primitive. Every signal rechecks the
target birth identity, and terminal tracked-group scans detect late members without admitting
them. Historical numeric process groups are never signaled. Only a member admitted during the
valid root window with the same current birth identity and group remains eligible for an
individual signal; every uncaptured or changed-identity member makes coverage unknown.
Receipts publish semantic target outcomes, coverage, and optional remaining counts. Child-failure
verification accepts cleanup success only from consistent `authenticated` or
`no-target-observed` evidence with complete coverage and known zero process and group counts.
Missing, malformed, inconsistent, write-failed, unknown, or incomplete cleanup evidence fails
closed and cannot contribute false zero event or process claims.

Command-runner watcher publication is a separate authenticated boundary. Each capture registers
under the publication lock before collecting its root-bound scope. Cleanup closes new admission,
drains already-registered captures for a bounded interval, and then freezes the immutable target
tuple. A completed capture may publish before freeze. A capture that is still alive, discarded,
failed, or affected by a join error makes semantic cleanup unknown and cannot produce a signed
zero-process result. It instead writes a separate owner-only diagnostic receipt that binds a fixed
unknown reason, watcher-drain state, coverage state, target count, null remaining counts, and a
digest. Only a drained or not-applicable watcher state may enter an authenticated completion
receipt. Child failure, candidate-runner, and outer publication carry the unknown receipt digest
and reason without promoting model, MCP, Saxo, broker, mutation, purchase, or disclaimer negatives.

- [ ] **Step 4: Run focused producer and evaluation tests twice**

- [ ] **Step 5: Run all related proof, install, and evaluation tests**

- [ ] **Step 6: Run Ruff and BasedPyright**

```bash
uv run ruff check .
uv run basedpyright
```

- [ ] **Step 7: Commit the native proof producer**

```bash
git add src/saxo_bank_mcp/qa_analytics_proof_producer.py \
  scripts/run_analytics_proof_matrix.py scripts/run_dual_harness_skill_evals.py \
  tests/test_qa_analytics_evidence.py tests/test_agent_skill_eval_runtime.py
git commit -m "feat: run analytics proof with Codex only"
```

### Task 4: Freeze and validate one native candidate

**Files:**
- Generate privately: `.omo/evidence/saxo-bank-mcp/analytics-final/runtime-codex-native-<candidate>/`

**Interfaces:**
- Consumes: the committed `codex_native_v1` install and proof entry points.
- Produces: owner-only install, test, static, type, privacy, and auth receipts bound to one commit.

- [ ] **Step 1: Run focused tests twice, Ruff, BasedPyright, static gates, catalog checks, and privacy checks**

- [ ] **Step 2: Commit any collected fixes, then freeze the candidate**

- [ ] **Step 3: Run the Codex-only clean install once**

- [ ] **Step 4: Run the guarded deterministic suite once through `scripts/run-pytest`**

- [ ] **Step 5: Verify exactly 60 tools and no LIVE-enabled configuration**

### Task 5: Run safe proof gates or stop at exact SIM auth refusal

**Files:**
- Generate privately: new candidate proof, hard-task, SIM matrix, cleanup, and state receipts.
- Modify only after a complete pass: `data/analytics/proof_profiles.json` and its loader tests.

**Interfaces:**
- Consumes: the exact native candidate install report.
- Produces: either a full 54-profile passed proof or a refusal with no activation.

- [ ] **Step 1: Prove requested and effective environment are SIM with LIVE reads and writes disabled**

- [ ] **Step 2: Check safe owner-cache and retained headless auth only**

Do not open a browser. If the token is expired and one safe refresh is rejected, stop broker work and preserve the refusal.

- [ ] **Step 3: If auth is valid, run the Codex-native hard workflow, per-analysis proof, 60-tool SIM matrix, timeout reconciliation, cleanup, and unchanged-account proof once**

- [ ] **Step 4: Activate profiles only after all 54 receipts pass**

Write an explicit active profile document with exact source revisions, engine binding, proof expiry, and evidence digest. Commit it as a new candidate and repeat affected gates once. If any receipt is missing, keep all profiles quarantined.

### Task 6: Document and hand off

**Files:**
- Modify: `docs/analytics-bi-vision.md`
- Modify: `docs/analytics-bi-validation.md`
- Modify privately: `.superpowers/sdd/2026-07-30-saxo-analytics-bi-suite/progress.md`
- Modify privately: `.superpowers/sdd/2026-07-30-saxo-analytics-bi-suite/task-24-report.md`

- [ ] **Step 1: Record only executed results and exact blockers**

- [ ] **Step 2: Run public secret and private-value scans**

- [ ] **Step 3: Commit safe public documentation**

- [ ] **Step 4: Update the existing Knowledge Base row and read it back**

- [ ] **Step 5: Leave the branch unpushed and request separate root review**

Current local handoff: source `a59b231`, tree `8f3f6b5`, preserves incomplete process-table and
terminal-observation coverage as authenticated cleanup uncertainty with nullable counts. Local
focused, related, auth/privacy, type, plugin, static, catalog, eval-manifest, privacy, and diff gates
pass. It remains uninstalled and unsealed pending independent static review; the latest sealed
result remains `e5dc932` at 10/11 hard cases and 0/54 active profiles.

Superseding local handoff: source `dd85c9c`, tree `b8ad581`, extends that strict uncertainty to
every post-launch unobservable result. Timeout, uncertain or residual cleanup, parser failure,
malformed output, and post-spawn process-observation failure retain null grading, tool identities,
and model/command/MCP/Saxo event counts through authenticated failure publication. A successful
spawn records cleanup pending and the root as created before fallible PGID or scope capture. The
source passed focused tests twice plus related, auth/privacy, type, plugin, static, catalog,
eval-manifest, privacy, and diff gates. It remains uninstalled and unsealed pending independent
static review; sealed `e5dc932` and 0/54 activation remain unchanged.

Superseding local handoff: source `9fbd2cb`, tree `40d6525`, applies the shared unobservable record
contract to router cases. Both harnesses gate timeout, unknown/null cleanup, residue, structured-
output failure, and post-spawn exceptions before observable router construction. Router decision,
grading, invoked tools, and model/command/MCP/Saxo counts stay null through authenticated child,
candidate, and outer evidence; observable successful router records remain unchanged. Local
focused-twice, related, auth/privacy, catalog-status, Ruff/format, type, plugin, static, catalog,
eval-manifest, privacy, and diff gates pass. The source remains uninstalled and unsealed pending
independent static review; sealed `e5dc932` and 0/54 activation remain unchanged.

Superseding local handoff: source `f96a3b2`, tree `265a11a`, extends that strict router boundary to
the post-model client-version child. Timeout, unknown/null or noncomplete cleanup, residue, and
caught operating-system or value errors produce an unobservable record for both harnesses. Report
completion rechecks sticky timeout, semantic cleanup, and known-zero remaining processes instead of
trusting the summary flag. Fixed lowercase reason codes propagate through authenticated report,
child, candidate, and outer evidence without private exception details or negative broker facts.
Local focused tests passed twice plus related, auth/privacy, Ruff/format, type, plugin, static,
catalog, eval-manifest, privacy, and diff gates. The source remains uninstalled and unsealed pending
independent static review; sealed `e5dc932` and 0/54 activation remain unchanged.

Terminal validation handoff: independent review approved `f96a3b2`, but the one authorized exact
install invocation refused before execution with `codex_install_run_root_exists`. The empty
owner-only installer run root had been pre-created even though the transactional installer requires
that path to be absent. No clone, plugin install, client, retained runtime, model, MCP, Saxo, or
global-state work ran, and no retry was permitted. A separately authorized attempt must pass an
absent installer-owned run root. No exact `f96a3b2` install, suite, proof, or activation evidence
exists; sealed `e5dc932` and 0/54 activation remain unchanged.
