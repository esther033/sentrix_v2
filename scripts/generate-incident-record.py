#!/usr/bin/env python3
"""
Fills incidents/<incident_id>.yaml from a completed fault run, applying
the FIXED-SHAPE thresholds in incidents/evaluation-policy-v1.md -- never
tuned post-hoc to the result. The multipliers/windows below (5pp, 3x,
1.5x, 10-transaction, 120s) are fixed literals from that policy; the
BASELINE values they're applied against are NOT hardcoded here -- they
are computed from a --control-run-id's own steady-state, per the
policy's own requirement for "a fresh, same-length, same-environment
control run", not from a prior milestone's numbers. Reusing a stale
constant from a different run/day was flagged as a distinct defect
(comparing this run's post-fault latency against a borrowed baseline
that had already drifted) and is why this argument is required, not
optional.
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import yaml
from view_integrity import validate_views
from telemetry_export import atomic_write

ROOT = os.path.join(os.path.dirname(__file__), "..")

# Fixed per incidents/evaluation-policy-v1.md.
IMPACT_SUCCESS_DROP_PP = 5.0
IMPACT_P99_MULTIPLIER = 3.0
IMPACT_WINDOW_S = 30
RECOVERY_WINDOW_TXN_COUNT = 10
RECOVERY_P50_MULTIPLIER = 1.5
RECOVERY_CLEAN_OBSERVATION_S = 120
# An observation window must cover at least this fraction of
# RECOVERY_CLEAN_OBSERVATION_S in actual elapsed transaction time before
# it can be used to declare stability -- otherwise a short burst of a
# handful of transactions (e.g. the last few seconds of available data)
# could pass simply because there wasn't enough data left to fail.
RECOVERY_MIN_OBSERVED_SPAN_FRACTION = 0.9
EVALUATION_POLICY_VERSION = "v3"


def parse_duration_seconds(s):
    import re

    m = re.match(r"^(\d+)([smh])$", s.strip())
    value, unit = int(m.group(1)), m.group(2)
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


def load_transactions(run_dir):
    rows = []
    with open(os.path.join(run_dir, "transaction-results.jsonl"), "r", encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    return [r for r in rows if r["stage"] == "TRANSACTION_SUMMARY"]


def compute_control_baseline(control_run_dir):
    """Steady-state (warmup..duration-cooldown) success rate / p50 / p99
    from a control run's OWN transactions -- same window definition
    scripts/compute-baseline.py uses for M1B, applied here to whatever
    run_id is passed as --control-run-id. Returns a dict, and raises if
    the control run has no data (never silently falls back to a
    hardcoded number)."""
    with open(os.path.join(control_run_dir, "run-manifest.yaml"), "r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    profile = manifest["workload"]["profile"]
    warmup_s = profile["warmup_seconds"]
    cooldown_s = profile["cooldown_seconds"]
    duration_s = parse_duration_seconds(profile["duration"])

    summaries = load_transactions(control_run_dir)
    if not summaries:
        raise ValueError(f"control run {control_run_dir} has no transactions")
    times = [datetime.fromisoformat(r["event_time"].replace("Z", "+00:00")) for r in summaries]
    run_start = min(times)
    steady_start = run_start + timedelta(seconds=warmup_s)
    steady_end = run_start + timedelta(seconds=duration_s - cooldown_s)

    steady = [
        r for r, t in zip(summaries, times) if steady_start <= t < steady_end
    ]
    if not steady:
        raise ValueError(f"control run {control_run_dir} has no steady-state transactions")

    success_rate = sum(1 for r in steady if r["success"]) / len(steady) * 100
    durations = sorted(r["duration_ms"] for r in steady)
    p50 = durations[len(durations) // 2]
    p99 = durations[int(len(durations) * 0.99)]
    return {
        "control_run_dir": control_run_dir,
        "target_rps": profile["target_rps"],
        "n_transactions": len(steady),
        "success_rate_pct": success_rate,
        "p50_ms": p50,
        "p99_ms": p99,
    }


from evaluation_v2 import impact as evaluate_impact, recovery as evaluate_recovery, assess_run, match_control


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--incident-id", required=True)
    ap.add_argument("--experiment-id", required=True)
    ap.add_argument("--data-generation", default="v2")
    ap.add_argument("--control-data-generation", default="v2")
    ap.add_argument(
        "--control-run-id",
        required=True,
        help="run_id of a same-environment/workload/duration no-fault control run "
        "(e.g. produced by scripts/run-experiment.sh with the same profile). "
        "Its own steady-state success/p50/p99 is the baseline impact/recovery "
        "are evaluated against -- required, not optional, per evaluation-policy-v1.md.",
    )
    args = ap.parse_args()

    run_dir = os.path.join(ROOT, "runs", args.run_id)
    control_dir = os.path.join(ROOT, "runs", args.control_run_id)
    with open(os.path.join(run_dir, "fault-record.json"), "r", encoding="utf-8") as f:
        fault_record = json.load(f)
    with open(os.path.join(run_dir, "run-manifest.yaml"), "r", encoding="utf-8") as f:
        run_manifest = yaml.safe_load(f)
    with open(os.path.join(run_dir, "quality-report.json"), "r", encoding="utf-8") as f:
        quality_report = json.load(f)

    baseline = compute_control_baseline(control_dir)

    data_status = validate_views(run_dir, args.data_generation)
    control_data_status = validate_views(control_dir, args.control_data_generation)
    raw_export_complete = data_status["raw_export_complete"]
    profile = run_manifest["workload"]["profile"]

    summaries = load_transactions(run_dir)
    run_assessment = assess_run(run_dir, args.data_generation)
    before = json.load(open(os.path.join(run_dir, "environment-before.json"), encoding="utf-8"))
    match = match_control(control_dir, run_manifest["workload"]["profile"], before["fingerprint"],
                          min(datetime.fromisoformat(r["event_time"].replace("Z", "+00:00")) for r in summaries), args.control_data_generation)

    fault_started = fault_record.get("started_at")
    fault_ended = fault_record.get("ended_at")
    removal_confirmed = fault_record.get("removal_confidence") == "confirmed"

    impact_confirmed, critical_failure, impact_started_at = (False, None, None)
    if fault_started and fault_ended:
        fs = datetime.fromisoformat(fault_started)
        fe = datetime.fromisoformat(fault_ended)
        fault_rate = profile.get("traffic_phase_rps", {}).get("fault")
        impact_confirmed, critical_failure, impact_started_at = evaluate_impact(summaries, fs, fe, baseline, fault_rate)

    first_passing_at, stable_at, observation_status = (None, None, "unknown")
    if fault_ended and removal_confirmed:
        fe = datetime.fromisoformat(fault_ended)
        first_passing_at, stable_at, observation_status = evaluate_recovery(summaries, fe, baseline)

    if not fault_started or not fault_ended or not removal_confirmed:
        status = "unknown"
    elif not impact_confirmed:
        status = "fault_active_no_impact"
    elif stable_at:
        status = "recovered"
    else:
        status = "recovery" if observation_status == "unknown" else "abnormal_impacted"

    times = [datetime.fromisoformat(r["event_time"].replace("Z", "+00:00")) for r in summaries]
    total_dropped = 0
    ts_path = os.path.join(run_dir, "workload-timeseries.json")
    if os.path.exists(ts_path):
        with open(ts_path, "r", encoding="utf-8") as f:
            timeseries = json.load(f)
        total_dropped = sum(d["dropped_iterations"] for d in timeseries)
    # Elapsed wall-clock time from the transactions' own actual first/last
    # event_time, NOT len(workload-timeseries.json) -- that file only has
    # an entry for seconds with at least one iteration/http_req/dropped_
    # iteration; empty seconds are simply absent from it, which silently
    # shrinks the denominator and inflates the computed rate (observed:
    # 2.166 reported vs. ~2.0 actual over the true ~1140.7s span).
    elapsed_s = (max(times) - min(times)).total_seconds() if times else None
    actual_rps = round(len(summaries) / elapsed_s, 3) if elapsed_s else None

    fault_type = fault_record["type"]
    taxonomy = {
        "cpu_saturation": ("resource", "Platform / compute", "service_instance"),
        "pod_failure": ("availability", "Kubernetes / process", "service_instance"),
        "network_delay": ("network", "L3/L4", "dependency_edge"),
        "packet_loss": ("network", "L3/L4", "dependency_edge"),
        "traffic_surge": ("workload", "Workload / L7", "entrypoint"),
    }
    cause_family, injection_layer, scope_kind = taxonomy.get(fault_type, ("unknown", "unknown", "unknown"))
    scope_kind = profile.get('fault', {}).get('scope_kind', scope_kind)
    if scope_kind not in ('service_instance', 'dependency_edge', 'entrypoint'):
        raise ValueError('Unsupported root-cause scope')
    chaos_object = None if fault_record["fault_kind"] == "workload" else f"{fault_record['fault_kind']}/{fault_record['fault_name']}"
    record = {
        "incident_id": args.incident_id,
        "experiment_id": args.experiment_id,
        "run_id": args.run_id,
        "control_run_id": args.control_run_id,
        "baseline": baseline,
        "fault": {
            "injected": True,
            "type": fault_type,
            "cause_family": cause_family,
            "injection_layer": injection_layer,
            "scope_kind": scope_kind,
            "scope_description": profile.get('fault', {}).get('scope_description'),
            "root_cause_service": fault_record["root_cause_service"],
            "target_pod_uid": fault_record["target_pod_uid"],
            "chaos_mesh_object": chaos_object,
            "started_at": fault_started,
            "ended_at": fault_ended,
            "removal_confidence": fault_record.get("removal_confidence"),
            "injector_status": fault_record["injector_status"],
        },
        "workload": {
            "profile": profile["profile_name"],
            "target_rps": profile["target_rps"],
            "actual_rps": actual_rps,
            "dropped_iterations": total_dropped,
        },
        "impact": {
            "confirmed": impact_confirmed,
            "critical_failure": critical_failure,
            "affected_endpoint": "checkout",
            "impact_started_at": impact_started_at,
            "evaluation_policy_version": EVALUATION_POLICY_VERSION,
        },
        "recovery": {
            "fault_removed_at": fault_ended if removal_confirmed else None,
            "transaction_observation": {
                "profile": profile["profile_name"],
                "first_passing_at": first_passing_at,
                "stable_at": stable_at,
                "observation_status": observation_status,
            },
            "evaluation_policy_version": EVALUATION_POLICY_VERSION,
        },
        "status": status,
        "data_views": {key: f"runs/{args.run_id}/{value}/" for key, value in data_status["views"].items()},
        "data_generation": args.data_generation,
        "data_validation": {"run": data_status, "control": control_data_status},
        "validity": {
            "injector_succeeded": fault_record["injector_status"] == "succeeded",
            "removal_confirmed": removal_confirmed,
            "telemetry_present_all_signals": quality_report.get("m1a_overall") == "PASS",
            "actual_rate_meets_minimum": run_assessment.get("valid", False),
            "runtime_assessment": run_assessment,
            "control_match": match,
            "dropped_iterations_under_threshold": total_dropped < max(10, len(summaries) * 0.02),
            # None (not True) if raw/export-manifest.json is missing entirely
            # -- distinct from "ran and confirmed incomplete".
            "raw_export_complete": raw_export_complete,
            "data_views_complete": data_status["complete"],
            "control_data_views_complete": control_data_status["complete"],
            "is_valid_run": None,  # set below
        },
    }
    v = record["validity"]
    v["is_valid_run"] = bool(
        v["injector_succeeded"]
        and v["removal_confirmed"]
        and v["telemetry_present_all_signals"]
        and v["raw_export_complete"] is True
        and v["data_views_complete"]
        and v["control_data_views_complete"]
        and v["actual_rate_meets_minimum"]
        and match["valid"]
        and v["dropped_iterations_under_threshold"]
    )

    incidents_dir = os.path.join(ROOT, "incidents")
    os.makedirs(incidents_dir, exist_ok=True)
    out_path = os.path.join(incidents_dir, f"{args.incident_id}.yaml")
    atomic_write(out_path, yaml.dump(record, sort_keys=False, default_flow_style=False), preserve=True)

    print(f"wrote {out_path}")
    print(yaml.dump(record, sort_keys=False, default_flow_style=False))
    if not v["is_valid_run"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
