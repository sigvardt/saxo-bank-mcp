# Streaming limitations

## Table of contents

- Supported
- Explicitly unsupported
- Safe claims
- Forbidden claim patterns
- Catalog alignment

## Supported

The MCP supports bounded SIM price-stream lifecycle guidance:

- Create a SIM price subscription with `saxo_create_streaming_price_subscription`.
- Record REST snapshot evidence.
- Read one WebSocket frame.
- Identify control-only frames.
- Pass a `last_message_id` cursor value.
- Clean local records with `saxo_cleanup_streaming_subscriptions`.
- Attempt remote cleanup when a cached SIM token exists.
- Recover from missing auth by fixing local auth/cache before retry.

## Explicitly unsupported

Refuse these capabilities directly:

- LIVE streaming is unsupported. The tools deny LIVE with `streaming_sim_only`.
- Split-frame reassembly is unsupported. The parser expects a complete binary frame.
- Delta reduction is unsupported. The runtime does not apply deltas to snapshots.
- Queued pre-snapshot updates are unsupported. The runtime does not buffer deltas before a snapshot.
- Reauthorization is unsupported. The runtime does not refresh auth inside an active stream.
- Reset repair is unsupported. `_resetsubscriptions` is a control message, not a repaired subscription state.
- `_disconnect` recovery is unsupported. The runtime does not recover from `_disconnect`.

Do not mention these phrases outside this unsupported/refusal context.

## Safe claims

Say a stream completed only when status is `completed` and `streaming_completion_claim_allowed=true`.

Say local cleanup completed only when local registry fields prove zero local records remain.

Say remote cleanup was accepted only when `remote_cleanup_accepted=true`.

Say remote deletion is not proved when `remote_cleanup_confirmed=false`.

Say no network call occurred when `network_call_made=false`.

Say Authorization header use is true only from `authorization_header_used=true`. Do not expose header values.

## Forbidden claim patterns

Do not say the skill or MCP supports full streaming.

Do not say a cursor repaired the stream.

Do not say a reset was fixed.

Do not say a disconnect was recovered.

Do not say remote cleanup deleted the broker subscription unless `remote_cleanup_confirmed=true`.

Do not say a local leak cleanup is broker cleanup proof.

Do not say LIVE is available through a SIM success path.

## Catalog alignment

The generated tool catalog assigns both streaming tools to `saxo-streaming`.

The generated scenario catalog marks both as SIM executable controlled lifecycle tools:

- `saxo_create_streaming_price_subscription`: create a SIM price subscription, then call cleanup.
- `saxo_cleanup_streaming_subscriptions`: clean a stale or newly created SIM price subscription context, then verify local records are empty.

The generated status catalog marks `control_only_no_data`, `incomplete_no_frame`, and `cleanup_remote_failed` as cleanup-before-retry states with possible mutation. Treat `auth_required` as no network write sent.
