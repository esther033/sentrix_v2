# SentriX 데이터 추출 현황

기준일: 2026-10-06  
실제 데이터 수집 종료 시점: 2026-09-26  
판정 기준: `audits/m2-closeout/status.json`, 각 run의 `pipeline-v2.json`, `run-assessment-v2.json`, incident registry

## 1. 결론

현재 M2 데이터셋은 **부분적으로 유효한 데이터셋(partial valid dataset)** 이다.

- 공식 사용 가능: 장애 실험 3개와 각각의 matched control 3개, 총 6개 run
- 확보한 장애 유형: `cpu_saturation`, `pod_failure`, `traffic_surge`
- 이 중 실제 영향이 확인된 양성 incident: `pod_failure`, `traffic_surge`
- 주입은 성공했지만 정책상 영향이 확인되지 않은 음성 사례: `cpu_saturation`
- 미확보: `network_delay`, `packet_loss`
- M2 전체 완료 조건은 아직 충족하지 못했으므로 `complete: false`

즉, 현재 데이터는 파이프라인 검증과 초기 RCA 실험에는 쓸 수 있지만, 반복성이 확보된 학습·통계 데이터셋으로 보기는 이르다.

## 2. 전체 저장 현황

| 구분 | 현황 |
|---|---:|
| `runs/`의 실제 run 디렉터리 | 28개 (lock 디렉터리 제외) |
| M1 run | 4개 |
| M2 `pipeline-v2.json` 보유 run | 24개 |
| v2 파이프라인 완료 run | 20개 |
| v2 파이프라인 미완료 run | 4개 |
| runtime assessment 유효 run | 19개 |
| 전체 transaction stage 레코드 | 360,780개 |
| 완료 파이프라인의 RE2 metric 행 | 20,085개 |
| 완료 파이프라인의 RE2 trace span 행 | 4,989,117개 |
| 완료 파이프라인의 RE2 log 행 | 2,590,146개 |
| `runs/` 저장 용량 | 약 27.56 GiB / 84,665 files |

위 전체 집계에는 smoke test, fastcheck, 재시도, superseded run이 포함된다. 따라서 **20개 완료 run 전체를 공식 연구 데이터로 간주하면 안 된다.**

## 3. 데이터 구성과 계보

각 M2 run은 다음 계층으로 구성된다.

| 계층 | 주요 파일 | 용도 |
|---|---|---|
| 실행 증거 | `run-manifest.yaml`, `runtime.json`, `fault-record.json`, `transaction-results.jsonl`, `workload-timeseries.json` | workload, 장애 주입 시각, 요청별 결과와 환경 기록 |
| Raw view | `raw-v2/` | Tempo, Prometheus, Loki 원본 API 응답과 Kubernetes 환경 snapshot 보존 |
| Canonical view | `canonical-v2-<attempt>/` | metrics, traces, logs, events를 identity가 정규화된 long-format Parquet으로 저장 |
| RE2-compatible view | `re2-v2-<attempt>/` | RCAEval/RE2-OB 형태에 맞춘 metrics, traces, logs Parquet |
| 무결성/상태 | `pipeline-v2.json`, `export-manifest.json`, `canonicalize-summary.json`, `projection-summary.json` | 단계별 성공 여부, 누락, SHA-256 provenance 기록 |
| Incident label | `incidents/inc-<run-id>.yaml` | 원인 서비스, 장애 유형, 영향, 복구, control 매칭, 최종 유효성 |

데이터 흐름은 다음과 같다.

`workload + fault` → `raw-v2` → `canonical-v2-*` → `re2-v2-*` → `incident YAML`

`pipeline-v2.json`은 최신 유효 attempt를 가리키며, 이전 결과는 덮어쓰지 않고 `_history/`에 남긴다.

## 4. 공식 사용 가능 matched pairs

`audits/m2-closeout/status.json`에서 사용 가능하다고 명시된 fault run과 그 matched control만 선별했다.

| 유형 | Control run | Fault run | 최종 상태 | 영향 판정 | 복구 판정 |
|---|---|---|---|---|---|
| CPU saturation | `m2-control-cpu-saturation-001` | `m2-cpu-checkoutservice-003` | `fault_active_no_impact` | 영향 미확인 | pass |
| Pod failure | `m2-control-pod-failure-005` | `m2-pod-failure-checkoutservice-005` | `recovered` | error-rate 증가 | pass |
| Traffic surge | `m2-control-traffic-surge-005` | `m2-traffic-surge-frontend-003` | `recovered` | latency 증가 | pass |

세 쌍 모두 다음 조건을 통과했다.

- fault injector 성공 및 제거 확인
- fault/control workload 호환성 확인
- runtime assessment 유효
- raw export 완료
- canonical 및 RE2-compatible view 완료
- artifact hash와 데이터 generation 검증

### 4.1 추출량

| Run | 고유 transaction | Stage record | Stage 성공률 | 실제 RPS | RE2 metric | Trace span | Log |
|---|---:|---:|---:|---:|---:|---:|---:|
| `m2-control-cpu-saturation-001` | 2,285 | 13,710 | 100.000% | 2.004 | 1,215 × 51 | 299,335 | 158,433 |
| `m2-cpu-checkoutservice-003` | 2,284 | 13,704 | 100.000% | 2.004 | 1,215 × 51 | 299,204 | 158,368 |
| `m2-control-pod-failure-005` | 2,282 | 13,692 | 100.000% | 2.002 | 1,210 × 51 | 298,942 | 158,229 |
| `m2-pod-failure-checkoutservice-005` | 2,284 | 13,704 | 91.039% | 2.004 | 1,215 × 51 | 282,544 | 148,548 |
| `m2-control-traffic-surge-005` | 2,283 | 13,698 | 100.000% | 2.003 | 1,215 × 51 | 299,072 | 158,300 |
| `m2-traffic-surge-frontend-003` | 3,161 | 18,966 | 99.884% | 2.773 | 1,215 × 51 | 413,989 | 218,819 |
| **합계** | **14,579** | **87,474** | — | — | **7,285 rows** | **1,893,086** | **1,000,697** |

Stage record는 한 transaction의 `browse_home → browse_product → cart_add → cart_view → checkout` 흐름에서 발생한 개별 요청 기록이다. 따라서 stage record 수와 transaction 수는 같은 의미가 아니다.

### 4.2 사례별 해석

#### CPU saturation

- 대상: `checkoutservice`
- 주입 구간: 약 5분
- fault/control 모두 약 2 RPS, transaction stage 성공률 100%
- 주입과 제거는 확인됐지만 v3 정책의 임계치를 넘는 사용자 영향은 확인되지 않음
- 결론: 실패한 실험이 아니라 **주입 성공·영향 미확인인 유효 음성 사례**

#### Pod failure

- 대상: `checkoutservice`
- fault run의 transaction stage 성공률: 91.039%
- critical failure: `error_rate_increase`
- 제거 후 안정 상태 관찰: pass
- 결론: 명확한 영향과 복구가 모두 기록된 양성 incident

#### Traffic surge

- 대상: `frontend` entrypoint
- fault 구간에서 2 RPS → 5 RPS, 전체 실제 평균 2.773 RPS
- dropped iteration: 22
- critical failure: `latency_increase`
- 제거 후 안정 상태 관찰: pass
- 결론: workload 계층의 양성 incident

## 5. 신호별 내용

### Transactions

`transaction-results.jsonl`은 요청마다 `run_id`, `transaction_id`, `stage`, `trace_id`, `span_id`, HTTP 상태, 성공/timeout, duration, assertion 결과와 timestamp를 기록한다. 이 ID를 통해 synthetic transaction과 backend trace를 직접 연결한다.

### Metrics

RE2-compatible metrics는 51개 컬럼이며 CPU, memory, workload, error, latency p50/p90 계열을 포함한다.

- CPU/memory: cAdvisor 기반
- workload/error/latency: OTel spanmetrics 기반
- 원래 Prometheus 조회 간격은 5초
- RE2의 1초 형태는 최대 5초 미만 범위에서 과거 값을 forward-fill한 투영 결과
- 따라서 1초 행을 모두 독립적으로 관측된 raw sample로 해석하면 안 됨
- disk I/O와 socket count는 현재 수집하지 않음

### Traces

Trace Parquet은 trace/span/parent ID, service, operation, 시작 시각, duration, gRPC status를 보존한다. Canonical view에는 원래 span/resource JSON도 남아 있어 Kubernetes pod UID와 instance identity를 추적할 수 있다.

### Logs and events

Logs는 timestamp, container/service identity, message를 제공한다. Kubernetes events는 raw/canonical 계층에 보존되지만 RE2-compatible 최종 3종 파일에는 포함되지 않는다.

## 6. 공식 데이터에서 제외하거나 보조 증거로만 쓸 항목

| Run | 사유 |
|---|---|
| `m2-control-traffic-surge-002` | export 중단, 완전한 multimodal control 아님 |
| `m2-traffic-surge-frontend-001` | workload는 끝났지만 장시간 공백과 backend retention 만료로 telemetry export 미완료 |
| `m2-control-pod-failure-004` | fault 전에 supervisor를 의도적으로 중지했고 이후 host restart로 환경 변경 |
| `m2-control-pod-failure-002` | 주입 전 Kubernetes configuration 접근 실패 |
| `m2-pod-failure-checkoutservice-003` | 주입 전 control workload 정의 불일치 |
| `m2-control-traffic-surge-004` | 현재 `pipeline-v2.complete=false` |
| `m2-fastcheck-traffic-surge-frontend-005` | 현재 `pipeline-v2.complete=false`, incident validity=false |

그 밖의 M1, smoke, fastcheck, 예전 policy run은 파이프라인 개발과 회귀 검증 증거로 보존하되, 최신 공식 3개 matched pair와 분리해서 사용한다.

## 7. 미확보 범위와 한계

### 미확보 fault

- `network_delay`: Docker Desktop/WSL kernel에 netem traffic-control qdisc가 없어 사전 capability probe 단계에서 차단
- `packet_loss`: 같은 netem 의존성 때문에 별도 중복 실험을 수행하지 않음

### 해석상 한계

- fault 유형별 공식 사례가 1개뿐이어서 반복성과 분산을 평가할 수 없음
- CPU 사례는 영향 미확인 음성 사례이므로 CPU 장애의 양성 학습 샘플이 아님
- known trace ID 조회 성공은 원래 생성된 모든 span의 ingest 완전성을 증명하지 않음
- RE2 metrics의 1초 해상도는 native 1 Hz가 아니라 5초 관측값의 제한적 forward-fill
- invalid timestamp span은 canonical에 보존하되 RE2 projection에서는 제외하며 손실량을 summary에 기록
- RE2-OB에 있는 disk I/O와 socket feature는 없음
- 현재 약 27.56 GiB에는 이전 attempt와 raw response checkpoint가 포함돼 실제 학습용 최종 Parquet 크기보다 훨씬 큼

## 8. 권장 사용 방식

1. 공식 분석 입력은 위 6개 run의 `pipeline-v2.json`이 가리키는 최신 view만 사용한다.
2. control과 fault를 반드시 표의 pair 단위로 비교한다.
3. 영향 탐지는 `pod_failure`와 `traffic_surge`, 음성 대조는 `cpu_saturation`으로 구분한다.
4. 원인 분석과 identity 추적에는 canonical view를, RE2 계열 모델 호환성 실험에는 RE2-compatible view를 사용한다.
5. 이전 attempt, smoke, fastcheck 데이터는 별도 development split으로 두고 공식 성능 수치에 섞지 않는다.
6. 다음 수집에서는 각 fault 유형을 최소 여러 차례 반복하고, netem 가능한 Linux/Kubernetes 환경에서 network delay와 packet loss를 추가한다.

## 9. 기준 파일

- 최종 M2 판정: `audits/m2-closeout/status.json`
- 데이터 파이프라인 규칙: `docs/TELEMETRY_PIPELINE.md`
- raw export 완전성 규칙: `docs/TELEMETRY_EXPORT.md`
- RE2 스키마와 손실 정보: `docs/RE2_SCHEMA.md`
- incident label: `incidents/inc-*.yaml`
- 각 run의 최신 데이터 포인터: `runs/<run_id>/pipeline-v2.json`
