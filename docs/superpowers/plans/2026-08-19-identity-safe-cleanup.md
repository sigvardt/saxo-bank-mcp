# Identity-safe cleanup correction plan

> Execute locally only. Do not invoke model, MCP, Saxo, broker, browser, network, install, or proof workflows.

1. Add deterministic RED regressions for PID reuse between selection and signal, reused group leaders, late group members, survivor/zombie/unknown coverage, nested eval reuse, exact-install reuse, and strict child-failure cleanup evidence.
2. Introduce one shared birth-bound cleanup primitive. In `run_command`, admit new identities only while the original `Popen` root is unreaped, active, and still matches its captured birth identity and group; close admission permanently after completion, reaping, absence, reuse, unknown observation, or mismatch. Discover candidate numeric PIDs, observe their birth identities, and then require each observed PID and its process group to remain in a second root-bound scope snapshot bracketed by those same root checks. Re-observe every candidate after that snapshot and require the same PID, birth identity, and group before the final root check. Replaced, moved, missing, unknown, or incompletely observed candidates stay detection-only and make coverage unknown if still present. Freeze only admitted identities as signal targets, treat every later PID/group scan as detection-only, recheck identity inside each signal operation, and perform a terminal coverage rescan.
   Command-runner watcher publication must register under the same lock before scope capture. Cleanup transitions admission from open to draining, closes the root gate, and stops the watcher before the target tuple is frozen. An already-registered capture may publish only during that bounded drain; a pass still alive, discarded after freeze, failed during capture, or affected by a join error makes coverage unknown. No watcher may mutate signal targets after freeze, and authenticated cleanup receipts bind only a drained or not-applicable watcher state.
3. Migrate the command runner, native eval manager, and exact install cleanup/audit paths away from raw historical PID/PGID signalling and counting. The command runner and native eval manager must use the same root-handle-bound, two-snapshot identity-admission gate; neither path may admit from a numeric PID/PGID scope captured without `Popen.poll()` brackets and exact post-scope PID, birth, and group confirmation.
4. Make child-failure verification fail closed unless cleanup evidence is authenticated or proves that no target was observed, with consistent digest and known complete semantic counts. Preserve any unknown result in a separate owner-only digest-bound receipt with one fixed allowlisted reason, watcher-drain state, coverage state, target count, and null remaining counts. Propagate its digest and reason through child, candidate-runner, and outer failure receipts without promoting any event or broker-safety negative.
5. Run focused tests twice, broader local auth/privacy tests, Ruff/format, BasedPyright, plugin/static/catalog/eval/privacy checks, then update tracked docs, ignored Task 24 evidence, and existing Knowledge Base rows with readback.
6. Commit the verified source and documentation, report exact SHA/tree/clean state, and stop before install or proof.

Coverage completion rule: every checked process-table snapshot contributes to one sticky coverage
state. Failure or incomplete coverage in the first snapshot, second snapshot, watcher, or terminal
observation cannot be erased by a later successful observation. Terminal cleanup still runs, but
the owner-only digest-bound receipt reports a fixed unknown reason and null remaining counts.
Nested evaluation must retain those nulls through aggregation and publication; no terminal snapshot
is unknown, not passed.

Post-launch observability completion rule: once `Popen` succeeds, cleanup is pending, the root is
counted as created, and remaining processes are unknown before any PGID or process-table read.
Timeout, uncertain or residual cleanup, parser failure, malformed output, and post-spawn operating-
system failure use the unobservable record path. Every parse-derived grading, tool identity, and
model/command/MCP/Saxo event count stays null or unknown through the signed case, child, candidate,
and outer schemas. Only a completely decoded observable result may publish zero or a no-call fact.

Router completion rule: router and non-router cases use the same unobservable failure constructor.
The router path checks timeout, null or unknown cleanup, residue, and post-spawn exceptions before
parsing or observable record construction; invalid or malformed structured output also remains
unobservable for either harness. Unknown router evidence has no decision, grading result, invoked
tool identity, or model/command/MCP/Saxo count. A fully decoded successful router decision remains
eligible for observable exact-zero event evidence.

Diagnostic provenance completion rule: cleanup uncertainty carries one authenticated stage and
subreason through the scope, terminal snapshot, owner-only receipt, child failure, candidate runner,
and outer publication. The fixed vocabulary distinguishes first snapshot, second snapshot, watcher
capture, post-exit snapshot, final snapshot, group-table incompleteness, uncaptured member, changed
identity/group member, and target observation. Unknown coverage keeps remaining counts null.
Missing, inconsistent, or tampered current diagnostics fail closed; legacy digest-bound receipts
remain readable without being rewritten.

Non-Saxo descriptor completion rule: a failed case may retain only an outer event type, item type,
fixed server category, and either an allowlisted protocol name or SHA-256 server/tool/name
identities. Unknown/empty identity remains explicit. Raw names outside the allowlist, arguments,
payloads, paths, handles, transcripts, and account or money values are forbidden. The descriptor is
strictly authenticated through the failed-case summary and may diagnose a future attempt only; it
must never be used to infer missing facts from historical evidence.

Historical descriptor compatibility rule: use one shared material helper across the summary,
child envelope, verified-child receipt, and outer verified-failure publication. It may remove the
nested descriptor field only when every case in a present parsed historical summary omitted that
field from `model_fields_set`. It must preserve `None` after parsing and reject current, mixed,
extra, malformed, or tampered material. Source `1d213ff` passes focused-twice, related,
auth/privacy, Ruff/format, type, plugin, static, catalog, eval-manifest, privacy, and diff gates and
remains uninstalled/unsealed pending independent review.
