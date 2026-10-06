"""Offline numerical and identity regression checks; temporary outputs only."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

import pandas as pd

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


re2 = module("re2-projection")
canonical = module("canonicalize")


class DataViewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def metric(self, ts, value, container="server", group="container_cpu_usage_rate"):
        return dict(timestamp_unix=ts, value=value, canonical_service_id="checkoutservice",
                    metric_group=group, labels_json=json.dumps({"container": container}))

    def project(self, rows):
        pd.DataFrame(rows).to_parquet(self.root / "metrics.parquet")
        re2.project_metrics(self.root, self.root / "out.parquet")
        return pd.read_parquet(self.root / "out.parquet")

    def test_resource_sum_excludes_pause_and_pod_totals(self):
        rows = [self.metric(0, .2), self.metric(0, .1, "sidecar"),
                self.metric(0, .3, ""), self.metric(0, .01, "POD")]
        out = self.project(rows)
        self.assertAlmostEqual(out.checkoutservice_cpu.iloc[0], 30)

    def test_memory_and_workload_sum(self):
        rows = [self.metric(0, 100, group="container_memory_working_set_bytes"),
                self.metric(0, 20, "sidecar", "container_memory_working_set_bytes"),
                self.metric(0, 2, group="spanmetrics_calls_rate"),
                self.metric(0, 3, group="spanmetrics_calls_rate")]
        out = self.project(rows)
        self.assertEqual(out.checkoutservice_mem.iloc[0], 120)
        self.assertEqual(out.checkoutservice_workload.iloc[0], 5)

    def test_missing_native_interval_stays_missing(self):
        out = self.project([self.metric(.5, .2), self.metric(10.5, .3)])
        self.assertEqual(out.time.iloc[0], 1)
        self.assertEqual(out.checkoutservice_cpu.iloc[0], 20)
        self.assertTrue(out.loc[out.time.between(6, 10), "checkoutservice_cpu"].isna().all())

    def test_unrecognized_metric_produces_zero_rows(self):
        pd.DataFrame([self.metric(0, 1, group="unknown")]).to_parquet(self.root / "metrics.parquet")
        self.assertEqual(re2.project_metrics(self.root, self.root / "out.parquet"), {"rows": 0})

    def test_trace_hex_and_grpc_status_are_distinct_from_otel(self):
        import base64
        tid, sid = "ab" * 16, "12" * 8
        row = dict(canonical_service_id="checkoutservice", trace_id=tid,
                   span_id=base64.b64encode(bytes.fromhex(sid)).decode(), parent_span_id=None,
                   start_time_unix_nano=1_000_000_000, end_time_unix_nano=1_002_000_000,
                   span_name="test", status_code=2,
                   span_attributes_json=json.dumps({"rpc.grpc.status_code": 14}))
        pd.DataFrame([row, dict(row, span_attributes_json="{}")]).to_parquet(self.root / "traces.parquet")
        re2.project_traces(self.root, self.root / "out.parquet")
        out = pd.read_parquet(self.root / "out.parquet")
        self.assertEqual(out.spanID.iloc[0], sid)
        self.assertEqual(out.statusCode.iloc[0], 14)
        self.assertTrue(pd.isna(out.statusCode.iloc[1]))
        self.assertEqual(out.duration.iloc[0], 2000)

    def test_invalid_timestamp_span_is_preserved_in_canonical_but_excluded_from_re2(self):
        row = dict(canonical_service_id="checkoutservice", trace_id="ab" * 16,
                   span_id="12" * 8, parent_span_id=None,
                   start_time_unix_nano=2_000, end_time_unix_nano=1_999,
                   timestamp_valid=False, span_name="bad",
                   span_attributes_json="{}")
        pd.DataFrame([row]).to_parquet(self.root / "traces.parquet")
        summary = re2.project_traces(self.root, self.root / "out.parquet")
        self.assertEqual(summary, {"rows": 0, "excluded_invalid_timestamp_spans": 1})
        self.assertFalse((self.root / "out.parquet").exists())

    def test_typed_attributes_preserved(self):
        self.assertEqual(canonical.otel_value({"boolValue": False}), False)
        self.assertEqual(canonical.otel_value({"doubleValue": 1.25}), 1.25)
        self.assertEqual(canonical.otel_value({"intValue": "42"}), 42)
        self.assertEqual(canonical.otel_value({"arrayValue": {"values": [{"boolValue": True}]}}), [True])

    def test_latency_interpolation_and_windowed_errors(self):
        rows = []
        for bound, count in (("100", 2), ("200", 8), ("+Inf", 10)):
            row = self.metric(0, count, group="spanmetrics_duration_bucket_rate")
            row["labels_json"] = json.dumps({"le": bound})
            rows.append(row)
        rows.append(self.metric(0, 2.5, group="spanmetrics_error_increase_30s"))
        out = self.project(rows)
        self.assertAlmostEqual(out["checkoutservice_latency-50"].iloc[0], .15)
        self.assertAlmostEqual(out["checkoutservice_latency-90"].iloc[0], .2)
        self.assertEqual(out.checkoutservice_error.iloc[0], 2.5)

    def test_empty_histogram_is_unknown_and_malformed_is_rejected(self):
        import math
        self.assertTrue(math.isnan(re2.histogram_quantile([(100, 0), (math.inf, 0)], .5)))
        for buckets in ([(100, 2)], [(100, 2), (math.inf, 1)], [(100, 1), (100, 2), (math.inf, 3)]):
            with self.assertRaises(ValueError):
                re2.histogram_quantile(buckets, .5)

    def test_trimmed_trace_file_alias_does_not_duplicate_spans(self):
        folder = self.root / "traces"
        folder.mkdir()
        tid = "0" + "a" * 31
        span = {"traceId": tid, "spanId": "b" * 16, "name": "test"}
        data = {"batches": [{"scopeSpans": [{"spans": [span]}]}]}
        for name in (tid, tid[1:]):
            (folder / (name + ".json")).write_text(json.dumps(data))
        result = canonical.canonicalize_traces(self.root, {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result.trace_id.iloc[0], tid)
        span["name"] = "conflicting snapshot"
        (folder / (tid + ".json")).write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            canonical.canonicalize_traces(self.root, {})


if __name__ == "__main__":
    unittest.main()
