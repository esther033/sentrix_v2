#!/usr/bin/env python3
"""
Builds runs/<run_id>/run-manifest.yaml, the single file that joins
workload config, transaction results, telemetry export locations,
release/version metadata, and the quality report for one run_id.

Image digests are read from `kubectl get pod ... imageID` on the actually
running Pods AFTER deploy -- never guessed from the manifest's image tag
before rollout. Source commit SHA (what we asked Kubernetes to run) and
image digest (what actually got pulled and is running) are stored
separately; they answer different questions and can legitimately diverge
if a tag was repointed between build and pull.
"""
import argparse
import json
import os
import subprocess
from datetime import datetime, timezone

import yaml

ONLINE_BOUTIQUE_SOURCE = {
    "repo": "https://github.com/GoogleCloudPlatform/microservices-demo",
    "ref": "v0.9.0",
    "commit_sha": "d138db567079e2ef982d46b2993f8349cb18e2b2",
}

TRACED_SERVICES = [
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "frontend",
    "paymentservice",
    "productcatalogservice",
    "recommendationservice",
]
UNTRACED_SERVICES = ["adservice", "cartservice", "shippingservice", "redis-cart"]
ALL_SERVICES = TRACED_SERVICES + UNTRACED_SERVICES


def kubectl_image_digest(deployment, namespace="sentrix"):
    proc = subprocess.run(
        [
            "kubectl",
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            f"app={deployment}",
            "-o",
            "jsonpath={.items[0].status.containerStatuses[0].imageID}",
        ],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--profile", required=True, help="path to workload profile JSON")
    ap.add_argument("--namespace", default="sentrix")
    ap.add_argument(
        "--runs-dir", default=os.path.join(os.path.dirname(__file__), "..", "runs")
    )
    ap.add_argument("--evaluation-policy-version", default="v1")
    args = ap.parse_args()

    run_dir = os.path.join(args.runs_dir, args.run_id)
    os.makedirs(run_dir, exist_ok=True)

    with open(args.profile, "r", encoding="utf-8") as f:
        profile = json.load(f)

    image_digests = {}
    for svc in ALL_SERVICES:
        digest = kubectl_image_digest(svc, args.namespace)
        image_digests[svc] = digest if digest else "UNRESOLVED"

    unresolved = [s for s, d in image_digests.items() if d == "UNRESOLVED"]

    fault_record_path = os.path.join(run_dir, "fault-record.json")
    fault_injected = os.path.exists(fault_record_path)
    fault_block = {"injected": fault_injected}
    if fault_injected:
        with open(fault_record_path, "r", encoding="utf-8") as f:
            fr = json.load(f)
        fault_block.update(
            {
                "type": fr.get("type"),
                "root_cause_service": fr.get("root_cause_service"),
                "started_at": fr.get("started_at"),
                "ended_at": fr.get("ended_at"),
            }
        )

    profile_name = profile["profile_name"]
    if profile_name.startswith("m2-"):
        milestone = "M2"
    elif profile_name in ("pilot", "smoke-tiny"):
        milestone = "M1A"
    else:
        milestone = "M1B"

    manifest = {
        "run_id": args.run_id,
        "milestone": milestone,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cluster": {
            "provider": "docker-desktop-kind",
            "kubernetes_version": "v1.36.1",
            "node_count": 1,
        },
        "release": {
            "online_boutique": ONLINE_BOUTIQUE_SOURCE,
            "image_digests": image_digests,
            "image_digests_unresolved": unresolved,
            "otlp_env_patch": "deploy/online-boutique/otlp-env-patch.yaml",
            "traced_services": TRACED_SERVICES,
            "untraced_services": UNTRACED_SERVICES,
        },
        "workload": {
            "profile_path": os.path.relpath(args.profile, run_dir),
            "profile": profile,
        },
        "artifacts": {
            "transaction_results": "transaction-results.jsonl",
            "workload_timeseries": "workload-timeseries.json",
            "k6_summary": "k6-summary.json",
            "k6_raw_metrics": "k6-raw-metrics.jsonl",
            "k6_console_output": "k6-console-output.log",
            "quality_report": "quality-report.json",
        },
        "identity": {
            "alias_map": "identity/service-alias-map.yaml",
            "mapping_version": 1,
        },
        "evaluation_policy_version": args.evaluation_policy_version,
        "fault": fault_block,
    }

    out_path = os.path.join(run_dir, "run-manifest.yaml")
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(manifest, f, sort_keys=False, default_flow_style=False)

    print(f"wrote {out_path}")
    if unresolved:
        print(f"WARNING: could not resolve image digest for: {unresolved}")


if __name__ == "__main__":
    main()
