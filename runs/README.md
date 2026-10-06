# Experiment data

The full local `runs/` corpus is intentionally not stored in GitHub. At the
2026-10-06 inventory point it occupied approximately 27.56 GiB and contained
raw telemetry responses, canonical Parquet files, RE2-compatible projections,
checkpoints, and prior attempts.

See [`../docs/DATA-EXTRACTION-INVENTORY.md`](../docs/DATA-EXTRACTION-INVENTORY.md)
for the accepted run IDs, record counts, data lineage, exclusions, and known
limitations. Incident labels and final validity decisions are versioned under
[`../incidents/`](../incidents/) and [`../audits/m2-closeout/status.json`](../audits/m2-closeout/status.json).

To regenerate new run artifacts, deploy the stack and use the scripts described
in [`../docs/TELEMETRY_PIPELINE.md`](../docs/TELEMETRY_PIPELINE.md). Do not mix
regenerated data with the documented 2026-09-26 corpus without recording a new
data generation and provenance.
