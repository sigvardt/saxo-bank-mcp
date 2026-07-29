# Price stream lifecycle

## Table of contents

- Scope
- Create planning
- Frame interpretation
- Cleanup proof
- Recovery
- QA command

## Scope

Teach only the implemented SIM price-stream subset.

The runtime exposes two logical tools:

- `saxo_create_streaming_price_subscription`
- `saxo_cleanup_streaming_subscriptions`

The create tool validates IDs, checks the SIM token cache, creates one or more price subscription REST records, opens the SIM WebSocket endpoint, reads one frame, and returns structured evidence.

The cleanup tool removes local records for a context. With a cached SIM token, it also attempts Saxo root subscription cleanup for that context.

## Create planning

Use synthetic IDs:

- `context_id`: non-secret value, max 50 characters, letters, digits, `_`, or `-`.
- `reference_id`: non-secret value, max 50 characters, letters, digits, `_`, or `-`, and not starting with `_`.

Use a small SIM price fixture unless the user gives a safe SIM instrument. The tested fixture uses UIC `21` and `FxSpot` or `Stock` in local tests. Do not present the fixture as market advice.

Keep the limits in the response:

- 4 simultaneous streaming connections.
- 200 price instruments.

The create tool uses an Authorization header. Do not reveal or reconstruct the header value.

If more than one UIC is requested, the runtime derives per-UIC reference IDs by suffixing the base reference. Keep the user's base reference synthetic.

## Frame interpretation

Treat `streaming_completion_claim_allowed=true` as required before saying the stream completed.

Completion means both of these were observed:

- A REST subscription snapshot.
- A non-control WebSocket data frame.

Treat `_heartbeat`, `_resetsubscriptions`, and other leading-underscore references as control messages. A control-only frame is not usable price data.

Treat `last_message_id` as a Saxo `messageid` cursor input only. It does not prove that the runtime repaired a broken stream or replayed missed state.

Do not reduce deltas into snapshot state. The runtime reports frames and parsed payloads, but it does not maintain a reduced price book.

## Cleanup proof

Run cleanup after every create path that records or may record a subscription.

Report local cleanup proof from these fields:

- `local_registry_before_count`
- `local_registry_after_count`
- `local_removed_reference_ids`
- `local_open_records_after`
- `open_subscription_left`
- `open_subscription_left_scope`

Say local records are closed only when `local_registry_after_count=0` and `open_subscription_left=false`.

Report remote cleanup honestly:

- `cleanup_attempted=false`: no remote cleanup request was sent.
- `remote_cleanup_accepted=true`: Saxo accepted the delete request.
- `remote_cleanup_confirmed=false`: remote deletion is not proved.
- `remote_subscription_may_remain=true`: a broker-side subscription may remain.

Do not convert request acceptance into deletion proof.

## Recovery

For `auth_required`, no create network call or cleanup network call was made. Fix local SIM auth/cache through the auth skill, then retry the needed logical step.

For interruption after a create result, run cleanup for the same synthetic context before any retry.

For `control_only_no_data`, `incomplete_no_frame`, partial `http_error`, or `network_error` after snapshot evidence, run cleanup before retrying.

For invalid ID denials, choose new synthetic IDs before retrying.

For `streaming_sim_only`, refuse LIVE use. Do not retry in LIVE.

## QA command

Use this local simulation command when evidence needs cleanup proof without Saxo network access:

```bash
uv run python -m saxo_bank_mcp.qa stream-cleanup --simulate-leak --out <path>
```

The simulation registers a local fake subscription record and then calls cleanup through the in-process MCP server. With no cached SIM token, it produces `incomplete_auth_required`, closes local records, sets `network_call_made=false`, and does not contact Saxo.
