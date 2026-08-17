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

If Saxo rejects the refresh, the command stores only the cache revision in an owner-only marker.
Later runs make no network request while that revision is unchanged. A successful browser login or
other legitimate cache update changes the revision and permits one new attempt. Missing, unreadable,
wrong-environment, or non-refreshable caches fail closed without a network call.

Install the exact committed package into a dedicated owner-only runtime outside the repository and
common cloud folders. A single macOS launchd job runs the one-shot command every 60 seconds and at
login. launchd never starts a duplicate instance for the same label. Logs contain only redacted
status codes and live in the owner-only runtime. The existing owner state cache remains outside the
repository and common cloud folders.

## Verification

Tests first prove fresh-cache no-op behavior, near-expiry refresh and atomic rotated-token save,
rejection-marker suppression, cache-change recovery, SIM-only refusal, owner-only marker mode, and
secret-safe command output. Run the focused tests twice through `scripts/run-pytest`, then Ruff and
BasedPyright.

After installation, run one authorized SIM refresh and one session-capability read. Verify the
launchd definition, loaded process state, one label only, runtime and log permissions, restart
persistence, no secret leakage, and zero LIVE or write calls. Record and read back the interval job
in the Automations database.

## Limitation

Automatic renewal cannot make a Saxo refresh token permanent. If the machine stays offline long
enough for Saxo to expire or revoke the refresh token, or Saxo rejects it for another reason, the
keeper stops network retries and a new human browser login is required.
