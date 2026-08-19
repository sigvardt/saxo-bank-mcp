# Saxo analytics and BI suite: Task 24 native validation

Status: partial validation; analytics activation is not approved
Date: 2026-08-18
Harness policy: `codex_native_v1`
Independent review: pending the root orchestrator's separate native review

Candidate `c716047a332a78cccd0d1c94ed4348e7edbd1169`, tree
`3170c9d4dc7d3ff63987999892209e7b19b98275`, passed its exact Codex-only retained-runtime
installation, signed 2,835-test full suite, required local gates, and current SIM safety preflight.
Its one sealed proof command then returned an authenticated `verified_child_failure`. Ten of eleven
native hard cases passed. The `scenario` case failed `transcript_assertion_failed` despite a passed
grant check and exact invocation of its two required logical tools. Both raw decoded assistant
events and final parsed text were observable and had the same required-all vector
`[true, false, true]`: the exact required phrase `explicit numeric shocks` was absent. The outer
failure reason is `proof_child_cleanup_failed`.

No retry ran. The numerical proof, fresh 60-tool SIM matrix, Saxo reconciliation, account-equality
readback, and activation gates did not run. All 54 proof profiles remain quarantined and 0 are
active.

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
