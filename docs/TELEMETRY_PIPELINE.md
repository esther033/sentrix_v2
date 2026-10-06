# Resumable post-run pipeline

Use `python scripts/process-telemetry.py --run-id <run_id>` after a workload
has stopped. It does not run k6 or inject a fault. Both experiment entry
points invoke this shared pipeline. Do not rerun the experiment entry point
to repair exports; an existing run directory is refused.

The default export generation is `raw-v2`. Export retries reuse that
generation's checkpoints. Every conversion attempt creates new
`canonical-v2-<attempt>` and `re2-v2-<attempt>` directories. Prior outputs
remain available. `pipeline-v2.json` points to the latest attempt; previous
reports are archived under `_history`. One process per generation is allowed.

Changing export query definitions or window requires a new `--generation`
value. Existing checkpoint context is never silently reinterpreted. For
example use `--generation v3`, then pass `--data-generation v3` to the
incident generator (and `--control-data-generation` for the control).

The pipeline records nonzero export/conversion results, missing feature
sources and missing service columns. A canonical failure skips projection;
it cannot accidentally consume a prior canonical directory. Partial raw
conversion is useful diagnostic evidence but cannot produce a complete
pipeline. SHA-256 records bind source files and derived files to the selected
attempt. Incident generation validates those hashes for both fault and
control runs. Missing legacy manifests and legacy `complete: true` flags do
not qualify. Invalid incident reports are still saved, with exit code 1.

New metric definitions are in RE2_SCHEMA.md. Offline tests exercise actual
canonical/projection commands with synthetic raw responses, exact numerical
fixtures, retries, missing inputs, and artifact changes. They do not execute
PromQL against a running Prometheus server, prove scrape completeness, or
validate fault removal/recovery policy. Those checks remain necessary before
accepting a fresh incident dataset.

Existing run artifacts have not been overwritten or upgraded. An old raw
export without the new rate/error queries cannot acquire those signals
through conversion; expired backend data cannot be recovered this way.
