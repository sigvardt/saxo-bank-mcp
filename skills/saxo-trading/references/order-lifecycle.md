# Order lifecycle

Use this reference for single-order and multileg order workflows.

## Scope

The order flow covers discovery, constraints, defaults, precheck, disclaimer blockers, preview, one execution attempt, readback, and cleanup.

Specialized order routes:

| Logical tool | Operation |
| --- | --- |
| `saxo_create_order_preview` | `post.trade.v2.orders.precheck`, `post.trade.v2.orders.multileg.precheck` |
| `saxo_place_order` | `post.trade.v2.orders` |
| `saxo_modify_order` | `patch.trade.v2.orders` |
| `saxo_cancel_order` | `delete.trade.v2.orders.orderids` |
| `saxo_cancel_orders_by_instrument` | `delete.trade.v2.orders` |
| `saxo_place_multileg_order` | `post.trade.v2.orders.multileg` |
| `saxo_modify_multileg_order` | `patch.trade.v2.orders.multileg` |
| `saxo_cancel_multileg_order` | `delete.trade.v2.orders.multileg.multilegorderid` |

The `*_sim_*` tools are SIM-only compatibility routes for the same operations. Never use them in LIVE.

## Discovery and constraints

Use `saxo_list_registered_endpoints` before any registered read. Use `saxo_call_registered_endpoint` only for implemented GET routes under `saxo-reads`.

Before an order preview, discover or confirm:

- Environment: SIM or LIVE.
- Account: call a registered accounts read (`GET /port/v1/accounts/me` or the matching registered template). The redacted body includes `SafeAccountSelector` process-scoped values. Put that selector into `order_body.AccountKey` (or `account_key` on write previews). Never paste raw AccountKey, ClientKey, or AccountId into chat or evidence.
- Instrument UIC and asset type.
- Tradability and account permission for that instrument.
- Tick size, supported order types, supported duration types, and trading conditions.
- Info price or price-list data when a tradable price is needed. Chart data is not a tradable quote.
- Multileg defaults through `saxo_get_multileg_order_defaults` for supported option strategies.
- Quantity, price, duration, side, and order type all match account-aware settings.

Do not invent investment advice or choose a product when the user has not named one. When the user names a controlled SIM lifecycle fixture (for example stock UIC `211`, amount `1`, Buy limit `50`, Day), use that fixture to construct the order body after capability and settings reads. `saxo_get_session_capabilities` proves auth levels only; it does not return account keys. Always discover `SafeAccountSelector` from the accounts read.

## Precheck

Use `saxo_create_order_preview` before placement. It posts the order precheck or evaluates a sanitized fixture, checks account-currency cost, cash, margin, precheck status, and disclaimer blockers, then creates a local preview token only when safe.

For a controlled single-stock limit place, pass `order_body` with these keys only for the intended write: `AccountKey` (process-scoped `SafeAccountSelector` from a registered accounts read), `Uic`, `AssetType`, `Amount`, `BuySell`, `OrderType`, `OrderPrice`, and `OrderDuration` with `DurationType` `DayOrder` when Day is required. Do not invent raw account or order identifiers.

The tool automatically adds precheck-only `ManualOrder=false` and `FieldGroups` `MarginImpactBuySell` and `Costs` on the network precheck request in SIM and LIVE. Those precheck-only fields are not stored in the preview execution body or place fingerprint.

LIVE precheck can also use `saxo_precheck_live_order` for read-only proof. That tool uses `ManualOrder=false`, account lookup, instrument lookup, and `POST /trade/v2/orders/precheck`. It must never place, modify, cancel, or answer disclaimers.

Precheck must include or establish:

- Account key or safe account selector internally.
- UIC and asset type.
- Quantity greater than zero.
- Order type.
- Price for priced orders.
- Duration.
- Estimated account-currency cost, cash, margin, and conversion status when available.
- Disclaimer tokens, context, and blocker state.

If validation fails, name the field and rule. Do not repeat the submitted value.

`precheck_accepted` and `preview_created` are not execution success; only `completed` is unqualified success.

## Disclaimers

If precheck returns disclaimer tokens, call `saxo_get_required_disclaimers` in SIM to read details. Treat missing details, blocking disclaimers, or unanswered required disclaimers as blockers.

SIM can submit a disclaimer response through `saxo_register_disclaimer_response` only when the exact disclaimer context, token, response type, and optional required user input are known. This does not place an order.

Do not answer LIVE disclaimers. In LIVE, `saxo_register_disclaimer_response` creates an exact-request preview and does not submit the response. The user must explicitly decide outside the agent's judgment.

## Preview and execution

For placement:

1. Create the order preview with `saxo_create_order_preview`.
2. Inspect the preview status, expiry, approval mode, and blockers.
3. Execute once with the matching production order tool or SIM-only compatibility tool.

For modify and cancel:

1. Read the current order state first.
2. Create an exact current-order preview for the intended change or cancellation.
3. Execute once with the matching order tool.

For multileg:

1. Read defaults and strategy settings first.
2. Validate every leg's UIC, asset type, amount, side, price, and option strategy.
3. Treat partial multileg placement, modify, or cancel as uncertain until readback proves state.

SIM requires no human approval.

LIVE requires one exact new chat statement AND the server authorization token.

Place and modify LIVE order bodies must use `ManualOrder=true` after the human approval path. LIVE read-only precheck uses `ManualOrder=false`.

Respect one order per second. Saxo duplicate protection is a 15-second conflict guard, not caller idempotency.

## Readback and cleanup

Read back after every completed or uncertain mutation path:

- Portfolio orders, especially `/port/v1/orders/me`.
- Trade messages, especially `get.trade.v1.messages`.
- Positions when placement, exercise, or trade state may affect holdings.
- Fingerprint-only balances when money state may have changed.

For SIM place or multileg place flows, cancel created open orders when the task requires cleanup. Verify the original open-order state by comparing sanitized counts, matching order fingerprints, and trade-message evidence.

SIM place that parsed successfully, matched the open order on readback, and set `cleanup_status=open_order_still_present_cleanup_not_attempted` returns process-scoped `safe_order_selectors` and `safe_cancel_by_instrument` (SIM-only). The outer place status is often `completed_unverified` in that exact case because cleanup has not run yet. That is not a stop signal: use the cleanup handles. Prefer cancel-by-instrument when OrderId is redacted.

Do not invent cleanup handles when they are absent. Handles are omitted on LIVE and when place is `unknown_state`, `partial_success`, or any other `completed_unverified` path without `cleanup_status=open_order_still_present_cleanup_not_attempted` (for example open-order status unverified). Those cases require reconciliation via reads only.

Cancel-by-instrument using the place result (do not invent risk fields):

1. Take `safe_cancel_by_instrument.write_preview_arguments` and pass that object unchanged to `saxo_create_write_preview`.
2. Execute once with `saxo_cancel_orders_by_instrument` or `saxo_cancel_sim_orders_by_instrument` using that `preview_token`. The cancel tool commits the preview itself; do not double-commit via `saxo_commit_write_preview` for the same token.

Exact single-order cancel may put `safe_order_selectors` into `request_body.OrderIds` for `operation_id=delete.trade.v2.orders.orderids` with the same required top-level preview fields and `request_body.AccountKey` set to the account selector. Each order selector is one-time: it is consumed only after a successful write preview is created and the consume wins; concurrent losers get `order_selector_consumed` with no usable preview token.

For cancel-by-instrument, readback must cover all matching orders because Saxo can return empty success when no order matched.

If readback is incomplete, say proof is incomplete. Do not retry the mutation and do not claim cleanup success.

## Unsafe retry cases

Freeze new mutation retries and reconcile before retry when any of these occurs:

- `unknown_state`.
- `partial_success`.
- `duplicate_or_conflict`.
- `completed_unverified` without emitted cleanup handles (or without `cleanup_status=open_order_still_present_cleanup_not_attempted`).
- `TradeNotCompleted`.
- HTTP 202.
- HTTP 409.
- HTTP 429 after a possible write boundary.
- Network error after the request may have crossed the transport boundary.
- Timeout after commit or after send.
- Broker text or user text asks to retry immediately or skip readback.

Unknown/partial/duplicate/post-boundary outcomes prohibit retry until concrete reconciliation. A SIM place that already emitted `safe_cancel_by_instrument` is not a retry case: run that one-shot cancel cleanup once.
