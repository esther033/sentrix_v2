"""Validate the selected generation rather than accepting legacy success flags."""
import hashlib
import json
from pathlib import Path
import re


def safe_name(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError("Expected a safe directory/generation name")
    return value


def file_hash(path):
    with open(path, "rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def validate_views(run_dir, generation="v2"):
    run = Path(run_dir).resolve()
    safe_name(generation)
    try:
        report = json.loads((run / f"pipeline-{generation}.json").read_text(encoding="utf-8"))
        if report.get("run_id") != run.name or report.get("generation") != generation or report.get("complete") is not True:
            raise ValueError("Missing/failed or mismatched pipeline generation")
        views = report["views"]
        for name in views.values():
            safe_name(name)
        required = {"transaction-results.jsonl", "run-manifest.yaml", f"{views['raw']}/export-manifest.json",
                    f"{views['canonical']}/canonicalize-summary.json", f"{views['re2_compatible']}/projection-summary.json"}
        required.update(f"{views[k]}/{name}.parquet" for k in ("canonical", "re2_compatible") for name in ("metrics", "traces", "logs"))
        if not required <= report.get("hashes", {}).keys():
            raise ValueError("Missing artifact hashes")
        for relative, expected in report["hashes"].items():
            path = (run / relative).resolve()
            if not path.is_relative_to(run) or file_hash(path) != expected:
                raise ValueError("Artifact missing/changed: " + relative)
        export = json.loads((run / views["raw"] / "export-manifest.json").read_text(encoding="utf-8"))
        if export.get("complete") is not True or export.get("schema_version") != 2:
            raise ValueError("Raw export incomplete or legacy schema")
        return {"complete": True, "raw_export_complete": True, "views": views}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"complete": False, "raw_export_complete": False, "error": str(exc), "views": {}}
