# Saxo protocol contracts

Use official Saxo pages as protocol truth:

- Environments: https://www.developer.saxo/openapi/learn/environments
- Reference data: https://www.developer.saxo/openapi/learn/reference-data
- Chart: https://www.developer.saxo/openapi/learn/chart
- OpenAPI request/response: https://www.developer.saxo/openapi/learn/openapi-request-response
- Versioning and obsolescence: https://www.developer.saxo/openapi/learn/versioning-and-obsolescence-policy
- Rate limiting: https://www.developer.saxo/openapi/learn/rate-limiting
- Service catalog: https://www.developer.saxo/openapi/referencedocs
- Order placement: https://www.developer.saxo/openapi/learn/order-placement

## Environment rules

Keep SIM and LIVE separate. SIM and LIVE have separate endpoints, credentials, token caches, and rollout timing. A SIM result does not prove LIVE behavior.

Simulation can run a higher OpenAPI version than LIVE. Treat SIM/LIVE differences as possible until a current read proves otherwise.

Do not execute LIVE mutations from this skill. LIVE write safety belongs to `saxo-trading` and requires its approval flow.

## Registered endpoint rules

Use only relative Saxo paths that match the generated registry. Reject absolute URLs and arbitrary undocumented methods before any network call.

Do not call a method/path pair unless the catalog marks it `implemented` and the execution skill confirms the route.

Do not assume a documented Saxo operation is callable through this MCP. The catalog status is the boundary.

## Request and response rules

Treat omitted optional fields as normal. Saxo can omit null fields.

Preserve returned paging data exactly. Supported mechanisms include `$top`, `$skip`, `$skiptoken`, and returned next links such as `_next`.

Do not synthesize, normalize, or rebase paging tokens. Continue from Saxo's returned link or token.

Do not parse responses as closed schemas. Tolerate additive fields, additional optional query parameters, added enum values, added resources, and added methods.

Fail closed if a required field for the task is absent, if the operation changed in a breaking way, or if the response status means mutation state is uncertain.

## Reference data

Reference data is account-aware. Saxo returns instruments and trading conditions based on the user's setup.

Always pair instrument planning with the relevant account context. Tick size, supported order types, durations, and permissions can differ by account, asset type, and environment.

Do not treat entitlement, reference data, or trading condition output as financial advice.

## Chart data

Chart data is for chart display and history.

Do not use chart sample close values as an authoritative tradable quote. Plan Trading price or info price reads when the user needs a tradable quote.

If `DataVersion` changes, invalidate cached chart history for that instrument and horizon, then refetch. For streaming chart resets, treat reset as cache invalidation.

## Rate and duplicate rules

Budget calls across app, session, and service group limits. Default Saxo limits include application daily budget, per-session per-service-group minute budget, and one order per second.

On HTTP 429, do not blind retry. Respect returned rate headers and wait or reduce request volume.

Saxo duplicate protection rejects identical order POST/PATCH requests in a rolling 15-second window. This protects against accidental duplicate order operations.

Duplicate protection is not true caller idempotency. It does not prove no order exists, and it does not make retry safe after an unknown outcome.

Use a distinct `x-request-id` only when the Trading workflow deliberately sends two identical order operations and the user understands the risk.

## Order outcome rules

Precheck is not order approval and does not place an order.

Timeout-like order outcomes, `TradeNotCompleted`, duplicate conflicts, and post-commit network failures can mean an order exists. Reconcile before retry or success wording.

Only the status `completed` is unqualified mutation success.
