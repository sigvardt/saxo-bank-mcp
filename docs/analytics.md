# Analytics

Use `saxo_analytics_capabilities` to discover the installed recipes, source coverage and limits.
A module's availability proves its implementation was checked; it does not grant market-data
entitlements or supply missing account history. LIVE owner computation and private delivery use
the same runtime as offline fixture checks. Analytics makes no trading write.

Source capture is bounded and on demand. Capture the intended source families, then calculate
from their opaque dataset handles. The [analytics workflow](../skills/saxo-analytics/SKILL.md)
owns agent routing and interpretation; public tool schemas own the available choices.

Frozen source contracts validate the fields consumed by calculations. Partial response contracts
ignore additional unmodeled response fields without assigning meaning to them; required values,
known field types and known enums still fail on drift. Chart contracts retain strict additional
field checks because bar revision and adjustment semantics affect historical identities.

Current instrument lookup selects pages matching the installed source contract. Earlier contract
versions remain stored for history and cannot block fresh valid captures. Every selected page
still passes its full integrity checks; saved datasets and analyses retain their exact original
page bindings and refuse replay when those bindings are invalid.

Saved results bind original captures, explicit choices, prior model results and installed code.
Replay authenticates that dependency graph before explanation, charting or export. Changed,
deleted, invalid or expired evidence needs a fresh calculation. A combined source dataset does
not replace its original captures' validity checks.

The [release receipt](../data/analytics/production_release.json) records verification for exact
implementation, dependency, source and definition fingerprints. Changes quarantine the new
release until its checks pass. The runtime registry owns recipe inventory; the older catalog's
historical refusal and SIM fixture evidence cannot activate a changed implementation. Preserve
those historical receipts rather than rewriting their outcome as a production success.

Metrics and dimensioned table cells carry units and currency; some ancillary cells have no
declared unit. Degraded results retain missing dimensions,
delay, source warnings and approximation labels. Model outputs remain explicitly labelled:
option valuation needs declared assumptions, a fixed-income model needs explicit cash flows,
and Monte Carlo outcomes describe a model distribution. Historical backtesting is a simulation
with documented costs, fills and data limits; it does not verify broker execution. Trade-activity
export is distinct from authoritative tax-lot reporting.

Long calculations run in process-owned jobs. Cancellation and result commitment share one atomic
boundary: early cancellation publishes no result, while completed work keeps its exact saved
analysis. Shutdown stops the worker before closing its store; restarting requires a new job.

Charts and reports replay the saved values. Generic table charts require explicit fields with
units; reports include table-only results and all limitations. Small artifacts can be delivered
inline privately. Larger artifacts use an owner resource that rechecks proof and file integrity
when read. Neither route accepts caller paths, visibility stamps or replacement values.

Verify changes with the repository's pytest launcher, lint and type checks, normal MCP fixture
calls, packaging checks and a bounded LIVE GET readback. Record evidence outside public values;
never turn a successful refusal or an upstream `NoAccess` into a complete-source success.
