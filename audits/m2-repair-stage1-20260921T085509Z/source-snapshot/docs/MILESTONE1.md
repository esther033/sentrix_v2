# Milestone 1 — Observable evaluation path

Status: **M1A and M1B both complete.** M1A telemetry smoke gate passes
(`runs/m1a-pilot-004`). M1B's three 30-minute no-fault control runs all
pass the same gate (`runs/m1b-low-001`, `runs/m1b-main-001`,
`runs/m1b-high-normal-001`) -- see "M1B control-run baseline results"
below for the numbers and "Known limitations" for the infra issues found
and fixed while getting there.

## What this milestone does NOT claim

- No fault injection (Chaos Mesh is Milestone 2).
- No RCA, no root-cause ranking.
- No "confirmed recovery" claim of any kind -- there is no fault to recover
  from yet. This milestone only establishes the measurement path that a
  later recovery-signal comparison (Milestone 2+) will run on top of.
- No Datadog/Dynatrace integration (deferred per the project context doc;
  OTel-only collection path first).
- No destructive or automated remediation action of any kind.

## Architecture deployed

All in the `sentrix` namespace, on a single-node Docker Desktop
(kind-backed) Kubernetes cluster.

| Component | What | Why |
|---|---|---|
| Online Boutique | pinned at tag `v0.9.0`, commit `d138db567079e2ef982d46b2993f8349cb18e2b2` | target application |
| otel-collector gateway (Deployment, 1 replica) | receives OTLP traces from the 7 instrumented services, runs `spanmetrics` connector, k8sobjects receiver for Kubernetes Events | traces -> Tempo, RED metrics -> Prometheus, events -> Loki |
| otel-collector daemonset (1 pod, single node) | `filelog` receiver (CRI format) on `/var/log/pods`, `k8sattributes` enrichment | Pod stdout/stderr -> Loki |
| Tempo (single binary) | trace storage, 6h retention, ephemeral (no PVC) | |
| Prometheus (minimal `prometheus` chart, not kube-prometheus-stack) | 6h retention, Alertmanager off, node-exporter off, kube-state-metrics on | RED metrics + object state |
| Loki (SingleBinary mode, filesystem storage, 2Gi PVC) | log/event storage, native OTLP ingest at `/otlp/v1/logs` | |
| k6 (run from the host, not in-cluster) | `browse -> cart -> checkout` synthetic transaction | workload/transaction generator |

Online Boutique's own built-in Locust loadgenerator is permanently scaled
to 0 replicas via a kustomize patch (`deploy/online-boutique/kustomization.yaml`)
-- SentriX uses only its own k6 workload so request rate is fully under our
control.

### Why "minimal": Docker Desktop single-node resource budget

The Docker Desktop VM in this environment is allocated ~7.5GiB / 16 vCPU
(`kubectl describe node` capacity). Online Boutique (11 services) plus a
full kube-prometheus-stack would not comfortably fit. Every backend here
runs single-binary/single-replica with disabled non-essential components
(Alertmanager, Grafana, node-exporter, Loki's chunks-cache, MinIO) and
short retention. **This is explicitly a pilot configuration, not a
production reference architecture** -- see `deploy/*/values-minimal.yaml`
inline comments for exactly what was turned off and why.

## Signal pipelines, and why each one needed explicit wiring

Deploying the Collector does **not** automatically produce traces, logs,
or events -- each pipeline required a specific, separately-verified fix:

### Traces: app -> OTLP -> Collector gateway -> Tempo

- Only 7 of 11 services have OpenTelemetry SDK wiring in this pinned
  version: `checkoutservice`, `currencyservice`, `emailservice`,
  `frontend`, `paymentservice`, `productcatalogservice`,
  `recommendationservice`. Confirmed by grepping `src/<service>/` for
  `ENABLE_TRACING` / `COLLECTOR_SERVICE_ADDR`.
  **`adservice`, `cartservice`, `shippingservice`, `redis-cart` have zero
  tracing instrumentation and will never appear in Tempo.** This is a
  property of the pinned application version, not a collector
  misconfiguration -- tracked in
  `identity/service-alias-map.yaml: known_untraced_services`.
- Tracing is **off by default**; each traced service needs
  `ENABLE_TRACING=1` and `COLLECTOR_SERVICE_ADDR=<gateway
  service>:4317` (a legacy-named env var from the app's own source, not
  an OTel standard one). Applied via
  `deploy/online-boutique/otlp-env-patch.yaml`.
- **Critical identity bug found and fixed during implementation**: none
  of these services call `sdktrace.WithResource(...)`, so without an
  explicit `OTEL_SERVICE_NAME`, every one of the (Go) services' traces
  would resolve to the SDK's fallback `unknown_service:server` --
  because the compiled binary inside every one of these containers is
  literally named `server`. All 7 would have collided on the exact same
  fallback service name. Fixed by setting `OTEL_SERVICE_NAME` and
  `OTEL_RESOURCE_ATTRIBUTES=service.namespace=sentrix,...` explicitly per
  service in the same patch file.
- **Service address bug found and fixed**: the Helm-chart-generated
  Collector gateway Service name is
  `otel-gateway-opentelemetry-collector` (release name + chart name), not
  the more readable `otel-collector-gateway` originally assumed in the
  design doc. Traces silently failed (`no such host`) until this was
  corrected.

### RED metrics: traces -> spanmetrics connector -> Prometheus exporter -> Prometheus scrape

Online Boutique emits no first-party request-rate/latency/error metrics.
The `spanmetricsconnector` on the gateway derives them from the same trace
stream, exposed as `traces_span_metrics_calls_total` /
`traces_span_metrics_duration_milliseconds_*` on a `prometheus` exporter
(port 8889), scraped via the cluster Prometheus's default
annotation-based `kubernetes-pods` job (`prometheus.io/scrape: "true"` pod
annotation on the gateway Deployment). This means RED metrics only exist
for the 7 traced services -- the same coverage gap as traces, for the same
reason.

### Pod stdout/stderr logs: filelog (DaemonSet) -> Collector -> Loki OTLP

The DaemonSet Collector's `logsCollection` + `kubernetesAttributes`
presets (from the official `opentelemetry-collector` Helm chart) handle
CRI log parsing and pod/deployment enrichment. Verified this actually
reaches Loki with a live query, not assumed from the presets existing.
Covers all 11 services (log collection does not depend on app-side
instrumentation).

### Kubernetes Events: k8sobjects receiver (gateway) -> Collector -> Loki OTLP

Verified independently of any transaction, via a sentinel Event object
created directly with `kubectl create` (see Gate B below) -- **a normal
successful HTTP transaction does not generate a Kubernetes Event**, so the
transaction pipeline and the events pipeline are two separate gates, not
one.

## Identity resolution (priority order, implemented in `smoke/quality-check.py`)

`identity/service-alias-map.yaml` documents this in detail. Short version:
a record is identity-resolved if **any** of these succeed, evaluated in
order --

1. `service.namespace` + `service.name` (OTel resource attrs -- traces, RED metrics)
2. `k8s.namespace.name` + `k8s.deployment.name` (k8sattributes enrichment -- logs, events)
3. `k8s.pod.uid` / `container.id` (pod-level identity without a resolved service name)
4. Prometheus source-native labels (`namespace`/`pod`/`job`)
5. the alias map, for genuine raw-name-vs-canonical-name mismatches (currently empty -- none exist in the pinned manifest)

A record that exhausts all five is **invalid**, listed in
`quality-report.json`, never silently merged. This is deliberately
**not** "every OTel field must be present" -- that rule would fail 100% of
stdout logs and 100% of Kubernetes Events, which structurally never carry
`service.name`.

## Transaction recorder: iteration-level JSONL, W3C trace context

`workload/k6/browse-cart-checkout.js`:

- One `trace_id` per transaction (k6 iteration); a **new** `span_id`
  (used as the `traceparent` parent-id) per request/stage, so Tempo
  renders one trace per transaction while individual stages
  (`browse_home`, `browse_product`, `cart_add`, `cart_view`, `checkout`)
  are still distinguishable spans within it.
- Every request emits its own JSONL record (`stage`, `http_status`,
  `success`, `timeout`, `duration_ms`, `event_time`, `trace_id`,
  `span_id`, `traceparent`) -- **not** a k6 end-of-test summary. One
  additional `TRANSACTION_SUMMARY` record per iteration rolls up
  overall pass/fail for that transaction.
- k6's default stdout wraps `console.log()` output in its own logrus-style
  log line, and even `--console-output=<file>` does not strip that
  wrapper in this k6 version (v1.7.1) -- `recorder/transaction-recorder.py`
  has to regex-unwrap `msg="TXN_RECORD:...\"` and un-escape it. Documented
  in code since it is easy to silently "fix" by matching the wrong line
  shape and get zero records with no error.
- Actual request rate / dropped iterations: built from k6's raw
  `--out json=` point stream (`workload-timeseries.json`, 1s rollup),
  **not** from the k6 end-of-test summary -- summary numbers are
  aggregate-only and cannot show a mid-run rate dip.
- `event_time` (request start, from k6) and `observed_at` (recorder-side
  wall-clock when the record was parsed) are stored separately, per the
  event_time / observed_at / ingested_at split the M1A skew check needs.
  A backend's own ingest timestamp (`ingested_at`) is read at quality-check
  time from that backend, never invented client-side.

## M1A gate (implemented in `smoke/quality-check.py`)

**Gate A -- transaction telemetry** (sampled from `checkout`-stage records):
1. `transaction-results.jsonl` has records for the run_id
2. `trace_id` resolves in Tempo
3. `checkoutservice`'s RED metric exists in Prometheus for the transaction's
   time window (joined by **service name + time window**, not by trace_id
   -- Prometheus metrics do not carry trace ids)
4. `checkoutservice`'s pod stdout log exists in Loki for the same
   **pod + time window** (not joined by trace_id -- app stdout is not
   guaranteed to carry it; some records do, checked opportunistically)
5. canonical service identity resolves via the priority order above

**Gate B -- Kubernetes Event pipeline** (independent of any transaction):
1. create a harmless sentinel `Event` (`kubectl create`, `reason:
   SentrixM1ASentinel`, attached to the `sentrix` Namespace object --
   never targets a real workload object)
2. confirm the k8sobjects receiver -> Loki path picked it up, matched by
   **UID**, not just name (names could theoretically collide)
3. record source vs. collected timestamps and the lag between them

**M1A passes only if both gates pass.** Verified end-to-end on a live
5-transaction dev run before committing to the real pilot run
(`runs/m1a-pilot-001/`).

## Running it

Prerequisites already running (see bottom of this doc for exact
commands): `kubectl port-forward` to `frontend-external:8080`,
`tempo:3200`, `prometheus-server:9090`, `loki-gateway:3100`.

```bash
# M1A: pilot profile (~9 min: 2m warmup + 5m steady + 2m cooldown)
bash scripts/run-experiment.sh m1a-pilot-001 workload/profiles/pilot.json

# M1B: only after the pilot passes quality-check, per profile
bash scripts/run-experiment.sh m1b-low-001 workload/profiles/low.json
bash scripts/run-experiment.sh m1b-main-001 workload/profiles/main.json
bash scripts/run-experiment.sh m1b-high-normal-001 workload/profiles/high-normal.json
```

Each run produces `runs/<run_id>/`:
- `run-manifest.yaml` -- the join key: workload profile, pinned source
  commit, **actually-running** image digests (read from `kubectl get pod
  ... imageID` after rollout, not guessed pre-deploy), artifact paths,
  `evaluation_policy_version`
- `transaction-results.jsonl`, `workload-timeseries.json`,
  `k6-summary.json`, `k6-raw-metrics.jsonl`, `k6-console-output.log`
- `quality-report.json` -- Gate A / Gate B results

### Port-forwards (run once per session, in the background)

```bash
kubectl port-forward -n sentrix svc/frontend-external 8080:80 &
kubectl port-forward -n sentrix svc/tempo 3200:3200 &
kubectl port-forward -n sentrix svc/prometheus-server 9090:80 &
kubectl port-forward -n sentrix svc/loki-gateway 3100:80 &
```

## M1B control-run baseline results

Three 30-minute no-fault control runs, each passing the M1A-style quality
gate (`runs/<run_id>/quality-report.json`), starting from a freshly
verified telemetry pipeline (M1A pilot immediately before).

**These are two different measurements, not one.** The protocol
(sentrix-project-context.md section 9) defines a run as warm-up (10 min,
excluded) -> baseline (15 min) -> cooldown (5 min, excluded). "Full run"
below is everything k6 recorded, ramp-up and ramp-down included --
useful for sanity-checking the whole run, but NOT the number a later
fault run's during-fault window gets compared against. "Steady-state
baseline" is the [warmup_seconds, duration-cooldown_seconds) slice only,
computed by `scripts/compute-baseline.py` from each run's own
`run-manifest.yaml` profile timing and written to
`runs/<run_id>/baseline-summary.json`. Use steady-state for any future
comparison; full-run is context, not the baseline.

### Full run (includes warm-up + cooldown ramp)

| Profile | Target rate | Transactions | Success rate | Timeouts | Dropped iterations | p50 | p90 | p99 |
|---|---|---|---|---|---|---|---|---|
| low | 0.5 rps | 901 | 100.00% | 0 | 0 | 324ms | 587ms | 787ms |
| main | 2 rps | 3601 | 98.17% | 0 | 0 | 271ms | 350ms | 885ms |
| high-normal | 5 rps | 8983 | 99.90% | 9 | 18 | 3.77s | 4.70s | 6.54s |

### Steady-state baseline (warm-up and cooldown excluded -- this is the comparison point for later fault runs)

| Profile | Target rate | Transactions | Success rate | Timeouts | Dropped iterations | p50 | p90 | p99 |
|---|---|---|---|---|---|---|---|---|
| low | 0.5 rps | 450 | 100.00% | 0 | 0 | 306ms | 372ms | 601ms |
| main | 2 rps | 1799 | 98.22% | 0 | 0 | 275ms | 335ms | 649ms |
| high-normal | 5 rps | 4499 | 99.80% | 9 | 1 | 3542ms | 4531ms | 5881ms |

Note `high-normal`'s 18 dropped iterations are mostly (17 of 18) a
warm-up effect -- k6 still ramping its VU pool up to sustain 5 rps in the
first ~10 minutes -- but not entirely: 1 drop occurred well inside the
steady-state window, at `2026-09-18T12:50:32Z` (the nearest other drop
was ~10 minutes earlier, during warm-up). It's an isolated, non-recurring
event rather than a sustained steady-state problem, but it is a real
steady-state data point and is counted as such above -- it is not
excluded as warm-up noise. `main`'s 32 steady-state failures (of 1799, HTTP 500 across all
5 stages) were cross-checked against pod restart timestamps and do not
correlate with any infra restart in that window -- treated as genuine
application-level baseline noise on this pinned Online Boutique version,
not something to chase away. `high-normal`'s much higher steady-state
latency reflects real saturation of the single-node pilot cluster at
sustained 5 rps -- this is the intended purpose of a high-but-still-normal
profile (teaching later fault/RCA work that high load alone is not, by
itself, an incident).

## Known limitations / open items

- **Coverage gap, not a bug**: `adservice`, `cartservice`,
  `shippingservice`, `redis-cart` have no tracing instrumentation in the
  pinned `v0.9.0` and never appear in Tempo or the spanmetrics-derived
  Prometheus metrics. Their stdout logs and Kubernetes-native identity
  still resolve fine (`k8s.deployment.name`).
- **Tempo memory scales with trace ingestion rate, not just wall time**:
  a 500Mi limit OOMKilled repeatedly under even the pilot's light load,
  1Gi was fine through `low`/`main` (0.5-2 rps) but OOMKilled again under
  `high-normal` (5 rps); settled on 2Gi (`deploy/tempo/values-minimal.yaml`).
  If a future profile pushes well past 5 rps, expect to raise this again.
  Persistence (a small PVC) is enabled specifically so a future OOM
  restart doesn't also erase already-ingested trace data.
- **A host sleep/suspend mid-run silently breaks everything downstream
  of the port-forwards** (they die, `kubectl port-forward` does not
  auto-reconnect) and, separately, can leave Tempo's WAL/blocks needing
  replay on every restart -- worth watching for if a run spans a laptop
  sleep cycle. Restart the four port-forwards (see below); if Tempo is
  stuck CrashLoopBackOff, **do not wipe its PVC on the assumption that
  "6h retention means nothing in it is worth keeping" -- that's only true
  if nothing has written to it recently.** A PVC that's been collecting
  continuously has this run's freshest traces in it too, not just stale
  ones. Before `helm uninstall tempo && kubectl delete pvc storage-tempo-0
  && helm install ...`, check both:
  1. No collection is in progress right now (no `k6.exe`/transaction-recorder
     process running, and no `runs/<run_id>/` present without its
     `quality-report.json` -- that's an in-flight run).
  2. No run finished inside the last `tempo.retention` window (6h) whose
     traces you might still want to inspect manually -- check the newest
     `generated_at` across `runs/*/quality-report.json`; if it's under 6h
     old, that run's traces are still live in the PVC and wiping loses
     them, even though the automated gate already extracted what it
     needed at the time.
  Only wipe when both hold. This is exactly what caused
  `runs/m1b-low-001`'s first quality-check to legitimately FAIL after a
  host sleep -- the fix there was correct because the sleep itself, not
  the later wipe, was what broke that run's Tempo data; the wipe just
  cleared an already-corrupted crash-loop state with no live run
  depending on it at the time.
- The `grafana/tempo` chart (single-binary mode) is marked deprecated
  upstream in favor of `tempo-distributed`; kept here because
  `tempo-distributed` requires object storage, which is out of scope for
  a single-node pilot. Revisit if the chart is removed from the repo.
- No metrics-server installed, so `kubectl top` is unavailable for manual
  resource checks; resource requests/limits in `deploy/*/values-minimal.yaml`
  were sized from chart defaults and observed steady-state behavior, not
  from `kubectl top` numbers.
- Loki's `chunksCache` is disabled (memory budget); only affects query
  performance, not data completeness.
- k6 runs on the Windows host via `kubectl port-forward`, not as an
  in-cluster Job. Simpler for a single-operator pilot; means workload
  timing includes port-forward overhead, which is consistent across runs
  but is not what a truly in-cluster synthetic monitor would see.
- Gate A's Prometheus/Loki checks are joined by **service + time window**,
  not by `trace_id` -- this is a deliberate design correction (Prometheus
  never carries trace ids; app stdout is not guaranteed to). Do not
  "fix" this to require trace_id-based joining for metrics/logs; that
  would make the gate fail on a fully-working pipeline.
