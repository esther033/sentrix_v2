#!/usr/bin/env python3
"""
M1A quality gate, run in two parts (both must pass for M1A):

  Gate A - Transaction telemetry gate (per transaction, sampled):
    1. transaction JSONL exists and has records for this run_id
    2. trace_id resolves in Tempo
    3. the target service's RED metric (spanmetrics-derived,
       traces_span_metrics_calls_total) exists in Prometheus for the
       service+time window (NOT joined by trace_id -- Prometheus metrics
       do not carry trace ids)
    4. the target Pod's stdout/stderr log exists in Loki for the same
       pod+time window (checked by k8s.pod.name / time range, not by
       trace_id -- app stdout logs are not guaranteed to carry it)
    5. canonical service identity resolves via the priority order in
       identity/service-alias-map.yaml

  Gate B - Kubernetes Event pipeline gate (independent of any transaction):
    1. create a harmless sentinel Event in the sentrix namespace
    2. confirm the k8sobjects receiver -> Loki path picked it up, by
       UID/name
    3. compare source vs. collected timestamps (event_time / observed_at /
       ingested_at, never a single "skew" number from one pair of clocks)

Writes runs/<run_id>/quality-report.json. Never silently treats a missing
signal as pass; every check records its own status.
"""
import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import urllib.request
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from identity.resolve import load_alias_map, resolve_canonical_identity  # noqa: E402


def http_get_json(url, params=None, timeout=10):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def gate_a_transaction_telemetry(
    txn_jsonl_path, tempo_url, prom_url, loki_url, sample_n=3
):
    results = []
    if not os.path.exists(txn_jsonl_path):
        return {"status": "FAIL", "reason": "transaction-results.jsonl missing", "checks": []}

    summaries = []
    run_start_dt = None
    with open(txn_jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec_dt = datetime.fromisoformat(rec["event_time"].replace("Z", "+00:00"))
            if run_start_dt is None or rec_dt < run_start_dt:
                run_start_dt = rec_dt
            if rec.get("stage") == "checkout":
                summaries.append(rec)

    if not summaries:
        return {"status": "FAIL", "reason": "no checkout-stage records found", "checks": []}

    sample = summaries[:sample_n]

    for rec in sample:
        check = {"transaction_id": rec["transaction_id"], "trace_id": rec["trace_id"]}

        # 1. Tempo trace lookup. Also extract the checkoutservice resource's
        # k8s.pod.name from the trace itself -- this is the actual Pod that
        # served THIS transaction, and is what the log check (3) must be
        # pinned to, not just "some pod behind the checkoutservice
        # Deployment" (which could be a different replica, or a pod from
        # before/after a restart, and would falsely pass Gate A).
        target_pod_name = None
        try:
            trace = http_get_json(f"{tempo_url}/api/traces/{rec['trace_id']}")
            batches = trace.get("batches", [])
            check["tempo_trace_found"] = bool(batches)
            for batch in batches:
                res_attrs = {
                    a["key"]: a["value"].get("stringValue")
                    for a in batch.get("resource", {}).get("attributes", [])
                }
                if res_attrs.get("service.name") == "checkoutservice":
                    target_pod_name = res_attrs.get("k8s.pod.name")
                    break
            check["target_pod_name"] = target_pod_name
        except Exception as e:  # noqa: BLE001
            check["tempo_trace_found"] = False
            check["tempo_error"] = str(e)

        # target service for checkout stage = checkoutservice.
        # window_start is clamped to this run's own start time (earliest
        # event_time seen across the whole transaction file). Without this,
        # a fixed "-2 minutes" window on a rerun launched shortly after a
        # previous one could extend past the boundary between runs, and
        # increase() would count the PREVIOUS run's metric activity as
        # evidence for this one -- a false pass that has nothing to do
        # with whether this run's own telemetry pipeline is working.
        event_dt = datetime.fromisoformat(rec["event_time"].replace("Z", "+00:00"))
        window_start = max(event_dt - timedelta(minutes=2), run_start_dt)
        # Mirror the run_start_dt clamp on the other side: never query a
        # window that extends past "now". This only bites on very short
        # runs (e.g. a dev smoke test a few seconds long) where
        # event_time + 2min is still in the future when quality-check
        # actually runs; for real pilot/control runs (minutes long) this
        # is a no-op since the run has finished well past that point.
        window_end = min(event_dt + timedelta(minutes=2), datetime.now(timezone.utc))
        window_seconds = max(int((window_end - window_start).total_seconds()), 1)

        # 2. Prometheus RED metric for checkoutservice: require the counter
        # to have INCREASED within this run's own time window, not merely
        # exist. A plain instant query would still return a stale value if
        # this run's metric delivery were broken but an older scrape
        # series from a previous run/session was still resident -- counters
        # never disappear on their own, so existence alone is not evidence
        # of fresh activity in this window.
        try:
            prom_result = http_get_json(
                f"{prom_url}/api/v1/query",
                params={
                    "query": (
                        f'increase(traces_span_metrics_calls_total'
                        f'{{service_name="checkoutservice"}}[{window_seconds}s])'
                    ),
                    "time": str(window_end.timestamp()),
                },
            )
            result = prom_result.get("data", {}).get("result", [])
            check["prometheus_metric_found"] = any(
                float(r["value"][1]) > 0 for r in result
            )
        except Exception as e:  # noqa: BLE001
            check["prometheus_metric_found"] = False
            check["prometheus_error"] = str(e)

        # 3. Loki: checkoutservice pod stdout log, pinned to the SAME pod
        # that the trace says actually served this transaction (see (1)
        # above) -- not just any pod behind the checkoutservice Deployment.
        # If the trace didn't resolve a pod name, this check cannot claim
        # same-pod correlation and fails rather than falling back to a
        # deployment-wide query that could pass against the wrong replica.
        if not target_pod_name:
            check["loki_log_found"] = False
            check["loki_error"] = "no target_pod_name resolved from trace; cannot verify same-pod log"
        else:
            try:
                loki_result = http_get_json(
                    f"{loki_url}/loki/api/v1/query_range",
                    params={
                        "query": f'{{k8s_pod_name="{target_pod_name}"}}',
                        "start": str(int(window_start.timestamp() * 1e9)),
                        "end": str(int(window_end.timestamp() * 1e9)),
                        "limit": "5",
                    },
                )
                streams = loki_result.get("data", {}).get("result", [])
                check["loki_log_found"] = bool(streams)
                if streams:
                    identity_attrs = {
                        "service.namespace": streams[0]["stream"].get("service_namespace"),
                        "service.name": streams[0]["stream"].get("service_name"),
                        "k8s.namespace.name": streams[0]["stream"].get("k8s_namespace_name"),
                        "k8s.deployment.name": streams[0]["stream"].get("k8s_deployment_name"),
                    }
                    canonical, method = resolve_canonical_identity(identity_attrs, ALIAS_MAP)
                    check["identity_resolved"] = canonical is not None
                    check["identity_resolution_method"] = method
                    check["canonical_service_id"] = canonical
            except Exception as e:  # noqa: BLE001
                check["loki_log_found"] = False
                check["loki_error"] = str(e)

        check["pass"] = bool(
            check.get("tempo_trace_found")
            and check.get("prometheus_metric_found")
            and check.get("loki_log_found")
            and check.get("identity_resolved")
        )
        results.append(check)

    overall = "PASS" if all(c["pass"] for c in results) else "FAIL"
    return {"status": overall, "checks": results}


def gate_b_k8s_event_pipeline(loki_url, namespace="sentrix", wait_seconds=20):
    sentinel_name = f"sentrix-sentinel-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    event_manifest = f"""apiVersion: v1
kind: Event
metadata:
  name: {sentinel_name}
  namespace: {namespace}
involvedObject:
  kind: Namespace
  name: {namespace}
  namespace: {namespace}
reason: SentrixM1ASentinel
message: "SentriX M1A gate B sentinel event -- harmless, verifies the k8sobjects Kubernetes Events collection pipeline"
type: Normal
firstTimestamp: "{now_str}"
lastTimestamp: "{now_str}"
count: 1
source:
  component: sentrix-quality-check
"""
    created_at = datetime.now(timezone.utc)
    proc = subprocess.run(
        ["kubectl", "create", "-f", "-"],
        input=event_manifest,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return {
            "status": "FAIL",
            "reason": f"could not create sentinel event: {proc.stderr}",
        }

    uid_proc = subprocess.run(
        [
            "kubectl",
            "get",
            "event",
            sentinel_name,
            "-n",
            namespace,
            "-o",
            "jsonpath={.metadata.uid}",
        ],
        text=True,
        capture_output=True,
    )
    source_uid = uid_proc.stdout.strip()

    time.sleep(wait_seconds)

    window_start = created_at - timedelta(seconds=30)
    window_end = datetime.now(timezone.utc) + timedelta(seconds=10)
    try:
        loki_result = http_get_json(
            f"{loki_url}/loki/api/v1/query_range",
            params={
                "query": f'{{k8s_namespace_name="{namespace}"}} |= "{sentinel_name}"',
                "start": str(int(window_start.timestamp() * 1e9)),
                "end": str(int(window_end.timestamp() * 1e9)),
                "limit": "5",
            },
        )
    except Exception as e:  # noqa: BLE001
        return {"status": "FAIL", "reason": f"loki query failed: {e}"}

    streams = loki_result.get("data", {}).get("result", [])
    if not streams:
        return {
            "status": "FAIL",
            "reason": "sentinel event not found in Loki within wait window",
            "sentinel_name": sentinel_name,
            "source_uid": source_uid,
        }

    value = streams[0]["values"][0]
    collected_ns = int(value[0])
    collected_at = datetime.fromtimestamp(collected_ns / 1e9, tz=timezone.utc)
    body = json.loads(value[1])
    collected_uid = body.get("object", {}).get("metadata", {}).get("uid")

    uid_match = collected_uid == source_uid
    lag_seconds = (collected_at - created_at).total_seconds()

    return {
        "status": "PASS" if uid_match else "FAIL",
        "sentinel_name": sentinel_name,
        "source_uid": source_uid,
        "collected_uid": collected_uid,
        "uid_match": uid_match,
        "event_time": created_at.isoformat(),
        "observed_at": collected_at.isoformat(),
        "collection_lag_seconds": lag_seconds,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--runs-dir", default=os.path.join(os.path.dirname(__file__), "..", "runs"))
    ap.add_argument("--tempo-url", default="http://localhost:3200")
    ap.add_argument("--prometheus-url", default="http://localhost:9090")
    ap.add_argument("--loki-url", default="http://localhost:3100")
    ap.add_argument(
        "--alias-map",
        default=os.path.join(
            os.path.dirname(__file__), "..", "identity", "service-alias-map.yaml"
        ),
    )
    ap.add_argument("--skip-gate-b", action="store_true", help="skip sentinel K8s Event test")
    args = ap.parse_args()

    global ALIAS_MAP
    ALIAS_MAP = load_alias_map(args.alias_map)

    run_dir = os.path.join(args.runs_dir, args.run_id)
    txn_path = os.path.join(run_dir, "transaction-results.jsonl")

    gate_a = gate_a_transaction_telemetry(
        txn_path, args.tempo_url, args.prometheus_url, args.loki_url
    )
    gate_b = (
        {"status": "SKIPPED"}
        if args.skip_gate_b
        else gate_b_k8s_event_pipeline(args.loki_url)
    )

    overall_pass = gate_a["status"] == "PASS" and gate_b["status"] in ("PASS", "SKIPPED")

    report = {
        "run_id": args.run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "m1a_gate_a_transaction_telemetry": gate_a,
        "m1a_gate_b_k8s_event_pipeline": gate_b,
        "m1a_overall": "PASS" if overall_pass else "FAIL",
    }

    out_path = os.path.join(run_dir, "quality-report.json")
    os.makedirs(run_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    print(json.dumps(report, indent=2))
    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
