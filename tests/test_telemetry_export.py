"""Offline failure/recovery tests. No requests to the user's cluster."""
import base64
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import telemetry_export as ex

TID = "ab" * 16


def trace(tid=TID):
    return {"batches": [{"scopeSpans": [{"spans": [{"traceId": base64.b64encode(bytes.fromhex(tid)).decode(), "spanId": base64.b64encode(b"12345678").decode(), "startTimeUnixNano": "1", "endTimeUnixNano": "2"}]}]}]}


def streams(entries=()):
    return {"status": "success", "data": {"resultType": "streams", "result": [{"stream": {"app": "test"}, "values": [[str(t), line] for t, line in entries]}] if entries else []}}


def matrix(times, service="checkoutservice"):
    return {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {"service_name": service, "pod": service + "-pod"}, "values": [[t, "1"] for t in times]}]}}


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return json.dumps(self.data).encode()


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.raw = Path(self.tmp.name) / "raw"
        self.store = ex.Store(self.raw, {"version": 2}, retries=1)

    def fake_backend(self, fn):
        def call(url, timeout):
            parsed = urllib.parse.urlparse(url)
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            return Response(fn(parsed.path, params))
        return patch.object(ex.urllib.request, "urlopen", side_effect=call)

    def test_atomic_replace_failure_preserves_original(self):
        p = self.raw / "data.json"
        p.write_text("original")
        with patch.object(ex.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                ex.atomic_write(p, "replacement", preserve=True)
        self.assertEqual(p.read_text(), "original")
        self.assertEqual(list((p.parent / "_history").iterdir())[0].read_text(), "original")

    def test_windows_transient_rename_lock_retries_without_losing_original(self):
        p = self.raw / "data.json"
        p.write_text("original")
        replace = ex.os.replace
        attempts = []
        def flaky(src, dest):
            attempts.append(1)
            if len(attempts) < 3:
                self.assertEqual(p.read_text(), "original")
                raise PermissionError("sharing violation")
            replace(src, dest)
        with patch.object(ex.os, "replace", side_effect=flaky), patch.object(ex.time, "sleep"):
            ex.atomic_write(p, "replacement", preserve=True)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(p.read_text(), "replacement")

    def test_legacy_directory_is_not_overwritten(self):
        d = Path(self.tmp.name) / "legacy"
        d.mkdir()
        p = d / "logs.ndjson"
        p.write_text("legacy evidence")
        with self.assertRaisesRegex(ValueError, "Legacy"):
            ex.Store(d, {"version": 2})
        self.assertEqual(p.read_text(), "legacy evidence")

    def test_changed_context_is_refused(self):
        with self.assertRaisesRegex(ValueError, "context changed"):
            ex.Store(self.raw, {"version": 3})

    def test_os_lock_rejects_second_writer_and_releases(self):
        with ex.exclusive_export(self.raw):
            with self.assertRaises(OSError):
                with ex.exclusive_export(self.raw):
                    self.fail("Second writer acquired export lock")
        with ex.exclusive_export(self.raw):
            pass

    def test_successful_response_resumes_without_network(self):
        with self.fake_backend(lambda p, q: {"ok": True}):
            self.store.request("http://fake/test", {}, lambda d: None)
        with patch.object(ex.urllib.request, "urlopen", side_effect=AssertionError("must use checkpoint")):
            resumed = ex.Store(self.raw, {"version": 2})
            self.assertEqual(resumed.request("http://fake/test", {}, lambda d: None), {"ok": True})

    def test_corrupt_checkpoint_refetched_and_preserved(self):
        with self.fake_backend(lambda p, q: {"value": 1}):
            self.store.request("http://fake/test", {}, lambda d: None)
        path = next((self.store.state / "responses").glob("*.json"))
        path.write_text("truncated")
        with self.fake_backend(lambda p, q: {"value": 2}):
            self.assertEqual(self.store.request("http://fake/test", {}, lambda d: None)["value"], 2)
        self.assertTrue(any(p.read_text() == "truncated" for p in (path.parent / "_history").iterdir()))

    def test_transient_timeout_retried(self):
        self.store.retries = 3
        with patch.object(ex.urllib.request, "urlopen", side_effect=[TimeoutError("slow"), Response({"ok": 1})]) as request, patch.object(ex.time, "sleep"):
            self.assertEqual(self.store.request("http://fake/test", {}, lambda d: None), {"ok": 1})
        self.assertEqual(request.call_count, 2)

    def test_budget_exhaustion_preserves_successful_checkpoint(self):
        with self.fake_backend(lambda p, q: {"ok": 1}):
            self.store.request("http://fake/first", {}, lambda d: None)
        self.store.deadline = 0
        with self.assertRaises(ex.BudgetExpired):
            self.store.request("http://fake/second", {}, lambda d: None)
        self.assertEqual(len(list((self.store.state / "responses").glob("*.json"))), 1)

    def test_known_trace_is_fetched_even_when_search_fails(self):
        def backend(path, params):
            if path.endswith("/search"):
                raise TimeoutError("search unavailable")
            return trace()
        with self.fake_backend(backend):
            r = ex.export_traces(self.store, "http://fake", 0, 10**9, {TID})
        self.assertEqual(r["known_trace_ids_missing"], [])
        self.assertTrue(r["failed_search_windows"])
        self.assertTrue(ex.partial("traces", r))
        self.assertTrue((self.raw / "traces" / (TID + ".json")).exists())

    def test_background_search_traces_do_not_expand_transaction_export_scope(self):
        background = "cd" * 16
        calls = []
        def backend(path, params):
            if path.endswith("/search"):
                return {"traces": [{"traceID": TID}, {"traceID": background}]}
            calls.append(path)
            return trace(TID)
        with self.fake_backend(backend):
            result = ex.export_traces(self.store, "http://fake", 0, 10**9, {TID})
        self.assertEqual(result["exported"], 1)
        self.assertEqual(result["unexported_background_traces"], 1)
        self.assertEqual(calls, ["/api/traces/" + TID])

    def test_search_limit_subdivision_and_unsplittable_gap(self):
        def backend(path, params):
            if path.endswith("/search"):
                return {"traces": [{"traceID": TID}] * 5000}
            return trace()
        with self.fake_backend(backend):
            r = ex.export_traces(self.store, "http://fake", 0, 2 * 10**9, {TID})
        self.assertEqual(len(r["failed_search_windows"]), 2)
        self.assertEqual(r["exported"], 1)

    def test_empty_or_wrong_trace_cannot_pass(self):
        for response in ({"batches": []}, trace("cd" * 16)):
            with self.assertRaises(ValueError):
                ex.validate_trace(response, TID)

    def test_malformed_child_timestamp_is_an_audited_anomaly_not_a_missing_trace(self):
        response = trace()
        response["batches"][0]["scopeSpans"][0]["spans"][0]["endTimeUnixNano"] = "0"
        anomalies = ex.validate_trace(response, TID)
        self.assertEqual(len(anomalies), 1)
        with self.fake_backend(lambda p, q: {"traces": [{"traceID": TID}]} if p.endswith("/search") else response):
            result = ex.export_traces(self.store, "http://fake", 0, 10**9, {TID})
        self.assertEqual(result["known_trace_ids_missing"], [])
        self.assertEqual(result["timestamp_anomaly_span_count"], 1)
        self.assertTrue((self.raw / "traces" / (TID + ".json")).exists())

    def test_search_leading_zero_alias_is_normalized(self):
        tid = "0" + "a" * 31
        with self.fake_backend(lambda p, q: {"traces": [{"traceID": tid[1:]}]} if p.endswith("/search") else trace(tid)):
            result = ex.export_traces(self.store, "http://fake", 0, 10**9, {tid})
        self.assertEqual(result["exported"], 1)
        self.assertFalse(result["failed_search_windows"])
        self.assertFalse(result["known_trace_ids_missing"])

    def test_histogram_series_holes_cannot_hide_behind_service_coverage(self):
        page = matrix([0, 5])
        page["data"]["result"].append(matrix([0])["data"]["result"][0])
        with self.assertRaisesRegex(ValueError, "aggregated metric series"):
            ex.validate_metrics(page, {0, 5}, ["checkoutservice"], require_each_series=True)

    def test_metrics_empty_holes_services_nan_are_rejected(self):
        invalid = [
            {"status": "success", "data": {"resultType": "matrix", "result": []}},
            matrix([0, 10]), matrix([0, 5, 10], "wrongservice"),
        ]
        nan = matrix([0, 5, 10])
        nan["data"]["result"][0]["values"][1][1] = "NaN"
        invalid.append(nan)
        for page in invalid:
            with self.assertRaises(ValueError):
                ex.validate_metrics(page, {0, 5, 10}, ["checkoutservice"])

    def test_rejected_response_is_preserved_but_not_cached_as_success(self):
        with self.fake_backend(lambda p, q: {"status": "success", "data": {"resultType": "matrix", "result": []}}):
            with self.assertRaises(RuntimeError):
                self.store.request("http://fake/empty", {}, lambda d: ex.validate_metrics(d, {0}, []))
        self.assertFalse((self.store.state / "responses").exists())
        self.assertEqual(len(list((self.store.state / "rejected").glob("*.json"))), 1)

    def test_metric_submillisecond_window_uses_prometheus_millisecond_grid(self):
        seen = []
        def backend(path, q):
            seen.append(float(q["start"]))
            return matrix([float(q["start"])])
        with patch.dict(ex.PROM_EXPRESSIONS, {"container_cpu_usage_rate": "cpu"}, clear=True), self.fake_backend(backend):
            r = ex.export_metrics(self.store, "http://fake", 123456000, 123999000, ["checkoutservice"], [])
        self.assertEqual(seen, [0.123])
        self.assertEqual(r["container_cpu_usage_rate"]["status"], "ok")

    def test_metric_partial_resume_does_not_overwrite_prior_file(self):
        dest = self.raw / "metrics/container_cpu_usage_rate.json"
        ex.atomic_write(dest, "original evidence")
        seen = []
        def backend(path, q):
            start, end = int(float(q["start"])), int(float(q["end"]))
            seen.append(start)
            if start == 300:
                raise TimeoutError("missing tail")
            return matrix(range(start, end + 1, 5))
        with patch.dict(ex.PROM_EXPRESSIONS, {"container_cpu_usage_rate": "cpu"}, clear=True):
            with self.fake_backend(backend):
                r = ex.export_metrics(self.store, "http://fake", 0, 310 * 10**9, ["checkoutservice"], [])
            self.assertEqual(dest.read_text(), "original evidence")
            self.assertEqual(r["container_cpu_usage_rate"]["status"], "error")
            seen.clear()
            def retry(path, q):
                seen.append(int(float(q["start"])))
                return matrix(range(int(float(q["start"])), int(float(q["end"])) + 1, 5))
            with self.fake_backend(retry):
                r = ex.export_metrics(self.store, "http://fake", 0, 310 * 10**9, ["checkoutservice"], [])
        self.assertEqual(seen, [300])
        self.assertEqual(len(json.loads(dest.read_text())["data"]["result"][0]["values"]), 63)
        self.assertTrue(any(p.read_text() == "original evidence" for p in (dest.parent / "_history").iterdir()))

    def test_loki_resume_fetches_only_gap_and_preserves_original(self):
        dest = self.raw / "logs.ndjson"
        dest.write_text("prior evidence")
        seen = []
        def backend(path, q):
            lo = int(q["start"])
            seen.append(lo)
            if lo == 20 * 10**9:
                raise TimeoutError("failed chunk")
            return streams([(lo, "log")])
        with self.fake_backend(backend):
            r = ex.export_loki(self.store, "http://fake", 0, 40 * 10**9, "logs", "query")
        self.assertEqual(dest.read_text(), "prior evidence")
        self.assertEqual(len(r["failed_chunks"]), 1)
        seen.clear()
        def retry(path, q):
            seen.append(int(q["start"]))
            return streams([(int(q["start"]), "log")])
        with self.fake_backend(retry):
            r = ex.export_loki(self.store, "http://fake", 0, 40 * 10**9, "logs", "query")
        self.assertEqual(seen, [20 * 10**9])
        self.assertEqual(r["lines"], 2)
        self.assertEqual(len(dest.read_text().splitlines()), 2)
        self.assertTrue(any(p.read_text() == "prior evidence" for p in (dest.parent / "_history").iterdir()))

    def test_loki_half_open_boundaries_no_duplicates_or_losses(self):
        entries = [(-1, "outside-before"), (0, "a"), (20 * 10**9 - 1, "b"), (20 * 10**9, "c"), (40 * 10**9 - 1, "d"), (40 * 10**9, "outside-after")]
        def backend(path, q):
            return streams([(t, s) for t, s in entries if int(q["start"]) <= t < int(q["end"])])
        with self.fake_backend(backend):
            r = ex.export_loki(self.store, "http://fake", 0, 40 * 10**9, "logs", "query")
        self.assertEqual(r["lines"], 4)
        exported = [tuple(entry) for line in (self.raw / "logs.ndjson").read_text().splitlines()
                    for stream in json.loads(line)["data"]["result"] for entry in stream["values"]]
        self.assertEqual(exported, [(str(t), s) for t, s in entries[1:-1]])

    def test_loki_configurable_chunks_keep_half_open_boundaries(self):
        seen = []
        def backend(path, q):
            seen.append((int(q["start"]), int(q["end"])))
            return streams()
        with self.fake_backend(backend):
            ex.export_loki(self.store, "http://fake", 0, 12 * 10**9, "events", "query", chunk_seconds=5)
        self.assertEqual(seen, [(0, 5 * 10**9), (5 * 10**9, 10 * 10**9), (10 * 10**9, 12 * 10**9)])

    def test_loki_saturated_timestamp_never_claims_complete(self):
        with self.fake_backend(lambda p, q: streams([(0, "a"), (0, "b")])):
            r = ex.export_loki(self.store, "http://fake", 0, 1, "logs", "query", limit=2)
        self.assertTrue(r["failed_chunks"])
        self.assertFalse((self.raw / "logs.ndjson").exists())

    def test_empty_logs_and_missing_expected_sentinel_are_incomplete(self):
        with self.fake_backend(lambda p, q: streams()):
            logs = ex.export_loki(self.store, "http://fake", 0, 10, "logs", "logs")
            events = ex.export_loki(self.store, "http://fake", 0, 10, "events", "events", expected_text="sentinel-1")
        self.assertTrue(ex.partial("logs", logs))
        self.assertTrue(ex.partial("events", events))

    def test_loki_limit_splits_until_all_entries_are_exported(self):
        entries = [(i, str(i)) for i in range(8)]
        def backend(path, q):
            lo, hi, limit = int(q["start"]), int(q["end"]), int(q["limit"])
            return streams([(t, s) for t, s in entries if lo <= t < hi][:limit])
        with self.fake_backend(backend):
            result = ex.export_loki(self.store, "http://fake", 0, 8, "logs", "query", limit=3)
        self.assertEqual(result["lines"], 8)
        self.assertFalse(result["failed_chunks"])
        exported = [tuple(entry) for line in (self.raw / "logs.ndjson").read_text().splitlines()
                    for stream in json.loads(line)["data"]["result"] for entry in stream["values"]]
        self.assertEqual(exported, [(str(t), s) for t, s in entries])

    def test_cli_watchdog_terminates_worker(self):
        proc = SimpleNamespace(wait=unittest.mock.Mock(side_effect=[ex.subprocess.TimeoutExpired("worker", 16), 1]), kill=unittest.mock.Mock())
        argv = ["export-telemetry.py", "--run-id", "test", "--start", "2020-01-01T00:00:00Z", "--end", "2020-01-01T00:01:00Z", "--budget-seconds", "1"]
        with patch.object(sys, "argv", argv), patch.object(ex.subprocess, "Popen", return_value=proc):
            self.assertEqual(ex.main(), 1)
        proc.kill.assert_called_once()

    def test_environment_command_failure_does_not_write_empty_yaml(self):
        with patch.object(ex.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="offline")):
            r = ex.export_environment(self.store)
        self.assertTrue(ex.partial("environment", r))
        self.assertFalse((self.raw / "environment/deployment.yaml").exists())

    def test_environment_resume_keeps_first_snapshot(self):
        with patch.object(ex.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="kind: List\nitems: []\n", stderr="")):
            first = ex.export_environment(self.store)
        with patch.object(ex.subprocess, "run", side_effect=AssertionError("must reuse original snapshot")):
            second = ex.export_environment(self.store)
        self.assertEqual(first, second)

    def test_exact_timezone_conversion(self):
        self.assertEqual(ex.parse_time("2026-09-21T16:00:00.123456+09:00"), ex.parse_time("2026-09-21T07:00:00.123456Z"))
        with self.assertRaises(ValueError):
            ex.parse_time("2026-09-21T07:00:00")

    def test_full_pipeline_writes_failure_then_resumes(self):
        run_dir = Path(self.tmp.name) / "runs/test"
        run_dir.mkdir(parents=True)
        (run_dir / "transaction-results.jsonl").write_text(json.dumps({"run_id": "test", "trace_id": TID, "event_time": "2020-01-01T00:00:01Z"}) + "\n")
        (run_dir / "run-manifest.yaml").write_text("release:\n  traced_services: [checkoutservice]\n  untraced_services: []\n")
        args = SimpleNamespace(run_id="test", runs_dir=str(run_dir.parent), raw_subdir="raw", start="2020-01-01T00:00:00Z", end="2020-01-01T00:00:40Z", tempo_url="http://fake", prometheus_url="http://fake", loki_url="http://fake", settle_seconds=60, budget_seconds=60)
        bad_logs = True
        def backend(path, q):
            if path.endswith("/search"):
                return {"traces": [{"traceID": TID}]}
            if "/traces/" in path:
                return trace()
            if path == "/api/v1/query_range":
                return matrix(range(int(float(q['start'])), int(float(q['end'])) + 1, 5))
            if bad_logs and int(q['start']) == ex.parse_time(args.start) + 20 * 10**9:
                raise TimeoutError("log gap")
            return streams([(int(q['start']), "record")])
        with self.fake_backend(backend), patch.object(ex.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="kind: List\nitems: []\n", stderr="")), patch.object(ex.time, "sleep"), redirect_stdout(StringIO()):
            self.assertEqual(ex.run(args), 1)
            m = json.loads((run_dir / "raw/export-manifest.json").read_text())
            self.assertFalse(m["complete"])
            self.assertTrue(m["logs"]["failed_chunks"])
            bad_logs = False
            self.assertEqual(ex.run(args), 0)
        m = json.loads((run_dir / "raw/export-manifest.json").read_text())
        self.assertTrue(m["complete"])
        self.assertGreater(m["cache_hits"], 0)
        self.assertEqual(len(list((run_dir / "raw/_export").glob('attempt-*.json'))), 2)


if __name__ == "__main__":
    unittest.main()
