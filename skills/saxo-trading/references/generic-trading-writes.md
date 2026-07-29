# Generic Trading writes

Use this reference for implemented non-order Trading writes that are not routed through specialized order tools.

## Route selection

Start with `saxo_list_trading_write_operations`. It is local metadata and makes no Saxo network call.

Use specialized order tools for these operations:

- `post.trade.v2.orders`
- `patch.trade.v2.orders`
- `delete.trade.v2.orders`
- `delete.trade.v2.orders.orderids`
- `post.trade.v2.orders.multileg`
- `patch.trade.v2.orders.multileg`
- `delete.trade.v2.orders.multileg.multilegorderid`

Use `saxo_prepare_trading_write` and `saxo_execute_trading_write` for other implemented non-GET Trading operations only. Do not use this gateway for non-Trading writes, refused operations, arbitrary URLs, or undocumented methods.

## Preparation

Before calling `saxo_prepare_trading_write`, confirm:

- `operation_id` appears in `saxo_list_trading_write_operations`.
- Method, path template, required path parameters, required query parameters, and risk class match the catalog.
- Money-moving writes bind account key, instrument UIC, quantity, and estimated notional to safety limits.
- Path parameters are named fields. Do not echo submitted path values.
- Query values are documented scalar values only.
- Request body is the documented Saxo body and contains no copied secrets.

If preparation returns `denied` with `validation_errors`, name the field and rule only. Do not repeat the submitted value.

SIM previews need no human approval. LIVE previews return one exact approval statement for the human to send in the agent chat.

## Execution

Call `saxo_execute_trading_write` at most once per preview token.

For SIM, omit `approval_statement`. SIM requires no human approval.

For LIVE, pass the exact approval statement unchanged. The server also checks the short-lived preview token, request fingerprint, environment, safety settings, account allowlist, instrument allowlist, quantity limit, notional limit, expiry, single-use state, and approval match.

Never blind retry a Trading write. The execution transport disables automatic request retries.

Treat cleanup rules from the listed operation as mandatory planning inputs. For subscription writes, delete created subscriptions when cleanup is in scope. For order-like or position-affecting writes, read back orders, positions, trade messages, and fingerprint-only balances as needed.

## Status handling

Only `completed` is unqualified success.

`preview_created` means only a local preview exists. It does not mean Saxo accepted or executed the write.

`approved_for_execution` means only the local LIVE approval gate accepted the exact statement. It does not mean the Saxo request was sent or completed.

`auth_required`, `denied`, `failed`, `http_error`, `network_error`, `rate_limited`, `unknown_state`, `partial_success`, `duplicate_or_conflict`, and `completed_unverified` are not success.

HTTP 202 means `unknown_state`. HTTP 409 means duplicate or conflict. HTTP 429 means rate limited. A network error after send means mutation may have occurred.

For any not-completed mutation-capable outcome, freeze new writes and reconcile through `saxo-reads`. Do not call the same preview token again and do not prepare a replacement until readback gives concrete state.

## Disclaimer response

`saxo_register_disclaimer_response` is a Trading write with a special route.

In SIM, it submits the response autonomously after exact context, token, response type, and optional required user input are known. It never places an order.

In LIVE, it creates a preview for `post.dm.v2.disclaimers`. Do not answer LIVE disclaimers. The human must decide and send the exact approval statement. Then `saxo_execute_trading_write` executes once.

Do not expose disclaimer tokens, submitted response values, or broker disclaimer text in evidence.

## Generic write refusal examples

Refuse and make no tool call when the user asks to:

- Use a non-Trading write.
- Use a refused Trading operation.
- Call an absolute URL or undocumented path.
- Place, modify, or cancel an order through the generic gateway.
- Execute without a preview token.
- Reuse a consumed preview.
- Reuse an old, expired, mismatched, or opaque approval.
- Retry immediately after `TradeNotCompleted`, timeout, partial multileg, duplicate conflict, or post-boundary network error.
