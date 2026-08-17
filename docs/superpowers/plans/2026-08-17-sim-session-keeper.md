# SIM session keeper implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the owner-local Saxo SIM token refreshed through one safe launchd interval job while Saxo continues to accept its refresh token.

**Architecture:** A one-shot CLI checks the SIM cache under a process lock. It refreshes only inside a five-minute margin, durably records the cache revision before network work, refuses to overwrite a concurrently changed cache, and clears the marker only after the full rotated token is durably saved. launchd runs the installed command every 60 seconds from a dedicated owner-only runtime.

**Tech stack:** Python 3.12+, AnyIO, httpx transport injection, Pydantic token models, macOS launchd, Notion Automations registry.

## Global constraints

- SIM only. Reject LIVE and any enabled LIVE-read setting before cache or network work.
- Auth renewal only. Never call account, market, order, trade, purchase, or disclaimer tools.
- Never print credential values, token/cache content, cache paths, callback values, or raw broker payloads.
- Preserve rotated refresh tokens by saving the complete Saxo token response through `save_token_cache`.
- Use `scripts/run-pytest` only, with `TMPDIR`, `TMP`, and `TEMP` set to `/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics`.
- Keep runtime, logs, marker, lock, pending state, and token cache owner-only and outside the repository and cloud folders.
- Do not invoke Claude, codex-power, or subagents.

---

### Task 1: Add the one-shot SIM refresh decision

**Files:**
- Create: `src/saxo_bank_mcp/sim_token_refresh.py`
- Create: `tests/test_sim_token_refresh.py`

**Interfaces:**
- Consumes: `SimAuthSettings`, `inspect_token_cache`, `refresh_access_token`, and `save_token_cache`.
- Produces: `SimRefreshOutcome(status: SimRefreshStatus, network_call_made: bool)`.
- Produces: `refresh_sim_token_if_needed(settings, *, minimum_validity=timedelta(minutes=5), now=None, transport=None) -> SimRefreshOutcome`.

- [ ] **Step 1: Write failing behavior tests**

Cover these independent outcomes with literal assertions:

```python
assert await refresh_sim_token_if_needed(settings, now=fixed_now) == SimRefreshOutcome(
    status="fresh",
    network_call_made=False,
)
assert saved.refresh_token == "rotated-refresh"
assert second_rejected_run == SimRefreshOutcome(
    status="refresh_attempt_suppressed",
    network_call_made=False,
)
```

Use a real temporary token cache and an `httpx.MockTransport` only at the Saxo token endpoint.
Prove fresh-cache no-op, near-expiry refresh, rotated-token save, wrong-environment refusal,
non-refreshable refusal, owner-only attempt-marker mode, rejection suppression, marker-write and
cache-save failure, concurrent-cache preservation, and cache-change recovery.

- [ ] **Step 2: Run RED**

Run:

```bash
TMPDIR=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
TMP=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
TEMP=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
scripts/run-pytest tests/test_sim_token_refresh.py -q
```

Expected: collection fails because `saxo_bank_mcp.sim_token_refresh` does not exist.

- [ ] **Step 3: Implement the minimal locked decision**

Implement these statuses:

```python
type SimRefreshStatus = Literal[
    "fresh",
    "refreshed",
    "token_missing",
    "wrong_environment",
    "login_required",
    "refresh_rejected",
    "refresh_attempt_suppressed",
    "attempt_marker_failed",
    "cache_save_failed",
    "cache_changed",
    "marker_clear_failed",
]
```

Acquire an owner-only `flock` beside the cache. Read only through `inspect_token_cache`. Compare the
expiry to `now + minimum_validity`. Before a network call, stop if the attempt marker contains the
current cache revision; otherwise durably write that revision before the request. Retain it for
rejection, unknown result, or save failure. After the response, compare the cache revision again and
never overwrite a concurrent update. On success, call `save_token_cache` with the complete refreshed
token, sync and verify the cache, and only then remove the marker.

- [ ] **Step 4: Run GREEN twice**

Run the Task 1 command twice. Expected: all tests pass both times.

---

### Task 2: Add the secret-safe SIM keeper command

**Files:**
- Create: `src/saxo_bank_mcp/sim_session_keeper.py`
- Modify: `pyproject.toml`
- Create: `tests/test_sim_session_keeper.py`

**Interfaces:**
- Consumes: `refresh_sim_token_if_needed` and `resolve_sim_auth_settings(require_redirect=False)`.
- Produces: console entry point `saxo-bank-sim-session-keeper`.
- Produces: one JSON line containing only `status`, `environment`, and `network_call_made`.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_main_refuses_live_before_refresh(monkeypatch, capsys) -> None:
    monkeypatch.setenv("SAXO_MCP_ENVIRONMENT", "LIVE")
    assert sim_session_keeper.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        "environment": "SIM",
        "network_call_made": False,
        "status": "sim_environment_required",
    }
```

Also prove enabled LIVE reads are refused, a fresh/refreshed outcome exits zero, a rejected outcome
exits nonzero, and an injected credential detail never appears in stdout or stderr.

- [ ] **Step 2: Run RED**

Run the guarded focused command for `tests/test_sim_session_keeper.py`. Expected: collection fails
because the module and entry point do not exist.

- [ ] **Step 3: Implement the minimal CLI and entry point**

Reject unless the requested and effective environment are SIM and LIVE reads are disabled. Resolve
SIM settings without requiring a redirect. Emit only the three allowed fields. Map `fresh` and
`refreshed` to exit zero; all other statuses exit nonzero.

- [ ] **Step 4: Run focused GREEN twice and related auth tests once**

Run both new test files twice, then run:

```bash
TMPDIR=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
TMP=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
TEMP=/Volumes/ssd_1/codex/tmp/saxo-bank-mcp-analytics \
scripts/run-pytest tests/test_auth_session.py tests/test_live_session_keeper.py \
  tests/test_sim_token_refresh.py tests/test_sim_session_keeper.py -q
```

- [ ] **Step 5: Run source checks and commit**

Run `uv run ruff check .`, `uv run basedpyright`, and `git diff --check`. Commit source, tests,
entry point, and lockfile if it changed.

---

### Task 3: Install and prove restart-safe renewal

**Files:**
- Create locally: dedicated owner-only installed runtime outside repository and cloud folders.
- Create locally: `~/Library/LaunchAgents/com.saxobank.mcp-sim-session.plist`.
- Update: existing or new Notion Automations row keyed by source ID `com.saxobank.mcp-sim-session`.
- Update: existing `Saxo Bank MCP` Knowledge Base project row.

**Interfaces:**
- Consumes: committed `saxo-bank-sim-session-keeper` entry point.
- Produces: one launchd interval job with `StartInterval=60`, `RunAtLoad=true`, SIM environment, and LIVE reads disabled.

- [ ] **Step 1: Prove no duplicate live job or registry row**

Read launchd by exact label and query Automations by exact Source ID and Name. Do not scan broad
home or cloud directories.

- [ ] **Step 2: Install the exact committed package owner-only**

Create the runtime and logs with mode `0700`. Install the committed project into its dedicated
virtual environment. Verify the executable and every created log or state file are owner-only.

- [ ] **Step 3: Create and load one launchd interval definition**

Use the exact label once. Set `StartInterval` to 60 seconds, `RunAtLoad` to true, and `Umask` to
`0077`. The job invokes only the installed one-shot keeper with SIM selected and LIVE reads disabled.

- [ ] **Step 4: Reconcile and read back Automations**

Create or update the exact Source ID row. Record launchd as owner, `Interval` as trigger,
`every 60s` as schedule, enabled/OK status, local delivery, exact runtime plist, and the required
ten-section runbook without secrets. Read back properties and page body.

- [ ] **Step 5: Verify one refresh and capability cycle**

Call the registered SIM refresh tool once, then `saxo_get_session_capabilities` once. Publish only
redacted status, network provenance, environment, capability-field names, and no-write flags.

- [ ] **Step 6: Verify launchd restart persistence and privacy**

Unload and load the exact label once. Verify the definition remains present, one label is loaded,
the keeper exits cleanly when fresh, logs contain no secret findings, and there are zero LIVE or
write calls. Read back the Knowledge Base project update and report the refresh-token limitation.
