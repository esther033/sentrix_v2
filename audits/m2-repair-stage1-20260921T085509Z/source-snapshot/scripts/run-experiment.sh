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

# Raw/canonical/RE2-compatible export runs immediately after the quality
# gate, in the same invocation -- not as a separate manual step -- so it
# happens before any backend's retention window (Tempo 1h, Prometheus 6h)
# can expire. Window is the transaction file's own [min, max] event_time
# with a small margin for trailing async telemetry (collector batching,
# Loki ingest lag).
WINDOW=$(python - "$ROOT_DIR/runs/$RUN_ID/transaction-results.jsonl" <<'PYEOF'
import json, sys
from datetime import datetime, timedelta, timezone
times = []
with open(sys.argv[1]) as f:
    for line in f:
        times.append(json.loads(line)["event_time"])
start = min(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times) - timedelta(seconds=15)
end = max(datetime.fromisoformat(t.replace("Z", "+00:00")) for t in times) + timedelta(seconds=60)
print(start.isoformat())
print(end.isoformat())
PYEOF
)
WINDOW_START=$(echo "$WINDOW" | sed -n 1p)
WINDOW_END=$(echo "$WINDOW" | sed -n 2p)

echo "[run-experiment] exporting raw telemetry for window $WINDOW_START .. $WINDOW_END"
python "$ROOT_DIR/scripts/export-telemetry.py" \
  --run-id "$RUN_ID" --start "$WINDOW_START" --end "$WINDOW_END"

python "$ROOT_DIR/scripts/canonicalize.py" --run-id "$RUN_ID"
python "$ROOT_DIR/scripts/re2-projection.py" --run-id "$RUN_ID"

echo "[run-experiment] done (quality_gate_exit=$QUALITY_RC). See $ROOT_DIR/runs/$RUN_ID/"
exit $QUALITY_RC
