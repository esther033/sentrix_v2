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
import math

import pandas as pd
import yaml
from telemetry_export import trace_hex


def histogram_quantile(buckets, q):
    """Classic cumulative bucket quantile; linear interpolation, milliseconds.

    Reject malformed histograms instead of repairing counters after aggregation.
    An empty observation window yields NaN, never a fabricated zero latency.
    """
    points = sorted(buckets)
    if len(points) < 2 or points[-1][0] != math.inf:
        raise ValueError("Histogram requires finite buckets and +Inf")
    if len({b for b, _ in points}) != len(points):
        raise ValueError("Duplicate histogram boundaries")
    previous = 0.0
    for bound, count in points:
        if math.isnan(bound) or bound < 0 or not math.isfinite(count) or count < previous:
            raise ValueError("Invalid/nonmonotonic cumulative histogram")
        previous = count
    if points[-1][1] == 0:
        return float("nan")
    rank = q * points[-1][1]
    low, before = 0.0, 0.0
    for bound, count in points:
        if count >= rank:
            if bound == math.inf:
                return low
            return low + (bound - low) * (rank - before) / (count - before)
        low, before = bound, count
    raise ValueError("Unreachable quantile")


def project_metrics(canonical_dir, out_path, resample_seconds=5):
    path = os.path.join(canonical_dir, "metrics.parquet")
    if not os.path.exists(path):
        return {"rows": 0}
    m = pd.read_parquet(path)
    m = m[m["canonical_service_id"].notna()].copy()
    if len(m) == 0:
        return {"rows": 0}

    m["dt"] = pd.to_datetime(m["timestamp_unix"], unit="s", utc=True)
    m["labels"] = m["labels_json"].apply(json.loads)

    def containers(df, group):
        sub = df[df["metric_group"] == group].copy()
        # Exclude pause containers and pod-level cgroup totals: summing them
        # with real containers would count resource usage twice.
        return sub.loc[sub["labels"].apply(lambda x: bool(x.get("container")) and x["container"] != "POD").astype(bool)].copy()

    # metric_group -> RE2-OB column suffix, and unit conversion applied
    # before pivoting. See docs/RE2_SCHEMA.md for the source of each.
    def cpu_rows(df):
        sub = containers(df, "container_cpu_usage_rate")
        sub = sub.loc[sub["labels"].apply(lambda x: x.get("cpu", "total") == "total").astype(bool)].copy()
        sub["value"] = sub["value"] * 100  # core-fraction -> "core-fraction x 100"
        sub["suffix"] = "cpu"
        return sub

    def mem_rows(df):
        sub = containers(df, "container_memory_working_set_bytes")
        sub["suffix"] = "mem"  # already bytes, matches RE2-OB
        return sub

    def workload_rows(df):
        sub = df[df["metric_group"] == "spanmetrics_calls_rate"].copy()
        sub["suffix"] = "workload"
        return sub

    error = m[m["metric_group"] == "spanmetrics_error_increase_30s"].copy()
    error["suffix"] = "error"
    latency = []
    hist = m[m["metric_group"] == "spanmetrics_duration_bucket_rate"]
    for (dt, service), group in hist.groupby(["dt", "canonical_service_id"]):
        buckets = [(float(r.labels["le"]), float(r.value)) for r in group.itertuples()]
        for q, suffix in ((.5, "latency-50"), (.9, "latency-90")):
            latency.append({"dt": dt, "canonical_service_id": service,
                            "value": histogram_quantile(buckets, q) / 1000, "suffix": suffix})
    parts = [cpu_rows(m), mem_rows(m), workload_rows(m), error, pd.DataFrame(latency)]
    if not any(len(p) for p in parts):
        return {"rows": 0}
    long_df = pd.concat([p for p in parts if len(p)], ignore_index=True)

    long_df["column"] = long_df["canonical_service_id"] + "_" + long_df["suffix"]

    # Native samples are on our 5s Prometheus scrape grid (see the commit
    # tightening server.global.scrape_interval), not RE2-OB's native 1Hz.
    # Resample to 1s via forward-fill -- an explicit, documented
    # information-loss step (docs/RE2_SCHEMA.md), not a claim of true
    # 1Hz-native fidelity.
    pivot = long_df.groupby(["dt", "column"])["value"].sum(min_count=1).unstack("column")
    # Use only past samples and never fill across a missing native interval.
    grid = pd.date_range(pivot.index.min().ceil("s"), pivot.index.max().floor("s"), freq="1s")
    positions = pivot.index.get_indexer(grid, method="pad")
    ages = (grid - pivot.index[positions]).total_seconds()
    pivot = pivot.reindex(grid, method="ffill")
    pivot.loc[ages >= resample_seconds, :] = float("nan")
    pivot["time"] = (pivot.index.astype("int64") // 10**9).astype(int)
    pivot = pivot.reset_index(drop=True)
    cols = ["time"] + [c for c in pivot.columns if c != "time"]
    pivot = pivot[cols]

    pivot.to_parquet(out_path, index=False)
    missing = [g for g in ("spanmetrics_duration_bucket_rate", "spanmetrics_error_increase_30s")
               if g not in set(m.metric_group)]
    return {"rows": len(pivot), "columns": len(pivot.columns),
            "missing_feature_sources": missing,
            "undefined_values": int(pivot.drop(columns="time").isna().sum().sum()),
            "error_window_seconds": 30, "latency_window_seconds": 30}


def project_traces(canonical_dir, out_path):
    path = os.path.join(canonical_dir, "traces.parquet")
    if not os.path.exists(path):
        return {"rows": 0}
    t = pd.read_parquet(path)
    t = t[t["canonical_service_id"].notna()].copy()
    invalid_timestamp_spans = int((~t.get("timestamp_valid", pd.Series(True, index=t.index)).fillna(False)).sum())
    # RE2 duration cannot represent a negative interval.  Keep every source
    # span in canonical/raw and record the exact projection loss here.
    t = t[t.get("timestamp_valid", pd.Series(True, index=t.index)).fillna(False)].copy()
    if len(t) == 0:
        return {"rows": 0, "excluded_invalid_timestamp_spans": invalid_timestamp_spans}

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
            "traceID": t["trace_id"].apply(lambda v: trace_hex(v, 16)),
            "spanID": t["span_id"].apply(lambda v: trace_hex(v, 8)),
            # NOT replicating RE2-OB's "frontend" -> "frontendservice"
            # naming quirk (docs/RE2_SCHEMA.md) -- uses SentriX's own
            # canonical name consistently across all three data views.
            "serviceName": t["canonical_service_id"],
            "methodName": t["span_attributes_json"].apply(extract_method),
            "operationName": t["span_name"],
            "parentSpanID": t["parent_span_id"].apply(lambda v: trace_hex(v, 8) if pd.notna(v) and v else None),
            "startTimeMillis": (t["start_time_unix_nano"] // 1_000_000).astype("Int64"),
            "startTime": (t["start_time_unix_nano"] // 1_000).astype("Int64"),
            "duration": (
                (t["end_time_unix_nano"] - t["start_time_unix_nano"]) // 1_000
            ).astype("Int64"),
            "statusCode": pd.array(t["span_attributes_json"].apply(lambda v: json.loads(v).get("rpc.grpc.status_code")), dtype="Int64"),
        }
    )
    out.to_parquet(out_path, index=False)
    return {"rows": len(out), "excluded_invalid_timestamp_spans": invalid_timestamp_spans}


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
    ap.add_argument("--canonical-subdir", default="canonical")
    ap.add_argument("--output-subdir", default="re2-compatible")
    ap.add_argument(
        "--runs-dir", default=os.path.join(os.path.dirname(__file__), "..", "runs")
    )
    args = ap.parse_args()

    run_dir = os.path.join(args.runs_dir, args.run_id)
    for name in (args.run_id, args.canonical_subdir, args.output_subdir):
        if not name or name in (".", "..") or any(c in name for c in "/\\:"):
            raise ValueError("Expected a single directory name")
    canonical_dir = os.path.join(run_dir, args.canonical_subdir)
    out_dir = os.path.join(run_dir, args.output_subdir)
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise ValueError("Output already contains data; choose a new --output-subdir")
    os.makedirs(out_dir, exist_ok=True)

    summary = {
        "metrics": project_metrics(canonical_dir, os.path.join(out_dir, "metrics.parquet")),
        "traces": project_traces(canonical_dir, os.path.join(out_dir, "traces.parquet")),
        "logs": project_logs(canonical_dir, os.path.join(out_dir, "logs.parquet")),
    }
    with open(os.path.join(run_dir, "run-manifest.yaml"), encoding="utf-8") as source:
        release = yaml.safe_load(source)["release"]
    expected = {f"{svc}_{suffix}" for svc in release["traced_services"] for suffix in ("workload", "error", "latency-50", "latency-90")}
    expected.update(f"{svc}_{suffix}" for svc in release["traced_services"] + release["untraced_services"] for suffix in ("cpu", "mem"))
    metric_path = os.path.join(out_dir, "metrics.parquet")
    columns = set(pd.read_parquet(metric_path).columns) if os.path.exists(metric_path) else set()
    summary["metrics"]["missing_service_columns"] = sorted(expected - columns)
    with open(os.path.join(out_dir, "projection-summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
