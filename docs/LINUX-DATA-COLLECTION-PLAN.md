# SentriX Linux 데이터 수집 설계 v1 (제안)

작성: 2026-09-26. 설계 문서이며 아직 Linux 배포·수집을 수행하거나 검증한 결과가 아니다.
기존 WSL run, 평가 정책 v3, 원본 데이터는 변경하지 않는다.

## 1. 목적과 주장 범위

- 1차 과제: 사용자 영향 탐지와 원인 서비스/의존성 edge 후보 순위화.
- 보조 과제: fault 종류 분류. 주입된 fault와 사용자 장애를 별도 라벨로 둔다.
- 회복 과제: fault 제거 이후 사용자 관점의 안정 회복 시간. 실제 운영 MTTR이나 자동 복구 조치 효과와 동일하지 않다.
- Linux 수집 완료 자체는 제품 적합성 증명이 아니다. 대상 운영 환경의 별도 평가가 필요하다.
- 기존 WSL 유효 데이터는 해당 조건의 실험 데이터로 보존한다. OS 때문에 무효가 되거나 사전학습용으로만 제한되는 것은 아니다.
- environment_id는 출처/분할/분석용이다. 환경과 fault 종류가 완전히 겹치는 데이터의 교란을 이 필드만으로 제거할 수 없다.

## 2. 환경과 자원

첫 수집 환경 E1: 상시 가동 Ubuntu LTS 서버/VM, Kubernetes + containerd. 노트북 종료와 독립적으로 동작해야 한다.
OS/커널/Kubernetes/containerd/CNI/Chaos Mesh/Helm chart/이미지 digest를 사전 호환성 시험 후 고정한다. 최신 버전 자동 추종 금지.
초기 용량 계획 가정은 전체 16 vCPU, 64 GiB RAM, SSD 500 GB~1 TB이며 보장된 요구 사양은 아니다.
가능하면 앱 worker(8 vCPU/32 GiB), telemetry worker(8 vCPU/32 GiB)를 분리하고 control plane 자원은 별도로 확보한다.
단일 서버이면 앱과 관측 프로세스의 자원 경쟁을 기록하고 production topology로 표현하지 않는다.
실제 자원과 저장 공간은 1시간 용량 pilot의 peak RSS, CPU, queue, bytes/min, export 처리량으로 확정한다.

- 앱/관측/주입기를 분리된 namespace에 두되 namespace만으로 자원 격리가 된다고 주장하지 않는다.
- 앱 CPU/memory limit, replica, placement, HPA 설정을 campaign 동안 고정한다. 초기는 HPA off, replica=1.
- 현재 Online Boutique v0.9.0과 instrumentation을 우선 유지한다. 계측 변경 시 새 release/campaign으로 분리한다.
- k6는 in-cluster Job, orchestrator는 Linux service/Job으로 실행한다. port-forward/Windows 세션에 의존하지 않는다.
- 시간은 UTC로 기록하고 노드 clock offset을 측정한다. 목표 <100 ms; 초과하면 정밀 시각 라벨의 오차를 표시한다.
- backend retention은 임시 목표 48시간. 원본 export deadline은 run 종료 30분 이내이며 retention 연장으로 무제한 대기를 허용하지 않는다.
- raw와 파생본은 별도 영속 저장소에 저장하고 해시 검증한다. PVC는 archive가 아니며 자동 삭제하지 않는다.

## 3. 실험 전 capability gate

앱을 배포하기 전에 전용 probe Pod 쌍에서 수행한다. host lo/eth0와 운영 Pod에는 시험 qdisc를 적용하지 않는다.

1. sch_netem 지원, tc/iproute2, Chaos Daemon runtime socket, CNI 및 edge selector 지원을 확인한다.
2. probe 간 왕복 지연/패킷 수를 측정하고 delay와 loss를 각각 주입한다. 선언값만 보지 않고 qdisc/filter 통계와 측정 분포를 보존한다.
3. direction, interface, IP/port, 대상 Pod UID를 검증한다. RTT 변화는 단방향 delay 설정값과 동일하다고 가정하지 않는다.
4. 제거 후 qdisc/filter가 원상복구되고 연결이 정상화되는지 확인한다.
5. probe 범위 밖 트래픽, DNS, OTLP, scrape 경로와 Chaos 제어 연결이 영향받지 않는지 검사한다.
6. CPU stress 프로세스가 대상 앱과 동일한 cgroup quota를 공유하는지 확인한다.
7. orchestrator 중단/timeout/재시작 시 fault 제거 및 stop-after-control 요청이 작동하는지 확인한다.

하나라도 실패하면 본 수집을 시작하지 않는다. NetworkChaos는 제어 수단이고 tc/netem의 실제 효과를 별도로 확인한다.

## 4. 부하 교정

현재 2 rps는 사용자 transaction 시작률이다. HTTP RPS 및 내부 gRPC 호출률과 구분한다.
각 run에 offered transactions/s, started/completed/s, HTTP requests/s, RPC calls/s, dropped를 따로 저장한다.
목표 시스템에서 transaction rate를 단계적으로 올려 정상 SLO와 관측 안정성을 유지하는 지속 가능 용량 C를 측정한다.
초기 workload 수준은 0.2C/0.4C/0.6C 후보로 교정하고 이후 고정한다. 숫자는 실제 측정 후 확정한다.
기본 browse/cart/checkout mix와 timeout은 사전에 고정하고 payload/workload seed를 보존한다.
동일 matched pair는 동일 workload schedule/seed와 길이를 사용한다. 서로 다른 pair는 seed를 바꾼다.
본 수집에 constant 부하와 seeded piecewise 부하를 포함한다. injector seed와 workload seed는 분리한다.
RCAEval의 10~200 requests/s를 transaction/s로 복사하지 않는다. benchmark 비교에서는 실제 요청 정의와 달성량을 공개한다.

## 5. fault 범위 및 실측 증거

| 종류 | 첫 scope | 교정 후보 | 반드시 보존할 증거 |
|---|---|---|---|
| CPU | 한 서비스 컨테이너 | worker 1/2/4와 load 조합으로 교정 | 대상 cgroup, quota, usage cores, throttling, stress PID, collateral resource 변화 |
| pod_failure | 한 서비스 Pod | 지속시간 60/180/300초 | UID, container/readiness/endpoints 상태, 주입/제거 응답, 사용자 실패 |
| network_delay | 한 dependency edge | 20/100/300 ms, jitter는 별도 조건 | source/destination/방향/port, qdisc/filter, packet counter, 실제 지연 |
| packet_loss | 한 dependency edge | 0.5/2/5%, random loss | qdisc drop counter/packet denominator, retransmission, 적용/원복 |
| traffic_surge | 사용자 entrypoint | 정상 대비 1.5/2/3배 | 실제 arrival schedule/rate, generator headroom, dropped, 사용자 영향 |

모두 초기 교정 후보이며 결과를 본 뒤 평가 threshold를 변경하지 않는다. no-impact 결과도 보존한다.
CPU worker load=100은 앱 CPU limit 100% 사용의 증거가 아니다. 실제 stress/cgroup 측정이 필요하다.
pod_failure는 pod-kill과 구분한다. replica=1에서 Pod 비가용과 다중 replica에서 한 Pod 실패도 다른 조건이다.
network edge scope를 기본으로 유지한다. Pod-wide network 결과는 별도 cohort로 분리하며 동일 label로 섞지 않는다.
network root cause는 edge로 기록한다. exporter가 붙은 서비스 또는 피해 서비스만 자동 root cause로 지정하지 않는다.
traffic_surge는 외생 workload 원인이다. frontend는 entrypoint이며 반드시 결함 있는 서비스라는 뜻이 아니다.

대상 후보: checkoutservice, currencyservice, paymentservice, productcatalogservice, recommendationservice.
모두 실제 계측/트래픽 경로를 확인한 후 확정한다. 네트워크는 실제 관측된 5개 edge를 별도로 선정한다.
CPU/Pod 서비스 5개와 edge 5개의 수가 같다는 이유로 같은 target 축으로 해석하지 않는다.

## 6. 표준 protocol v4 제안

19분 고정 workload 대신 회복 관찰을 늘린 최대 25분 protocol을 사용한다:
warmup 3분 -> baseline 5분 -> intervention slot 5분 -> recovery 10분 -> cooldown 2분.
fault duration이 5분보다 짧으면 slot 안에서 시작/종료하고 실제 제거 시각부터 회복을 계산한다.
각 실험 조합의 control과 fault는 같은 총길이/seed/부하를 사용한다. control에는 같은 길이의 sham slot이 있다.
각 pair 순서: 정상성/원복 확인 -> control -> control export/validation -> 환경 재확인 -> fault -> export/validation -> 원복 확인.
초기에는 control을 여러 fault가 공유하지 않는다. 후속 최적화는 별도 정책 변경으로 검증한다.
fault onset은 campaign 내 사전 계획된 offset으로 바꿔 고정 시각 암기를 줄인다. 최대 총길이와 control도 함께 맞춘다.
다음 pair 전 5분 정상 관찰을 확보한다. 자원/사용자 지표가 회복되지 않으면 queue를 중지한다.
고정 환경 지문 외에 reboot/rollout/재시작/시간 변경도 확인한다. 현재 지문만으로 reboot를 항상 검출한다고 가정하지 않는다.

## 7. 수집량과 시간

단계 A: 5개 유형 × 대표 target 1개 × 2반복 = fault 10 + control 10. 용량/capability probe는 별도이며 정식 학습본에서 제외.
단계 B: CPU/Pod/delay/loss 각각 5개 target × 3반복 = 60 fault; surge 1개 entrypoint × 3부하 mix × 3반복 = 9 fault.
총 fault 69 + 독립 control 69 = 138 runs. A와 조건이 같고 사전 기준을 만족하는 건만 포함하며 숫자를 이중 계산하지 않는다.
단계 C: 앞의 60 fault 구조에 3개 severity 적용 =180; surge 3mix × 3severity × 3반복 =27. 총 207 fault +207 control.
반복은 날짜/세션과 workload seed를 달리하고 순서를 무작위화한다. 순서/seed는 실행 전에 registry에 고정한다.
이 숫자는 운영 계획이며 딥러닝 충분 표본수의 보장이 아니다. 학습 곡선/holdout 결과로 추가량을 결정한다.

25분 workload/run, export+검증 10~20분/run, pair reset 5분을 가정하면 pair당 75~95분이다.
A: 12.5~15.8시간. B: 86.3~109.3시간. C: 258.8~327.8시간. 설치·교정·실패 재시도는 별도.
독립 fault 69건을 3일 안에 단일 환경에서 안정적으로 끝내겠다고 약속하지 않는다.
서로 격리된 클러스터만 병렬화한다. 동일 앱/노드에서 동시 fault를 독립 사례로 세지 않는다.
복제 클러스터를 쓰면 각 환경이 모든 fault 종류를 경험하게 분산해 환경-라벨 교란을 줄인다.
정상 장기 run도 별도로 최소 6~12시간 확보해 false alarm과 benign workload 변화 평가에 사용한다.

## 8. 데이터 계층

Raw: 원본 API 응답/요청 범위, transaction events, source timestamps, traces/logs/events, injector 응답, 환경 snapshot.
Canonical: 공통 시간/단위/identity와 provenance를 가진 long-format metrics, spans, logs, topology.
Model view: 과거 정보만 사용하는 window features, masks, feature schema/version. 정답과 injector 증거 제외.
RE2 adapter: 고정한 RCAEval release/loader와 실제 sample을 대상으로 검증한 호환 projection. 이름/형태만 비슷하면 통과시키지 않는다.

- metrics: CPU seconds/rate cores 및 limit ratio, throttle seconds/periods, memory bytes/limit, requests/errors/duration histograms,
  TCP retransmission, interface drops, readiness/restarts, node pressure, scrape/collector health.
- traces: trace_id/span_id/parent_span_id, service/Pod UID, start/end/duration, span kind, protocol-specific status, attributes.
- logs: event/observed timestamps, service/Pod UID/container, severity/body, trace_id가 실제 존재할 때만 보존.
- transaction: start/end, stage, success/error/timeout, actual rate와 dropped. HTTP 500은 정상적으로 수집된 장애 결과다.
- topology: 실행 중 관측된 caller/callee, operation과 시간. 주입 target을 이용해 graph를 생성하지 않는다.
- identity: 앱 7개 tracing 서비스와 미계측 서비스의 coverage map. uninstrumented를 zero traffic으로 해석하지 않는다.

초기 Linux resource scrape와 exporter grid 목표는 5초로 통일한다. cAdvisor 내부 갱신 주기도 별도 확인한다.
transaction은 event 단위, trace/log는 원래 정밀도로 보존한다. histogram rate window와 source sample age를 기록한다.
1초 모델 grid가 필요하면 resampling provenance/mask를 함께 제공한다. forward fill은 진짜 1초 관측이 아니다.
CPU 단위 cores와 memory bytes를 명시한다. CPU와 spanmetrics/RED의 수치 합이 같아야 한다는 검증은 하지 않는다.
missing/null과 true zero를 구별한다. 잘못된 source timestamp는 원본에 남기고 projection 제외 수를 공개한다.

## 9. ground truth와 recovery

incident_id/pair_id/run_id/environment_id/campaign_id, fault_type/target_kind/target_id, parameters,
requested/applied/observed/removed times, clock error/observation uncertainty, workload_seed,
impact_status, recovery_status, censoring, validation flags, policy/schema/injector versions를 보존한다.
환경 ID/Pod 랜덤 이름/run 파일명/Chaos 이벤트/주입 로그/known target은 기본 모델 입력에서 제외한다.

v4 제안:
- impact는 정상 control 대비 error 5%p 증가 또는 p99 3배 조건을 초기 비교 기준으로 유지하되 Linux calibration에서 민감도를 확인한다.
- source telemetry gap과 실제 요청 실패/traffic 중단을 구분한다. 전자는 품질 문제, 후자는 장애 신호일 수 있다.
- recovery 후보는 정상 성공률·latency 범위를 만족하는 연속 120초 구간의 시작. 전체 구간 확인 후에만 confirmed_at을 부여한다.
- recovery_time = candidate stable onset - measured fault removal. onset과 confirmed_at을 함께 공개한다.
- 영향 없는 CPU는 recovery not_applicable; near-zero recovery target으로 학습시키지 않는다.
- 영향 있고 끝까지 회복이 확인되지 않음: right_censored=true, observed horizon을 기록한다. 10분을 실제 recovery time으로 대입하지 않는다.
- telemetry가 끊겨 판단 불가: unknown/data-gap. 완전 관측된 right-censoring과 구별한다.
- 고정된 5분 fault 해제 실험은 운영자가 수리하기까지의 MTTR을 학습하는 데이터가 아니다.
- 미래 recovery 정보는 label 작성에는 쓸 수 있지만 실시간 RCA feature에는 쓰지 않는다.

기존 v3 결과를 위 규칙으로 덮어쓰지 않는다. 새 label generation과 변경 이력을 만든다.

## 10. 유효성 게이트와 무결성

G0: capability/endpoint/시간/정상 상태 및 기존 fault 없음.
G1: 충분한 generator capacity, 계획 arrival 대비 실제 시작/완료/실패/dropped 설명 가능.
G2: 실제 target에 fault effect 존재. CR 생성 성공만으로 injection_success를 부여하지 않는다.
G3: fault 제거 실측, 영향받은 리소스 원복. 관측 stack 장애는 앱 장애와 별도 분류.
G4: metric source coverage, trace search limit/known IDs/late spans, Loki chunk 경계·중복·실패, event identity 검사.
G5: raw/canonical/model/RE2 값·단위·행 수·변환 손실·해시 및 loader smoke check.
G6: control pairing/실측 영향/recovery/censoring 검토. pair/incident 단위로 human spot review.

data_valid, injection_valid, impact_confirmed, recovery_observable, eligible_tasks를 분리한다.
장애로 app log/span이 안 나오는 것은 자동 export 실패가 아니다. exporter 실패는 별도 health evidence로 판정한다.
complete=true는 수집기가 검사할 수 있는 응답/known ID 범위의 완료이며 모든 원본 span 존재의 증명이 아니다.
학습에는 quality threshold를 사전 만족한 run만 포함하되 no-impact와 invalid도 전체 registry에는 남긴다.

## 11. 분할과 모델 평가

동일 pair, control 재사용 group, 같은 incident의 모든 window는 한 split에 넣는다.
실행 전 repetition/session 단위 split을 계획한다. 작은 B 데이터는 고정 test+grouped validation, C에서 대략 60/20/20을 검토한다.
normalization, log template/embedding fitting, threshold 선택은 train/validation 범위로 제한한다.
미관측 target/날짜/부하/severity 평가와 별도 환경 E2 평가를 구분한다.
환경별 모든 fault를 포함시키고 platform만으로 fault를 예측하는 shortcut baseline도 검사한다.

RCA는 원인 서비스/edge 순위화와 fault type 분류를 별도 평가한다.
공식 RE2와의 fault 공통집합은 현재 계획 기준 CPU/delay/loss이다. Pod/surge를 RE2의 MEM/DISK/SOCKET로 바꾸어 매핑하지 않는다.
5종 classifier로 RE2 전체 6종 closed-set accuracy를 주장하지 않는다. 미학습 원인은 unknown 평가로 따로 다룬다.
RCAEval에는 이 설계의 recovery label이 제공된다고 가정하지 않는다. recovery 외부 평가는 별도 데이터가 필요하다.
평가용 RE2를 반복 보며 튜닝하면 외부 test가 아니게 된다. adapter contract 검증과 model selection을 분리한다.

비교: 정상 threshold/빈도 prior/random ranking/BARO류 baseline, temporal CNN(TCN), topology-aware temporal graph 모델.
RF/GBDT도 sanity baseline으로 유지한다. 복잡한 모델의 이득을 확인하기 위한 비교군이다.
Top-1/Top-3/MRR, macro-F1, false alarms/hour, detection delay, abstention coverage/accuracy,
incident-level confidence interval 및 censor-aware recovery metric을 보고한다. Top-5만 보고하지 않는다.
제품 단계는 실제 운영 shadow 평가, 미지 fault/배포 변화/관측 누락/복합 fault 시험 뒤 별도로 판단한다.

## 12. 구현 순서와 승인 지점

1. 하드웨어/VM 자원과 접근 방식 확정, 환경 manifest 설계. 유료 리소스 생성은 별도 승인.
2. capability probe와 cleanup/stop-after-control을 먼저 구현·검증.
3. Linux orchestrator, Job-based k6, timestamp/heartbeat/deadline, 영속 queue 상태와 알림 구현.
4. schema/label policy v4, true source coverage와 effect evidence, RE2 loader contract test 구현.
5. 정상 용량 교정 후 A단계 10 fault/10 control. 끝나면 데이터와 실제 비용/시간을 보고.
6. B의 조합표/예산/분할을 고정한 후 69 fault까지 확장. 필요하면 학습 곡선 보고 C 승인.

면담까지 3일이면 현실적 목표는 capability 증거, 부하 교정 결과, 5종별 end-to-end pilot,
데이터 dictionary, 기존 WSL 결과와의 차이 및 수집 시간 계획이다. 대규모 딥러닝 학습 완료를 약속하지 않는다.

## 근거

- RCAEval 논문: https://arxiv.org/html/2412.17015v5 (3.2 주입 도구, 3.3 부하/수집, 3.4 schema).
- RCAEval 공식 저장소: https://github.com/phamquiluan/RCAEval (버전과 실제 loader/sample을 고정해서 사용).
- Chaos Mesh NetworkChaos: https://chaos-mesh.org/docs/simulate-network-chaos-on-kubernetes/ (NET_SCH_NETEM 및 제어 연결 조건).
- 로컬 구현 확인: scripts/evaluation_v2.py, scripts/run-attended.py, scripts/re2-projection.py,
  incidents/evaluation-policy-v3.md, docs/TELEMETRY_EXPORT.md.
