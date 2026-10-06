# RE2-OB schema (validated against a real sample) and SentriX's RE2-compatible projection

sentrix-project-context.md section 5 defines three data views -- raw,
canonical, and a "RE2-compatible projection" -- but does not enumerate
RE2-OB's actual columns. Before building anything that claims to be
"RE2-compatible" for Milestone 2, this schema was validated against a
real downloaded case rather than inferred from documentation, per the
instruction not to implement against an unverified definition.

## How this was validated

Downloaded `re2ob_checkoutservice_cpu_1` (repetition 1) directly from
`https://huggingface.co/datasets/phamquiluan/RCAEval` (public dataset,
Apache-licensed research benchmark) -- `metrics.parquet`, `logs.parquet`,
`traces.parquet`, `inject_time.txt` -- plus the `cases.parquet` case
index, and inspected them with pandas/pyarrow. Not committed to the repo
(temporary, under `.re2-sample/`, gitignored); this document is the
durable record of what was found.

## `cases.parquet` (case index)

One row per case. Columns relevant to Milestone 2:

| Column | Example | Meaning |
|---|---|---|
| `case` | `re2ob_checkoutservice_cpu_1` | `{system}_{root_cause_service}_{fault}_{repetition}` |
| `root_cause_service` | `checkoutservice` | injected fault's target service |
| `fault` | `cpu` | one of `cpu, mem, disk, delay, loss, socket` |
| `inject_time` | `1705354566` | unix seconds, fault injection start |
| `time_start` / `time_end` | `1705353846` / `1705355286` | unix seconds, full case window |
| `normal_timesteps` / `faulty_timesteps` | `720` / `721` | seconds of baseline vs. fault (case 1: exactly 12 min each, back-to-back, no explicit recovery/cooldown phase recorded) |
| `n_metrics`, `has_logs`, `has_traces` | | presence/width, not used by our projection |

## `metrics.parquet` -- wide format, 1 row per second

- **73 columns** for this case: `time` + `<service>_<metric_family>` for
  every service present in that instance's topology.
- `time`: unix seconds, **confirmed strictly 1Hz** (`diff() == 1` for
  every row in the sample).
- Metric families observed (not every service has every family --
  e.g. `_diskio` only appears for 4 of 11 services in this case):
  - `_cpu` -- cAdvisor-style usage; values observed ranging 0.18-20.03
    for the fault-targeted service, jumping from a ~0.4-0.6 baseline to
    a ~20 plateau exactly at `inject_time` (confirmed row-by-row around
    the injection boundary). RCAEval does not document the exact unit;
    treated as "core-fraction x 100" (i.e. comparable to a percentage of
    one core, but can exceed 100 under multi-core stress) for our
    purposes, since that is the closest wnexisting cAdvisor-derived
    convention and matches the observed jump to ~20 under stress.
  - `_mem` -- raw bytes (e.g. `111521792.0` = ~106MiB).
  - `_diskio` -- bytes/sec, present only for some services.
  - `_socket` -- open socket count (small integers).
  - `_workload` -- request rate proxy (not HTTP RPS necessarily -- values
    like 14-143 observed, roughly consistent with per-second request
    counts across all of that service's callers).
  - `_error` -- error count, present only for services with observed
    errors (`currencyservice`, `frontend`, `frontend-external`,
    `productcatalogservice` in this case).
  - `_latency-50`, `_latency-90` -- latency percentiles; values are
    small decimals (0.002-0.9), consistent with **seconds**.

## `traces.parquet`

| Column | Type | Notes |
|---|---|---|
| `time` | string `HH:MM` | low precision, not used for correlation |
| `traceID`, `spanID`, `parentSpanID` | string (hex) | standard trace/span identity |
| `serviceName` | string | **`frontend` is named `frontendservice`** here -- a RE2-OB-specific naming quirk, not a SentriX identity bug. All other 6 traced-service names match ours exactly (`checkoutservice`, `currencyservice`, `emailservice`, `paymentservice`, `productcatalogservice`, `recommendationservice`). |
| `methodName`, `operationName` | string | gRPC method / span operation name |
| `startTimeMillis` | int64 | unix **milliseconds** |
| `startTime` | int64 | unix **microseconds** (confirmed: `startTime / 1000 - startTimeMillis` is a sub-millisecond fraction, i.e. `startTime` is `startTimeMillis` with microsecond precision appended, not an independent field) |
| `duration` | int64 | **microseconds** (observed range 2 - 2,396,393 -> 2us to 2.4s, consistent with real RPC/HTTP durations) |
| `statusCode` | int64, nullable | gRPC status code; null for some root/HTTP spans |

## `logs.parquet`

| Column | Type | Notes |
|---|---|---|
| `timestamp` | int64 | unix seconds |
| `container_name` | string | service/container name (matches `traces.serviceName`'s convention, i.e. `frontend`, not `frontendservice`, based on sample rows) |
| `message` | string | free text |

## SentriX's RE2-compatible projection: what it materializes and what it loses

Implemented in `scripts/re2-projection.py`. Reads the canonical export
(`runs/<run_id>/canonical/`) and writes `runs/<run_id>/re2-compatible/{metrics,traces,logs}.parquet`
in the schema above.

**Sampling interval -- explicit resampling rule, not native 1Hz:**
Our Prometheus scrape interval is 5s (see the commit tightening it from
the chart's 1m default), not RE2-OB's native 1Hz. The projection does
**not** fabricate 1-second granularity that doesn't exist. It resamples
to a 1-second grid by forward-filling each metric's last observed value
between actual 5s samples (`pandas.resample("1s").ffill()`), and every
resampled row's provenance is explicit: raw export retains the true
5s-interval samples with their real timestamps, so any consumer can tell
"was this an observed sample or a filled value" by cross-referencing
back to `runs/<run_id>/canonical/metrics.parquet`. This is a real,
documented information loss (RE2-OB's own values ARE observed once per
second; ours are observed once per 5 seconds and held constant in
between), not a claim of equivalent fidelity.

**CPU/memory source:** `container_cpu_usage_seconds_total` (rate, as
core-fraction x 100 to match RE2-OB's apparent convention) and
`container_memory_working_set_bytes`, both via cAdvisor
(`kubernetes-nodes-cadvisor` Prometheus job) -- not the OTel
spanmetrics-derived RED metrics, which have no CPU/memory dimension at
all. This is the fix for the earlier gap: Milestone 1 only had
request-rate/latency/error (RED) metrics from spanmetrics; Milestone 2
needed actual resource metrics for `cpu`/`mem` fault types, which come
from a completely different source (cAdvisor vs. Collector).

**`_latency-50`/`_latency-90`**: derived from spanmetrics' histogram
buckets (`traces_span_metrics_duration_milliseconds_bucket`) via
PromQL `histogram_quantile`, converted from milliseconds to seconds to
match RE2-OB's apparent unit.

**`_workload`**: `rate(traces_span_metrics_calls_total[<window>])`,
per service.

**`_error`**: count of spans/calls with a non-OK status in the window,
from the same spanmetrics data.

**`_diskio`, `_socket`**: **not materialized.** Neither cAdvisor's
default metric set (as scraped here) nor our OTel pipeline expose
per-container disk I/O or open-socket-count in a directly comparable
form. Columns are omitted rather than filled with a fabricated value;
this is a known, documented gap versus true RE2-OB cases, relevant if a
future RCA method specifically needs those signals.

**Traces**: `operationName`/`methodName` come directly from span
attributes (`rpc.method`, span name); `statusCode` from the span's OTel
status; `duration` converted from OTel's nanoseconds to RE2-OB's
microseconds. `serviceName` uses SentriX's own canonical name
(`frontend`, not `frontendservice`) -- the RE2-OB naming quirk is NOT
replicated, since matching our own identity model
(`identity/service-alias-map.yaml`) consistently across all three
SentriX views (raw/canonical/RE2-compatible) matters more than bit-for-bit
matching RE2-OB's one idiosyncratic label. Anything joining our
RE2-compatible export against actual RE2-OB cases needs to account for
this one name difference.

**Lost identity richness**: the RE2-compatible view drops every OTel
resource attribute that RE2-OB's own schema has no column for
(`k8s.pod.uid`, `k8s.node.name`, `service.instance.id`, `deployment.environment.name`,
etc.). The canonical view (`runs/<run_id>/canonical/`) is the one that
keeps these; the RE2-compatible view is intentionally a lossy projection
for benchmark-shape comparability only, per section 5 of the project
context doc ("Canonical view는 풍부한 OTel attribute를 보존한다").
