# Feature and pipeline checkpoint — 2026-09-22

Implemented trailing 30-second span histogram rates and error increases in
the raw exporter; added latency-50/90 (seconds) and estimated error-count
projection. Definitions and unit limitations are in docs/RE2_SCHEMA.md.

Both run entry points now use process-telemetry.py. Export generations are
explicit, conversion attempts isolated, source/output hashes checked, and
failed steps cannot consume an older derived directory. Incident validity
requires the selected fault and control generations to pass these checks.
Existing incident output is archived before replacement by the CLI.

Validation: 40 distinct offline tests passed. Includes actual canonical and
RE2 CLI subprocesses over synthetic raw fixtures; percentile interpolation
and units; malformed/empty histograms; missing series points; failed export
then retry; failed canonical stage; artifact modification; legacy success
flag rejection. Changed Python scripts passed syntax parsing and Git's
whitespace/error check passed.

No workload, chaos operation, live export, or replacement of existing run
data was performed. PromQL server execution and actual collection remain
unverified. Older raw files lack the new query outputs; they were not
silently labeled feature-complete. Recovery policy and control environment/
workload matching still require their own repair before a fresh pilot.
