#!/usr/bin/env python3
"""
Transforms runs/<run_id>/raw/ (untouched backend API responses) into
runs/<run_id>/canonical/{traces,metrics,logs,events}.parquet -- the long-
format view sentrix-project-context.md section 5 describes: identity
resolved via identity/resolve.py, raw identity AND raw value preserved
alongside the resolved canonical_service_id, nothing silently merged.

A record whose identity can't be resolved is kept (not dropped) with
canonical_service_id=None and identity_resolution_method="unresolved",
so a consumer can see exactly what fraction of the canonical view has
unresolved identity, per the project's own rule against silently
merging unmapped identity.
"""
import argparse
import json
import os
import re
import sys

import pandas as pd
from telemetry_export import trace_hex

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from identity.resolve import load_alias_map, resolve_canonical_identity  # noqa: E402


def otel_value(value):
    """Decode every OTLP AnyValue variant without dropping typed attributes."""
    for key in ("stringValue", "boolValue", "doubleValue", "bytesValue"):
        if key in value:
            return value[key]
    if "intValue" in value:
        return int(value["intValue"])
    if "arrayValue" in value:
        return [otel_value(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {a["key"]: otel_value(a["value"]) for a in value["kvlistValue"].get("values", [])}
    return None


def canonicalize_traces(raw_dir, alias_map):
    traces_dir = os.path.join(raw_dir, "traces")
    rows = []
    if not os.path.isdir(traces_dir):
        return pd.DataFrame()
    for fname in os.listdir(traces_dir):
        if fname == "_search_index.json" or not fname.endswith(".json"):
            continue
        trace_id = fname[: -len(".json")]
        if not re.fullmatch(r"[0-9a-fA-F]{1,32}", trace_id):
            raise ValueError("Invalid trace file name")
        trace_id = trace_id.lower().zfill(32)
        with open(os.path.join(traces_dir, fname), "r", encoding="utf-8") as f:
            trace = json.load(f)
        for batch in trace.get("batches", []):
            res_attrs = {
                a["key"]: otel_value(a["value"])
                for a in batch.get("resource", {}).get("attributes", [])
            }
            raw_service_id = res_attrs.get("service.name")
            canonical_service_id, method = resolve_canonical_identity(res_attrs, alias_map)
            for scope_span in batch.get("scopeSpans", []):
                for span in scope_span.get("spans", []):
                    if trace_hex(span["traceId"], 16) != trace_hex(trace_id, 16):
                        raise ValueError("Trace ID disagrees with raw file name")
                    start_time = int(span.get("startTimeUnixNano", 0))
                    end_time = int(span.get("endTimeUnixNano", 0))
                    span_attrs = {
                        a["key"]: otel_value(a["value"])
                        for a in span.get("attributes", [])
                    }
                    rows.append(
                        {
                            "telemetry_source": "traces",
                            "trace_id": trace_id,
                            "span_id": trace_hex(span["spanId"], 8),
                            "parent_span_id": trace_hex(span["parentSpanId"], 8) if span.get("parentSpanId") else None,
                            "raw_span_json": json.dumps(span),
                            "raw_resource_json": json.dumps(batch.get("resource", {})),
                            "raw_service_id": raw_service_id,
                            "canonical_service_id": canonical_service_id,
                            "identity_resolution_method": method,
                            "mapping_version": alias_map.get("mapping_version"),
                            "span_name": span.get("name"),
                            "span_kind": span.get("kind"),
                            "start_time_unix_nano": start_time,
                            "end_time_unix_nano": end_time,
                            # Preserve malformed source values verbatim while
                            # making their exclusion from duration-derived
                            # views auditable.
                            "timestamp_valid": start_time > 0 and end_time >= start_time,
                            "status_code": span.get("status", {}).get("code"),
                            "resource_attributes_json": json.dumps(res_attrs),
                            "span_attributes_json": json.dumps(span_attrs),
                        }
                    )
    result = pd.DataFrame(rows).drop_duplicates()
    if len(result) and result.duplicated(["trace_id", "span_id"]).any():
        raise ValueError("Conflicting versions of the same span; review raw snapshots")
    return result


def canonicalize_metrics(raw_dir, alias_map):
    metrics_dir = os.path.join(raw_dir, "metrics")
    rows = []
    if not os.path.isdir(metrics_dir):
        return pd.DataFrame()
    for fname in os.listdir(metrics_dir):
        if not fname.endswith(".json"):
            continue
        metric_group = fname[: -len(".json")]
        with open(os.path.join(metrics_dir, fname), "r", encoding="utf-8") as f:
            data = json.load(f)
        for series in data.get("data", {}).get("result", []):
            labels = series.get("metric", {})
            attrs = {
                "service.namespace": labels.get("service_namespace"),
                "service.name": labels.get("service_name"),
                "k8s.namespace.name": labels.get("namespace") or labels.get("k8s_namespace_name"),
                "k8s.deployment.name": labels.get("k8s_deployment_name"),
                "k8s.pod.uid": labels.get("k8s_pod_uid"),
                "namespace": labels.get("namespace"),
                "pod": labels.get("pod"),
                "job": labels.get("job"),
            }
            raw_service_id = (
                labels.get("service_name")
                or labels.get("k8s_deployment_name")
                or labels.get("pod")
            )
            canonical_service_id, method = resolve_canonical_identity(attrs, alias_map)
            metric_name = labels.get("__name__", metric_group)
            for point in series.get("values", []):
                ts, value = point
                rows.append(
                    {
                        "telemetry_source": "metrics",
                        "timestamp_unix": float(ts),
                        "raw_service_id": raw_service_id,
                        "canonical_service_id": canonical_service_id,
                        "identity_resolution_method": method,
                        "mapping_version": alias_map.get("mapping_version"),
                        "metric_group": metric_group,
                        "metric_name": metric_name,
                        "value": float(value),
                        "labels_json": json.dumps(labels),
                    }
                )
    return pd.DataFrame(rows)


def canonicalize_logs(raw_dir, alias_map, path="logs.ndjson", source="logs"):
    raw_path = os.path.join(raw_dir, path)
    rows = []
    if not os.path.exists(raw_path):
        return pd.DataFrame()
    with open(raw_path, "r", encoding="utf-8") as f:
        for line in f:
            page = json.loads(line)
            for stream in page.get("data", {}).get("result", []):
                labels = stream.get("stream", {})
                attrs = {
                    "service.namespace": labels.get("service_namespace"),
                    "service.name": labels.get("service_name"),
                    "k8s.namespace.name": labels.get("k8s_namespace_name"),
                    "k8s.deployment.name": labels.get("k8s_deployment_name"),
                    "k8s.pod.uid": labels.get("k8s_pod_uid"),
                }
                raw_service_id = labels.get("service_name") or labels.get("k8s_deployment_name")
                canonical_service_id, method = resolve_canonical_identity(attrs, alias_map)
                for ts_ns, line_text in stream.get("values", []):
                    rows.append(
                        {
                            "telemetry_source": source,
                            "timestamp_unix_nano": int(ts_ns),
                            "raw_service_id": raw_service_id,
                            "canonical_service_id": canonical_service_id,
                            "identity_resolution_method": method,
                            "mapping_version": alias_map.get("mapping_version"),
                            "message": line_text,
                            "labels_json": json.dumps(labels),
                        }
                    )
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--raw-subdir", default="raw")
    ap.add_argument("--output-subdir", default="canonical")
    ap.add_argument(
        "--runs-dir", default=os.path.join(os.path.dirname(__file__), "..", "runs")
    )
    ap.add_argument(
        "--alias-map",
        default=os.path.join(
            os.path.dirname(__file__), "..", "identity", "service-alias-map.yaml"
        ),
    )
    args = ap.parse_args()

    alias_map = load_alias_map(args.alias_map)
    run_dir = os.path.join(args.runs_dir, args.run_id)
    for name in (args.run_id, args.raw_subdir, args.output_subdir):
        if not name or name in (".", "..") or any(c in name for c in "/\\:"):
            raise ValueError("Expected a single directory name")
    raw_dir = os.path.join(run_dir, args.raw_subdir)
    canonical_dir = os.path.join(run_dir, args.output_subdir)
    if os.path.isdir(canonical_dir) and os.listdir(canonical_dir):
        raise ValueError("Output already contains data; choose a new --output-subdir")
    os.makedirs(canonical_dir, exist_ok=True)

    summary = {}
    for name, fn, kwargs in [
        ("traces", canonicalize_traces, {}),
        ("metrics", canonicalize_metrics, {}),
        ("logs", canonicalize_logs, {"path": "logs.ndjson", "source": "logs"}),
        ("events", canonicalize_logs, {"path": "events.ndjson", "source": "events"}),
    ]:
        df = fn(raw_dir, alias_map, **kwargs)
        out_path = os.path.join(canonical_dir, f"{name}.parquet")
        if len(df):
            df.to_parquet(out_path, index=False)
        unresolved = int((df["canonical_service_id"].isna()).sum()) if len(df) else 0
        summary[name] = {
            "rows": len(df),
            "unresolved_identity_rows": unresolved,
            "unresolved_identity_pct": round(unresolved / len(df) * 100, 2) if len(df) else None,
        }

    with open(os.path.join(canonical_dir, "canonicalize-summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
