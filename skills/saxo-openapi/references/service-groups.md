# Saxo service groups

Use `operation-catalog.md` for row-level truth. It is generated from `data/saxo/openapi_inventory.json` and must not be hand-edited.

## Fixed catalog counts

| Count | Value |
| --- | --- |
| Total operations | 294 |
| Implemented operations | 182 |
| Refused operations | 112 |
| Service groups | 17 |
| Source snapshot | 2026-07-01 |
| Source URL | https://www.developer.saxo/openapi/referencedocs |

## Groups

| Group | Rows | MCP boundary |
| --- | ---: | --- |
| Account History | 11 | Implemented registered reads only. Use for historical account, transaction, unsettled amount, position, performance, and account value planning. |
| Asset Transfers | 20 | GET reads may be implemented. Cash, securities, periodic payment, and withdrawal writes are refused unless catalog status says implemented. |
| Chart | 4 | Chart reads are implemented. Chart subscription writes are refused. Chart data is not a tradable quote. |
| Client Management | 17 | GET reads may be implemented. Signup, document, verification, account creation, and reset-request writes are refused. |
| Client Reporting | 5 | Implemented registered reads for report retrieval planning. Preserve client/date parameters and privacy rules. |
| Client Services | 18 | GET reads may be implemented for audit, support, reports, trading conditions, and transfer instructions. Non-Trading writes are refused. |
| Corporate Actions | 15 | GET reads may be implemented. Election, standing instruction, holding, and proxy action writes are refused. Some access can depend on licensing. |
| Disclaimer Management | 2 | Catalog marks both GET and POST. Do not infer write safety from the group name. Follow row status. |
| Ens | 4 | Activity read is implemented. Event subscription create/delete operations are refused. |
| Market Overview | 4 | Implemented market overview reads. Treat as market data, not an order instruction. |
| Partner Integration | 31 | GET reads may be implemented. Partner-side external account, funding, booking, pricing, and verification writes are refused. |
| Portfolio | 64 | Implemented registered reads for accounts, balances, positions, exposure, orders, users, and subscriptions where catalog status allows. Portfolio subscription writes are refused. |
| Reference Data | 18 | Implemented registered reads for instruments and static data. Results are account-aware. |
| Regulatory Services | 11 | GET reads may be implemented. Profile and financial overview writes are refused. |
| Root Services | 18 | Session, feature, diagnostics, and subscription reads may be implemented. Root diagnostics/session/subscription writes are refused unless the row says implemented. |
| Trading | 45 | GET and non-GET Trading operations may be implemented. Specialized orders use specialized tools. Other implemented non-GET Trading operations use generic Trading preview/execute. |
| Value Add | 7 | Price alert reads may be implemented. Price alert writes are refused. |

## Status meanings

`implemented` means this MCP has a registered route for that operation class. It does not remove auth, account, entitlement, environment, or safety checks.

`refused` means this MCP intentionally rejects that operation. Use the catalog `Refusal` value in the answer.

Known refusal reasons:

- `write_operations_disabled_by_policy`: Non-Trading writes and unsafe side-effect/protocol probes are not exposed.
- `todo_4_allows_get_reads_only`: The current registered endpoint gateway allows GET/read operations only.

## Safe alternatives

For a refused non-Trading write, propose one of these only when it matches the user request:

- Use `saxo_list_registered_endpoints` to find implemented reads in the same group.
- Use `saxo_call_registered_endpoint` later under `saxo-reads` for an implemented registered GET.
- Use an official Saxo portal or manual business workflow outside this MCP when no implemented read can satisfy the task.

Never propose a generic non-Trading write tool. Never invent a logical tool.
