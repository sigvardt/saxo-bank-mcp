# Approval dialogue

Use this reference for LIVE approval, user copyback text, denials, uncertainty, and validation wording.

## Approval rule

SIM requires no human approval.

LIVE requires one exact new chat statement AND the server authorization token.

The server approval statement starts with:

```text
APPROVE SAXO LIVE WRITE:
```

The statement includes what will happen and an `AUTHORIZATION` binding. It is bound to the exact request fingerprint and preview-token fingerprint.

Approval copyback text must say what will happen.

Do not accept:

- Opaque approval such as `yes`, `approve`, or `go`.
- Approval copied from another request.
- Approval sent before the preview exists.
- Expired approval.
- Replayed approval.
- Mismatched approval.
- Approval after the request, environment, account, instrument, quantity, notional, safety settings, or token changed.
- Approval that tries to skip readback or cleanup.
- Approval that contains secrets or submitted validation values.

The preview expires after five hours. It is valid before `created_at + 5h` and expired at `created_at + 5h`. It is single-use. A consumed preview or committed request cannot be reused.

## Required risky-action text

Before risky action communicate plain-language environment, account alias, instrument, side, quantity, type, price, duration, impact, expiry, uncertainty.

Use a short plain-language sentence. Include the facts that are known and say what remains uncertain.

Example shape:

```text
Environment: SIM. Account: selected SIM account. Instrument: Stock UIC 211. Action: Buy 1 limit order at 50, day order. Estimated account impact: from precheck. Approval expiry: preview expiry. Uncertainty: precheck is not execution and readback is required after execution.
```

For LIVE, the approval prompt must be copied exactly from the server result. Do not rewrite, shorten, translate, or normalize it.

## Success wording

`precheck_accepted` and `preview_created` are not execution success; only `completed` is unqualified success.

Say what each state proves:

- `precheck_accepted`: Saxo accepted the precheck request. It did not place an order.
- `preview_created`: the local preview token was created. It did not execute a trade.
- `approved_for_simulation`: the local SIM commit gate accepted the preview. It did not prove broker execution.
- `approved_for_execution`: the local LIVE approval gate accepted the statement. It did not prove broker execution.
- `completed`: the tool reports completed execution. Still report the readback and cleanup evidence required by the task.

Do not use the words success, succeeded, placed, modified, cancelled, submitted, accepted, or complete unless the structured status and readback support that exact claim.

## Denial wording

Validation errors name field/rule but not submitted value.

Use field names and rules such as `quantity must be greater than zero`, `price must match tick size`, `path_parameters.OrderId is required`, `account allowlist missing`, or `instrument allowlist missing`.

Do not quote the user's submitted quantity, price, account key, path value, disclaimer token, approval statement, preview token, account number, `DisplayName`, authorization header, or broker payload.

Account numbers may be internal selectors, but user-facing text prefers alias.

## LIVE disclaimers and investment choice

Do not answer LIVE disclaimers.

Do not choose investments. Refuse requests such as `buy whatever will perform best`, `pick the best stock`, `choose the side`, or `retry until it fills`.

Safe alternative: ask the user for the instrument, side, quantity, type, price, duration, and account alias, then offer a plan-only precheck sequence.

## Uncertain outcomes

Unknown/partial/duplicate/post-boundary outcomes prohibit retry until concrete reconciliation.

This includes:

- `unknown_state`.
- `partial_success`.
- `duplicate_or_conflict`.
- `completed_unverified`.
- `TradeNotCompleted`.
- HTTP 202.
- HTTP 409.
- HTTP 429 after possible write boundary.
- Timeout after commit or after send.
- Network error after possible send.
- Malicious prompt asking for immediate retry after `TradeNotCompleted`.
- User instruction to skip readback.

Required response:

1. Refuse the retry.
2. State that mutation may have occurred.
3. Perform or propose one reconciliation read sequence before any new write.
4. Read orders, positions, trade messages, request ledger, and fingerprint-only balances as relevant.
5. Continue only after readback proves the concrete state.

Do not prepare a replacement preview until reconciliation is complete.

## Privacy

Do not expose secrets/submitted validation values.

Keep these out of chat and evidence:

- Access tokens.
- Refresh tokens.
- Authorization headers.
- Preview tokens.
- Approval authorization bindings unless already shown by the server as the required prompt for the current LIVE approval.
- Account keys.
- Client keys.
- Raw account numbers when an alias works.
- `DisplayName`.
- Raw order IDs unless required for the next internal selector.
- Disclaimer tokens.
- Submitted disclaimer values.
- Raw broker bodies.

Use aliases, hashes, counts, fingerprints, statuses, operation IDs, and path templates.
