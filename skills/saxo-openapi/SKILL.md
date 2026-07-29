---
name: saxo-openapi
description: Guide Saxo Bank MCP OpenAPI service discovery, registered endpoint planning, implemented/refused operation boundaries, official Saxo protocol rules, and plan-only answers. Use when a user asks what Saxo OpenAPI groups or operations this MCP supports, how to route registered reads or Trading writes, why an operation is refused, how paging/version/rate limits work, or asks to plan/review/explain Saxo calls without executing them.
---

# Saxo OpenAPI

Use this skill to plan Saxo OpenAPI operation discovery and routing from the generated catalog.

## Invocation

For installed Codex, invoke this skill as `$saxo-bank-mcp:saxo-openapi`.

For Claude, invoke this skill as `/saxo-bank-mcp:saxo-openapi`.

Do not use an unnamespaced Codex invocation when asking another installed Codex client to load this skill.

## Plan-only boundary

Treat a request as PLAN-ONLY when it says plan, explain, review, dry run, do not execute, do not call tools, or similar wording.

In PLAN-ONLY mode, make zero Saxo MCP calls, zero broker network calls, zero auth calls, and zero browser calls. You may inspect this skill, its references, plugin metadata, and generated catalogs with bounded local read-only commands. Answer from the skill and catalog only.

If the user asks for execution later, switch only after the user explicitly asks to run a step and the target environment, account, endpoint, and risk are clear.

## Load references

- Read [service-groups.md](references/service-groups.md) when selecting a service group, checking counts, or explaining implemented/refused coverage.
- Read [protocol-contracts.md](references/protocol-contracts.md) before explaining paging, version tolerance, chart `DataVersion`, reference data, rate limits, duplicate protection, or SIM/LIVE rollout.
- Read [operation-catalog.md](references/operation-catalog.md) for operation-level truth. It is generated. Never edit it by hand.

## Catalog truth

Treat runtime/catalog data as implementation truth.

The generated operation catalog contains exactly 294 operations across 17 service groups: 182 implemented and 112 refused. Preserve those numbers unless the generated catalog and `data/saxo/openapi_inventory.json` change together.

Never imply a refused operation is callable. If an operation status is `refused`, state the `Refusal` value from the catalog and propose the safest implemented alternative. If no implemented alternative exists, say so.

Use official Saxo pages as protocol truth. Do not copy runtime schemas into answers.

## Routing rules

Use logical tool IDs only.

Use `saxo_list_registered_endpoints` to inspect implemented registered GET/read operations. This is local metadata and does not call Saxo.

Use `saxo_list_trading_write_operations` to inspect non-GET Trading operations and their specialized or generic routing. This is local metadata and does not call Saxo.

Route implemented registered reads to `saxo_call_registered_endpoint` only when the user explicitly asks to execute and the endpoint appears in the registered endpoint list. This execution workflow belongs to `saxo-reads`.

Route specialized Trading order operations to their specialized logical tools:

- `post.trade.v2.orders` -> `saxo_place_order`
- `patch.trade.v2.orders` -> `saxo_modify_order`
- `delete.trade.v2.orders` -> `saxo_cancel_orders_by_instrument`
- `delete.trade.v2.orders.orderids` -> `saxo_cancel_order`
- `post.trade.v2.orders.multileg` -> `saxo_place_multileg_order`
- `patch.trade.v2.orders.multileg` -> `saxo_modify_multileg_order`
- `delete.trade.v2.orders.multileg.multilegorderid` -> `saxo_cancel_multileg_order`

Route other implemented non-GET Trading operations through `saxo_prepare_trading_write` and `saxo_execute_trading_write` only under the `saxo-trading` skill's safety rules.

Do not route refused non-Trading writes to a generic write tool. None exists for them. Do not invent a logical tool.

Never call an arbitrary undocumented URL or method. A URL must be relative, registry-matched, and implemented before execution is considered.

## Response contract

For service support questions, answer with the group name, implemented/refused state, relevant logical discovery tool, and the generated catalog as the source.

For refused operation requests, say: the operation is refused, the catalog refusal reason, no tool will be called, and the nearest safe implemented read or metadata check. Do not mention a fabricated write path.

For chart data, say chart samples are for chart display and history. Do not treat chart data as an authoritative tradable quote. Use Trading price or info price endpoints for tradable quote planning.

For reference data, say instrument and trading condition data is account-aware. Do not assume one account's permissions, tick sizes, or order settings apply to another account.

For paged reads, preserve `$top`, `$skip`, `$skiptoken`, and returned next links exactly as returned by Saxo. Do not synthesize page tokens.

For version changes, tolerate additive fields, omitted optional fields, new enum values, and new resources. Fail closed only on a documented breaking change or a missing required field needed for the task.

For rate limits and duplicates, budget requests by app, session, and service group. Respect one order per second. Treat Saxo duplicate POST/PATCH protection as a 15-second conflict guard, not as true caller idempotency.
