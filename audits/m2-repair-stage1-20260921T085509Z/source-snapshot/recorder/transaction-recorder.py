#!/usr/bin/env python3
"""
Runs the k6 browse-cart-checkout script and produces the per-run artifacts
Milestone 1 needs. This script performs NO aggregation of its own beyond
what is explicitly named below -- transaction-results.jsonl is a straight
pass-through of k6's per-request TXN_RECORD lines, never a summary.

Outputs (all under runs/<run_id>/):
  transaction-results.jsonl   one line per TXN_RECORD emitted by k6
                               (per-stage + one TRANSACTION_SUMMARY per
                               iteration) -- see workload/k6/browse-cart-checkout.js
  workload-timeseries.json    1s rollup of actual request rate / iteration
                               count / dropped_iterations, built from k6's
                               --out json= raw point stream (NOT the k6
                               end-of-test summary)
  k6-summary.json             k6's own --summary-export output, kept
                               verbatim for reference only
  k6-raw-metrics.jsonl        raw k6 --out json= stream, kept verbatim

Usage:
  python transaction-recorder.py --run-id <run_id> --profile <profile.json>
      --base-url http://localhost:8080 [--k6-bin path/to/k6]
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

# Even with --console-output, k6 still wraps each console.log() call in its
# own logrus-style line: time="..." level=info msg="TXN_RECORD:{...}" source=console
# with the JSON payload Go-%q-escaped inside the quoted msg value. There is
# no k6 flag that emits raw unwrapped console output, so this regex peels
# the wrapper off instead.
CONSOLE_LINE_RE = re.compile(r'msg="(TXN_RECORD:.*)"\s*$')


def parse_console_line(line):
    m = CONSOLE_LINE_RE.search(line)
    if not m:
        return None
    raw = m.group(1)
    unescaped = raw.replace('\\"', '"').replace("\\\\", "\\")
    if not unescaped.startswith("TXN_RECORD:"):
        return None
    payload = unescaped[len("TXN_RECORD:") :]
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def load_profile(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_k6(k6_bin, script_path, profile, run_id, base_url, out_dir):
    env = os.environ.copy()
    env.update(
        {
            "RUN_ID": run_id,
            "PROFILE_NAME": profile["profile_name"],
            "BASE_URL": base_url,
            "TARGET_RPS": str(profile["target_rps"]),
            "DURATION": profile["duration"],
            "PRE_ALLOCATED_VUS": str(profile["pre_allocated_vus"]),
            "MAX_VUS": str(profile["max_vus"]),
            "STAGE_TIMEOUT": profile["stage_timeout"],
        }
    )

    raw_metrics_path = os.path.join(out_dir, "k6-raw-metrics.jsonl")
    summary_path = os.path.join(out_dir, "k6-summary.json")
    txn_path = os.path.join(out_dir, "transaction-results.jsonl")
    # k6's default stdout wraps console.log() lines in its own logrus-style
    # log format (`time="..." level=info msg="TXN_RECORD:{...}" ...`), which
    # is not directly JSON-parseable. --console-output writes console.log
    # calls to their own file, one call per line, with no wrapping.
    console_output_path = os.path.join(out_dir, "k6-console-output.log")

    cmd = [
        k6_bin,
        "run",
        f"--out=json={raw_metrics_path}",
        f"--summary-export={summary_path}",
        f"--console-output={console_output_path}",
        script_path,
    ]

    print(f"[recorder] running: {' '.join(cmd)}", file=sys.stderr)

    proc = subprocess.run(
        cmd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    print(proc.stdout, file=sys.stderr)

    txn_count = 0
    with open(txn_path, "w", encoding="utf-8") as txn_file:
        if os.path.exists(console_output_path):
            with open(console_output_path, "r", encoding="utf-8", errors="replace") as cf:
                for line in cf:
                    rec = parse_console_line(line.rstrip("\n"))
                    if rec is None:
                        continue
                    # add observed_at (recorder-side observation time)
                    # alongside k6's event_time (request start), per the
                    # event_time / observed_at / ingested_at separation the
                    # M1A gate needs for skew analysis. ingested_at is
                    # filled in later by whichever backend actually stores
                    # the record.
                    rec["observed_at"] = datetime.now(timezone.utc).isoformat()
                    txn_file.write(json.dumps(rec) + "\n")
                    txn_count += 1

    return proc.returncode, txn_count, raw_metrics_path, summary_path


def build_workload_timeseries(raw_metrics_path, out_dir):
    """1s rollup of actual request rate / iterations / dropped_iterations
    from k6's raw --out json= point stream. This is the "actual RPS"
    record -- summary-only numbers are not sufficient per the M1A design."""
    buckets = defaultdict(
        lambda: {"iterations": 0, "dropped_iterations": 0, "http_reqs": 0}
    )

    if not os.path.exists(raw_metrics_path):
        return []

    with open(raw_metrics_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                point = json.loads(line)
            except json.JSONDecodeError:
                continue
            if point.get("type") != "Point":
                continue
            metric = point.get("metric")
            data = point.get("data", {})
            ts = data.get("time")
            if not ts or metric not in ("iterations", "dropped_iterations", "http_reqs"):
                continue
            # k6's --out json= raw stream reports "time" in the host's LOCAL
            # offset (e.g. "...+09:00" on this machine), NOT UTC, unlike
            # event_time in the transaction records (which comes from k6's
            # own new Date().toISOString(), always UTC/"Z"). Slicing the
            # first 19 chars without converting first silently keeps the
            # local wall-clock digits but relabels them as if they were UTC
            # -- every downstream consumer of "second" (steady-state window
            # filtering in scripts/compute-baseline.py in particular) would
            # then be comparing against the wrong 9-hour-shifted bucket.
            # Convert to UTC before truncating so "second" is unambiguous.
            dt_utc = datetime.fromisoformat(ts).astimezone(timezone.utc)
            bucket_key = dt_utc.strftime("%Y-%m-%dT%H:%M:%S")
            buckets[bucket_key][metric] += 1

    rollup = [
        {
            "second": key,
            "iterations": v["iterations"],
            "dropped_iterations": v["dropped_iterations"],
            "http_reqs": v["http_reqs"],
        }
        for key, v in sorted(buckets.items())
    ]
    out_path = os.path.join(out_dir, "workload-timeseries.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(rollup, f, indent=2)
    return rollup


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--profile", required=True, help="path to profile JSON")
    ap.add_argument("--base-url", required=True)
    ap.add_argument(
        "--k6-bin", default=os.environ.get("K6_BIN", "k6"), help="path to k6 binary"
    )
    ap.add_argument(
        "--script",
        default=os.path.join(
            os.path.dirname(__file__), "..", "workload", "k6", "browse-cart-checkout.js"
        ),
    )
    ap.add_argument("--out-dir", default=None, help="defaults to runs/<run_id>")
    args = ap.parse_args()

    profile = load_profile(args.profile)

    out_dir = args.out_dir or os.path.join(
        os.path.dirname(__file__), "..", "runs", args.run_id
    )
    os.makedirs(out_dir, exist_ok=True)

    rc, txn_count, raw_metrics_path, summary_path = run_k6(
        args.k6_bin, args.script, profile, args.run_id, args.base_url, out_dir
    )

    rollup = build_workload_timeseries(raw_metrics_path, out_dir)

    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "profile": profile["profile_name"],
                "k6_exit_code": rc,
                "transaction_records_written": txn_count,
                "workload_timeseries_seconds": len(rollup),
                "out_dir": out_dir,
            },
            indent=2,
        )
    )

    sys.exit(0 if rc == 0 else rc)


if __name__ == "__main__":
    main()
