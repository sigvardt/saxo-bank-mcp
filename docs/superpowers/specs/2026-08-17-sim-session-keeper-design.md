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
After the response and before saving, the command compares the current cache revision with the
original revision. A concurrent browser login or tool update wins: the command never overwrites the
changed cache, and the marker remains tied to the superseded revision. Only a durable save of the
complete rotated token permits marker removal. A legitimate cache update changes the revision and
permits one later attempt when that new token reaches the refresh margin. Missing, unreadable,
wrong-environment, or non-refreshable caches fail closed without a network call.

Install the exact committed package into a dedicated owner-only runtime outside the repository and
common cloud folders. A single macOS launchd job runs the one-shot command every 60 seconds and at
login. launchd never starts a duplicate instance for the same label. Logs contain only redacted
status codes and live in the owner-only runtime. The existing owner state cache remains outside the
repository and common cloud folders.

## Verification

Tests first prove fresh-cache no-op behavior, a durable owner-only marker before network work,
near-expiry refresh and atomic rotated-token save, ordinary rejection/success, marker-write and
cache-save failure suppression, concurrent-cache preservation, cache-change recovery, SIM-only
refusal, and secret-safe command output. Run the focused tests twice through `scripts/run-pytest`,
then Ruff and BasedPyright.

After installation, run one authorized SIM refresh and one session-capability read. Verify the
launchd definition, loaded process state, one label only, runtime and log permissions, restart
persistence, no secret leakage, and zero LIVE or write calls. Record and read back the interval job
in the Automations database.

## Limitation

Automatic renewal cannot make a Saxo refresh token permanent. If the machine stays offline long
enough for Saxo to expire or revoke the refresh token, or Saxo rejects it for another reason, the
keeper stops network retries and a new human browser login is required.
