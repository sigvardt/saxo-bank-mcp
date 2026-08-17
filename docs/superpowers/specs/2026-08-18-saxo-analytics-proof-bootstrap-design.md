# Codex-native proof bootstrap and publication design

Date: 2026-08-18
Status: approved for implementation

## Purpose

Close the native proof startup gap before the installed producer module can import, and make every
published `codex_native_v1` proof result one authenticated object. Retain private, candidate-bound
JUnit evidence for the exact full suite used to authorize the next sealed attempt.

The legacy `dual_v1` producer, command, and publication shapes remain unchanged.

## Startup trust boundary

The native sealed command invokes `qa_analytics_proof_bootstrap.py` by its exact installed file
path. The bootstrap imports only Python standard-library modules. Its parent creates an owner-only
directory and supplies a unique envelope path plus the expected policy, candidate commit, installed
tree digest, bootstrap digest, producer digest, catalog digest, and contract digest.

Before importing the producer, the bootstrap atomically writes and durably syncs an entry envelope.
The envelope is strict JSON, bound to every expected value, and protected by a digest over all
fields other than the digest itself. The file must be a regular, single-link, owner-owned mode-0600
file under the parent-controlled runtime directory.

The bootstrap advances through these states:

1. `entered`: durable entry exists and no producer import has started.
2. `producer_imported`: the exact expected producer bytes imported successfully.
3. `producer_started`: control is about to enter producer `main`; proof, network, and broker facts
   become unknown until stronger evidence resolves them.
4. `failed`: the bootstrap caught and sanitized an import, argument, producer exception, or normal
   nonzero return.
5. `complete`: producer `main` returned zero.

An initial envelope-write failure stops before producer import. A hard child crash leaves the last
durable state. Import and binding failures can prove no proof execution or network activity. Once
`producer_started` is durable, absent producer phase evidence means unknown, never false or zero.

## Handoff to producer tracking

After successful import and binding validation, the bootstrap invokes the existing producer entry
point with reconstructed native arguments. The existing in-producer phase tracker remains the
authoritative source for SIM preflight, model, MCP, Saxo, broker, purchase, disclaimer, and cleanup
facts. On nonzero exit, the parent accepts those facts only when both the bootstrap envelope and the
producer phase envelope validate against the same command exit and expected bindings.

The parent never publishes raw stdout, stderr, exception text, filesystem paths, account values,
handles, credentials, tokens, URLs, or broker payloads. Fixed reason codes describe failures. A
missing, malformed, mode-unsafe, binding-mismatched, or digest-mismatched bootstrap remains unknown.

## Publication boundary

Every executed `codex_native_v1` matrix result is nested inside one strict outer publication
envelope. The outer object contains only:

- schema, receipt kind, native policy, and candidate binding;
- exact analysis-kind, evidence-receipt, and contract bindings;
- a typed result kind and its verified success, verified child failure, or boundary-failure receipt;
- redacted-publication status;
- one digest over every other publication field.

Strict validation rejects extras. Round-trip verification recomputes the digest and confirms the
candidate, policy, and contract agree with the nested result. The legacy dual and plan-only outputs
do not change.

## Exact full-suite evidence

A local stdlib runner verifies a clean candidate commit and tree, the mandated external temp root,
and an owner-only evidence directory. It invokes only `scripts/run-pytest`, retains its JUnit XML as
mode 0600, parses exact test counts, and writes a strict command receipt containing the candidate,
tree, external temp root, exit status, counts, JUnit digest, stdout and stderr digests, and one
receipt digest. The receipt stores no raw output. Both private artifacts remain outside public
documentation.

## Verification

Real subprocess tests cover bootstrap import failure, producer argument `SystemExit`, pre-tracker
failure, first-envelope write failure, hard child crash, normal nonzero, and normal success without
model or Saxo activity. Publication tests cover round trip, nested-result binding, extras, and
tampering. Full-suite receipt tests use a fake guarded launcher and JUnit fixture. Focused tests run
twice after GREEN, then related proof, auth, privacy, static, catalog, Ruff, and BasedPyright gates.
Only a committed clean candidate can proceed to one exact install, one retained full suite, and one
sealed native proof attempt.
