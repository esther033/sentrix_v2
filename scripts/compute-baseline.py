#!/usr/bin/env python3
"""
Splits a run's transaction/workload data into "full run" (everything k6
recorded, including warm-up and cooldown) and "steady-state baseline"
(the [warmup_seconds, duration - cooldown_seconds) window only), per the
protocol in sentrix-project-context.md section 9 / workload/profiles/*.json.

Milestone 1's completion criteria (docs/MILESTONE1.md, project context
section 9) call for a baseline computed over the STEADY-STATE window --
this is what a later fault run's during-fault window gets compared
against. Reporting only the full-run aggregate (which includes warm-up
ramp-up and cooldown ramp-down) is not the same measurement and cannot be
used as that comparison point. This script produces both, explicitly
labeled, and never blends them into one number.

Writes runs/<run_id>/baseline-summary.json.
"""
import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone

import yaml


def parse_duration(s):
    """'30m' -> 1800, '90s' -> 90, '2h' -> 7200. Simple single-unit parser,
    matches what workload/profiles/*.json actually uses."""
    m = re.match(r"^(\d+)([smh])$", s.strip())
    if not m:
        raise ValueError(f"unrecognized duration format: {s!r}")
    value, unit = int(m.group(1)), m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600}[unit]
    return value * mult


def percentile(sorted_values, p):
    """p in [0,1]. Nearest-rank on the sorted sample, matches the
    'sorted sample's 99% position' definition used in review."""
    if not sorted_values:
        return None
    idx = min(int(len(sorted_values) * p), len(sorted_values) - 1)
    return sorted_values[idx]


def summarize(records, dropped_by_second, window_label):
    total = 0
    failures = 0
    timeouts = 0
    durations = []
    for r in records:
        total += 1
        if not r["success"]:
            failures += 1
        if r["timeout"]:
            timeouts += 1
        durations.append(r["duration_ms"])
    durations.sort()
    dropped = sum(dropped_by_second.values())
    return {
        "window": window_label,
        "transactions": total,
        "failure_count": failures,
        "success_rate_pct": round((total - failures) / total * 100, 2) if total else None,
        "timeouts": timeouts,
        "dropped_iterations": dropped,
        "duration_ms_p50": percentile(durations, 0.5),
        "duration_ms_p90": percentile(durations, 0.9),
        "duration_ms_p99": percentile(durations, 0.99),
        "duration_ms_max": durations[-1] if durations else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--runs-dir", default=os.path.join(os.path.dirname(__file__), "..", "runs")
    )
    args = ap.parse_args()

    run_dir = os.path.join(args.runs_dir, args.run_id)
    with open(os.path.join(run_dir, "run-manifest.yaml"), "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    profile = manifest["workload"]["profile"]
    warmup_s = profile["warmup_seconds"]
    cooldown_s = profile["cooldown_seconds"]
    duration_s = parse_duration(profile["duration"])

    txn_path = os.path.join(run_dir, "transaction-results.jsonl")
    all_summaries = []
    run_start_dt = None
    with open(txn_path, "r", encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            rec_dt = datetime.fromisoformat(rec["event_time"].replace("Z", "+00:00"))
            if run_start_dt is None or rec_dt < run_start_dt:
                run_start_dt = rec_dt
            if rec["stage"] == "TRANSACTION_SUMMARY":
                rec["_dt"] = rec_dt
                all_summaries.append(rec)

    steady_start = run_start_dt + timedelta(seconds=warmup_s)
    steady_end = run_start_dt + timedelta(seconds=duration_s - cooldown_s)

    steady_summaries = [
        r for r in all_summaries if steady_start <= r["_dt"] < steady_end
    ]

    ts_path = os.path.join(run_dir, "workload-timeseries.json")
    with open(ts_path, "r", encoding="utf-8") as f:
        timeseries = json.load(f)

    def in_steady_state(sec_str):
        dt = datetime.fromisoformat(sec_str).replace(tzinfo=timezone.utc)
        return steady_start <= dt < steady_end

    dropped_full = {d["second"]: d["dropped_iterations"] for d in timeseries}
    dropped_steady = {
        d["second"]: d["dropped_iterations"]
        for d in timeseries
        if in_steady_state(d["second"])
    }

    result = {
        "run_id": args.run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile_name": profile["profile_name"],
        "run_start": run_start_dt.isoformat(),
        "steady_state_window": {
            "start": steady_start.isoformat(),
            "end": steady_end.isoformat(),
            "warmup_seconds": warmup_s,
            "cooldown_seconds": cooldown_s,
        },
        "full_run": summarize(all_summaries, dropped_full, "full_run (includes warm-up/cooldown)"),
        "steady_state_baseline": summarize(
            steady_summaries, dropped_steady, "steady_state (warmup_seconds..duration-cooldown_seconds)"
        ),
    }

    out_path = os.path.join(run_dir, "baseline-summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
