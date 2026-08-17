# SIM session keeper design

Date: 2026-08-17
Status: approved

## Goal

Keep the owner-local Saxo SIM token refreshed without another browser login while Saxo still accepts
the refresh token. The keeper must never use LIVE, trade, answer a disclaimer, inspect account or
market data, or expose credential or cache content.

## Design

Add one one-shot `saxo-bank-sim-session-keeper` command. Each run resolves SIM settings, acquires a
process lock, reads the owner-only cache, and exits without a network request while the access token
has more than five minutes left. Inside that margin, it calls Saxo's SIM token endpoint once and
atomically saves the complete returned token, including any rotated refresh token and the existing
PKCE verifier.

Before the refresh request, the command durably stores only the current cache revision in an
owner-only attempt marker. If the marker cannot be made durable, the command stops before network
work. The marker remains after Saxo rejection, an interrupted or unknown request result, cache-save
failure, or marker-clear failure, so an unchanged cache cannot issue a blind request every minute.
The keeper also holds the shared owner-only token-cache write lock from its cache read through the
network result, revision check, durable replacement, and marker resolution. Every legitimate cache
writer uses that same lock: ordinary saves (including the local SIM login callback), PKCE exchange,
explicit and capability-triggered refresh, entitlement-triggered refresh, and portal-token
read/replace decisions. An explicit write lease lets a lock holder save without acquiring the same
`flock` again; the lease is path-bound, expires at unlock, and contains no printable private path.
The existing keeper process lock remains outermost, and no cache writer acquires it, so the lock
order has no reverse edge.

After the response and before saving, the keeper still compares the current cache revision with the
original revision to detect an uncoordinated external change. A concurrent legitimate browser login
or tool update waits, then wins after the keeper transaction; the keeper cannot overwrite a writer
that started in the former post-check/pre-replace window. Only a durable save of the complete rotated
token permits marker removal. A legitimate cache update changes the revision and permits one later
attempt when that new token reaches the refresh margin. Missing, unreadable, wrong-environment, or
non-refreshable caches fail closed without a network call.

Install the exact committed package into a dedicated owner-only runtime outside the repository and
common cloud folders. A single macOS launchd job runs the one-shot command every 60 seconds and at
login. launchd never starts a duplicate instance for the same label. Logs contain only redacted
status codes and live in the owner-only runtime. The existing owner state cache remains outside the
repository and common cloud folders.

## Verification

Tests first prove fresh-cache no-op behavior, a durable owner-only marker before network work,
near-expiry refresh and atomic rotated-token save, ordinary rejection/success, marker-write and
cache-save failure suppression, pre-check and late-window concurrent-cache preservation, shared-lock
serialization for portal and refresh writers, ordinary login/auth saves, stale-lease refusal,
owner-only modes, cache-change recovery, SIM-only refusal, and secret-safe command output. Run the
focused tests twice through `scripts/run-pytest`, then Ruff and BasedPyright.

After installation, run one authorized SIM refresh and one session-capability read. Verify the
launchd definition, loaded process state, one label only, runtime and log permissions, restart
persistence, no secret leakage, and zero LIVE or write calls. Record and read back the interval job
in the Automations database.

## Limitation

Automatic renewal cannot make a Saxo refresh token permanent. If the machine stays offline long
enough for Saxo to expire or revoke the refresh token, or Saxo rejects it for another reason, the
keeper stops network retries and a new human browser login is required.
