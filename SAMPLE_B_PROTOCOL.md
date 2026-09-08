# Sample B v0.1 — prospective collection protocol

Status: implementation prepared; collection is disabled unless SAMPLE_B_ENABLED=true.
This protocol continues the exploratory audit in PR #6. No scoring parameter is changed.
The collection start is the first enabled startup after this implementation is committed
and deployed, stored once in SQLite. Earlier research rows are never enrolled.

## Frozen primary hypothesis and input identity

ETHUSDT continuation long, upward crossing of 70, TREND_UP, 240-minute cooldown,
240-minute holding window and 0.12 percentage-point round-trip proxy cost.
The live model is 0.1.2-research, including its existing book policy, rounding and
10-second price feature construction. This tests the live implementation prospectively;
it is not an exact replication of the minute-sampled Legacy Replay Sample A.
All other symbols, sides and thresholds in the existing research API remain exploratory.

The cohort persists code/protocol/settings/runtime-package SHA256 fingerprints and Railway source SHA.
A changed fingerprint or model version permanently blocks enrollment in this cohort;
reverting code does not silently resume it. A new implementation needs a new named
prospective sample and an explicit amendment. No deletion/reset to recover a favorable run.
Secrets and database location are excluded from the fingerprint.

## Enrollment and diagnostics

Require a fresh four hours after every process startup plus existing model data quality,
a finite positive price, finite score, and a last trade no more than five seconds old.
Restored pre-start prices cannot shorten this warmup. Missing data and every warmup
ETH observation are retained with exclusion reasons. Store the complete snapshot,
source hash, contemporaneous price features and past-only 1h realized volatility.
No interpolation of missing order flow.

A primary event requires two eligible TREND_UP observations at most 90 seconds apart,
with previous score <70 and current score >=70. A gap, warmup transition, or restart
cannot manufacture a crossing. Cooldown is based on prior enrolled events and persists
across restarts. Duplicate/out-of-order timestamps do not rewrite previous observations.

The pure-price gate is 1h return >=0.05% and 4h return >=0.15%, without Delta/OI or
flow-derived regime in its gate; it shares only the common observation quality screen.
Its benchmark enters on the first eligible observation and then on the first eligible
observation after each 240-minute cooldown, exactly as the fixed A diagnostic.
All price-eligible minutes are retained as potential matched controls, including minutes
that are themselves primary signals. Primary events need not pass the pure-price gate.

## Outcome preservation

For primary events and every price-eligible opportunity, materialize a 240-minute
outcome after horizon +180 seconds using the existing research path evaluator.
Use the first exit at or after the target, at most 180 seconds late. Require >=75%
10-second path coverage and max core path gap <=90 seconds. Keep excluded outcomes
and reasons. Store MAE/MFE, actual exit timestamps, and contemporaneous BTC endpoints.
The shared path sampler now refuses stale last trades even when WebSocket connected;
this is a measurement correction, with score formulas and thresholds untouched.
Price paths are retained 30 days; snapshot and materialized outcome records are not pruned.
A long outage can destroy pending path coverage: report exclusions, never backfill them
with interpolated paths. BTC historical beta inputs can be reconstructed from retained
minute snapshots under the fixed audit staleness rule; do not claim fine-path parity.

## Prespecified analysis after collection

Use only this cohort. Matched controls follow audit/PROTOCOL.md at PR #6 commit
19646183dfb9f2841bddb3a7cb4113032018d6ed: preceding 48 hours, control outcome complete
before signal, same direction and pure-price gate, >=30 past scaling rows, median/MAD
with standard-deviation fallback, per-coordinate distance <=2, RMS <=1, up to five
nearest controls separated by >=240 minutes. Features: 5m/15m/1h/4h price returns,
1h/4h efficiency, and 1h realized volatility. Control selection must not use outcome
return; exclude bad outcome quality after selection without choosing replacements.
Report unmatched signals, shared controls, unique days and full exclusion counts.

The collector saves inputs/outcomes; it does not run matching or statistical inference.
Before making a confirmatory claim, reproduce the frozen matching implementation on
these saved inputs, report net returns, matched differences and the fixed BTC beta
supplement, and inspect gaps. No caliper widening, feature replacement or threshold search.

Earliest confirmatory analysis: >=30 complete UTC calendar days elapsed after first
eligibility AND >=100 matured quality-qualified primary events (whichever later).
Counting eligible events instead of raw crossings is a conservative clarification of
the A protocol. Gates are not a power guarantee. Missing days/outages must be reported;
elapsed days are not independent observations. Status exposes counts, never a claim of edge.

For the confirmatory uncertainty calculation, aggregate event-weighted statistics by
UTC entry day, retain zero-event calendar days, and resample contiguous seven-day
blocks with replacement (circular bootstrap, 10,000 draws, seed 20260908), truncating
to original day count. Calculate mean primary net and mean matched difference separately;
report two-sided percentile 95% intervals and numbers of contributing days and events.
This is descriptive observational inference, not proof of causality. Fewer than ten
contributing UTC days, insufficient matching, or missing gates means inconclusive.
Require both lower confidence bounds >0 and both means >0 after separately removing
the best contributing day. Report all sensitivity results without picking a preferred one.
No interim efficacy decision or optional stopping; a failed test cannot justify retuning B.

## Operation

Deploy to the existing research service only, explicitly enable SAMPLE_B_ENABLED=true,
and verify /research/sample-b/status reports its committed source/fingerprint and warmup.
Default disabled ensures preparing/reviewing this change does not enroll data.
Existing /research/events and /research/threshold-sweep include pre-B observations and
must never be used as B results. Read /research/sample-b/export with bounded pagination;
re-export from after_id=0 after outcomes mature because earlier rows acquire outcomes.
Keep raw exports for reproducibility. Export pagination is by immutable observation ID.
No trading or order-placement capability is added.
