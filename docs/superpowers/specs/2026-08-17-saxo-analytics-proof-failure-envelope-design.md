# Codex-native analytics proof failure envelope design

Date: 2026-08-17
Status: approved for implementation

## Purpose

Make a failed `codex_native_v1` proof preserve truthful, privacy-safe execution provenance across
the child process, command runner, native wrapper, and public proof-matrix receipt. A nonzero child
exit must never be rewritten as no execution, no network, no model call, or no broker effect unless
the retained evidence proves that exact fact.

The legacy dual-agent result types and execution path stay unchanged.

## Trust boundary

The proof child is the only process that knows detailed phase progress. Before exiting nonzero it
emits one strict JSON failure envelope on stdout. The envelope is authenticated with a digest over
all material fields and bound to the schema, `codex_native_v1` policy, candidate commit, installed
cache digest, module inventory digest, catalog digest, contract digest, and child exit code.

The command runner retains failed child stdout in memory long enough for the native wrapper to
validate the envelope. Raw stdout and stderr are never copied into a receipt, log, or exception
message. The wrapper independently verifies the command receipt and every binding before accepting
the child envelope.

## Failure receipt

The child envelope records:

- completed phases and the current phase;
- child exit code;
- SIM preflight status and network provenance;
- model, MCP, and Saxo event counts when known;
- tri-state execution, broker-write, mutation, purchase, and disclaimer outcomes;
- child cleanup and process state;
- a fixed-vocabulary or otherwise sanitized reason;
- redacted-publication status and an envelope digest.

The wrapper adds the command receipt binding, outer runtime cleanup state, remaining process counts,
and a second digest. The proof-matrix script publishes only this verified receipt.

## Truth rules

Known facts remain known. Unknown facts remain `null` and are never converted to `false` or zero.
An authenticated failure before preflight may prove that no proof execution or broker work began.
Once preflight starts, network provenance is unknown until the typed preflight receipt resolves it.
Once model or controlled SIM work starts, write, mutation, purchase, and disclaimer outcomes remain
unknown until a completed receipt proves their values.

Missing, malformed, digest-mismatched, binding-mismatched, or crash-without-envelope output is not
trusted. The wrapper publishes an unknown failure receipt with only independently observed command
exit and cleanup facts. A cleanup failure also prevents false negative outcome claims.

## Privacy rules

The envelope schema has no field for credentials, tokens, PKCE values, account or client keys,
analysis handles, authorization URLs, callback data, request or response bodies, broker payloads,
raw stdout, or raw stderr. Reasons use redacted identifiers, not arbitrary exception text. Strict
schema validation rejects extra fields. Public privacy validation checks both field names and values.

## Phase model

The child advances monotonically through:

1. `before_preflight`
2. `sim_preflight`
3. `agent_evaluation`
4. `offline_proof`
5. `sim_matrix`
6. `bundle_validation`
7. `cleanup`
8. `complete`

The tracker updates completed phases only after their typed outputs validate. Phase-injected tests
cover failures before and after preflight, after model or MCP activity, during offline proof, during
the controlled SIM matrix, and during cleanup.

## Parent behavior

`CommandFailureError` retains child stdout and stderr only as ephemeral in-memory fields plus process
cleanup counts. The native wrapper accepts stdout only if it is exactly one valid envelope for the
expected candidate and install. It never includes raw output in its public result.

Expected child failures raise a typed native proof exception carrying the verified receipt. Missing,
malformed, tampered, or crashed output carries an unknown receipt. Generic native errors in the
proof-matrix script also publish unknown tri-state fields. Existing successful native results and
the complete legacy dual path keep their current schemas and behavior.

## Verification

Tests first demonstrate the current false-negative collapse. They then require correct results for
each injected phase, failed command transport, missing output, malformed output, tampering, child
crash, cleanup failure, and successful legacy regressions. Focused tests run twice through the
guarded pytest wrapper, followed by the related proof and command-runner suite, Ruff, BasedPyright,
static validation, and privacy checks. Only a committed clean candidate may proceed to install, one
full suite, and one sealed proof attempt.
