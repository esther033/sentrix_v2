"""Predeclared v2 observation coverage and matched-control checks."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import yaml
from view_integrity import validate_views

VERSION = "v3"


def stamp(row):
    value = datetime.fromisoformat(row["event_time"].replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("Timezone required")
    return value


def seconds(value):
    m = re.fullmatch(r"(\d+)([smh])", value)
    if not m:
        raise ValueError("Invalid duration")
    return int(m[1]) * {"s": 1, "m": 60, "h": 3600}[m[2]]


MAX_INTER_TRANSACTION_GAP_S = 15
PHASE_NAMES = ("warmup", "baseline", "fault", "recovery", "cooldown")


def phase_plan(profile):
    """Return the declared rate and bounds of each workload phase.

    Ordinary profiles have one constant rate. Traffic-surge profiles declare
    a rate for every existing protocol phase, so their 5 rps segment is
    measured rather than hidden in a whole-run average.
    """
    rates = profile.get("traffic_phase_rps")
    if not rates:
        return [("all", seconds(profile["duration"]), float(profile["target_rps"]))]
    plan = []
    for name in PHASE_NAMES:
        duration = int(profile[name + "_seconds"])
        rate = rates.get(name)
        if duration <= 0 or not isinstance(rate, (int, float)) or rate <= 0:
            raise ValueError("Invalid traffic phase plan")
        plan.append((name, duration, float(rate)))
    if sum(p[1] for p in plan) != seconds(profile["duration"]):
        raise ValueError("Traffic phase plan duration mismatch")
    return plan


def covered(rows, start, end, rate):
    times = sorted(stamp(r) for r in rows if start <= stamp(r) < end)
    gap = MAX_INTER_TRANSACTION_GAP_S
    return (len(times) >= .9 * rate * (end-start).total_seconds()
            and bool(times) and (times[0]-start).total_seconds() <= gap
            and (end-times[-1]).total_seconds() <= gap
            and all((b-a).total_seconds() <= gap for a,b in zip(times,times[1:])))


def impact(rows, start, end, baseline, rate=None):
    rate = baseline["target_rps"] if rate is None else rate
    ordered = sorted((r for r in rows if start <= stamp(r) < end), key=stamp)
    for r in ordered:
        a, b = stamp(r), stamp(r)+timedelta(seconds=30)
        if b > end:
            break
        window = [x for x in ordered if a <= stamp(x) < b]
        if not covered(window, a, b, rate):
            continue
        success = 100*sum(x["success"] for x in window)/len(window)
        d = sorted(x["duration_ms"] for x in window)
        if success < baseline["success_rate_pct"]-5:
            return True, "error_rate_increase", a.isoformat()
        if d[int(len(d)*.99)] > baseline["p99_ms"]*3:
            return True, "latency_increase", a.isoformat()
    return False, None, None


def recovery(rows, removed, baseline):
    ordered = sorted((r for r in rows if stamp(r) >= removed), key=stamp)
    first = next((r["event_time"] for r in ordered if r["success"]), None)
    rate = baseline["target_rps"]
    full_observation = False
    for i in range(max(0,len(ordered)-9)):
        ten = ordered[i:i+10]
        a = stamp(ten[-1]) + timedelta(milliseconds=ten[-1]["duration_ms"])
        b = a+timedelta(seconds=120)
        clean = [r for r in ordered if a <= stamp(r) < b]
        continuous = covered(clean,a,b,rate)
        if continuous:
            full_observation = True
        if not continuous or not all(r["success"] for r in ten):
            continue
        if any((stamp(y)-stamp(x)).total_seconds()>max(5,3/rate) for x,y in zip(ten,ten[1:])):
            continue
        if sorted(r["duration_ms"] for r in ten)[5] > baseline["p50_ms"]*1.5:
            continue
        if not impact(clean,a,b,baseline)[0]:
            return first, ten[0]["event_time"], "pass"
    return first, None, "fail" if full_observation else "unknown"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def assess_run(run_dir, generation="v2", control=False):
    p = Path(run_dir)
    errors = []
    try:
        manifest = yaml.safe_load((p/"run-manifest.yaml").read_text(encoding="utf-8"))
        profile = manifest["workload"]["profile"]
        duration = seconds(profile["duration"])
        plan = phase_plan(profile)
        rows = [json.loads(l) for l in (p/"transaction-results.jsonl").read_text().splitlines() if l]
        rows = sorted((r for r in rows if r["stage"]=="TRANSACTION_SUMMARY"),key=stamp)
        if not rows or any(r["run_id"]!=p.name for r in rows):
            raise ValueError("Empty/mismatched transactions")
        elapsed = (stamp(rows[-1])-stamp(rows[0])).total_seconds()
        expected_transactions = sum(duration_s * rate for _, duration_s, rate in plan)
        actual_rate = len(rows)/duration
        if not duration-5 <= elapsed <= duration+30: errors.append("transaction duration mismatch")
        if not .9*expected_transactions <= len(rows) <= 1.1*expected_transactions: errors.append("achieved rate outside 90-110%")
        phase_start = stamp(rows[0])
        for phase, phase_duration, phase_rate in plan:
            phase_end = phase_start + timedelta(seconds=phase_duration)
            if not covered(rows, phase_start, phase_end, phase_rate):
                errors.append("transaction coverage gap" if phase == "all" else "transaction coverage gap: " + phase)
            phase_start = phase_end
        if len({r['transaction_id'] for r in rows}) != len(rows): errors.append("duplicate transaction IDs")
        dropped = sum(r["dropped_iterations"] for r in read_json(p/"workload-timeseries.json"))
        if dropped > .02*expected_transactions: errors.append("dropped iterations exceed 2%")
        runtime = read_json(p/"runtime.json")
        if runtime.get("workload_rc") != 0 or runtime.get("errors"): errors.append("workload supervisor failed")
        if not duration-5 <= runtime.get("workload_elapsed_s",0) <= duration+60: errors.append("runtime duration mismatch")
        if manifest.get("evaluation_policy_version") != VERSION: errors.append("wrong policy version")
        quality = read_json(p/"quality-report.json")
        if any(quality.get(k,{}).get("status")!="PASS" for k in ("m1a_gate_a_transaction_telemetry","m1a_gate_b_k8s_event_pipeline")): errors.append("quality gate failed/skipped")
        if not validate_views(p,generation)["complete"]: errors.append("data views incomplete")
        pre, post = read_json(p/"environment-before.json"), read_json(p/"environment-after.json")
        if pre["fingerprint"]!=post["fingerprint"]: errors.append("environment configuration changed")
        if not post["healthy"]: errors.append("environment unhealthy after workload")
        if control:
            if manifest.get("fault",{}).get("injected") or (p/"fault-record.json").exists(): errors.append("control contains fault")
            if sum(r["success"] for r in rows)/len(rows)<.99: errors.append("control success below 99%")
            if pre["restarts"] != post["restarts"]: errors.append("control pod restart/change")
        return {"valid":not errors,"errors":errors,"actual_rps":actual_rate,"elapsed_s":elapsed,
                "started_at":rows[0]["event_time"],"ended_at":rows[-1]["event_time"],
                "fingerprint":pre["fingerprint"],"profile":profile,
                "expected_transactions":expected_transactions,"phase_plan":plan}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {"valid":False,"errors":[str(exc)]}


def match_control(control_dir, profile, fingerprint, at, generation="v2"):
    assessment = assess_run(control_dir,generation,control=True)
    errors = list(assessment["errors"])
    if assessment["valid"]:
        keys = ("target_rps","time_unit","duration","warmup_seconds","baseline_seconds","fault_seconds",
                "recovery_seconds","cooldown_seconds","pre_allocated_vus","max_vus","stage_timeout")
        if any(assessment['profile'].get(k)!=profile.get(k) for k in keys): errors.append("control workload mismatch")
        if profile.get("fault", {}).get("type") == "traffic_surge":
            if assessment["profile"].get("traffic_phase_rps") != profile.get("control_phase_rps"):
                errors.append("control traffic phase rates mismatch")
        elif assessment["profile"].get("traffic_phase_rps") != profile.get("traffic_phase_rps"):
            errors.append("control traffic phase rates mismatch")
        if assessment["fingerprint"] != fingerprint: errors.append("control environment mismatch")
        age = (at-datetime.fromisoformat(assessment["ended_at"].replace("Z","+00:00"))).total_seconds()
        if not 0 <= age <= 7200: errors.append("control is not preceding/fresh within 2h")
    return {"valid":not errors,"errors":errors}
