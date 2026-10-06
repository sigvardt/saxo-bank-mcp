# Auth workflows

Use this reference when starting login, exchanging callback values, refreshing a cache, or explaining credential setup.

## SIM and LIVE separation

Treat SIM and LIVE as separate systems. Saxo documents separate environments, hosts, app credentials, and possible version or data differences. A SIM token, SIM capability read, or SIM entitlement read never proves LIVE access.

Use SIM auth tools only for SIM:

- `saxo_start_pkce_login`
- `saxo_exchange_pkce_code`
- `saxo_refresh_token`
- `saxo_cache_sim_access_token`

Use LIVE read setup only for LIVE reads:

- Local LIVE credential file
- `SAXO_MCP_ENVIRONMENT=LIVE`
- `SAXO_MCP_ENABLE_LIVE_READS=1`
- `SAXO_MCP_LIVE_TOKEN_CACHE_PATH` outside the repo and common sync folders
- Local browser login through `saxo-bank-live-login`
- Optional external `saxo-bank-live-session-keeper`

Do not move a token between environments. If a cache reports the wrong environment, require replacement through the correct local login/cache flow.

## LIVE browser login on a headless host

When the MCP host has no browser the user can see, run `saxo-bank-live-login --no-browser` in the background with the LIVE environment settings. It does not open a browser on the host. It prints the authorization URL as one JSON line on stderr, and `--url-file` also writes it to an owner-only file.

Give the user that URL. The user's browser must reach the host's callback port, for example through `ssh -N -L 8080:127.0.0.1:8080 <host>` started on the browser machine, matching the configured redirect URI port. The receiver ignores requests to other paths, so a port check does not consume the login. Never ask the user to paste the callback URL, code, or state into chat. After the command reports `live_token_cached`, close the tunnel and prove the session with a LIVE read.

## SIM PKCE browser flow

Use `saxo_start_pkce_login` when SIM credentials and redirect URI are configured but no usable SIM cache exists.

Require these properties before exchange:

- PKCE verifier is high-entropy and stays local.
- Code challenge uses S256.
- State is generated per login and must match the browser callback.
- Redirect URI in the token request must match the value stored when login started.
- Pending PKCE state expires and must be restarted when stale.
- Browser callback carries code and state. Do not ask the user to paste those values into chat.

The tool returns a redacted authorization URL by default. If a user asks for the raw URL, warn that it is sensitive and should stay local to the browser flow.

Use `saxo_exchange_pkce_code` only after the browser callback has returned through the local flow. It exchanges code plus the stored verifier, stores tokens owner-only, and deletes pending PKCE state after success.

## SIM portal-token flow

Use `saxo_cache_sim_access_token` only when the user has a fresh Saxo developer portal SIM token and local tool input is acceptable.

Do not ask the user to paste the token in chat. Tell them to provide it through the local MCP tool input or local credential/cache procedure.

Respect the replacement guard. If a refresh-capable PKCE cache or pending PKCE state exists, do not replace it unless the user explicitly chooses that path with the tool flag.

Treat portal-token expiry as caller asserted. Saxo can reject it earlier. After caching, call `saxo_get_session_capabilities`.

## Refresh

Use `saxo_refresh_token` for SIM only. It requires cached refresh token material and cached PKCE verifier. If refresh material is missing, route to a fresh SIM portal token or PKCE login.

LIVE reads use `live_token_for_tool` inside LIVE-capable tools. It refreshes expired access tokens on demand under a cross-process lock. The external `saxo-bank-live-session-keeper` can keep a valid LIVE cache fresh, and it retries temporary token-endpoint failures, but it cannot recover a cache whose refresh token is no longer accepted or has passed the deadline Saxo returned.

Handle refresh race conditions conservatively:

- Do not start parallel refresh attempts from the agent.
- Prefer one tool call and then re-read status or session capability.
- If a refresh is rejected, require a fresh browser login.
- If a second process refreshes the cache, use the current cache through the next tool call instead of copying cache content.

## Session proof

Use `saxo_auth_status` for local state only. It can report missing config, unreadable cache, pending PKCE state, expiry, refresh support, and token environment. It makes no network call.

Use `saxo_get_session_capabilities` to prove that the current cached bearer token can read Saxo session capability fields. Treat these fields as current session proof, not trading readiness.

Use `saxo_get_entitlements` to prove a redacted entitlement summary. It does not prove price availability for a specific instrument, quote recency, account suitability, order safety, or LIVE write permission.

Use `saxo_list_live_accounts` after LIVE read proof when a later LIVE read/precheck needs account selection. In dialogue, convert the tool's account choices to stable aliases and avoid repeating sensitive account details.
