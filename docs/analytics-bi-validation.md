# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-19
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Candidate `e5dc932f92fc850c9a2d7608074f3af0ae57b1c6`, tree
`bd754a6288512ed9402dffbe0c4a336eae6130ef`, passed its exact Codex-only retained-runtime
installation, signed 2,883-test full suite, required local gates, and current SIM safety preflight.
Its one sealed proof command exited 1. The authenticated outer publication refused with
`proof_candidate_runner_cleanup_failed` because candidate-runner cleanup coverage was unknown.
The authenticated inner result retained the producer's `proof_child_cleanup_failed` boundary and
the privacy-safe hard-evaluation summary: ten of eleven native cases passed. `artifact-delivery`
failed `transcript_assertion_failed` even though its exact grant and all three required logical
tool calls passed. Observable raw decoded assistant events and observable final parsed text both
had required-all vector `[false, true, true]`, proving that `analysis_id` was absent before and
after parsing.

Nested evaluation cleanup reports complete with zero remaining evaluation processes and no raw
output retained. The one-shot proof runtime was consumed and durably removed, and final local
readback found zero run-owned processes. Those facts do not repair the authenticated unknown
outer cleanup coverage or prove broker/account state. No retry ran. The numerical proof, fresh
60-tool SIM matrix, Saxo reconciliation, account-equality readback, and activation gates did not
run. All 54 proof profiles remain quarantined and 0 are active. The dedicated exact `e5dc932`
section below supersedes earlier status summaries; historical candidate sections remain
unchanged evidence for their named runs.

Local corrective source `94a86430d90d4e8db770e1b9c4d96eceffa23b29`, tree
`40cef275436f178f202300290ef9f21c65ef583a`, addresses the two independently confirmed local
causes without changing the sealed result. Command cleanup now uses birth-bound process identities:
only a PID whose current birth identity still matches may receive a signal, historical process
groups are never signalled, and one terminal semantic snapshot supplies the authenticated target
outcomes and remaining process and group counts. Reused identities, absent processes, and
non-executing zombies are not candidate survivors; incomplete or unknown observation coverage
fails unknown rather than proving cleanup. The `scenario` native fixture contract now explicitly
requires the exact ordered receipt text `explicit numeric shocks -0.10 0.05`; the assertion itself
is unchanged.

This corrective source passed 47 focused tests twice, 313 related evaluation/proof tests, and 267
auth/privacy tests. Ruff check and changed-file format check passed; BasedPyright reported 0 errors,
0 warnings, and 0 notes; plugin, static, catalog, evaluation-manifest, and bounded privacy gates
passed. No model, MCP, Saxo, broker, browser, install, sealed proof, or broker/data network activity
ran for this correction. It is not an installed or sealed candidate. The latest sealed evidence
remains `c716047`; 54/54 profiles remain quarantined and 0/54 are active.

This exact candidate builds on the diagnostic source without changing the assertion, prompt,
successful evaluation behavior, or historical `dual_v1` path. The new raw-versus-final vectors
remove the earlier parser ambiguity for this run: the required phrase was absent before and after
final-text parsing.

Malformed or partly malformed Codex output is now explicitly unobservable for all parse-derived
tool, call, and count evidence. `no_mcp_call`, `no_saxo_call`, model/tool/MCP/Saxo event counts,
invoked logical tool identifiers and counts, and their runner and proof aggregates remain unknown
rather than becoming false zeroes or empty sets. The same state is authenticated through the
privacy-safe per-case failure summary and outer publication. Fully decoded valid streams retain
their prior behavior. A nonzero child exit cannot take precedence over a simultaneously malformed
Codex event stream and reintroduce false counts.

The earlier assistant-diagnostic protections remain: message presence, hashes, and assertion
vectors are unknown for malformed output. The local corrective source additionally makes timeout,
post-spawn operating-system failure, and ordinary terminal cleanup share the birth-bound semantic
snapshot described above. Cleanup status and optional digest remain strictly bound. No transcript,
arguments, raw output, account data, handles, payloads, or private paths are retained.

The source correction had already passed five focused tests twice, 232 related evaluation/proof
tests, and 243 auth/privacy tests. The exact candidate then passed the retained-runtime install,
signed full suite, Ruff check, BasedPyright, plugin, static, catalog, evaluation-manifest, privacy,
and install readback gates described below. The separate whole-repository Ruff format audit still
reports 95 pre-existing files that would reformat; it is not claimed passed and no such file was
changed.

This document contains no credentials, account identifiers, balances, holdings, money values,
private local paths, raw broker payloads, transcripts, model arguments, or private URLs.

## Candidate identity

| Item | Value |
| --- | --- |
| Sealed source | `c716047a332a78cccd0d1c94ed4348e7edbd1169` |
| Sealed tree | `3170c9d4dc7d3ff63987999892209e7b19b98275` |
| Harness policy | `codex_native_v1` |
| Installed surface | 60 tools, including 21 analytics tools; 9 skills; 1 MCP server |
| Installed inventory | 617 exact compared files |
| Saxo operation catalog | 294 operations: 182 implemented and 112 refused |
| Proof catalog | 54 analysis kinds and 54 proof profile IDs |
| Activation state | 54 quarantined as `implementation_pending`; 0 active |

The historical `dual_v1` policy and its evidence remain unchanged. The native policy uses only
the Codex harness. Shared package inventory metadata does not constitute another client, account,
process, or evaluation.

## Exact candidate gates

The retained-runtime installation passed with exact candidate and tree binding, 60 tools, 9 skills,
1 MCP server, 617 exact compared files, no inventory mismatch, unchanged caller state during the
install window, owner-only evidence, and complete install-process cleanup. Verify-only readback
passed before the retained runtime was consumed.

The signed exact-candidate full-suite receipt reports:

- 2,835 tests passed;
- zero failures, errors, or skips;
- the exact candidate and tree before and after;
- a clean source worktree before and after;
- owner-only JUnit and command receipts; and
- no retained raw test output.

All pytest execution used `scripts/run-pytest` with `TMPDIR`, `TMP`, and `TEMP` set to the required
external temporary root. Candidate-attributed temporary storage returned to its baseline, no
candidate-owned system-temporary pytest directory appeared, and the system disk stayed above the
50 GiB guard.

| Local gate | Result |
| --- | --- |
| Ruff check | passed |
| Whole-repository Ruff format audit | 95 pre-existing files would reformat; not claimed passed |
| BasedPyright | 0 errors, 0 warnings, 0 notes |
| Plugin validator | passed |
| Nine-skill static gate | 9 skills, 0 findings, 0 errors |
| Generated catalogs | 60 tools, 21 analytics tools, 294 operations, 10 analytics scenarios |
| Evaluation manifest | 34 cases, 60 tools, 9 skills, 0 errors |
| Public privacy scan | passed with 0 findings and 0 scan errors |
| Structural install readback | exact candidate, inventory, binding, modes, and unconsumed runtime verified |

## SIM authorization gate

Before the sealed command, local status proved requested and effective environment `SIM`, with
LIVE reads disabled and LIVE writes disabled. One separate read-only session-capability call passed
and truthfully recorded `network_call_made=true`.

The sealed producer then performed its own required SIM preflight before model work. The
authenticated child receipt records requested and effective `SIM`, LIVE reads false, LIVE writes
false, session capabilities passed, and `network_call_made=true`. These receipts prove only their
own read-only capability requests. They do not prove account equality or the later unknown broker
facts.

## One sealed proof attempt

Exactly one sealed `codex_native_v1` command ran for `c716047` and exited 1. The authenticated outer
publication has the complete 54-analysis and 54-receipt contract counts and result kind
`verified_child_failure`.

| Field | Authenticated value |
| --- | --- |
| Status | refused |
| Reason | `proof_child_cleanup_failed` |
| Completed phase | SIM preflight |
| Current phase | agent evaluation |
| Child exit | 1 |
| Execution performed | true |
| SIM preflight | passed |
| Network call made | true |
| Hard-case summary | 10 passed, 1 failed |
| Failing case | `scenario`: `transcript_assertion_failed` |
| Failing-case grant | passed |
| Failing-case required tools | exact two logical tools invoked |
| Raw and final observability | observable |
| Raw and final required-all vectors | `[true, false, true]` |
| Missing required phrase | `explicit numeric shocks` |
| Child cleanup status | not started |
| Outer retained-runtime cleanup | complete |
| Candidate-runner cleanup | complete |

The strict privacy-safe per-case summary contains only case identifiers, assertion and grant
outcomes, required and invoked logical tool identifiers and counts, safe event counts, and bound
digests. It contains no transcript, arguments, handles, broker payload, account data, raw output,
or private path.

The overall receipt deliberately leaves aggregate model, MCP, and Saxo event counts, broker-write
state, LIVE mutation count, purchase state, and disclaimer-response state unknown. The per-case
summary is not used to invent those aggregate or broker facts. The nested evaluation cleanup
summary reports complete runtime and process cleanup with zero remaining evaluation processes and
no retained raw output. The authenticated outer process-cleanup receipt covers 90 observed targets,
all terminally absent. After the command terminated, an independent local readback found no
exact-candidate process, no proof run root, and no retained proof runtime.

No second proof, diagnostic model run, numerical proof, fresh SIM matrix, account read, or
activation ran.

## Cleanup and activation

Authenticated consumption intent and cleanup receipts bind the exact candidate, tree, policy,
runtime binding, and durable removal of the one-shot proof runtime. The candidate-runner receipt
binds the authenticated child-result digest, child exit 1, and completed runner cleanup. Retained
receipts are owner-only. The candidate worktree remained clean and no exact-candidate process was
present at final local readback.

Those local cleanup facts do not prove broker account equality, no mutation, no purchase, or no
disclaimer response. No attempt-bound before/after account fingerprint exists, so account state is
reported as unproved rather than unchanged.

All 54 profiles remain quarantined and 0 are active. The branch remains local and unpushed. The
exact run proves the scenario assertion surface independently of final-text parsing, while the
separate `proof_child_cleanup_failed` boundary also remains unresolved. Root owns the independent
native review.

## Shared identity-safe cleanup correction after `94a8643`

Local source `003f8e8eee92c68ee188abd096ab3a24b688d6d5`, tree
`3c273b2e88cb0985644ddedf6ece8679ec53c9b6`, closes the remaining cleanup identity and failure-
publication gaps without relabeling the sealed `c716047` evidence. One shared primitive now owns
command-runner, nested native-evaluation, and exact-install cleanup. It captures birth-bound process
identities, discovers current members of owned process groups before signaling, rechecks each
target's birth identity immediately inside every individual signal operation, rejects a present
reused group leader, never signals a historical numeric process group, and terminally rescans
owned groups. A late member, incomplete observation, or unknown coverage cannot become a false
zero. An absent group leader does not prevent cleanup of a separately captured same-birth child in
the still-observed original group.

The exact-install and nested-evaluation paths no longer perform their former raw PID or process-
group cleanup. Their receipts consume the shared semantic outcomes. Strict child-failure
verification accepts cleanup completion only from consistent authenticated or no-target-observed
evidence with known zero process and group counts; missing or inconsistent digests, write failure,
unknown counts, incomplete coverage, or residual processes become `proof_child_cleanup_failed` or
unknown instead of a false success.

Final local verification passed 111 focused tests twice, 344 related cleanup/install/evaluation/
proof tests, and 431 safe auth/privacy tests. Ruff check and changed-file format passed;
BasedPyright reported 0 errors, 0 warnings, and 0 notes; plugin, static, catalog,
evaluation-manifest, bounded privacy, and `git diff --check` gates passed. One deliberately broad
test selection entered a fully isolated fake-client install fixture; it was immediately stopped,
its process and temporary state were cleaned, and it is excluded from the completed suite counts.
No real client/model process, exact candidate install, production MCP server, Saxo request, broker
operation, browser, or broker/data network activity ran in this correction.

This source is not installed or sealed. The latest sealed evidence remains `c716047` at 10/11 hard
cases with its authenticated unknown broker/account facts. No numerical proof, fresh 60-tool SIM
matrix, Saxo reconciliation, account equality, controlled lifecycle, or activation ran. All 54
profiles remain quarantined and 0/54 are active. The branch remains local and unpushed.

## Absent-leader discovery correction after `003f8e8`

Local source `4d80141cedaecbc6ee84a41d3264fa73d1c2e811`, tree
`f8371777eaeedbd5d130827eca81b0d063420a58`, closes the final review finding without changing the
sealed `c716047` result. When the original captured process-group leader is absent, numeric group
membership no longer authorizes discovery of a new cleanup target. The cleanup code compares each
current member with the captured PID and birth identity set. An uncaptured member or changed birth
identity is never added or signaled and makes cleanup coverage unknown. A child that was already
captured before the leader exited remains eligible for an individual signal only while its own
birth identity and group still match.

Final local verification passed 113 focused tests twice, 346 related cleanup/install/evaluation/
proof tests, and 431 safe auth/privacy tests. Ruff check and changed-file format passed;
BasedPyright reported 0 errors, 0 warnings, and 0 notes; plugin, static, catalog,
evaluation-manifest, bounded privacy, and `git diff --check` gates passed. No exact install, model,
production MCP server, Saxo request, broker operation, browser, sealed proof, or broker/data network
activity ran.

This source is not installed or sealed and does not relabel earlier evidence. The latest sealed
result remains `c716047` at 10/11 hard cases with unknown broker/account facts. No numerical proof,
fresh 60-tool SIM matrix, Saxo reconciliation, account equality, controlled lifecycle, or
activation ran. All 54 profiles remain quarantined and 0/54 are active. The branch remains local
and unpushed.

## Captured-target-only cleanup correction after `4d80141`

Local source `d81621d549f771970cdd8bb4cb0470c8c5a6e9a3`, tree
`abb390999b048d25593014feed9023230dc453d6`, strengthens the cleanup boundary without changing the
sealed `c716047` result. The signal target mapping is an immutable projection of identities
captured before cleanup starts. Process-group scans are detection only: they never add a PID,
regardless of whether the captured leader is present, absent, or reused. Every uncaptured member,
changed birth identity, or incomplete observation makes coverage unknown. Only a pre-captured PID
whose own birth identity still matches can receive an individual signal. A pre-captured same-birth
child remains individually cleanable after its leader exits while its own identity and group match.

Deterministic timing regressions cover leader loss or reuse between classification and member
observation, leader exit between member observation and signaling, and a stable leader with a new
same-group member. The RED run failed all four cases on the former admission path. Final local
verification passed 117 focused tests twice, 350 related cleanup/install/evaluation/proof tests,
and 431 safe auth/privacy tests. Ruff check and changed-file format passed; BasedPyright reported
0 errors, 0 warnings, and 0 notes; plugin, static, catalog, evaluation-manifest, bounded privacy,
and `git diff --check` gates passed.

No exact install, model, production MCP server, Saxo request, broker operation, browser, sealed
proof, or broker/data network activity ran. This source is not installed or sealed and does not
relabel earlier evidence. The latest sealed result remains `c716047` at 10/11 hard cases with
unknown broker/account facts. The numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation,
account equality, controlled lifecycle, and activation did not run. All 54 profiles remain
quarantined and 0/54 are active. The branch remains local and unpushed.

## Exact `e5dc932` stable gates and sealed outcome

Source `e5dc932f92fc850c9a2d7608074f3af0ae57b1c6`, tree
`bd754a6288512ed9402dffbe0c4a336eae6130ef`, was checked out in a clean detached worktree. One
owner-only retained-runtime installation passed with exact candidate and tree binding, 60 tools,
9 skills, 1 MCP server, 620 exact compared files, no inventory mismatch, unchanged caller Codex
state, privacy-clean evidence, and complete install-process cleanup. The one verify-only readback
passed. A later read-only structural receipt revalidated the source and installed-cache bytes,
dependency lock, producer module, interpreter, runtime binding, owner modes, current caller state,
and unconsumed runtime.

The one signed exact-candidate full suite passed 2,883 tests with zero failures, errors, or skips.
Its owner-only JUnit and authenticated receipt bind the candidate, tree, clean before and after
state, approved external temporary root, exit, counts, and digests. The guarded launcher removed
its candidate-owned temporary root, final process readback was zero, and the system disk remained
above the 50 GiB floor.

Post-suite gates passed Ruff check; BasedPyright with 0 errors, 0 warnings, and 0 notes; plugin
validation; the nine-skill static gate; the 60-tool, 294-operation catalog; the 34-case evaluation
manifest; and a 16-path source privacy scan with zero findings and zero scan errors. The historical
whole-repository format drift remains a separate observation and is not relabeled as passed.

Local no-network auth status proved requested and effective `SIM`, LIVE reads false, LIVE writes
false, a readable non-expired refreshable SIM cache, and no blocker. Exactly one separate
read-only session-capability call passed with `network_call_made=true`, `token_refreshed=false`,
LIVE write false, and no order or subscription creation. The sealed producer's authenticated SIM
preflight also passed and recorded truthful network provenance before model work.

Exactly one `codex_native_v1` sealed command then ran through the explicit exact
candidate-source-root contract and exited 1. No retry ran. The authenticated outer publication is
a `boundary_failure` with reason `proof_candidate_runner_cleanup_failed`: its runner receipt binds
the authenticated candidate result, but runner cleanup coverage is `unknown`. The outer receipt
therefore keeps execution, model/MCP/Saxo aggregates, broker writes, LIVE mutation count,
purchase state, disclaimer-response state, and cleanup state unknown.

The authenticated candidate result is a `verified_child_failure`. It preserves a passed SIM
preflight, `network_call_made=true`, execution performed, current phase `agent_evaluation`, and
reason `proof_child_cleanup_failed`. Its strict privacy-safe evaluation summary records 10 passed
cases and one failed case. All 11 grant checks passed and every case invoked its exact required
logical tool count. `artifact-delivery` alone failed `transcript_assertion_failed`: its three
required tools were invoked, while both raw assistant events and final parsed text had required-all
vector `[false, true, true]`. The missing required field was `analysis_id`; `owner-only` and
`quality warnings` were present. The summary retains hashes and allowlisted outcomes only, not
transcripts, arguments, handles, payloads, account data, or paths.

Nested evaluation cleanup is independently authenticated as complete: 12 processes were created,
zero remained, none timed out, runtime cleanup passed, and persisted raw output count was zero.
The retained proof runtime has authenticated consumption intent and cleanup receipts and is absent.
A final local process readback found zero run-owned processes and the proof temporary root absent.
However, the inner outer-process cleanup evidence is `observation-unknown`, and the candidate
runner cleanup receipt is also `unknown`; these later local observations cannot be promoted into
an authenticated cleanup pass. Cleanup, broker mutation, purchase, disclaimer, and account-state
claims therefore remain unknown.

An eight-path terminal proof privacy scan passed with zero findings and zero scan errors. No
numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation, controlled SIM lifecycle,
attempt-bound account comparison, or activation ran. All 54 profiles remain quarantined and 0/54
are active. Evidence is owner-only, the branch remains local and unpushed, and root retains the
separate independent-review gate.

## Root-bound identity admission correction after `d81621d`

Local source `d3efb6ebb2fde2bb679c0b6019a309ac41723062`, tree
`24343112fa3faf0efd5d895f1c4043498ca0c2e3`, closes the remaining upstream admission window without
changing the sealed `c716047` result. `run_command` now keeps a sticky admission gate bound to the
original `Popen` root. A new PID can enter the immutable cleanup target set only while that root is
unreaped, `poll()` still reports it active, and bracketing process observations retain the captured
birth identity and process group. A completed, reaped, absent, reused, unknown, or birth-mismatched
root permanently closes admission. Every later PID and process-group scan is detection only; an
uncaptured or replacement member is never signaled and makes cleanup coverage unknown.

The deterministic RED covered post-exit absent and reused roots plus a birth mismatch; three of
four cases failed before the correction. Companion coverage proves that a legitimate child
observed while the same-birth root remains active can still be admitted and cleaned. The real
redirected-child regression now uses an observation handshake rather than a timing delay: the root
exits nonzero only after the child's admission has been bracketed by matching root observations,
then the already captured child is safely cleaned.

Final local verification passed 121 focused cleanup/migration/failure-envelope tests twice, 354
related cleanup/install/evaluation/proof tests, and 431 safe auth/privacy tests. Ruff check and
changed-file format passed; BasedPyright reported 0 errors, 0 warnings, and 0 notes; plugin,
nine-skill static, 60-tool/294-operation catalog, 34-case evaluation-manifest, bounded privacy,
and `git diff --check` gates passed.

No exact install, model, production MCP server, Saxo request, broker operation, browser, sealed
proof, or broker/data network activity ran. This source is not installed or sealed and does not
relabel earlier evidence. The latest sealed result remains `c716047` at 10/11 hard cases with
unknown broker/account facts. The numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation,
account equality, controlled lifecycle, and activation did not run. All 54 profiles remain
quarantined and 0/54 are active. The branch remains local and unpushed.

## Second-scope child-admission correction after `d3efb6e`

Local source `ec73f8cff56f0033aa76526796726e38e00d79ab`, tree
`d6e2d7afaf204c2e77af7e8df06ce99c67a32c76`, closes the child-PID admission TOCTOU without
changing the sealed `c716047` evidence. `run_command` first discovers candidate numeric PIDs,
observes their birth identities, then takes a second root-bound PID and process-group scope
snapshot bracketed by the same active, unreaped, same-birth root checks. It admits only an
observed candidate that remains in both the second PID scope and the required process-group scope.
A candidate that was replaced, changed group, disappeared, or could not be re-observed is never
admitted or signaled; if it remains detectable without an authenticated identity, cleanup
coverage is unknown. A legitimate same-birth child that remains in both scopes can still be
captured and safely cleaned after its leader exits.

The deterministic RED reproduced a discovered child PID becoming an unrelated self-group leader
before identity observation: the former path admitted it, while the companion legitimate-child
case passed. Final local verification passed 151 focused cleanup/install/failure-envelope tests
twice, 355 related cleanup/install/evaluation/proof tests, and 431 safe auth/privacy tests. Ruff
check and changed-file format passed; BasedPyright reported 0 errors, 0 warnings, and 0 notes;
plugin, nine-skill static, 60-tool/294-operation catalog, 34-case evaluation-manifest, bounded
privacy, and `git diff --check` gates passed.

No exact install, model, production MCP server, Saxo request, broker operation, browser, sealed
proof, or broker/data network activity ran. This source is not installed or sealed and does not
relabel earlier evidence. The latest sealed result remains `c716047` at 10/11 hard cases with
unknown broker/account facts. The numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation,
account equality, controlled lifecycle, and activation did not run. All 54 profiles remain
quarantined and 0/54 are active. The branch remains local and unpushed.

## Post-scope identity re-observation correction after `ec73f8c`

Local source `7467f112feebf0d836a402596c36d3b5e089c069`, tree
`7d43d5db4ffebd924a182a652f668f6cceca02c6`, closes the remaining stale PID/group relationship
window without changing the sealed `c716047` evidence. After the second root-bound scope snapshot,
`run_command` re-observes every candidate and requires the same PID, birth identity, and process
group seen before that snapshot. Only a candidate whose first observation, second scope, and final
observation agree may pass the existing final active, unreaped, same-birth root check and enter the
immutable cleanup target set. A moved, replaced, missing, or unknown candidate is never admitted or
signaled; if it remains detectable without a confirmed identity, cleanup evidence stays unknown.

The deterministic RED kept the child PID visible while it moved from the original group to a new
group and a peer kept the original group present. Both group IDs were in the second scope, so the
former independent PID/group membership checks admitted the stale relationship. The companion
stable-child and already-refused replacement cases passed. Final coverage also proves missing and
unknown post-scope observations refuse fail-closed. Local verification passed 154 focused cleanup/
install/failure-envelope tests twice, 358 related cleanup/install/evaluation/proof tests, and 431
safe auth/privacy tests. Ruff check and changed-file format passed; BasedPyright reported 0 errors,
0 warnings, and 0 notes; plugin, nine-skill static, 60-tool/294-operation catalog, 34-case
evaluation-manifest, bounded privacy, and `git diff --check` gates passed.

No exact install, model, production MCP server, Saxo request, broker operation, browser, sealed
proof, or broker/data network activity ran. This source is not installed or sealed and does not
relabel earlier evidence. The latest sealed result remains `c716047` at 10/11 hard cases with
unknown broker/account facts. The numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation,
account equality, controlled lifecycle, and activation did not run. All 54 profiles remain
quarantined and 0/54 are active. The branch remains local and unpushed.

I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.

## Nested-evaluation capture-window correction after `7467f11`

Local source `9826b26cf19a45ed9aa0142ebf64585eb40ad0aa`, tree
`0bc77c9b72a31cb564c7a092407213c8e04aa725`, closes the nested
`EvalProcessManager` capture-time PID-reuse window without changing the sealed `c716047`
evidence. The command runner and nested evaluator now use the same root-handle-bound admission
gate. It requires the original `Popen` root to remain active, unreaped, and same-birth across two
PID/process-group snapshots, then re-observes every candidate and requires the same PID, birth
identity, and group before the final root check. Only confirmed identities enter the immutable
signal target set. Moved, replaced, missing, or unknown observations remain detection-only; a
still-visible unconfirmed process makes cleanup coverage unknown.

The deterministic RED failed all five stable, moved, replaced, missing, and unknown nested-eval
variants because the former path performed no `Popen.poll()` bracket or second snapshot. The
stable child is now admitted and cleaned. A same-birth child that moves while both old and new
groups remain visible, a replaced PID/new leader, and an unknown observation are excluded without
signals and retain unknown cleanup evidence while visible. A child that disappears before exact
confirmation is excluded and is not signaled.

Final local verification passed 192 focused cleanup/evaluation/failure-envelope tests twice, 363
related cleanup/install/evaluation/proof tests, and 431 safe auth/privacy tests. Ruff check and
changed-file format passed; BasedPyright reported 0 errors, 0 warnings, and 0 notes; plugin,
nine-skill static, 60-tool/294-operation catalog, 34-case evaluation-manifest, bounded privacy,
and `git diff --check` gates passed.

No exact install, model, production MCP server, Saxo request, broker operation, browser, sealed
proof, or broker/data network activity ran. This source is not installed or sealed and does not
relabel earlier evidence. The latest sealed result remains `c716047` at 10/11 hard cases with
unknown broker/account facts. The numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation,
account equality, controlled lifecycle, and activation did not run. All 54 profiles remain
quarantined and 0/54 are active. The branch remains local and unpushed.

## Watcher drain/freeze correction after `9826b26`

Local source `e5dc932f92fc850c9a2d7608074f3af0ae57b1c6`, tree
`bd754a6288512ed9402dffbe0c4a336eae6130ef`, closes the command-runner watcher publication race
without changing the sealed `c716047` evidence. The former cleanup path stopped the daemon watcher
and joined it for one second, but could freeze the tracked identity tuple while an already-started
capture pass was still between scope capture and publication. That pass could publish after cleanup
had authenticated a false complete/zero result.

Admission publication is now lock-bracketed in three states. A capture registers while publication
is open. Cleanup atomically moves admission to draining and closes the root admission gate, so no
new pass can start. An already-registered pass may publish before the bounded drain completes; after
that boundary the immutable target tuple is frozen and every late pass is discarded without target
mutation. A still-running watcher, discarded pass, failed capture, join error, or unknown drain
observation makes the semantic cleanup result unknown, with unknown remaining counts and no
authenticated cleanup receipt. A fully drained watcher remains eligible for authenticated cleanup;
the owner-only receipt digest includes the allowed drain state and strict verification rejects
tampering or an impossible authenticated late/discarded state.

The deterministic RED paused a watcher after its root-bound scope had been computed but before it
could publish, released the root, and expired the bounded join. The former source returned success
without an error; the corrected path refuses with `process_cleanup_unknown`, publishes no signed
zero, and does not admit the late identity. Companion coverage proves completed publication drains
and authenticates, join and capture operating-system errors remain unknown, and private command
arguments and paths do not enter the receipt.

Final local verification passed 199 focused cleanup/evaluation/failure-envelope tests twice, 370
related cleanup/install/evaluation/proof tests, and 431 safe auth/privacy tests. Ruff check and
changed-file format passed; BasedPyright reported 0 errors, 0 warnings, and 0 notes; plugin,
nine-skill static, 60-tool/294-operation catalog, 34-case evaluation-manifest, bounded privacy,
and `git diff --check` gates passed.

No exact install, model, production MCP server, Saxo request, broker operation, browser, sealed
proof, or broker/data network activity ran. This source is not installed or sealed and does not
relabel earlier evidence. The latest sealed result remains `c716047` at 10/11 hard cases with
unknown broker/account facts. The numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation,
account equality, controlled lifecycle, and activation did not run. All 54 profiles remain
quarantined and 0/54 are active. The branch remains local and unpushed.

## Current terminal status after exact `e5dc932`

The exact `e5dc932` section above is the current sealed result and supersedes historical sentences
that name `c716047` as the latest run. Candidate `e5dc932` passed every pre-proof gate and then
failed closed at authenticated runner/child cleanup boundaries with 10/11 hard cases. No retry or
downstream activation gate ran. Cleanup and broker/account facts remain unknown, and all 54
profiles remain quarantined with 0/54 active.

## Local artifact receipt and unknown-cleanup evidence correction after `e5dc932`

Local source `0e8b625911e92ffbe2125eca4a80539653317d1e`, tree
`59235a9fc52f5bc870f3c09d8ea2244eae886f22`, addresses both bounded review findings without
changing or relabeling the sealed `e5dc932` evidence. The exact subcause of that run's unknown
outer cleanup observation is not reconstructable from its retained privacy-safe receipt and is not
guessed here.

The native `artifact-delivery` fixture prompt now requires this exact final receipt contract:
`analysis_id: <returned or fixture id>; state: <verified|degraded|refused>; owner-only; quality
warnings`. Its assertions, server-side grants, and required logical tool checks are unchanged. The
prompt only makes the already-required receipt fields explicit to the model.

Command cleanup now preserves uncertainty in a separate owner-only, path-free diagnostic receipt.
The receipt binds a fixed allowlisted reason code, watcher-drain state, coverage state, target
count, null remaining process and group counts, and one digest. Observation failure, discarded or
still-running watcher publication, join failure, incomplete coverage, and unknown terminal state
remain refusal outcomes. Receipt-path absence, write failure, inconsistent evidence, and unavailable
candidate-runner evidence use distinct fixed failure reasons and cannot authenticate cleanup.

The typed unknown receipt digest and reason propagate through command failure, verified child
failure, candidate-runner receipt, and final outer boundary publication. Unknown or write-failed
cleanup makes remaining counts unknown and cannot promote zero model, MCP, or Saxo events, no
broker write, no mutation, no purchase, or no disclaimer response. Historical receipt shapes are
accepted only when every later cleanup field has its exact default; a historical digest cannot
authenticate newly supplied cleanup claims.

TDD first reproduced six missing-contract and missing-evidence failures, plus observation-error,
unknown-count, and strict outer propagation gaps. Final focused verification passed 197 tests twice.
The expanded cleanup, install-fixture, evaluation, proof, and publication suite passed 386 tests;
416 auth, session, evidence, redaction, token-cache, and privacy tests passed. Ruff and changed-file
format checks passed, BasedPyright reported 0 errors, 0 warnings, and 0 notes, and plugin, nine-skill
static, 60-tool/294-operation catalog, 34-case evaluation-manifest, and 11-path privacy gates passed
with zero privacy findings or scan errors.

No exact candidate or product install, model, production MCP server, Saxo request, broker operation,
browser, sealed proof, or broker/data network activity ran. This source is not installed or sealed.
The exact `e5dc932` result remains the latest sealed evidence, no retry occurred, all downstream
numerical, matrix, reconciliation, account-equality, and activation gates remain unrun, and all 54
profiles remain quarantined with 0/54 active.

## Local cleanup-coverage correction after `0e8b625`

Local source `a59b231ed31bf5254dd5e83bc8a1ba20408f6c90`, tree
`8f3f6b5989ed944c6ce54a566ad4aa434156f652`, carries process-table coverage as an explicit strict
field from both root-bound admission snapshots through watcher publication and terminal cleanup.
Any incomplete or failed first, second, watcher, or terminal observation is sticky, produces typed
`coverage_unknown` or `target_observation_unknown` evidence, and leaves remaining process and group
counts null. A later successful snapshot cannot turn that uncertainty into authenticated zero.

Nested native evaluation preserves nullable remaining counts through its manager, report, verified
child failure, candidate-runner receipt, and outer publication. It never converts `None` to zero or
labels an empty terminal-evidence set as passed. The model-case boundary refuses typed
`process_cleanup_unknown` before reading model output. Terminal observation exceptions are caught
inside the command runner's real cleanup boundary so the owner-only digest-bound refusal receipt is
written instead of losing the cleanup result to an escaping exception.

Deterministic RED tests reproduced incomplete first and second snapshots, a watcher observation
failure followed by recovery, terminal observation exceptions, nullable nested aggregation, empty
terminal evidence, and strict receipt/publication tampering. Final verification passed 204 focused
tests twice, a 191-test non-overlapping related partition, and a 480-test non-overlapping safe
auth/privacy partition. Earlier comprehensive runs in the same corrective batch also passed 393
related and 564 auth/privacy tests. Ruff and changed-file format passed; BasedPyright reported 0
errors, 0 warnings, and 0 notes; plugin, nine-skill static, 60-tool/294-operation catalog, 34-case
evaluation-manifest, ten-path privacy, and `git diff --check` gates passed.

Only isolated local fixture-install tests ran. No exact candidate or product install, model,
production MCP server, Saxo request, broker operation, browser, sealed proof, or broker/data network
activity ran. The exact `e5dc932` result remains the latest sealed evidence with 10/11 hard cases,
unknown broker/account/overall-cleanup facts, no retry, no downstream numerical proof or fresh SIM
matrix, and 0/54 active profiles. Independent static review of `a59b231` is the next gate.

## Local post-launch observability correction after `a59b231`

Local source `dd85c9ca55cb43a677195320b714ee37bb6d5c6b`, tree
`b8ad581990c36c06e22b7efadf98de59519749bd`, closes two independent false-negative paths without
changing any sealed evidence. After a model process starts, timeout, unknown or residual cleanup,
unparsed output, malformed output, and post-spawn operating-system failures now use one strict
unobservable failure state. Transcript grading, assistant evidence, tool identities, and all
model, command, MCP, and Saxo event counts remain null or unknown; they can no longer become false,
zero, or an empty tool set. The same nullable state is authenticated through the per-case summary,
verified child failure, candidate result, and outer publication.

Immediately after a successful process spawn, nested evaluation records cleanup as pending, counts
at least the root process as created, and leaves remaining processes unknown before any fallible
process-group or scope observation. A post-spawn `ProcessLookupError` or `OSError` therefore cannot
be reported as not-required cleanup with zero created or remaining processes. Legacy pre-launch
failures retain their prior behavior, and the historical `dual_v1` path remains supported;
malformed output is fail closed for either harness.

Deterministic RED tests covered timeout, cleanup uncertainty and residue, parser failure, mixed
malformed streams, both harnesses, post-spawn process-group failure through the production runner,
authenticated child/candidate/outer propagation, and tampering. Final verification passed 153
focused tests twice, 356 related evaluation/cleanup/proof tests, and 387 bounded auth/privacy tests.
Ruff check and changed-file format passed; BasedPyright reported 0 errors, 0 warnings, and 0 notes;
plugin, nine-skill static, 60-tool/294-operation catalog, 34-case evaluation-manifest, nine-path
privacy, and diff gates passed. One overbroad local test selection reached a synthetic install
fixture and was stopped; it is excluded from the counts and left no residual Saxo test or install
process. No exact or product install, model, production MCP, Saxo request, broker operation,
browser, sealed proof, or broker/data network activity ran.

This source is not installed or sealed. The exact `e5dc932` result remains the latest sealed
evidence at 10/11 hard cases with unknown broker/account/overall-cleanup facts. No retry or
downstream numerical proof, fresh SIM matrix, reconciliation, account equality, or activation ran;
all 54 profiles remain quarantined with 0/54 active. Independent static review of `dd85c9c` is the
next gate.

## Local router observability correction after `dd85c9c`

Local source `9fbd2cbeaead752e6c8e490d3782a1c1b9eda872`, tree
`40d65254641540e0e25cf685154750b298c857ce`, closes the router-model bypass without changing any
sealed evidence. Router and non-router execution now share one non-circular unobservable failure
constructor. For both Codex and the historical Claude harness, a router timeout, unknown or null
cleanup result, cleanup residue, malformed or invalid structured output, or post-spawn exception
retains null transcript grading, tool identities, and model, command, MCP, and Saxo event counts.
An unobservable router decision must also remain null.

Router results are gated immediately after the managed model process and before output parsing or
observable record construction. A successful, fully decoded router result still reports its
decision and exact zero tool, command, MCP, and Saxo events. A real production-shaped `Popen`
followed by injected PGID failure records the root as created, cleanup and remaining processes as
unknown, and preserves those nullable facts through the evaluation report, authenticated failure
summary, verified child failure, candidate result, and outer publication. Strict schemas reject an
injected router decision, event count, tool identity, grading result, or other parse-derived fact.

TDD first reproduced 14 router failures. Final verification passed 191 focused tests twice, 370
related evaluation/cleanup/proof tests, 388 bounded auth/privacy tests, and 15 catalog-status tests.
Ruff and changed-file format passed; BasedPyright reported 0 errors, 0 warnings, and 0 notes;
plugin, nine-skill static, 60-tool/294-operation catalog, 34-case evaluation-manifest, 11-path
privacy, and diff gates passed. The new internal failure-record module is explicitly excluded from
production MCP status routing, and generated catalog source digests were refreshed deterministically.

No exact or product install, model, production MCP, Saxo request, broker action, browser, sealed
proof, or broker/data network activity ran. This source is uninstalled and unsealed. Sealed
`e5dc932` remains unchanged at 10/11 hard cases with unknown broker/account/overall-cleanup facts;
no retry or downstream numerical proof, fresh SIM matrix, reconciliation, account equality, or
activation ran. All 54 profiles remain quarantined with 0/54 active. Independent static review of
`9fbd2cb` is the next gate.

## Local router version-lifecycle correction after `9fbd2cb`

Local source `f96a3b2df57ab66c71d6bf04e40b5637b40ef09b`, tree
`265a11abb3abbd19d6f54cdd122c19d8bb4453f0`, extends the router's fail-closed boundary through the
post-model client-version child. A version timeout, unknown or null remaining-process count,
unknown or noncomplete process cleanup, residue, or caught launch/runtime exception now returns the
same strict unobservable router record for Codex and the historical Claude harness. The decision,
grading, invoked tools, and all model, command, MCP, and Saxo counts remain null.

Report completion now independently checks the sticky timeout flag, semantic cleanup result, and
known-zero remaining count instead of trusting an optimistic summary boolean. Caught exceptions are
reduced to fixed lowercase allowlisted reasons, including distinct `permission_error`,
`file_not_found_error`, `os_error`, `value_error`, and `key_error` values. Those reasons survive the
authenticated report, child, candidate, and outer publication layers without exception messages,
paths, or negative broker facts.

TDD first reproduced 15 lifecycle and reason-propagation failures. Final focused verification
passed 16 tests twice; the related evaluation, cleanup, and proof suite passed 307 tests; and the
bounded auth/privacy partition passed 265 tests. Ruff and changed-file format passed, BasedPyright
reported 0 errors, 0 warnings, and 0 notes, and plugin, nine-skill static, 60-tool/294-operation
catalog, 34-case evaluation-manifest, bounded privacy, and diff gates passed.

No install, model, production MCP server, Saxo request, browser, sealed proof, or broker/data network
activity ran. Sealed `e5dc932` remains unchanged at 10/11 with unknown broker/account/overall
cleanup facts, no retry, and no downstream proof, fresh matrix, reconciliation, account equality,
or activation. All 54 profiles remain quarantined with 0/54 active. Independent static review of
`f96a3b2` is the next gate.

## Terminal exact `f96a3b2` install preflight refusal

Independent static review approved source `f96a3b2df57ab66c71d6bf04e40b5637b40ef09b`, tree
`265a11abb3abbd19d6f54cdd122c19d8bb4453f0`. The exact clean detached source, disk, process, and
owner-only evidence-root prechecks passed. The one authorized retained-runtime install then exited
1 before install work with typed reason `codex_install_run_root_exists`: the task had pre-created
the empty owner-only install-runtime directory, while the candidate contract requires that path to
be absent so the installer can create it transactionally.

The mode-0600 refusal report contains no secondary errors. Source readback confirms this check runs
before clone, plugin install, retained-runtime creation, client launch, or global-state mutation.
No verify-only, full suite, static/readback rerun, SIM auth, session capability call, model, MCP,
Saxo request, proof, numerical matrix, account comparison, or activation ran. No task-owned process
remained, the detached source stayed clean, and the system disk retained 77 GiB free.

The authorization allowed one install attempt and no retry, so the sequence stopped. This local
invocation error does not invalidate the already reviewed source, but it provides no install proof
for `f96a3b2`. Sealed `e5dc932` remains the latest sealed evidence, its downstream facts remain
unknown, and all 54 profiles remain quarantined with 0/54 active. A new exact install attempt needs
fresh authorization and an absent installer-owned run root.

## Terminal exact `f96a3b2` second install attempt

Fresh authorization reused the independently approved source
`f96a3b2df57ab66c71d6bf04e40b5637b40ef09b`, tree
`265a11abb3abbd19d6f54cdd122c19d8bb4453f0`, without relabeling or replacing the first refusal
receipt. Read-only prechecks proved that the first attempt's empty installer root was owner-only,
not a symlink, unused, and separate from its retained evidence. A new owner-only evidence namespace
was created while its unique installer-owned child run root was left absent for transactional
creation. The detached source was clean and exact, no task-owned install/proof process existed, and
the system disk had 76 GiB free before launch.

The one authorized retained-runtime install ran once and exited 1. Its mode-0600 typed report says
`codex_install_self_verify_failed` with the sole error
`codex_global_state_verify_window_mismatch`. The report publishes no candidate, inventory,
retained-runtime, or cleanup fields, so install exactness and authenticated cleanup completion are
unknown. A final local observation found no task-owned process or open file, but the remaining
owner-only 28 MiB run root was preserved because that later observation cannot repair the missing
authenticated cleanup result.

The stop-on-failure rule prevented verify-only, the signed full suite, post-suite static/readback
gates, SIM auth, session capability, model evaluation, sealed proof, numerical proof, the fresh
60-tool SIM matrix, reconciliation, account comparison, or activation. No later broker-bound stage
was invoked, no retry ran, and no result is promoted from this failed install. Sealed `e5dc932`
remains the latest sealed evidence; all 54 profiles remain quarantined with 0/54 active.

## Terminal exact `f96a3b2` third attempt

The first two attempts and their receipts remained untouched. After the reviewer's metadata
refresh, 12 read-only caller-cache fingerprints stayed identical for 575 seconds: digest
`92531540...93d4`, 22,434 cache nodes, and an unchanged latest cache mtime. The new owner-only
evidence parent was created only after that quiet window; its unique installer child remained
absent for transactional creation. The detached source
`f96a3b2df57ab66c71d6bf04e40b5637b40ef09b`, tree
`265a11abb3abbd19d6f54cdd122c19d8bb4453f0`, was clean and the system disk had 75 GiB free.

The single retained-runtime install passed with 621 byte-exact files, 9 skills, 1 MCP server,
60 tools, unchanged caller fingerprint, owner-only state, privacy pass, and complete process
cleanup with zero remaining PIDs or groups. The single verify-only readback also passed with
60 tools and unchanged caller state. The signed exact-candidate full suite then passed 2,943 tests
with zero failures, errors, or skips; candidate source was clean before and after, raw output was
not retained, and the candidate-owned external temp root was absent at completion.

Post-suite Ruff lint, BasedPyright (0 errors, 0 warnings, 0 notes), Codex plugin validation, all
nine skill validators, static gates, the 60-tool/294-operation catalog, the 34-case evaluation
manifest, and the 16-path privacy scan passed. Whole-repository Ruff formatting still reports the
known 95-file baseline drift; those files were not changed and formatting is not claimed as passed.

The terminal install-structure readback then failed closed because the caller plugin-cache
fingerprint had changed to `b44a6cf6...c610` after the full suite. All 19 other structural checks
passed, including binding, cache/source/interpreter/lock/producer/probe digests, exact inventory,
clean clone, candidate/tree binding, owner-only modes, authenticated install cleanup, and the
present unconsumed proof runtime. The exact concurrent writer is not established. Per the
stop-on-first-mismatch rule, local auth, session capability, model evaluation, sealed proof,
all-54 numerical proof, fresh 60-tool SIM matrix, reconciliation, controlled lifecycle, account
comparison, and activation did not run. No retry occurred; the owner-only runtime remains preserved
and unconsumed, no task-owned process remains, sealed `e5dc932` remains the latest sealed evidence,
and all 54 profiles remain quarantined with 0/54 active.

## Recovered third-attempt verification and sealed proof

Independent review attributed the earlier caller-cache change to concurrent Codex app-server
plugin refresh, not to the candidate or its 2,943-test suite. The preserved third-attempt install
was not reinstalled and the suite was not rerun. A fresh verify-only window against that same
owner-only unconsumed runtime passed. Its digest-bound structural receipt records all 20 checks
passing, caller fingerprint `b44a6cf6...c610` unchanged from start to end, 22,434 cache nodes,
exact source/tree and prior install/verify/suite/JUnit digests, zero task processes, and an
unconsumed runtime. The earlier failed structural receipt remains byte-for-byte unchanged.

Local no-network auth status then proved requested/effective `SIM`, LIVE reads false, LIVE writes
false, a readable unexpired SIM cache, and no blockers. The one separate read-only capability call
passed with `network_call_made=true`, `token_refreshed=false`, one call, and no retry. It did not
publish mutation, purchase, or disclaimer negatives, so those fields remain unknown.

The one sealed `codex_native_v1` proof ran through the exact candidate-source-root and failed with
no retry. Its authenticated candidate result records a passed SIM preflight and an 11-case native
evaluation with 10 passes. `backtest-limitations` alone failed with `non_saxo_mcp_event`: its one
required Saxo tool was invoked, but grant/assertion status failed because additional MCP events were
not classified as Saxo. All other cases, including artifact delivery and scenario, passed their
exact grants, required tool counts, and assertions. Nested evaluation cleanup is authenticated
complete with 12 created processes, zero remaining processes, and zero persisted raw outputs.

The proof still fails closed. The proof-child cleanup receipt and candidate-runner cleanup receipt
both report `observation-unknown` / `coverage_unknown` with nullable remaining counts. The inner
reason is `proof_child_cleanup_failed`; the authenticated outer publication is a
`proof_candidate_runner_cleanup_failed` boundary. Model/MCP/Saxo aggregate counts and broker write,
mutation, purchase, and disclaimer-response facts remain unknown rather than false. The one-shot
runtime consumption and cleanup receipts authenticate complete removal, the proof temp is absent,
and later observation finds zero task processes, but those later facts do not repair unknown
cleanup coverage. No numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation, controlled
lifecycle, account equality, or activation ran. All 54 profiles remain quarantined with 0/54 active.

## Local cleanup and non-Saxo diagnostic hardening

Local source `a7bbf4aca5100a6e8ee06d31adc66d857193728a`, tree
`603ef56df322ca32c93c7ff22b9c07436a2108c6`, adds diagnostic provenance without changing the
sealed result. Cleanup uncertainty now retains one strict stage and subreason across the process
scope, terminal snapshot, owner-only digest-bound receipt, child failure, candidate runner, and
outer publication. The allowlist distinguishes first- and second-snapshot failure, watcher capture
failure, post-exit and final-snapshot failure, incomplete group tables, uncaptured or changed group
members, and target-observation uncertainty. Current receipts with unknown coverage require this
diagnostic and nullable remaining counts; malformed, inconsistent, or tampered combinations fail
closed. Historical authenticated receipts retain their legacy verification path and are not
reinterpreted.

Failed-case evidence now also carries a strict privacy-safe descriptor for a classified non-Saxo
event. It records only the outer event type, item type, fixed server category, and either an
allowlisted protocol name or SHA-256 identities for server/tool/name. Raw names, arguments,
payloads, handles, paths, transcripts, and broker/account values are forbidden by schema and tests.
Foreign MCP, known Codex protocol, empty/unknown identity, tamper, and privacy cases are covered.

TDD first reproduced 19 expected failures. Focused verification passed 195 tests twice; the selected
related/auth/privacy partition passed 676 tests. Ruff lint and changed-file formatting passed,
BasedPyright reported 0 errors, 0 warnings, and 0 notes, and plugin, nine-skill static,
60-tool/294-operation catalog, 34-case evaluation-manifest, nine-path source privacy, and diff checks
passed. No install, model, production MCP server, Saxo request, browser, sealed proof, or broker/data
network activity ran in this source/test batch.

This correction does not reconstruct or guess the sealed attempt's exact cleanup subcause or the
identity of its non-Saxo event. Its 10/11 result, unknown aggregate broker and cleanup facts, no-retry
boundary, and 0/54 activation remain unchanged. Independent static review of `a7bbf4a` is the next
gate; no exact install or sealed proof is authorized by this local correction alone.

## Descriptor-free historical failure compatibility

Local source `1d213ff9180dd2e65c424a49d3449aaa4412bb18`, tree
`edd8347350b15f79072652362d4daf8361b6d3c7`, completes the historical compatibility boundary for
the privacy-safe non-Saxo descriptor. One shared material helper removes the nested
`non_saxo_event_descriptors` field only when a present historical evaluation summary proves that
every case omitted the field from its parsed field set. The same helper now governs the summary
digest, child-envelope digest, verified-child material, and outer verified-failure publication.
The parsed descriptor remains `None`; current, mixed-presence, extra, malformed, or recomputed
tamper material remains strict and is rejected.

TDD reproduced the child and publication digest mismatch before the correction. Final local
verification passed the five focused regressions twice, 130 failure/publication tests, 385 related
proof/evaluation tests, and 262 auth/privacy tests. Ruff lint and changed-file format checks passed;
BasedPyright reported 0 errors, 0 warnings, and 0 notes; plugin validation, nine-skill static gates,
the 60-tool/294-operation catalog, the 34-case eval manifest, three-path source privacy, and diff
checks passed. No install, model, production MCP, Saxo request, browser, network, or proof ran.
The prior sealed 10/11 evidence remains unchanged and is not relabeled; 0/54 profiles remain active.
Independent static review of `1d213ff` is the next gate.
