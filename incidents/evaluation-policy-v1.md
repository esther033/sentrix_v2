# Evaluation policy v1 -- fixed BEFORE any Milestone 2 fault is injected

Per sentrix-project-context.md section 9/10 and the explicit instruction
for Milestone 2: normal/degraded/recovery criteria must be fixed before
the experiment, not tuned to the result afterward. This file is that
fixed policy. `evaluation_policy_version: v1` in every M2 run manifest
refers to exactly this document; if the thresholds below are ever
revised, that becomes v2, and v1 stays as the historical record of what
governed the runs that cite it.

## Baseline: a fresh, matching control run -- REQUIRED, not a stored constant

Every fault run's impact/recovery thresholds are computed against a
`--control-run-id`'s own steady-state (warmup..duration-cooldown)
success rate / p50 / p99, produced by the SAME workload profile, SAME
duration, SAME environment, run separately with no fault applied (e.g.
`scripts/run-experiment.sh <control_run_id> <same profile.json>`).
`scripts/generate-incident-record.py --control-run-id` is a required
argument specifically so this can never silently fall back to a number
from a different day's run.

**Corrected after review**: an earlier version of this document (and
the code implementing it) used `runs/m1b-main-001`'s Milestone-1 numbers
(98.22% / 275ms / 649ms) as the actual comparison baseline, not just a
sanity check. That is wrong for two reasons independent of each other:
(1) a baseline from a different day's run cannot account for whatever
this specific run's environment looked like immediately before the
fault, and (2) it was discovered, by recomputing p50 directly from a
real pilot run's own pre-fault transactions, that the environment
immediately before fault injection can ITSELF already be running well
above the M1B reference (596ms observed vs. the 275ms constant) --
meaning a fault run judged against the stale constant could show
"impact" or "no recovery" that is actually just this run's own baseline
being different from M1B's, not an effect of the fault at all. The
control run must be freshly measured every time, per fault run, and
compared using its own numbers, never a cached constant from a prior
milestone.

## Recovery observation must have ACTUALLY been observed for the full window

`scripts/generate-incident-record.py`'s recovery check requires the
post-window-start clean period to have actual transaction data spanning
at least 90% of `RECOVERY_CLEAN_OBSERVATION_S` (108 of 120 seconds) --
not just "whatever data happens to be available after
`fault_removed_at`". A candidate window with only a few seconds of
trailing data (e.g. near the very end of the run) cannot be used to
declare `recovered`; it is treated as insufficient evidence, and the
search continues to the next candidate window rather than passing on
too little data.

## Fault removal must be positively confirmed, not just "not seen to be still injected"

`scripts/run-fault-experiment.py`'s `poll_fault_removed()` only accepts
two outcomes as removal confirmation: the Chaos Mesh object explicitly
reporting `AllRecovered: "True"`, or the object being genuinely gone
(kubectl reporting `NotFound`, not any other kubectl error). A
communication failure (API server unreachable, auth error, transient
network issue) while polling is NOT treated as removal -- the poll keeps
retrying until it gets one of the two real answers or times out.
`fault.removal_confidence` in the fault record and incident record is
`"confirmed"` or `"timeout"`; an incident record's `impact`/`recovery`
evaluation only runs when removal was `"confirmed"`.

## Raw trace export completeness is verified against known transaction trace_ids

Tempo's `/api/search` alone is not sufficient evidence of a complete
trace export -- a single call is capped at its `limit` and can silently
truncate. `scripts/export-telemetry.py` now (a) searches in
sub-windows and unions results, and (b) directly fetches every
`trace_id` that this run's OWN transactions reference (from
`transaction-results.jsonl`), independent of whether search discovered
it. `raw/export-manifest.json`'s `traces.known_trace_ids_missing` is
the authoritative completeness signal; `discovered == exported` is NOT
evidence of completeness on its own.

## Impact criteria (fault_active_no_impact vs. abnormal_impacted)

A fault run's during-fault window is `abnormal_impacted` if, during the
fault injection interval, EITHER:

1. **success rate** drops below the matching control run's own
   success_rate minus 5 percentage points, sustained over a rolling
   30-second window, OR
2. **p99 latency** exceeds 3x the matching control run's own p99,
   sustained over a rolling 30-second window (not a single sample -- a
   single slow request is noise, not impact).

If the fault's injector reports success but NEITHER condition is met
during the fault window, the run is `fault_active_no_impact`, per
sentrix-project-context.md section 6 -- preserved, but excluded from
the impacted-incident RCA subset.

"Sustained over a rolling 30-second window" means: computed per-second
from `workload-timeseries.json` + `transaction-results.jsonl`, a
30-second window's aggregate success rate / p99 must cross the
threshold, not any single 1-second sample.

## Recovery criteria (transaction_stable_at)

Per section 10's required separation of fault-removal, vendor-recovery,
and transaction-observed-recovery signals (SentriX has no vendor
monitor, so only the first and third apply here):

- `fault_removed_at`: timestamp Chaos Mesh reports the fault object's
  `.status` as no longer active (verified by re-querying the fault
  resource, not assumed from the scheduled duration).
- `transaction_first_passing_at`: first individual transaction, at or
  after `fault_removed_at`, with `success: true` (from
  `transaction-results.jsonl`).
- `transaction_stable_at`: the start of the first rolling 10-consecutive-
  transaction window, at or after `fault_removed_at`, where ALL 10
  transactions have `success: true` AND p50 latency across that window
  is within 1.5x the control run's own p50 (see "Baseline" above --
  this number is computed per fault run from its matching control run,
  not a fixed constant). This window must itself be followed by an
  additional 2-minute observation period, with actual transaction data
  covering at least 90% of that window (see "Recovery observation must
  have ACTUALLY been observed" above), with no further
  `abnormal_impacted`-triggering 30-second window (per Impact criteria
  above) -- a single clean burst right after fault removal that then
  relapses is NOT `recovered`.

`recovered` is assigned only once both the 10-transaction window and the
2-minute clean-observation period are satisfied. Anything after
`fault_removed_at` but before that point is `recovery` (fault ended,
recovery criteria not yet fully met).

## What this policy does NOT claim

- Not a claim of "true" service recovery for all users -- only for the
  synthetic `browse -> cart -> checkout` transaction profile actually
  run, per sentrix-project-context.md section 10's `transaction_stable_at`
  definition.
- Not validated against a vendor recovery signal (no Datadog/Dynatrace
  integration in this milestone) -- SentriX's own recovery evaluator is
  the only source here.
- The 5-point / 3x / 1.5x / 10-transaction / 2-minute numbers are this
  project's own first-pass judgment calls, informed by the M1B baseline's
  observed variance (98.22% success, p50/p90/p99 spread), not a
  universal standard. They are declared explicitly so that later runs
  can be judged consistently against them, not to claim they are
  optimal thresholds.
