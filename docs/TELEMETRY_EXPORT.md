# Raw telemetry export v2 — stage 2 repair

This stage changes the exporter only. Canonical/RE2 transformations, incident
classification and the fault-run supervisor still require their separate repair
stages. Do not start another formal fault run on the strength of these tests.

## Preservation and resume

`scripts/export-telemetry.py` remains the CLI entry point. Implementation lives in
`scripts/telemetry_export.py`.

- A new run can use the default `raw/` destination. A legacy `raw/` directory is
  refused, not overwritten. Re-export into a separate generation with
  `--raw-subdir raw-v2`. Existing canonicalization scripts do not yet select that
  alternate directory; downstream integration is deferred to the next stage.
- `raw/_export/context.json` fixes run ID, window, backend URLs, transaction trace
  ID digest, query expressions, required services and expected gate sentinel.
  Resume with exactly the same inputs. A changed context requires a new directory.
- Each validated API response is saved with the original request and a SHA-256
  digest under `_export/responses/`. Successful responses survive interruption and
  are reused; failed requests are retried. Invalid/partial API responses are kept
  under `_export/rejected/` but never treated as successful checkpoints.
- Existing files in a legacy output directory are never imported by filename as
  proof of completeness. Reusing or repairing legacy partial exports requires a
  separate, explicit migration with provenance checks.
- Final metric and Loki files are published only when that signal's requested
  chunks pass. Partial successful responses remain available in checkpoints.
  Publication uses a same-directory temporary file, flush/fsync and atomic replace.
  Replaced materializations are archived by content hash under `_history/`.
- A per-directory OS lock prevents concurrent writers and is released if the
  process dies. Another output generation may be used independently.
- First successful environment snapshots are retained on resume. They are
  export-time snapshots, not proof of the environment during the entire run.

## Completeness checks and boundaries

Traces: split search into 60-second windows, subdivide capped results to one-second
resolution, and mark any still-capped or failed search window incomplete. Fetch
the union of search results and all known transaction trace IDs. Validate nonempty
spans, trace-ID consistency, span-ID representation and timestamps before caching.
Known transaction IDs are prioritized over background IDs during fetching.

Metrics: use five-second query evaluation steps in chunks of at most 60 points.
Reject API errors/warnings, empty responses, non-finite values and missing query
grid points for required services from the run manifest. CPU/memory require all
listed app services; spanmetrics require the traced services. A service can span
multiple Pod series during a rollout. This is query-grid coverage, not validation
of every label combination or every original Prometheus scrape. PromQL lookback
and `rate()` can mask missing source samples. No new CPU aggregation is done here.
Prometheus query timestamps are aligned down to milliseconds; original responses
and query parameters are retained.

Logs are queried in five-second half-open windows `[start, end)` to keep
high-volume workload responses below the Loki gateway timeout; events retain
20-second windows. Either signal subdivides any response at the result limit. A capped one-nanosecond interval is incomplete,
never silently accepted. Namespace logs must not be entirely empty. If a PASS
quality-gate sentinel falls inside the export window, its name must appear in the
events export. Without an expected sentinel, an empty event result alone does not
prove a failure (an otherwise quiet run need not generate a Kubernetes Event).
The boundaries follow the [Loki HTTP API](https://grafana.com/docs/loki/latest/reference/loki-http-api/).

`complete: true` means these API-response/known-ID/query-grid checks passed.
It **does not prove that every emitted span/log was ingested** or that every trace
is structurally complete. A successfully fetched trace may still have missing or
late spans. Successful response caching freezes this export generation's observed
responses, not an assumption that backend trace contents are immutable. To inspect
later-arriving data, use another generation and compare it explicitly.

## Deadlines and progress

- Every network operation has a socket timeout and at most three attempts.
- `--budget-seconds` defaults to 900 (15 minutes). The CLI runs a worker with a
  wall-clock watchdog (budget plus 15 seconds), addressing slow trickling responses
  as well as ordinary socket timeouts. Resume after a timeout; do not wait forever
  for a final file. Host suspend can delay any local timer until the host resumes.
- The requested end must be in the past by `--settle-seconds` (default 60) before
  queries begin. A bounded settling wait up to 120 seconds accommodates the
  existing orchestrator's post-run padding. This wait consumes the budget. Far
  future windows are rejected, not silently shortened.
- `export-manifest.json` is written with `complete: false` before the steps and is
  updated after each step. `_export/progress.json` identifies the current request
  and attempt; final reports are saved as `_export/attempt-*.json`.
- Failures leave nonzero exit status and explicit gaps. A successful retry must
  finish all required steps before setting complete=true. A killed worker can leave
  a manifest saying running/pending: that is incomplete, not an active-process test.

## Offline verification

Run from the repository root:

```powershell
python -B -m unittest discover -s tests -p test_telemetry_export.py -v
```

Tests use temporary directories and mocked backend/process calls. They cover
atomic-write failure, legacy protection, context changes, OS locking, corrupt
checkpoints, retries/deadlines, known trace IDs despite failed search, search/log
caps, metric gaps/empty results, missing sentinel, nanosecond chunk boundaries,
failed-chunk-only resume and a complete failure-to-success pipeline transition.
No live cluster requests or experiment runs are made by the tests.
