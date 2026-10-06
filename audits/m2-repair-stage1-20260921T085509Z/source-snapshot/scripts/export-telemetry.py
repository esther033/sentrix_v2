#!/usr/bin/env python3
"""
Raw telemetry export for one run_id. Must be run BEFORE the backend's own
retention window expires (Tempo: 1h: Prometheus: 6h, Loki: default chart
retention -- see docs/MILESTONE1.md / docs/RE2_SCHEMA.md). Pulls
everything the run's time window touched out of the live backends and
writes it to runs/<run_id>/raw/, unmodified (no identity resolution, no
resampling, no unit conversion -- that's canonicalize.py's job).

Writes:
  raw/traces/<trace_id>.json     one Tempo /api/traces/{id} response each
  raw/traces/_search_index.json  the Tempo /api/search result used to
                                  enumerate trace IDs (includes traces
                                  Tempo knows about that our own
                                  transaction records don't reference,
                                  e.g. health-check probes)
  raw/metrics/<expr_name>.json   one Prometheus /api/v1/query_range
                                  response per tracked PromQL expression
  raw/logs.ndjson                Loki query_range result for the whole
                                  sentrix namespace in the window
  raw/events.ndjson              Loki query_range result filtered to
                                  k8sobjects-sourced (event_domain="k8s")
                                  records
  raw/environment/*.yaml         kubectl get -o yaml snapshots of the
                                  Deployments/manifests/values that
                                  defined the environment this run
                                  observed
  raw/export-manifest.json       what was exported, when, and from which
                                  backend endpoints -- so a later reader
                                  can tell whether this export is
                                  complete or partial
"""
import argparse
import json
import os
import subprocess
import time
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# PromQL expressions tracked for raw export. Kept as a flat list (not
# auto-discovered) so the export is reproducible and auditable -- adding
# a new expression is a deliberate, reviewed change, not something that
# silently changes shape between runs.
PROM_EXPRESSIONS = {
    "container_cpu_usage_rate": 'rate(container_cpu_usage_seconds_total{namespace="sentrix"}[30s])',
    "container_memory_working_set_bytes": 'container_memory_working_set_bytes{namespace="sentrix"}',
    # rate(), not the raw cumulative counter -- RE2-OB's "_workload"
    # column is a request-rate proxy (observed values ~14-143 in the
    # validated sample, docs/RE2_SCHEMA.md), not a monotonically growing
    # total.
    "spanmetrics_calls_rate": "rate(traces_span_metrics_calls_total[30s])",
    "spanmetrics_duration_bucket": "traces_span_metrics_duration_milliseconds_bucket",
}

K8S_RESOURCES_TO_SNAPSHOT = [
    ("deployment", None),
    ("statefulset", None),
    ("service", None),
    ("configmap", None),
]


def http_get_json(url, params=None, timeout=30):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_text(url, params=None, timeout=30):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def export_traces(tempo_url, start_ns, end_ns, out_dir, known_trace_ids=None, search_window_s=60):
    """`/api/search` alone is NOT sufficient for completeness: a single
    call is capped at `limit` (5000) results, and against a real M2-scale
    run a 5000-trace cap silently drops whatever didn't fit -- discovered
    by directly diffing transaction trace_ids against exported files
    (1,205 of 2,279 known transaction traces were missing from a run
    whose manifest still claimed discovered==exported==5000, i.e. the
    search was truncated, not "found everything"). Two independent
    fixes, both required:

      1. Search in `search_window_s` sub-windows and union the results,
         so a single call's cap is far less likely to truncate within
         any one window.
      2. Directly fetch every trace_id in `known_trace_ids` (the actual
         transaction records this run generated) via /api/traces/{id},
         regardless of whether search discovered it. This is the
         completeness guarantee -- search is only a discovery aid for
         traces we don't already know about (health checks etc.).

    Completeness must be judged against `known_trace_ids` explicitly
    (see the returned `known_trace_ids_missing`), never inferred from
    discovered==exported alone."""
    os.makedirs(out_dir, exist_ok=True)
    known_trace_ids = set(known_trace_ids or [])

    search_pages = []
    discovered = set()
    cur = start_ns
    window_ns = search_window_s * 1_000_000_000
    while cur < end_ns:
        w_end = min(cur + window_ns, end_ns)
        page = http_get_json(
            f"{tempo_url}/api/search",
            params={"start": str(cur // 1_000_000_000), "end": str(w_end // 1_000_000_000), "limit": "5000"},
        )
        search_pages.append({"start_ns": cur, "end_ns": w_end, "result": page})
        discovered.update(t["traceID"] for t in page.get("traces", []))
        cur = w_end
    with open(os.path.join(out_dir, "_search_index.json"), "w", encoding="utf-8") as f:
        json.dump({"pages": search_pages}, f, indent=2)

    to_fetch = discovered | known_trace_ids
    # Skip trace_ids whose file was already written by a prior run of this
    # export (e.g. a retry after a partial failure) -- a trace's content
    # is immutable once Tempo has it, so re-fetching an already-saved file
    # is pure waste. Observed directly: a retry re-fetching ~2100 traces
    # sequentially (one HTTP round-trip each, no parallelism) took over 10
    # minutes for what should have been a handful of new fetches for the
    # few chunks that actually failed.
    already_have = {
        fn[: -len(".json")]
        for fn in os.listdir(out_dir)
        if fn.endswith(".json") and fn != "_search_index.json"
    }
    to_actually_fetch = to_fetch - already_have

    exported, failed = 0, []
    for tid in sorted(to_actually_fetch):
        try:
            trace = http_get_json(f"{tempo_url}/api/traces/{tid}")
            with open(os.path.join(out_dir, f"{tid}.json"), "w", encoding="utf-8") as f:
                json.dump(trace, f)
            exported += 1
        except Exception as e:  # noqa: BLE001
            failed.append({"trace_id": tid, "error": str(e)})
    exported += len(to_fetch & already_have)

    failed_ids = {f["trace_id"] for f in failed}
    known_trace_ids_missing = sorted(known_trace_ids & failed_ids)
    return {
        "discovered_via_search": len(discovered),
        "known_transaction_trace_ids": len(known_trace_ids),
        "to_fetch": len(to_fetch),
        "skipped_already_saved": len(to_fetch & already_have),
        "exported": exported,
        "failed": failed,
        "known_trace_ids_missing": known_trace_ids_missing,
    }


def export_metrics(prom_url, start_dt, end_dt, out_dir, step_s=5):
    os.makedirs(out_dir, exist_ok=True)
    results = {}
    for name, expr in PROM_EXPRESSIONS.items():
        try:
            data = http_get_json(
                f"{prom_url}/api/v1/query_range",
                params={
                    "query": expr,
                    "start": str(start_dt.timestamp()),
                    "end": str(end_dt.timestamp()),
                    "step": f"{step_s}s",
                },
            )
            with open(os.path.join(out_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                json.dump(data, f)
            n_series = len(data.get("data", {}).get("result", []))
            results[name] = {"status": "ok", "series": n_series}
        except Exception as e:  # noqa: BLE001
            results[name] = {"status": "error", "error": str(e)}
    return results


def _query_loki_chunk(loki_url, query, start_ns, end_ns, limit, timeout=60, retries=3):
    """A single transient timeout here must never be allowed to propagate
    and kill the entire export (it did exactly that during the CPU-
    saturation pilot: one Loki chunk query timeout took down
    export-telemetry.py mid-run, before canonicalize.py / re2-projection.py
    / generate-incident-record.py ever ran, and by the time this was
    noticed Tempo's 1h and Prometheus's 6h retention had both already
    expired -- unrecoverable). Retries with backoff first; the caller is
    still responsible for catching the final exception and recording the
    chunk as failed rather than crashing."""
    last_exc = None
    for attempt in range(retries):
        try:
            return http_get_json(
                f"{loki_url}/loki/api/v1/query_range",
                params={
                    "query": query,
                    "start": str(start_ns),
                    "end": str(end_ns),
                    "limit": str(limit),
                    "direction": "forward",
                },
                timeout=timeout,
            )
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise last_exc


def export_loki(loki_url, start_dt, end_dt, out_path, query, chunk_seconds=20, limit_per_chunk=4000):
    """Loki query_range, split into fixed-size time chunks rather than
    Loki's own count-based backward pagination. A single query across the
    whole run window with a high `limit` was observed to time out (>60s)
    against real M2-scale log volume -- one namespace-wide query has to
    scan every stream before it can return even a small limit's worth of
    the newest entries. Fixed small time windows bound each individual
    request's scan cost regardless of overall run length or log volume,
    at the cost of more (but each fast) HTTP calls. Written as one JSON
    object (one Loki API response) per line -- raw API pages, not
    individually parsed records; canonicalize.py does that.

    If a chunk's result hits limit_per_chunk exactly (meaning there was
    more data in that window than fetched), it's halved and retried --
    this only matters for chunks with unusually bursty logging."""
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    start_ns = int(start_dt.timestamp() * 1e9)
    end_ns = int(end_dt.timestamp() * 1e9)
    chunk_ns = chunk_seconds * 1_000_000_000

    total_lines = 0
    chunks_written = 0
    failed_chunks = []
    with open(out_path, "w", encoding="utf-8") as f:
        cur = start_ns
        while cur < end_ns:
            window_end = min(cur + chunk_ns, end_ns)
            window = window_end - cur
            limit = limit_per_chunk
            try:
                while True:
                    data = _query_loki_chunk(loki_url, query, cur, window_end, limit)
                    streams = data.get("data", {}).get("result", [])
                    n = sum(len(s["values"]) for s in streams)
                    if n < limit or window < 1_000_000:  # < 1ms window: give up subdividing
                        break
                    window = window // 2
                    window_end = cur + window
                f.write(json.dumps(data) + "\n")
                chunks_written += 1
                total_lines += n
            except Exception as e:  # noqa: BLE001
                # A chunk that fails even after _query_loki_chunk's own
                # retries must NOT take down the rest of the export (this
                # crashed the whole pilot run's pipeline before
                # canonicalize/re2-projection/incident-record ever ran).
                # Record the gap explicitly instead -- a documented hole
                # in raw/logs.ndjson, not a silent one and not a fatal one.
                failed_chunks.append(
                    {"start_ns": cur, "end_ns": window_end, "error": str(e)}
                )
            cur = window_end
    return {
        "chunks": chunks_written,
        "lines": total_lines,
        "chunk_seconds": chunk_seconds,
        "failed_chunks": failed_chunks,
    }


def export_environment(out_dir, namespace="sentrix"):
    os.makedirs(out_dir, exist_ok=True)
    results = {}
    for kind, _ in K8S_RESOURCES_TO_SNAPSHOT:
        proc = subprocess.run(
            ["kubectl", "get", kind, "-n", namespace, "-o", "yaml"],
            text=True,
            capture_output=True,
        )
        out_path = os.path.join(out_dir, f"{kind}.yaml")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(proc.stdout)
        results[kind] = {"status": "ok" if proc.returncode == 0 else "error", "stderr": proc.stderr[:500]}
    return results


def step_has_partial_failure(name, result):
    """A step can return normally (no exception) while still being
    incomplete internally -- export_traces()'s `failed`/
    `known_trace_ids_missing`, export_metrics()'s per-expression
    status=="error", export_loki()'s `failed_chunks`,
    export_environment()'s per-resource status!="ok". Previously none of
    these were inspected, so `manifest["complete"]` only reflected
    whether an exception had propagated out of the step entirely --
    a run with real, silent partial failures (missing traces, a stalled
    Loki chunk that gave up after retries) could still report
    complete: true, which then fed directly into
    generate-incident-record.py's validity.is_valid_run."""
    if name == "traces":
        return bool(result.get("failed")) or bool(result.get("known_trace_ids_missing"))
    if name == "metrics":
        return any(v.get("status") == "error" for v in result.values())
    if name in ("logs", "events"):
        return bool(result.get("failed_chunks"))
    if name == "environment":
        return any(v.get("status") != "ok" for v in result.values())
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--start", required=True, help="ISO8601 UTC, e.g. 2026-09-19T10:00:00+00:00")
    ap.add_argument("--end", required=True, help="ISO8601 UTC")
    ap.add_argument("--tempo-url", default="http://localhost:3200")
    ap.add_argument("--prometheus-url", default="http://localhost:9090")
    ap.add_argument("--loki-url", default="http://localhost:3100")
    ap.add_argument(
        "--runs-dir", default=os.path.join(os.path.dirname(__file__), "..", "runs")
    )
    args = ap.parse_args()

    start_dt = datetime.fromisoformat(args.start)
    end_dt = datetime.fromisoformat(args.end)
    run_dir = os.path.join(args.runs_dir, args.run_id)
    raw_dir = os.path.join(run_dir, "raw")

    # Ground truth for trace-export completeness: every trace_id this
    # run's own transactions actually generated. See export_traces().
    known_trace_ids = set()
    txn_path = os.path.join(run_dir, "transaction-results.jsonl")
    if os.path.exists(txn_path):
        with open(txn_path, "r", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                if rec.get("trace_id"):
                    known_trace_ids.add(rec["trace_id"])

    manifest = {
        "run_id": args.run_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "window_start": start_dt.isoformat(),
        "window_end": end_dt.isoformat(),
        "backends": {
            "tempo_url": args.tempo_url,
            "prometheus_url": args.prometheus_url,
            "loki_url": args.loki_url,
        },
    }

    # Every step is independently fault-tolerant: one signal's backend
    # being unreachable/slow must not cost the others, and the manifest
    # (which is what a later reader checks for completeness) is always
    # written at the end regardless of how many steps failed. A prior
    # version let one uncaught Loki timeout crash the whole script before
    # export-manifest.json (or anything downstream: canonicalize.py,
    # re2-projection.py, generate-incident-record.py) ever ran.
    steps = [
        (
            "traces",
            lambda: export_traces(
                args.tempo_url,
                int(start_dt.timestamp() * 1e9),
                int(end_dt.timestamp() * 1e9),
                os.path.join(raw_dir, "traces"),
                known_trace_ids=known_trace_ids,
            ),
        ),
        (
            "metrics",
            lambda: export_metrics(
                args.prometheus_url, start_dt, end_dt, os.path.join(raw_dir, "metrics")
            ),
        ),
        (
            "logs",
            lambda: export_loki(
                args.loki_url,
                start_dt,
                end_dt,
                os.path.join(raw_dir, "logs.ndjson"),
                query='{k8s_namespace_name="sentrix"}',
            ),
        ),
        (
            "events",
            lambda: export_loki(
                args.loki_url,
                start_dt,
                end_dt,
                os.path.join(raw_dir, "events.ndjson"),
                # event_domain is OTLP structured metadata attached
                # per-entry by the k8sobjects receiver, NOT a Loki stream
                # label, even though it appears inside each result's
                # "stream" object in the API response. A stream selector
                # `{event_domain="k8s"}` silently matches zero streams
                # (confirmed directly against live data: {event_domain="k8s"}
                # alone returns 0 results despite 11 such entries actually
                # being present in the same window) -- it must be a
                # LogQL label/line filter STAGE instead.
                query='{k8s_namespace_name="sentrix"} | event_domain="k8s"',
            ),
        ),
        (
            "environment",
            lambda: export_environment(os.path.join(raw_dir, "environment")),
        ),
    ]

    any_step_failed = False
    for name, fn in steps:
        print(f"[export] {name}...", file=sys.stderr)
        try:
            manifest[name] = fn()
        except Exception as e:  # noqa: BLE001
            any_step_failed = True
            manifest[name] = {"status": "step_failed", "error": str(e)}
            print(f"[export] {name} FAILED: {e}", file=sys.stderr)
            continue
        if step_has_partial_failure(name, manifest[name]):
            any_step_failed = True
            print(f"[export] {name} PARTIAL: {manifest[name]}", file=sys.stderr)

    manifest["complete"] = not any_step_failed
    os.makedirs(raw_dir, exist_ok=True)
    with open(os.path.join(raw_dir, "export-manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    print(json.dumps(manifest, indent=2))
    if any_step_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
