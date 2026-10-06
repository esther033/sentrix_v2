# SentriX

Kubernetes-based microservice observability RCA research project. See
[`sentrix-project-context.md`](sentrix-project-context.md) for the full
project definition, research questions, and roadmap, and
[`docs/MILESTONE1.md`](docs/MILESTONE1.md) for what's implemented so far
and how to run it.

## Status

Milestone 1 (observable evaluation path) — M1A (telemetry smoke path)
implemented and passing; M1B (control-run baseline) implemented, runs in
progress. No fault injection, no RCA, no recovery claims yet — see
`docs/MILESTONE1.md` for exact scope.

## Layout

```
deploy/             Kubernetes manifests + Helm values for Online Boutique
                     and the observability stack (OTel Collector,
                     Prometheus, Tempo, Loki)
workload/            k6 synthetic transaction script + workload profiles
recorder/            k6 output -> per-request JSONL transaction records
identity/            canonical service identity resolution rules
smoke/               M1A quality gate (transaction telemetry + K8s events)
scripts/             run orchestration, run-manifest generation
runs/<run_id>/       per-run artifacts: manifest, transaction results,
                     workload timeseries, quality report
docs/                Milestone write-ups
```

## Quick start

Requires a running Kubernetes cluster (Docker Desktop's built-in
Kubernetes, or `kind`), `kubectl`, `helm`, `k6`, `python3` with `pyyaml`.

```bash
# 1. deploy the stack (namespace, observability backends, Online Boutique)
kubectl apply -f deploy/namespace.yaml
helm install loki grafana/loki --version 7.3.0 -n sentrix -f deploy/loki/values-minimal.yaml
helm install tempo grafana/tempo --version 1.24.4 -n sentrix -f deploy/tempo/values-minimal.yaml
helm install prometheus prometheus-community/prometheus --version 29.30.0 -n sentrix -f deploy/prometheus/values-minimal.yaml
helm install otel-gateway open-telemetry/opentelemetry-collector --version 0.173.1 -n sentrix -f deploy/otel-collector/gateway-values.yaml
helm install otel-daemonset open-telemetry/opentelemetry-collector --version 0.173.1 -n sentrix -f deploy/otel-collector/daemonset-values.yaml
kubectl apply -k deploy/online-boutique/

# 2. port-forward the entrypoints (background, one session)
kubectl port-forward -n sentrix svc/frontend-external 8080:80 &
kubectl port-forward -n sentrix svc/tempo 3200:3200 &
kubectl port-forward -n sentrix svc/prometheus-server 9090:80 &
kubectl port-forward -n sentrix svc/loki-gateway 3100:80 &

# 3. run the M1A pilot
bash scripts/run-experiment.sh m1a-pilot-001 workload/profiles/pilot.json
```

See `docs/MILESTONE1.md` for what each step actually does, the identity
and telemetry-pipeline gotchas found while building this, and the full
list of known limitations.
