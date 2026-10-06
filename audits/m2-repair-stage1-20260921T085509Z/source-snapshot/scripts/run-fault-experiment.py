#!/usr/bin/env python3
"""
Orchestrates one Milestone 2 fault run: standard protocol timing
(warm-up -> baseline -> fault injection -> recovery observation ->
cooldown), workload running throughout via the same k6/transaction-
recorder used in M1, fault injected via a Chaos Mesh CR applied
mid-run.

fault_removed_at and started_at are both taken from OBSERVED Chaos Mesh
object status via polling, never assumed from the scheduled duration --
per the instruction to separate injector-reported fault lifecycle from
anything else, and to verify (not assume) fault removal and environment
restoration.

Usage:
  python scripts/run-fault-experiment.py \
    --run-id m2-cpu-checkoutservice-pilot-001 \
    --profile workload/profiles/m2-cpu-checkoutservice-pilot.json \
    --incident-id inc-m2-cpu-checkoutservice-pilot-001 \
    --experiment-id exp-cpu-checkoutservice-pilot
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def kubectl(*args, check=True, capture=True):
    proc = subprocess.run(
        ["kubectl", *args], text=True, capture_output=capture
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)} failed: {proc.stderr}")
    return proc


def render_fault_manifest(template_path, subs):
    with open(os.path.join(ROOT, template_path), "r", encoding="utf-8") as f:
        content = f.read()
    for key, val in subs.items():
        content = content.replace("{{" + key + "}}", str(val))
    return content


def apply_fault(manifest_text):
    proc = subprocess.run(
        ["kubectl", "apply", "-f", "-"], input=manifest_text, text=True, capture_output=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl apply fault failed: {proc.stderr}")
    return proc.stdout


def get_fault_status(kind, name, namespace="sentrix"):
    """Returns (obj_or_None, genuinely_not_found_bool). A kubectl failure
    that is NOT the API server reporting NotFound (connection refused,
    auth failure, apiserver timeout, transient network blip) must NOT be
    treated the same as the object legitimately being gone -- previously
    ALL kubectl failures collapsed to the same `None`, and the caller
    read that as "fault removed", which means a communication error
    during the removal-confirmation poll could get recorded as a
    verified fault removal. genuinely_not_found_bool is only True when
    kubectl's own stderr says so."""
    proc = kubectl("get", kind, name, "-n", namespace, "-o", "json", check=False)
    if proc.returncode == 0:
        return json.loads(proc.stdout), False
    stderr_lower = (proc.stderr or "").lower()
    not_found = "notfound" in stderr_lower.replace(" ", "")
    return None, not_found


def poll_fault_injected(kind, name, timeout_s=60, poll_interval=2):
    """Poll until the fault object reports at least one instance injected.
    Returns (observed_at_iso, raw_status_dict)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        obj, _ = get_fault_status(kind, name)
        if obj:
            status = obj.get("status", {})
            conditions = status.get("conditions", [])
            injected = any(
                c.get("type") == "AllInjected" and c.get("status") == "True"
                for c in conditions
            )
            instances = status.get("instances", {})
            if injected or instances:
                return now_iso(), status
        time.sleep(poll_interval)
    return None, None


def poll_fault_removed(kind, name, timeout_s=120, poll_interval=3):
    """Poll until the fault object explicitly reports AllRecovered=True,
    or is confirmed genuinely gone (kubectl NotFound, not just any
    error). Returns (observed_at_iso_or_None, confidence) where
    confidence is "confirmed" (AllRecovered=True or verified NotFound) or
    "timeout" (neither happened before timeout_s -- caller must not treat
    this as removal). A communication error never returns as removed;
    the poll just keeps retrying until it either gets a real answer or
    times out."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        obj, not_found = get_fault_status(kind, name)
        if obj is None:
            if not_found:
                return now_iso(), "confirmed"  # genuinely gone
            time.sleep(poll_interval)
            continue
        status = obj.get("status", {})
        conditions = status.get("conditions", [])
        all_recovered = any(
            c.get("type") == "AllRecovered" and c.get("status") == "True"
            for c in conditions
        )
        if all_recovered:
            return now_iso(), "confirmed"
        time.sleep(poll_interval)
    return None, "timeout"


def get_target_pod_uid(app_label, namespace="sentrix"):
    proc = kubectl(
        "get", "pods", "-n", namespace, "-l", f"app={app_label}",
        "-o", "jsonpath={.items[0].metadata.uid}", check=False,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--profile", required=True)
    ap.add_argument("--incident-id", required=True)
    ap.add_argument("--experiment-id", required=True)
    ap.add_argument(
        "--control-run-id",
        required=True,
        help="run_id of a matching no-fault control run, passed through to "
        "generate-incident-record.py",
    )
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--k6-bin", default=os.environ.get("K6_BIN", "k6"))
    args = ap.parse_args()

    with open(args.profile, "r", encoding="utf-8") as f:
        profile = json.load(f)

    fault_cfg = profile["fault"]
    warmup_s = profile["warmup_seconds"]
    baseline_s = profile["baseline_seconds"]
    fault_s = profile["fault_seconds"]

    run_dir = os.path.join(ROOT, "runs", args.run_id)
    os.makedirs(run_dir, exist_ok=True)

    print(f"[fault-experiment] launching workload for {profile['duration']}...", file=sys.stderr)
    recorder_proc = subprocess.Popen(
        [
            sys.executable,
            os.path.join(ROOT, "recorder", "transaction-recorder.py"),
            "--run-id", args.run_id,
            "--profile", args.profile,
            "--base-url", args.base_url,
            "--k6-bin", args.k6_bin,
        ]
    )

    wait_before_fault = warmup_s + baseline_s
    print(f"[fault-experiment] waiting {wait_before_fault}s (warmup+baseline) before injecting fault...", file=sys.stderr)
    time.sleep(wait_before_fault)

    fault_name = f"{args.run_id}-{fault_cfg['type'].replace('_','-')}"[:63]
    target_service = fault_cfg["root_cause_service"]
    subs = {
        "NAME": fault_name,
        "TARGET_SERVICE": target_service,
        "DURATION": f"{fault_s}s",
        **fault_cfg.get("params", {}),
    }
    manifest_text = render_fault_manifest(fault_cfg["template"], subs)
    print(f"[fault-experiment] applying fault {fault_name}...", file=sys.stderr)
    apply_fault(manifest_text)

    fault_kind = yaml.safe_load(manifest_text)["kind"].lower()
    target_pod_uid = get_target_pod_uid(target_service)

    started_at, inject_status = poll_fault_injected(fault_kind, fault_name)
    print(f"[fault-experiment] fault injected observed at: {started_at}", file=sys.stderr)

    print(f"[fault-experiment] waiting {fault_s}s for fault duration...", file=sys.stderr)
    time.sleep(fault_s)

    ended_at, removal_confidence = poll_fault_removed(fault_kind, fault_name)
    print(f"[fault-experiment] fault removal observed at: {ended_at} (confidence={removal_confidence})", file=sys.stderr)

    print("[fault-experiment] waiting for workload to finish (recovery+cooldown)...", file=sys.stderr)
    recorder_proc.wait()

    fault_record = {
        "fault_kind": fault_kind,
        "fault_name": fault_name,
        "type": fault_cfg["type"],
        "root_cause_service": target_service,
        "target_pod_uid": target_pod_uid,
        "started_at": started_at,
        "ended_at": ended_at,
        "removal_confidence": removal_confidence,
        "injector_status": (
            "succeeded" if started_at and ended_at and removal_confidence == "confirmed" else "unknown"
        ),
        "raw_inject_status": inject_status,
    }
    with open(os.path.join(run_dir, "fault-record.json"), "w", encoding="utf-8") as f:
        json.dump(fault_record, f, indent=2)

    print(json.dumps(fault_record, indent=2))

    # standard post-run pipeline: manifest, quality gate, export/canonical/re2
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "generate-run-manifest.py"),
         "--run-id", args.run_id, "--profile", args.profile],
        check=True,
    )
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "smoke", "quality-check.py"), "--run-id", args.run_id],
        check=False,
    )

    txn_path = os.path.join(run_dir, "transaction-results.jsonl")
    times = []
    with open(txn_path, "r", encoding="utf-8") as f:
        for line in f:
            times.append(json.loads(line)["event_time"])
    start_dt = min(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times) - timedelta(seconds=15)
    end_dt = max(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times) + timedelta(seconds=60)

    # Every remaining step runs regardless of whether an earlier one
    # failed or was partial (check=False + explicit warning) -- a
    # previous run lost its ENTIRE canonicalize/re2-projection/incident-
    # record output because export-telemetry.py's non-zero exit (itself
    # caused by one unhandled Loki timeout) was treated as fatal
    # (check=True) here, and by the time that was noticed backend
    # retention had already expired the raw data that export step didn't
    # get to. Downstream scripts must work from whatever raw/canonical
    # data actually exists, however incomplete, and say so -- not be
    # skipped because an earlier step wasn't 100%.
    export_rc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "export-telemetry.py"),
         "--run-id", args.run_id, "--start", start_dt.isoformat(), "--end", end_dt.isoformat()],
        check=False,
    ).returncode
    if export_rc != 0:
        print(f"[fault-experiment] WARNING: export-telemetry.py exited {export_rc} (partial raw export -- see raw/export-manifest.json 'complete' field). Continuing.", file=sys.stderr)

    subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "canonicalize.py"), "--run-id", args.run_id],
        check=False,
    )
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "re2-projection.py"), "--run-id", args.run_id],
        check=False,
    )
    subprocess.run(
        [
            sys.executable, os.path.join(ROOT, "scripts", "generate-incident-record.py"),
            "--run-id", args.run_id,
            "--incident-id", args.incident_id,
            "--experiment-id", args.experiment_id,
            "--control-run-id", args.control_run_id,
        ],
        check=False,
    )

    print(f"[fault-experiment] done (export_rc={export_rc}). See runs/{args.run_id}/ and incidents/{args.incident_id}.yaml")


if __name__ == "__main__":
    main()
