"""Resumable API-response export. No inference of end-to-end ingestion completeness."""
import argparse
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.parse
import urllib.request

VERSION = 2
PROM_EXPRESSIONS = {
    "container_cpu_usage_rate": 'rate(container_cpu_usage_seconds_total{namespace="sentrix"}[60s])',
    "container_memory_working_set_bytes": 'container_memory_working_set_bytes{namespace="sentrix"}',
    "spanmetrics_calls_rate": "rate(traces_span_metrics_calls_total[30s])",
    "spanmetrics_duration_bucket": "traces_span_metrics_duration_milliseconds_bucket",
    # Rate each original counter before aggregating, so pod/collector resets
    # cannot be hidden by another instance's increase.
    "spanmetrics_duration_bucket_rate": 'sum by (service_namespace, service_name, le) (rate(traces_span_metrics_duration_milliseconds_bucket{service_namespace="sentrix"}[30s]))',
    "spanmetrics_error_increase_30s": '(sum by (service_namespace, service_name) (increase(traces_span_metrics_calls_total{service_namespace="sentrix",status_code="STATUS_CODE_ERROR"}[30s]))) or (0 * sum by (service_namespace, service_name) (increase(traces_span_metrics_calls_total{service_namespace="sentrix"}[30s])))',
}
ENVIRONMENT_KINDS = ("deployment", "statefulset", "service", "configmap")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path, content, preserve=False):
    """Only this export generation's materializations may be replaced; keep predecessors."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if preserve and path.exists():
        previous = path.read_bytes()
        if previous == content.encode("utf-8"):
            return
        history = path.parent / "_history" / (path.name + "." + hashlib.sha256(previous).hexdigest())
        history.parent.mkdir(exist_ok=True)
        if not history.exists():
            shutil.copy2(path, history)
    fd, tmp = tempfile.mkstemp(prefix=".export-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Windows readers/AV can briefly hold the destination without delete
        # sharing. Keep the original and retry only the atomic rename.
        for attempt in range(6):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2 ** attempt))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_json(path, obj, preserve=False):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), preserve)


@contextmanager
def exclusive_export(raw):
    """OS lock is released on process death; no stale PID-lock bypass is needed."""
    state = raw / "_export"
    state.mkdir(parents=True, exist_ok=True)
    with (state / "lock").open("a+b") as f:
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class BudgetExpired(RuntimeError):
    pass


class Store:
    def __init__(self, raw, context, budget_seconds=900, retries=3, timeout=30):
        self.raw = Path(raw)
        self.state = self.raw / "_export"
        self.state.mkdir(parents=True, exist_ok=True)
        context_path = self.state / "context.json"
        if context_path.exists():
            if json.loads(context_path.read_text(encoding="utf-8")) != context:
                raise ValueError("Export context changed. Use a new --raw-subdir; do not mix windows/backends/versions.")
        else:
            if any(p.name != "_export" for p in self.raw.iterdir()):
                raise ValueError("Legacy/raw artifacts exist without v2 provenance. Use a NEW --raw-subdir (e.g. raw-v2).")
            save_json(context_path, context)
        self.deadline = time.monotonic() + budget_seconds
        self.retries = retries
        self.timeout = timeout
        self.cache_hits = 0
        self.requests = 0
        self.progress_lock = threading.Lock()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetExpired("Export time budget exhausted; successful checkpoints are preserved. Resume with identical arguments.")
        return remaining

    def request(self, url, params, validate):
        key = {"url": url, "params": params}
        path = self.state / "responses" / (digest(key) + ".json")
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached["request"] != key or cached["sha256"] != digest(cached["response"]):
                    raise ValueError("Checkpoint digest mismatch")
                validate(cached["response"])
                self.cache_hits += 1
                return cached["response"]
            except (ValueError, KeyError, TypeError):
                pass  # Refetch a corrupt checkpoint; its original bytes go to _history.
        last_error = None
        for attempt in range(self.retries):
            remaining = self.remaining()
            with self.progress_lock:
                save_json(self.state / "progress.json", {"updated_at": utc_now(), "request": key, "attempt": attempt + 1, "state": "requesting"})
            try:
                target = url + ("?" + urllib.parse.urlencode(params) if params else "")
                # urlopen's timeout is a socket timeout; the outer process watchdog
                # enforces a hard deadline even for a slow trickling response.
                with urllib.request.urlopen(target, timeout=min(self.timeout, remaining)) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                self.requests += 1
                try:
                    validate(data)
                except Exception as exc:
                    # Keep partial/error API evidence, but never promote it to
                    # a reusable successful checkpoint.
                    save_json(self.state / "rejected" / (digest(key) + "-" + str(time.time_ns()) + ".json"),
                              {"request": key, "response": data, "error": str(exc), "received_at": utc_now()})
                    raise
                save_json(path, {"request": key, "stored_at": utc_now(), "sha256": digest(data), "response": data}, preserve=True)
                return data
            except BudgetExpired:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(min(2 ** attempt, self.remaining()))
        raise RuntimeError(f"Request failed after {self.retries} attempts: {last_error}")


def api_success(data, kind):
    if data.get("status") != "success" or data.get("data", {}).get("resultType") != kind:
        raise ValueError(f"Expected successful {kind} API response")
    if not isinstance(data["data"].get("result"), list):
        raise ValueError("Missing API result array")
    if data.get("warnings") or data.get("infos"):
        raise ValueError("API returned warnings/infos; completeness needs review")


def trace_hex(value, size):
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{" + str(size * 2) + "}", value):
        return value.lower()
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) != size:
        raise ValueError("Wrong trace/span ID length")
    return decoded.hex()


def validate_trace(data, expected_id):
    spans = [s for b in data.get("batches", []) for scope in b.get("scopeSpans", []) for s in scope.get("spans", [])]
    if not spans:
        raise ValueError("Trace response contains no spans")
    timestamp_anomalies = []
    for span in spans:
        if trace_hex(span.get("traceId"), 16) != expected_id:
            raise ValueError("Trace ID mismatch")
        trace_hex(span.get("spanId"), 8)
        if int(span.get("endTimeUnixNano", 0)) < int(span.get("startTimeUnixNano", 0)) or int(span.get("startTimeUnixNano", 0)) <= 0:
            # The response is still a real, transaction-linked trace.  Do not
            # turn a malformed *child* span into a false “trace missing”
            # result, and never alter its raw timestamps.  Consumers receive
            # the anomaly ledger and exclude it from derived duration fields.
            timestamp_anomalies.append({
                "span_id": trace_hex(span.get("spanId"), 8),
                "span_name": span.get("name"),
                "start_time_unix_nano": int(span.get("startTimeUnixNano", 0)),
                "end_time_unix_nano": int(span.get("endTimeUnixNano", 0)),
            })
    return timestamp_anomalies


def export_traces(store, url, start_ns, end_ns, known_ids, trace_workers=1):
    discovered, pages, gaps = set(), [], []
    def search(lo, hi):
        try:
            def validate(page):
                if not isinstance(page.get("traces"), list):
                    raise ValueError("Malformed Tempo search response")
                for item in page["traces"]:
                    if not re.fullmatch(r"[0-9a-fA-F]{1,32}", item.get("traceID", "")):
                        raise ValueError("Invalid search trace ID")
            page = store.request(url + "/api/search", {"start": str(lo), "end": str(hi), "limit": "5000"}, validate)
            discovered.update(t["traceID"].lower().zfill(32) for t in page["traces"])
            if len(page["traces"]) >= 5000:
                if hi - lo <= 1:
                    raise ValueError("Tempo search is truncated even at one-second resolution")
                mid = (lo + hi) // 2
                search(lo, mid)
                search(mid, hi)
            else:
                pages.append({"start": lo, "end": hi, "result": page})
        except BudgetExpired:
            raise
        except Exception as exc:
            gaps.append({"start": lo, "end": hi, "error": str(exc)})
    lo, stop = start_ns // 10**9, (end_ns + 10**9 - 1) // 10**9
    while lo < stop:
        hi = min(lo + 60, stop)
        search(lo, hi)
        lo = hi
    out = store.raw / "traces"
    save_json(out / "_search_index.json", {"pages": pages, "failed_windows": gaps}, preserve=True)
    failed, saved, timestamp_anomalies = [], set(), {}
    # Known transaction IDs first so background probes cannot consume the entire budget.
    def fetch(tid):
        try:
            target = out / (tid + ".json")
            # A saved, validated trace is a durable checkpoint.  Do not make
            # it dependent on a later backend request when resuming an export.
            if target.exists():
                data = json.loads(target.read_text(encoding="utf-8"))
                return tid, None, validate_trace(data, tid)
            # Tempo otherwise searches every block it has ever retained for
            # each ID.  Bounds are required by the API to restrict that scan
            # to this frozen run window.
            params = {"start": str(start_ns // 10**9), "end": str((end_ns + 10**9 - 1) // 10**9)}
            data = store.request(url + "/api/traces/" + tid, params, lambda d: validate_trace(d, tid))
            save_json(target, data, preserve=True)
            return tid, None, validate_trace(data, tid)
        except BudgetExpired:
            raise
        except Exception as exc:
            return tid, str(exc), []
    # A Tempo trace-id lookup scans its active blocks.  On this single-node
    # deployment four simultaneous scans can OOM the collector, so keep the
    # default deliberately serial.  Callers may opt in to a higher value only
    # after measuring their Tempo capacity.
    with ThreadPoolExecutor(max_workers=trace_workers) as pool:
        # Search is an inventory/limit check. Fetch only transaction-linked
        # traces: background health probes can be arbitrarily numerous and
        # are outside this run's stated completeness scope.
        for tid, error, anomalies in pool.map(fetch, sorted(known_ids)):
            if error is None:
                saved.add(tid)
                if anomalies:
                    timestamp_anomalies[tid] = anomalies
            else:
                failed.append({"trace_id":tid,"error":error})
    return {"discovered_via_search": len(discovered), "known_transaction_trace_ids": len(known_ids), "exported": len(saved), "unexported_background_traces": len(discovered-known_ids), "failed": failed, "failed_search_windows": gaps, "known_trace_ids_missing": sorted(known_ids - saved), "timestamp_anomalies": timestamp_anomalies, "timestamp_anomaly_span_count": sum(len(v) for v in timestamp_anomalies.values())}


def validate_metrics(data, expected_times, required_services, require_each_series=False):
    api_success(data, "matrix")
    series = data["data"]["result"]
    if not series:
        raise ValueError("Empty metric result")
    coverage = {s: set() for s in required_services} if required_services else {"*": set()}
    for item in series:
        labels = item.get("metric", {})
        times = set()
        for ts, val in item.get("values", []):
            if not math.isfinite(float(val)):
                raise ValueError("Non-finite metric sample")
            times.add(round(float(ts), 6))
        if require_each_series and expected_times - times:
            raise ValueError("Missing points in an aggregated metric series")
        if not required_services:
            coverage["*"].update(times)
        for svc in required_services:
            pod = labels.get("pod", "")
            if labels.get("service_name") == svc or pod.startswith(svc + "-"):
                coverage[svc].update(times)
    missing = {s: len(expected_times - ts) for s, ts in coverage.items() if expected_times - ts}
    if missing:
        raise ValueError(f"Missing metric query-grid points by required service: {missing}")


def export_metrics(store, url, start_ns, end_ns, services, traced_services, step=5):
    results = {}
    # Prometheus evaluation timestamps have millisecond resolution.
    # Record/query that actual grid rather than expecting microsecond samples.
    start_ns = (start_ns // 1_000_000) * 1_000_000
    end_ns = (end_ns // 1_000_000) * 1_000_000
    for name, expr in PROM_EXPRESSIONS.items():
        cur, pages, failed = start_ns, [], []
        required = traced_services if name.startswith("spanmetrics") else services
        while cur <= end_ns:
            hi = min(cur + 59 * step * 10**9, end_ns)
            expected = {round(t / 10**9, 6) for t in range(cur, hi + 1, step * 10**9)}
            try:
                page = store.request(url + "/api/v1/query_range", {"query": expr, "start": str(cur / 10**9), "end": str(hi / 10**9), "step": f"{step}s"}, lambda d: validate_metrics(d, expected, required, name in ("spanmetrics_duration_bucket_rate", "spanmetrics_error_increase_30s")))
                pages.append(page)
            except BudgetExpired:
                raise
            except Exception as exc:
                failed.append({"start_ns": cur, "end_ns": hi, "error": str(exc)})
            cur += 60 * step * 10**9
        merged = {}
        for page in pages:
            for s in page["data"]["result"]:
                key = json.dumps(s["metric"], sort_keys=True)
                target = merged.setdefault(key, {"metric": s["metric"], "values": []})
                target["values"].extend(s["values"])
        # Partial responses stay in immutable checkpoints. Never replace a prior
        # materialized metric with an incomplete/empty version.
        if not failed:
            save_json(store.raw / "metrics" / (name + ".json"), {"status": "success", "data": {"resultType": "matrix", "result": list(merged.values())}}, preserve=True)
        results[name] = {"status": "error" if failed else "ok", "series": len(merged), "failed_chunks": failed, "required_services": required}
    return results


def export_loki(store, url, start_ns, end_ns, name, query, limit=4000, expected_text=None, chunk_seconds=20):
    if chunk_seconds <= 0:
        raise ValueError("Loki chunk duration must be positive")
    pages, failed = [], []
    def chunk(lo, hi):
        try:
            def validate(page):
                api_success(page, "streams")
                for s in page["data"]["result"]:
                    for entry in s.get("values", []):
                        if len(entry) < 2 or not lo <= int(entry[0]) < hi:
                            raise ValueError("Loki entry outside requested half-open window")
            # Loki's end is exclusive (official HTTP API). Subtracting 1ns
            # would lose an entry at the last nanosecond of every chunk.
            page = store.request(url + "/loki/api/v1/query_range", {"query": query, "start": str(lo), "end": str(hi), "limit": str(limit), "direction": "forward"}, validate)
            n = sum(len(s["values"]) for s in page["data"]["result"])
            if n >= limit:
                if hi - lo <= 1:
                    raise ValueError("Loki response capped at a single nanosecond; cannot prove completeness")
                mid = (lo + hi) // 2
                chunk(lo, mid)
                chunk(mid, hi)
            else:
                pages.append(page)
        except BudgetExpired:
            raise
        except Exception as exc:
            failed.append({"start_ns": lo, "end_ns": hi, "error": str(exc)})
    chunk_ns = chunk_seconds * 10**9
    for lo in range(start_ns, end_ns, chunk_ns):
        chunk(lo, min(lo + chunk_ns, end_ns))
    lines = sum(len(s["values"]) for p in pages for s in p["data"]["result"])
    errors = []
    if name == "logs" and not lines:
        errors.append("No namespace logs found in an active workload window")
    if expected_text and not any(expected_text in entry[1] for p in pages for s in p["data"]["result"] for entry in s["values"]):
        errors.append("Expected quality-gate sentinel is missing from event export")
    if not failed and not errors:
        atomic_write(store.raw / (name + ".ndjson"), "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pages), preserve=True)
    return {"chunks": len(pages), "lines": lines, "failed_chunks": failed, "validation_errors": errors, "materialized": not failed and not errors}


def export_environment(store):
    results = {}
    for kind in ENVIRONMENT_KINDS:
        path = store.state / ("environment-" + kind + ".json")
        try:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["sha256"] != digest(saved["text"]):
                    raise ValueError("Environment snapshot checksum mismatch")
            else:
                proc = subprocess.run(["kubectl", "--request-timeout=10s", "get", kind, "-n", "sentrix", "-o", "yaml"], capture_output=True, text=True, timeout=min(15, store.remaining()))
                if proc.returncode or not proc.stdout.strip():
                    raise RuntimeError(proc.stderr or "Empty environment snapshot")
                saved = {"text": proc.stdout, "captured_at": utc_now(), "sha256": digest(proc.stdout)}
                save_json(path, saved)
            atomic_write(store.raw / "environment" / (kind + ".yaml"), saved["text"], preserve=True)
            results[kind] = {"status": "ok", "captured_at": saved["captured_at"]}
        except BudgetExpired:
            raise
        except Exception as exc:
            results[kind] = {"status": "error", "error": str(exc)}
    return results


def partial(name, result):
    if result.get("status") in ("step_failed", "pending", "running", "budget_exhausted"):
        return True
    if name == "traces":
        return not result.get("known_transaction_trace_ids") or any(result.get(k) for k in ("failed", "failed_search_windows", "known_trace_ids_missing"))
    if name in ("metrics", "environment"):
        expected = set(PROM_EXPRESSIONS) if name == "metrics" else set(ENVIRONMENT_KINDS)
        return set(result) != expected or any(v.get("status") != "ok" or (name == "metrics" and not v.get("series")) for v in result.values())
    return bool(result.get("failed_chunks")) or bool(result.get("validation_errors")) or not result.get("materialized")


def parse_time(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp must carry an explicit timezone")
    dt = dt.astimezone(timezone.utc)
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def run(args):
    import yaml
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.raw_subdir) or args.raw_subdir in (".", ".."):
        raise ValueError("raw-subdir must be a single directory name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.run_id):
        raise ValueError("Invalid run-id")
    start, end = parse_time(args.start), parse_time(args.end)
    if start >= end:
        raise ValueError("Require start < end")
    settle_wait = max(0, (end + args.settle_seconds * 10**9 - time.time_ns()) / 10**9)
    if settle_wait > 120:
        raise ValueError("Window is too far in the future. Retry later with the SAME window; maximum settling wait is 120 seconds.")
    run_dir = Path(args.runs_dir) / args.run_id
    known = set()
    with (run_dir / "transaction-results.jsonl").open(encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("run_id") != args.run_id:
                raise ValueError("Transaction run_id mismatch")
            tid = rec.get("trace_id")
            if not isinstance(tid, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", tid):
                raise ValueError("Missing/invalid transaction trace ID")
            if not start <= parse_time(rec["event_time"]) < end:
                raise ValueError("Export window does not cover all transaction starts")
            if parse_time(rec["event_time"]) + int(float(rec.get("duration_ms", 0)) * 1_000_000) > end:
                raise ValueError("Export window does not cover transaction completion")
            known.add(tid.lower())
    if not known:
        raise ValueError("No transaction trace IDs; cannot attest coverage")
    manifest = yaml.safe_load((run_dir / "run-manifest.yaml").read_text(encoding="utf-8"))
    traced = manifest["release"]["traced_services"]
    services = sorted(set(traced + manifest["release"]["untraced_services"]))
    if not traced or not services:
        raise ValueError("Required service coverage is undefined")
    sentinel = None
    quality_path = run_dir / "quality-report.json"
    if quality_path.exists():
        gate = json.loads(quality_path.read_text(encoding="utf-8")).get("m1a_gate_b_k8s_event_pipeline", {})
        if gate.get("status") == "PASS" and gate.get("event_time") and start <= parse_time(gate["event_time"]) < end:
            sentinel = gate.get("sentinel_name")
    context = {"version": VERSION, "run_id": args.run_id, "start_ns": start, "end_ns": end, "tempo": args.tempo_url, "prometheus": args.prometheus_url, "loki": args.loki_url, "known_ids_sha256": digest(sorted(known)), "expressions": PROM_EXPRESSIONS, "services": services, "traced_services": traced, "settle_seconds": args.settle_seconds, "expected_sentinel": sentinel}
    raw = run_dir / args.raw_subdir
    with exclusive_export(raw):
        store = Store(raw, context, args.budget_seconds)
        report = {"schema_version": VERSION, "run_id": args.run_id, "exported_at": utc_now(), "window_start": args.start, "window_end": args.end, "complete": False,
                  "completeness_scope": "API query coverage and nonempty responses for known trace IDs; NOT proof of every span, original scrape sample or emitted log being ingested.",
                  "raw_subdir": args.raw_subdir}
        steps = [("environment", lambda: export_environment(store)), ("traces", lambda: export_traces(store, args.tempo_url, start, end, known, getattr(args, "trace_workers", 1))), ("metrics", lambda: export_metrics(store, args.prometheus_url, start, end, services, traced)), ("logs", lambda: export_loki(store, args.loki_url, start, end, "logs", '{k8s_namespace_name="sentrix"}', chunk_seconds=5)), ("events", lambda: export_loki(store, args.loki_url, start, end, "events", '{k8s_namespace_name="sentrix"} | event_domain="k8s"', expected_text=sentinel))]
        for name, _ in steps:
            report[name] = {"status": "pending"}
        path = raw / "export-manifest.json"
        save_json(path, report, preserve=True)
        if settle_wait:
            report["settling_until"] = datetime.fromtimestamp(end / 10**9 + args.settle_seconds, timezone.utc).isoformat()
            save_json(path, report)
            print(f"[export] settling for {settle_wait:.1f}s before querying the frozen window", file=sys.stderr, flush=True)
            target = time.monotonic() + settle_wait
            while time.monotonic() < target:
                time.sleep(max(0, min(1, target - time.monotonic(), store.remaining())))
        for name, fn in steps:
            report[name] = {"status": "running"}
            report["updated_at"] = utc_now()
            save_json(path, report)
            print(f"[export] {name}", file=sys.stderr, flush=True)
            try:
                report[name] = fn()
            except Exception as exc:
                report[name] = {"status": "budget_exhausted" if isinstance(exc, BudgetExpired) else "step_failed", "error": str(exc)}
            report["updated_at"] = utc_now()
            save_json(path, report)
            if report[name].get("status") == "budget_exhausted":
                break
        report["complete"] = not any(partial(n, report[n]) for n, _ in steps)
        report["finished_at"] = utc_now()
        report["cache_hits"] = store.cache_hits
        save_json(path, report)
        save_json(store.state / ("attempt-" + str(time.time_ns()) + ".json"), report)
        print(json.dumps(report, indent=2))
        return 0 if report["complete"] else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--runs-dir", default=str(Path(__file__).resolve().parent.parent / "runs"))
    ap.add_argument("--raw-subdir", default="raw")
    ap.add_argument("--tempo-url", default="http://localhost:3200")
    ap.add_argument("--prometheus-url", default="http://localhost:9090")
    ap.add_argument("--loki-url", default="http://localhost:3100")
    ap.add_argument("--settle-seconds", type=int, default=60)
    ap.add_argument("--budget-seconds", type=int, default=900)
    ap.add_argument("--trace-workers", type=int, default=1)
    ap.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.budget_seconds <= 0 or args.settle_seconds < 0 or args.trace_workers <= 0:
        ap.error("budget-seconds and trace-workers must be positive; settle-seconds must be nonnegative")
    if not args.worker:
        # Enforce a wall-clock ceiling even if a backend trickles data forever.
        command = [sys.executable, "-B", str(Path(__file__).with_name("export-telemetry.py")), *sys.argv[1:], "--worker"]
        proc = subprocess.Popen(command)
        try:
            return proc.wait(timeout=args.budget_seconds + 15)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            proc.wait(timeout=10)
            print("Export interrupted/timed out. complete remains false; resume identical window from checkpoints.", file=sys.stderr)
            return 1
    try:
        return run(args)
    except Exception as exc:
        print(f"Export refused/failed: {exc}", file=sys.stderr)
        return 1
