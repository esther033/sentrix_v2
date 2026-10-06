#!/usr/bin/env python3
"""
Projects runs/<run_id>/canonical/ into runs/<run_id>/re2-compatible/
{metrics,traces,logs}.parquet, matching the schema validated in
docs/RE2_SCHEMA.md against a real downloaded RE2-OB sample case.

This is a LOSSY projection by design (per sentrix-project-context.md
section 5: "Canonical view는 풍부한 OTel attribute를 보존한다" -- the
canonical view is the rich one; this one intentionally is not). What is
dropped and why is documented in docs/RE2_SCHEMA.md, not just in code
comments, since "what got lost" is itself a result that needs to survive
independent review.
"""
import argparse
import json
import os

import pandas as pd


def project_metrics(canonical_dir, out_path, resample_seconds=5):
    path = os.path.join(canonical_dir, "metrics.parquet")
    if not os.path.exists(path):
        return {"rows": 0}
    m = pd.read_parquet(path)
    m = m[m["canonical_service_id"].notna()]
    if len(m) == 0:
        return {"rows": 0}

    m["dt"] = pd.to_datetime(m["timestamp_unix"], unit="s", utc=True)

    # metric_group -> RE2-OB column suffix, and unit conversion applied
    # before pivoting. See docs/RE2_SCHEMA.md for the source of each.
    def cpu_rows(df):
        sub = df[df["metric_group"] == "container_cpu_usage_rate"].copy()
        sub["value"] = sub["value"] * 100  # core-fraction -> "core-fraction x 100"
        sub["suffix"] = "cpu"
        return sub

    def mem_rows(df):
        sub = df[df["metric_group"] == "container_memory_working_set_bytes"].copy()
        sub["suffix"] = "mem"  # already bytes, matches RE2-OB
        return sub

    def workload_rows(df):
        sub = df[df["metric_group"] == "spanmetrics_calls_rate"].copy()
        sub["suffix"] = "workload"
        return sub

    parts = [cpu_rows(m), mem_rows(m), workload_rows(m)]
    long_df = pd.concat([p for p in parts if len(p)], ignore_index=True)
    if len(long_df) == 0:
        return {"rows": 0}

    long_df["column"] = long_df["canonical_service_id"] + "_" + long_df["suffix"]

    # Native samples are on our 5s Prometheus scrape grid (see the commit
    # tightening server.global.scrape_interval), not RE2-OB's native 1Hz.
    # Resample to 1s via forward-fill -- an explicit, documented
    # information-loss step (docs/RE2_SCHEMA.md), not a claim of true
    # 1Hz-native fidelity.
    pivot = long_df.pivot_table(
        index="dt", columns="column", values="value", aggfunc="mean"
    )
    pivot = pivot.resample("1s").ffill()
    pivot["time"] = (pivot.index.astype("int64") // 10**9).astype(int)
    pivot = pivot.reset_index(drop=True)
    cols = ["time"] + [c for c in pivot.columns if c != "time"]
    pivot = pivot[cols]

    pivot.to_parquet(out_path, index=False)
    return {"rows": len(pivot), "columns": len(pivot.columns)}


def project_traces(canonical_dir, out_path):
    path = os.path.join(canonical_dir, "traces.parquet")
    if not os.path.exists(path):
        return {"rows": 0}
    t = pd.read_parquet(path)
    t = t[t["canonical_service_id"].notna()].copy()
    if len(t) == 0:
        return {"rows": 0}

    def extract_method(attrs_json):
        try:
            attrs = json.loads(attrs_json)
        except Exception:  # noqa: BLE001
            return None
        return attrs.get("rpc.method") or attrs.get("http.method")

    out = pd.DataFrame(
        {
            "time": pd.to_datetime(t["start_time_unix_nano"], unit="ns", utc=True).dt.strftime(
                "%H:%M"
            ),
            "traceID": t["trace_id"],
            "spanID": t["span_id"],
            # NOT replicating RE2-OB's "frontend" -> "frontendservice"
            # naming quirk (docs/RE2_SCHEMA.md) -- uses SentriX's own
            # canonical name consistently across all three data views.
            "serviceName": t["canonical_service_id"],
            "methodName": t["span_attributes_json"].apply(extract_method),
            "operationName": t["span_name"],
            "parentSpanID": t["parent_span_id"],
            "startTimeMillis": (t["start_time_unix_nano"] // 1_000_000).astype("Int64"),
            "startTime": (t["start_time_unix_nano"] // 1_000).astype("Int64"),
            "duration": (
                (t["end_time_unix_nano"] - t["start_time_unix_nano"]) // 1_000
            ).astype("Int64"),
            "statusCode": t["status_code"],
        }
    )
    out.to_parquet(out_path, index=False)
    return {"rows": len(out)}


def project_logs(canonical_dir, out_path):
    path = os.path.join(canonical_dir, "logs.parquet")
    if not os.path.exists(path):
        return {"rows": 0}
    lg = pd.read_parquet(path)
    lg = lg[lg["canonical_service_id"].notna()].copy()
    if len(lg) == 0:
        return {"rows": 0}
    out = pd.DataFrame(
        {
            "timestamp": (lg["timestamp_unix_nano"] // 1_000_000_000).astype("Int64"),
            "container_name": lg["canonical_service_id"],
            "message": lg["message"],
        }
    )
    out.to_parquet(out_path, index=False)
    return {"rows": len(out)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--runs-dir", default=os.path.join(os.path.dirname(__file__), "..", "runs")
    )
    args = ap.parse_args()

    run_dir = os.path.join(args.runs_dir, args.run_id)
    canonical_dir = os.path.join(run_dir, "canonical")
    out_dir = os.path.join(run_dir, "re2-compatible")
    os.makedirs(out_dir, exist_ok=True)

    summary = {
        "metrics": project_metrics(canonical_dir, os.path.join(out_dir, "metrics.parquet")),
        "traces": project_traces(canonical_dir, os.path.join(out_dir, "traces.parquet")),
        "logs": project_logs(canonical_dir, os.path.join(out_dir, "logs.parquet")),
    }
    with open(os.path.join(out_dir, "projection-summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
