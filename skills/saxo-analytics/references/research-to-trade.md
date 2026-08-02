# Research to trade

Analytics can produce research and typed precheck input. It cannot approve, execute, purchase,
place, modify, or cancel anything.

## Preconditions

Continue to `saxo_propose_trade_from_analysis` only when all are true:

1. The user explicitly asks to turn a current analysis into a trade precheck proposal.
2. The user supplies the instrument, side, quantity or sizing inputs, and risk budget; analytics
   chooses none of them.
3. The stored `analysis_id`, result fingerprint, source revision, proof binding, quality state, and
   validity window replay successfully.
4. Any degraded input is disclosed and the domain service still permits a proposal.

The output is typed preview input and a pre-trade impact card only. It grants no approval or
execution authority and creates no order.

## Mandatory stop

After `saxo_propose_trade_from_analysis`, report its `analysis_id` and
`verified`/`degraded`/`refused` state, then stop before broker write. Do not call
`saxo_create_order_preview`, `saxo_prepare_trading_write`, `saxo_execute_trading_write`, any place,
modify, or cancel tool, or `saxo_register_disclaimer_response`.

There is no disclaimer response in an analytics workflow. If a disclaimer appears, refuse and
stop. A later user request to proceed is a separate explicit follow-on routed to `saxo-trading`,
which must apply its own current environment, precheck, preview, approval, execution, readback, and
cleanup rules. Never carry approval or a preview token across this boundary.
