---
name: saxo-streaming
description: "Guide bounded Saxo Bank MCP SIM price-stream lifecycle work. Use when a user asks to plan, explain, create, inspect, recover, or clean up the implemented SIM price streaming subset: create price subscription, snapshot evidence, one frame read, control-frame handling, message cursor planning, auth-missing recovery, and cleanup proof. Also use when refusing unsupported Saxo streaming capabilities such as LIVE streaming, split-frame reassembly, delta reduction, queued pre-snapshot updates, reauthorization, reset repair, or `_disconnect` recovery."
---

# Saxo streaming

Use this skill for the implemented Saxo Bank MCP SIM price-stream subset only.

## Invocation

For installed Codex, invoke this skill as `$saxo-bank-mcp:saxo-streaming`.

For Claude, invoke this skill as `/saxo-bank-mcp:saxo-streaming`.

Use logical tool IDs only:

- `saxo_auth_status` (optional SIM preflight: local cache/config only; no secrets in chat)
- `saxo_create_streaming_price_subscription`
- `saxo_cleanup_streaming_subscriptions`

Do not use harness-qualified names, wildcard grants, broad tool grants, arbitrary URLs, direct Saxo HTTP calls, direct WebSocket calls, or copied runtime input schemas.

## Load references

- Read [price-stream-lifecycle.md](references/price-stream-lifecycle.md) before planning, explaining, creating, inspecting, recovering, or cleaning up a SIM price stream.
- Read [streaming-limitations.md](references/streaming-limitations.md) before answering any question about supported scope, unsupported behavior, refusals, cleanup proof, or official Saxo streaming features outside this MCP subset.

## Plan-only boundary

Treat a request as PLAN-ONLY when it asks to plan, explain, interpret, review, dry run, describe, or not execute.

In PLAN-ONLY mode, emit zero Saxo MCP calls, zero broker network calls, zero WebSocket calls, zero auth calls, and zero cleanup calls. Inspect only this skill, its direct references, plugin metadata, and checked-in generated catalogs with bounded local read-only commands.

Name the logical tool sequence as text only. Do not execute any step.

Do not write files, create plan files, update task files, or call editing tools in PLAN-ONLY mode. Answer in chat only.

Switch to execution only after the user explicitly asks to run a step and gives a clear SIM scope. Execution still requires every create path to include cleanup.

## Supported matrix

| Capability | Support | Rule |
| --- | --- | --- |
| SIM price subscription create | Supported | Use `saxo_create_streaming_price_subscription` with synthetic `context_id` and `reference_id`. |
| REST snapshot evidence | Supported | Treat `subscription_snapshot_recorded=true` as snapshot evidence only. |
| One WebSocket frame read | Supported | Treat completion as true only when status is `completed` and `streaming_completion_claim_allowed=true`. |
| Control-frame recognition | Supported | Treat control-only frames as incomplete and requiring cleanup. |
| Message cursor parameter | Supported with limits | Pass `last_message_id` only as Saxo `messageid` cursor input. Do not promise transport recovery. |
| Local leak cleanup | Supported | Use `saxo_cleanup_streaming_subscriptions` and prove local records closed with local counts. |
| Remote cleanup attempt | Supported with limits | Report request acceptance separately from deletion proof. |
| Auth-missing recovery | Supported | Stop after `auth_required`; fix local auth/cache, then retry from create or cleanup. |
| LIVE streaming | Unsupported | Refuse. This MCP streaming subset is SIM-only. |
| Split-frame reassembly | Unsupported | Refuse. The runtime parses a complete binary frame only. |
| Delta reduction | Unsupported | Refuse. The runtime does not merge snapshots and deltas. |
| Queued pre-snapshot updates | Unsupported | Refuse. The runtime does not queue deltas before snapshots. |
| Reauthorization | Unsupported | Refuse. The runtime does not refresh auth inside a stream. |
| Reset repair | Unsupported | Refuse. `_resetsubscriptions` is observed as control-only evidence, not repaired. |
| `_disconnect` recovery | Unsupported | Refuse. The runtime does not recover from `_disconnect`. |

## Required lifecycle

Use this order for execution:

1. Optional: call `saxo_auth_status` once as local SIM preflight when session readiness is unclear. It is local-only and does not replace create or cleanup.
2. Choose synthetic IDs. Use non-secret `context_id` and `reference_id` values, max 50 characters, using letters, digits, `_`, or `-`. Do not start `reference_id` with `_`.
3. Keep limits visible. State Saxo's declared limits: 4 simultaneous streaming connections and 200 price instruments.
4. Create the SIM price subscription with `saxo_create_streaming_price_subscription`.
5. Inspect the structured result.
6. Treat `completed` plus `streaming_completion_claim_allowed=true` as the only successful stream completion condition.
7. Treat `control_only_no_data`, `incomplete_no_frame`, `http_error` after a partial create, or interruption after create as cleanup-required.
8. Always run `saxo_cleanup_streaming_subscriptions` for the same context after any create path that records or may record a subscription.
9. Report cleanup proof exactly. Local closure means local registry counts reached zero. Remote proof is not available unless the tool explicitly says `remote_cleanup_confirmed=true`; current runtime does not confirm remote deletion.

## Result interpretation

Handle create statuses as follows:

- `completed`: Say a REST snapshot and a non-control data frame were observed. Still run cleanup when the stream is no longer needed.
- `control_only_no_data`: Say only a control frame arrived. Do not claim usable prices. Run cleanup before any retry.
- `incomplete_no_frame`: Say no usable frame completed. Run cleanup before any retry.
- `auth_required`: Say no Saxo network call was made. Fix local SIM auth/cache, then retry from the needed step.
- `denied` with `streaming_sim_only`: Refuse LIVE streaming and do not retry in LIVE.
- `http_error` or `network_error`: Read cleanup fields. If any snapshot may have succeeded, run cleanup for the context before retry.

Handle cleanup statuses as follows:

- `completed`: Say the local registry was closed and Saxo accepted the cleanup request when `remote_cleanup_accepted=true`. Do not claim remote deletion.
- `auth_required`: Say local records can be closed, but remote cleanup was not attempted. Remote subscription may remain.
- `cleanup_remote_failed`: Say local records can be closed, but remote cleanup did not complete. Remote subscription may remain.
- `denied`: Fix the denied input or SIM-only mismatch before retry.

## Safety rules

Use Authorization headers only through MCP tools. Never put bearer tokens in URLs, logs, prompts, examples, evidence, or summaries.

Never ask the user to paste access tokens, refresh tokens, credential files, cache contents, authorization headers, or broker payloads into chat.

Do not say "live verified", "remote deleted", "stream recovered", or "subscription repaired" unless the structured fields prove that exact claim.

Do not infer price continuity from a cursor. `last_message_id` is only an input cursor for the next connect attempt.

Do not treat local cleanup as broker deletion proof. It proves only the local registry count for the current process.

Do not claim the server has no streaming transport. The server has a bounded SIM streaming transport for one price-stream create, one frame inspection, cursor input, and cleanup. Refuse only the unsupported behavior listed in the matrix.

## Refusal wording

For unsupported requests, answer with the unsupported capability name and the safe alternative.

Use this form:

`Unsupported: <capability>. This MCP implements only bounded SIM price-stream create, one-frame inspection, cursor input, and cleanup through saxo_create_streaming_price_subscription and saxo_cleanup_streaming_subscriptions. I can plan that SIM lifecycle without calls, or run it only after an explicit SIM execution request.`
