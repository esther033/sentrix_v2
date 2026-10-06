# Data view repair checkpoint — 2026-09-22

This checkpoint repairs existing canonical/RE2 transformations. No cluster
commands, experiments, raw modifications, or replacement of existing run
artifacts were performed.

## Changes

- CPU/memory now sum named containers; exclude pod totals and pause containers.
- Workload is summed across exported span series, explicitly a span-rate proxy.
- One-second output uses only past samples less than five seconds old;
  missing intervals remain null.
- Trace identifiers normalized to hex; gRPC status comes from its own attribute,
  with null for absent gRPC status rather than an OTel enum substitution.
- All OTLP attribute value types decoded; original resource/span JSON retained.
- Leading-zero-trimmed legacy trace file aliases normalized, identical span
  copies collapsed, conflicting snapshots rejected for review.
- CLI output directories must be empty; explicit input/output subdirectory
  options allow later regeneration without overwriting earlier evidence.
- Documentation corrected: latency/error features are not yet materialized.

## Validation

`python -B -m unittest discover -s tests -v`: 32 tests passed (7 data view,
25 export tests). Synthetic checks cover numerical sums, cgroup exclusion,
missing intervals, ID conversion, nullable gRPC status, typed attributes,
and duplicate/conflicting trace snapshots.

Existing `m2-fastcheck-control-001/raw` was read and converted in a temporary
directory: 65,343 resolved trace rows; metric output 315 rows, 30 columns.
Every projected span ID matched 16 lowercase hex digits; statusCode was
nullable Int64. Temporary outputs were discarded; existing run artifacts
were not regenerated or relabeled valid.

## Remaining work

Latency/error feature definitions and extraction; generation selection and
raw-validity propagation through the run pipeline; recovery/control validity
logic; then a fresh short live validation. Existing RE2 artifacts retain their
old values and must not be presented as outputs of the repaired code.
