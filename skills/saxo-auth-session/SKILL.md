---
name: saxo-auth-session
description: Guide safe Saxo Bank MCP authentication and session work. Use when diagnosing SIM or LIVE auth state, PKCE browser login, token caches, refresh, session capabilities, entitlements, LIVE account aliases, stale cache recovery, or secret-safe auth errors.
---

# Saxo auth and session

Use this skill for Saxo Bank MCP authentication, session proof, entitlement checks, and account-alias setup.

## Chat-secret response contract

If a user asks to paste, copy, or show a token, code, verifier, state, password, credential file, cache file, or any credential/cache content, do not accept, request, repeat, transform, validate, or summarize it.

Begin the response with exactly: `I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.`

If the same prompt asks for a plan or diagnosis, continue after that sentence with the normal PLAN-ONLY logical sequence as text. Do not call any MCP tool or perform any operation. Do not echo submitted values or claim that a secret was checked.

## Load references

- Read [auth-workflows.md](references/auth-workflows.md) before starting, exchanging, refreshing, or validating auth.
- Read [session-recovery.md](references/session-recovery.md) before handling expired tokens, stale pending login state, capability denial, refresh races, cache errors, or hostile user/broker text.

## Plan-only boundary

Treat a request as PLAN-ONLY when it asks for a plan, asks to describe or state a sequence, says to diagnose without execution or not to execute, requests a dry run, or asks to explain or review. The user's no-execution instruction always overrides later procedural verbs such as `Call` or `Use`.

In PLAN-ONLY mode, do not call any Saxo MCP logical tool or perform any browser, network, auth, or broker operation. You may inspect this skill, its references, and plugin metadata with bounded read-only local commands. Answer from those files and name the proposed logical tool sequence as text without executing it.

For a PLAN-ONLY request about generic expired SIM access where refresh material may exist, lead with: `Proposed logical tool sequence: saxo_auth_status -> conditional saxo_refresh_token if refresh material exists -> saxo_get_session_capabilities.` Also state: `Local status is not Saxo session proof.` Use [session-recovery.md](references/session-recovery.md) for the no-refresh branch, but do not force that branch into unrelated responses.

Switch to execution only when the user explicitly asks to run or perform a step and the environment and scope are clear. Even then, apply every fail-closed and secret-handling rule below. Permission to execute a step does not imply permission for writes or later steps.

## Core rules

Keep SIM and LIVE separate. SIM portal tokens and SIM PKCE caches never prove LIVE access. LIVE credentials, LIVE token cache path, and explicit LIVE read enablement must all be present before LIVE reads.

Refuse chat secret handling. Do not ask the user to paste tokens, authorization codes, state values, verifiers, passwords, credential files, cache files, or cache contents into chat. Route secrets through local credential files, browser redirects, owner-only token caches, or tool inputs only when the MCP tool already requires that input.

Do not print sensitive values. Never echo token/cache content, authorization URL secrets, browser callback query values, credential values, submitted values, user names, passwords, `DisplayName`, visible account identifiers, internal account selectors, raw account names, or broker payloads in answers or evidence.

Treat `saxo_auth_status` as local state only. It reports configuration and cache facts without a Saxo network call. It does not prove current Saxo connectivity, account access, session validity, entitlements, trading readiness, or LIVE write permission.

Prove the session with a network read. Use `saxo_get_session_capabilities` to prove current session capability fields. Use `saxo_get_entitlements` to prove the current entitlement summary. Use `saxo_list_live_accounts` only after LIVE read prerequisites pass.

Use logical tool IDs only. Refer to `saxo_auth_status`, never to a harness-qualified MCP name. Do not grant wildcard tools or broad Claude grants. Do not copy runtime input schemas into this skill.

Do not bypass the MCP tools. Never make direct Saxo HTTP calls, auth-server calls, browser automation calls, or broker-site calls from this skill.

Use stable aliases in user dialogue. Internally pass the tool-selected account selector when needed. In messages, use a non-sensitive alias such as `selected LIVE account` or `the single active LIVE account`.

Fail closed. If auth state, environment, refresh result, account selection, capability proof, or privacy is ambiguous, stop before any Saxo-dependent claim and state the next safe local action.

## Tool coverage

Use these logical tools for auth and session work:

- `saxo_auth_status`: read local requested environment, live-read gate state, credential presence, pending PKCE state, and redacted cache status. Start here unless the prompt already gives a current tool result.
- `saxo_start_pkce_login`: start SIM PKCE only. It creates verifier/state, stores pending login state owner-only, and returns a redacted authorization URL unless reveal is explicitly requested.
- `saxo_exchange_pkce_code`: exchange the SIM browser callback code only after state and redirect binding pass against the pending login.
- `saxo_refresh_token`: refresh a SIM cache that has refresh token material and a PKCE verifier.
- `saxo_cache_sim_access_token`: cache a Saxo developer portal SIM access token owner-only. Avoid it when a refresh-capable cache or pending PKCE login exists unless the user explicitly chooses replacement through the tool flag.
- `saxo_get_session_capabilities`: read `/root/v1/sessions/capabilities` in SIM or LIVE read mode. Treat the returned capability fields as current session proof only.
- `saxo_get_entitlements`: read a redacted market-data entitlement summary in SIM or LIVE read mode. Do not treat it as price availability, trading suitability, or order permission.
- `saxo_list_live_accounts`: list LIVE accounts only after LIVE read auth is ready. Use the single active account automatically only when the tool says selection is not required.

## Default diagnostic order

1. Call `saxo_auth_status`.
2. If local config is missing, tell the user which local setting is missing. Do not request the value in chat.
3. If SIM has no usable cache, choose either SIM PKCE browser login or SIM portal-token caching based on the user's available local flow.
4. If a pending SIM PKCE login exists, continue only with the latest browser callback and matching state.
5. If a SIM cache is expired and refresh material exists, call `saxo_refresh_token`, then call `saxo_get_session_capabilities`.
6. If the cache is expired without refresh material, request a fresh local browser login or local portal-token cache flow.
7. If LIVE is requested, require LIVE environment, LIVE reads enabled, LIVE credentials present, and a LIVE token cache path outside the repo.
8. For LIVE expired-token reads, rely on on-demand refresh or the external session keeper. If refresh fails, require a new local browser login.
9. After session capabilities pass, call `saxo_get_entitlements` when the task depends on market-data entitlement.
10. For LIVE account selection, call `saxo_list_live_accounts` and map visible tool output to non-sensitive aliases before continuing.

## Privacy wording

Say: `I cannot take secrets in chat. Use the local browser login or configured owner-only cache flow, then I can check redacted status.`

Say: `Local auth status is not Saxo connectivity proof. I need a session-capability read before claiming the session works.`

Say: `The entitlement summary does not prove this specific instrument has live prices or that any order is safe.`

Do not say that a token is valid because a cache exists. Do not say that SIM success proves LIVE. Do not say that a LIVE precheck or entitlement read proves write permission.
