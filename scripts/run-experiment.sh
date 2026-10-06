#!/usr/bin/env bash
# Orchestrates one M1A/M1B run: runs the k6 transaction workload, generates
# the run manifest (with actually-running image digests), and runs the
# quality gate. Requires port-forwards to frontend-external (8080), tempo
# (3200), prometheus-server (9090 as :80 target), and loki-gateway (3100)
# to already be running -- this script does not manage those, since they
# are long-lived and shared across multiple runs in the same session.
#
# Usage:
#   scripts/run-experiment.sh <run_id> <profile.json> [--skip-quality-gate-b]
set -euo pipefail

RUN_ID="${1:?run_id required}"
PROFILE="${2:?profile json path required}"
SKIP_GATE_B="${3:-}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
K6_BIN="${K6_BIN:-k6}"

if [ -e "$ROOT_DIR/runs/$RUN_ID" ]; then
  echo "Run already exists; retry process-telemetry.py instead." >&2
  exit 1
fi

echo "[run-experiment] run_id=$RUN_ID profile=$PROFILE"

python "$ROOT_DIR/recorder/transaction-recorder.py" \
  --run-id "$RUN_ID" \
  --profile "$PROFILE" \
  --base-url "http://localhost:8080" \
  --k6-bin "$K6_BIN"

python "$ROOT_DIR/scripts/generate-run-manifest.py" \
  --run-id "$RUN_ID" \
  --profile "$PROFILE"

QUALITY_ARGS=(--run-id "$RUN_ID")
if [ "$SKIP_GATE_B" = "--skip-quality-gate-b" ]; then
  QUALITY_ARGS+=(--skip-gate-b)
fi

# Quality gate failure must NOT skip the export below -- a failed run's
# raw telemetry is exactly what's needed to debug why it failed, and it
# is just as subject to retention expiry as a passing run's. Capture the
# exit code and re-raise it only after export completes.
set +e
python "$ROOT_DIR/smoke/quality-check.py" "${QUALITY_ARGS[@]}"
QUALITY_RC=$?
set -e

# Shared resumable pipeline; failure remains visible in pipeline-v2.json.
set +e
python "$ROOT_DIR/scripts/process-telemetry.py" --run-id "$RUN_ID"
PIPELINE_RC=$?
set -e
if [ "$QUALITY_RC" -ne 0 ]; then
  exit "$QUALITY_RC"
fi
exit "$PIPELINE_RC"
