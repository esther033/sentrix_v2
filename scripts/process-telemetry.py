"""Post-run export and isolated derived views; safe to retry without new load."""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import uuid

from telemetry_export import save_json, exclusive_export
from view_integrity import file_hash, safe_name

SCRIPTS = Path(__file__).resolve().parent


def process(run_id, runs_dir, generation="v2", export_budget_seconds=3600, trace_workers=1, runner=subprocess.run):
    safe_name(run_id)
    safe_name(generation)
    if export_budget_seconds <= 0:
        raise ValueError("export_budget_seconds must be positive")
    with exclusive_export(Path(runs_dir) / run_id / f"pipeline-lock-{generation}"):
        return process_locked(run_id, runs_dir, generation, export_budget_seconds, trace_workers, runner)


def process_locked(run_id, runs_dir, generation, export_budget_seconds, trace_workers, runner):
    for name in (run_id, generation):
        safe_name(name)
    if trace_workers <= 0:
        raise ValueError("trace_workers must be positive")
    run = Path(runs_dir) / run_id
    manifest_path = run / f"pipeline-{generation}.json"
    raw = f"raw-{generation}"
    attempt = uuid.uuid4().hex[:12]
    canonical = f"canonical-{generation}-{attempt}"
    re2 = f"re2-{generation}-{attempt}"
    state = {"schema_version": 1, "run_id": run_id, "generation": generation,
             "complete": False, "views": {"raw": raw, "canonical": canonical, "re2_compatible": re2},
             "steps": {}, "hashes": {}}
    save_json(manifest_path, state, preserve=True)
    try:
        transactions = [json.loads(line) for line in (run / "transaction-results.jsonl").read_text(encoding="utf-8").splitlines() if line]
        if not transactions or any(r.get("run_id") != run_id for r in transactions):
            raise ValueError("Missing/mismatched transaction run IDs")
        starts = [datetime.fromisoformat(r["event_time"].replace("Z", "+00:00")) for r in transactions]
        if any(t.tzinfo is None for t in starts):
            raise ValueError("Transaction timestamps must include a timezone")
        start = min(starts) - timedelta(seconds=15)
        end = max(t + timedelta(milliseconds=float(r.get("duration_ms", 0))) for t, r in zip(starts, transactions)) + timedelta(seconds=60)
        commands = [
            ("export", "export-telemetry.py", ["--raw-subdir", raw, "--start", start.isoformat(), "--end", end.isoformat(),
                                                   "--budget-seconds", str(export_budget_seconds), "--trace-workers", str(trace_workers)], export_budget_seconds + 30),
            ("canonical", "canonicalize.py", ["--raw-subdir", raw, "--output-subdir", canonical], 300),
            ("re2", "re2-projection.py", ["--canonical-subdir", canonical, "--output-subdir", re2], 300),
        ]
        for name, script, args, timeout in commands:
            if name == "re2" and state["steps"].get("canonical") != 0:
                state["steps"][name] = "skipped: canonical failed"
                continue
            try:
                rc = runner([sys.executable, "-B", str(SCRIPTS / script), "--run-id", run_id,
                             "--runs-dir", str(runs_dir), *args], timeout=timeout, check=False).returncode
                state["steps"][name] = rc
            except Exception as exc:
                state["steps"][name] = str(exc)
            save_json(manifest_path, state, preserve=True)
        export = json.loads((run / raw / "export-manifest.json").read_text(encoding="utf-8"))
        projection = json.loads((run / re2 / "projection-summary.json").read_text(encoding="utf-8"))
        state["raw_export_complete"] = export.get("complete") is True
        state["features_complete"] = (projection.get("metrics", {}).get("rows", 0) > 0
                                      and projection["metrics"].get("missing_feature_sources") == []
                                      and projection["metrics"].get("missing_service_columns") == [])
        required = ["transaction-results.jsonl", "run-manifest.yaml", f"{raw}/export-manifest.json",
                    f"{canonical}/canonicalize-summary.json", f"{re2}/projection-summary.json"]
        required += [f"{directory}/{name}.parquet" for directory in (canonical, re2) for name in ("metrics", "traces", "logs")]
        required += [p.relative_to(run).as_posix() for p in (run / raw).rglob("*") if p.is_file()
                     and "_export" not in p.parts and "_history" not in p.parts]
        state["hashes"] = {p: file_hash(run / p) for p in required}
        state["complete"] = (all(state["steps"].get(s) == 0 for s in ("export", "canonical", "re2"))
                             and state["raw_export_complete"] and state["features_complete"])
    except Exception as exc:
        state["error"] = str(exc)
    save_json(manifest_path, state, preserve=True)
    return 0 if state["complete"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--runs-dir", default=str(SCRIPTS.parent / "runs"))
    ap.add_argument("--generation", default="v2")
    ap.add_argument("--export-budget-seconds", type=int, default=3600)
    ap.add_argument("--trace-workers", type=int, default=1)
    args = ap.parse_args()
    raise SystemExit(process(args.run_id, args.runs_dir, args.generation, args.export_budget_seconds, args.trace_workers))
