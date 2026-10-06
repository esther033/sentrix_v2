"""Offline generation isolation and failure propagation tests."""
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from test_data_views import module
from view_integrity import validate_views

pipeline = module("process-telemetry")
attended = module("run-attended")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "test"
        self.run.mkdir()
        (self.run / "transaction-results.jsonl").write_text(json.dumps({"run_id": "test", "event_time": "2020-01-01T00:00:00Z", "duration_ms": 2000})+'\n')
        (self.run / "run-manifest.yaml").write_text("run_id: test")
        self.failed = None
        self.calls = []
        self.commands = []

    def runner(self, cmd, **kwargs):
        script = Path(cmd[2]).name
        self.calls.append(script)
        self.commands.append(cmd)
        def arg(key):
            return cmd[cmd.index(key) + 1]
        if script == "export-telemetry.py":
            folder = self.run / arg("--raw-subdir")
            folder.mkdir(exist_ok=True)
            (folder / "export-manifest.json").write_text(json.dumps({"schema_version": 2, "complete": self.failed != "export"}))
            self.assertEqual(arg("--end"), "2020-01-01T00:01:02+00:00")
            return SimpleNamespace(returncode=1 if self.failed == "export" else 0)
        folder = self.run / arg("--output-subdir")
        folder.mkdir()
        if script == "canonicalize.py" and self.failed == "canonical":
            return SimpleNamespace(returncode=1)
        for name in ("traces", "metrics", "logs"):
            (folder / (name + ".parquet")).write_bytes(b"fixture bytes")
        summary = "canonicalize-summary.json" if script == "canonicalize.py" else "projection-summary.json"
        (folder / summary).write_text(json.dumps({"metrics": {"rows": 1, "missing_feature_sources": [], "missing_service_columns": []}}))
        return SimpleNamespace(returncode=0)

    def test_failed_export_stays_invalid_and_retry_uses_new_derived_views(self):
        self.failed = "export"
        self.assertEqual(pipeline.process("test", self.root, runner=self.runner), 1)
        first = json.loads((self.run / "pipeline-v2.json").read_text())
        self.assertFalse(validate_views(self.run)["complete"])
        self.failed = None
        self.assertEqual(pipeline.process("test", self.root, runner=self.runner), 0)
        second = json.loads((self.run / "pipeline-v2.json").read_text())
        self.assertNotEqual(first["views"]["canonical"], second["views"]["canonical"])
        self.assertTrue((self.run / first["views"]["canonical"] / "metrics.parquet").exists())
        self.assertTrue(validate_views(self.run)["complete"])
        (self.run / second["views"]["re2_compatible"] / "metrics.parquet").write_bytes(b"changed")
        self.assertFalse(validate_views(self.run)["complete"])

    def test_canonical_failure_skips_re2_and_never_uses_old_outputs(self):
        self.failed = "canonical"
        self.assertEqual(pipeline.process("test", self.root, runner=self.runner), 1)
        self.assertNotIn("re2-projection.py", self.calls)
        self.assertFalse(validate_views(self.run)["complete"])

    def test_legacy_complete_flag_is_not_a_valid_generation(self):
        (self.run / "raw").mkdir()
        (self.run / "raw/export-manifest.json").write_text('{"complete":true}')
        self.assertFalse(validate_views(self.run)["complete"])

    def test_postprocessing_wall_clock_gap_is_explicit(self):
        self.assertEqual(attended.observation_gap_seconds(100.0, 102.0), 2.0)
        self.assertEqual(attended.observation_gap_seconds(100.0, 99.0), 0.0)
        self.assertGreater(attended.observation_gap_seconds(100.0, 116.0), 15.0)

    def test_trace_worker_count_is_forwarded_to_export(self):
        self.assertEqual(pipeline.process("test", self.root, trace_workers=2, runner=self.runner), 0)
        export = next(cmd for cmd in self.commands if Path(cmd[2]).name == "export-telemetry.py")
        self.assertEqual(export[export.index("--trace-workers") + 1], "2")

    def test_real_conversion_commands_use_selected_raw_generation(self):
        import subprocess
        import pandas as pd
        (self.run / "run-manifest.yaml").write_text("release:\n  traced_services: [checkoutservice]\n  untraced_services: []\n")
        def runner(cmd, **kwargs):
            if Path(cmd[2]).name != "export-telemetry.py":
                return subprocess.run(cmd, capture_output=True, **kwargs)
            raw = self.run / cmd[cmd.index("--raw-subdir") + 1]
            (raw / "metrics").mkdir(parents=True)
            (raw / "traces").mkdir()
            labels = {"service_name": "checkoutservice", "service_namespace": "sentrix", "container": "server"}
            groups = {"container_cpu_usage_rate": .2, "container_memory_working_set_bytes": 100,
                      "spanmetrics_calls_rate": 4, "spanmetrics_error_increase_30s": 2.5}
            for name, value in groups.items():
                (raw / "metrics" / (name + ".json")).write_text(json.dumps({"data": {"result": [{"metric": labels, "values": [[0, value], [5, value]]}]}}))
            (raw / "metrics/spanmetrics_duration_bucket_rate.json").write_text(json.dumps({"data": {"result": [
                {"metric": {**labels, "le": bound}, "values": [[0, value], [5, value]]}
                for bound, value in (("100", 2), ("200", 8), ("+Inf", 10))]}}))
            attrs = [{"key": "service.name", "value": {"stringValue": "checkoutservice"}},
                     {"key": "service.namespace", "value": {"stringValue": "sentrix"}}]
            (raw / "traces" / ("a" * 32 + ".json")).write_text(json.dumps({"batches": [{"resource": {"attributes": attrs}, "scopeSpans": [{"spans": [
                {"traceId": "a" * 32, "spanId": "b" * 16, "name": "test", "startTimeUnixNano": "1000000", "endTimeUnixNano": "2000000"}]}]}]}))
            (raw / "logs.ndjson").write_text(json.dumps({"data": {"result": [{"stream": labels, "values": [["1", "log"]]}]}})+'\n')
            (raw / "export-manifest.json").write_text('{"schema_version":2,"complete":true}')
            return SimpleNamespace(returncode=0)
        self.assertEqual(pipeline.process("test", self.root, runner=runner), 0)
        status = validate_views(self.run)
        self.assertTrue(status["complete"])
        out = pd.read_parquet(self.run / status["views"]["re2_compatible"] / "metrics.parquet")
        self.assertAlmostEqual(out["checkoutservice_latency-50"].iloc[0], .15)
        self.assertEqual(out["checkoutservice_error"].iloc[0], 2.5)


if __name__ == "__main__":
    unittest.main()
