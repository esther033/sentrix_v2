# SentriX 진행 상황 요약 (교수님 면담용)

작성일: 2026-09-22
프로젝트 전체 목표·설계 원칙은 [`sentrix-project-context.md`](sentrix-project-context.md) 참고. 이 문서는 지금까지 실제로 구현·검증한 것과 그 과정에서 발견한 문제/한계를 정리한 진행 보고서.

---

## 1. 전체 그림

| Milestone | 목표 | 상태 |
|---|---|---|
| M1 — Observable evaluation path | fault 없이 telemetry 수집 경로 자체를 검증 가능하게 만들기 | **완료** |
| M2 — 통제된 incident dataset | Chaos Mesh로 fault 주입, 영향·복구 측정 체계 구축 | **핵심 파이프라인 구현 완료, 신뢰성 강화 작업 진행 중** |

두 milestone 모두 "구현했다"에서 끝내지 않고, **다른 관점(코드 리뷰)에서 재검증 → 실제 버그 발견 → 수정 → 재검증**을 반복하는 방식으로 진행함. 아래에 그 과정의 결과(발견한 실제 버그들)를 의도적으로 포함시킴 — 이게 이 프로젝트에서 가장 시간을 많이 쓴 부분이자, "관측 파이프라인 자체의 정확성을 어떻게 담보하는가"라는 연구 질문에 대한 실질적 답이기도 함.

---

## 2. Milestone 1 — Observable evaluation path

### 구축한 것
- Docker Desktop 단일 노드 Kubernetes에 Online Boutique(공식 GitHub repo, commit SHA 고정) 배포
- OpenTelemetry Collector(gateway + daemonset), Prometheus, Tempo, Loki로 최소 구성 observability stack 구축
- k6 기반 `browse → cart → checkout` synthetic transaction generator
  - transaction마다 W3C `traceparent` 생성, stage(browse_home/browse_product/cart_add/cart_view/checkout)마다 새 span_id 부여 → 하나의 trace로 Tempo에서 조회 가능하면서 단계별 구분도 가능
  - 요청별 성공/실패/timeout/assertion을 JSONL로 개별 기록 (k6 요약 통계에 의존하지 않음)
- **M1A telemetry smoke gate**: 하나의 transaction으로 trace→Tempo, metric→Prometheus, log→Loki, identity 해석, Kubernetes Event 수집까지 전체 경로가 실제로 동작하는지 검증하는 자동화 게이트
- **M1B**: low(0.5rps)/main(2rps)/high-normal(5rps) 3개 no-fault control run으로 baseline 확보 (steady-state 구간과 전체 run 구간을 분리해서 집계)

### 진행 중 만난 문제와 해결
| 문제 | 원인 | 해결 |
|---|---|---|
| Kubernetes 클러스터 자체가 기동 안 됨 | Windows Update 이후 WSL2가 cgroup v1/v2 hybrid로 부팅되며 kubelet의 systemd cgroup driver 요구사항 불충족 | `.wslconfig`에 `cgroup_no_v1=all` 추가 |
| currencyservice가 tracing 켜자마자 crash-loop | tracing 오버헤드를 고려 안 한 CPU limit(200m)이 부족해 health probe 실패 | CPU limit 500m로 상향 |
| Tempo가 반복적으로 OOMKilled | 메모리 limit이 실제 trace 처리량 대비 부족, 게다가 host가 sleep했다 깨어날 때마다 WAL replay 부담 누적 | limit 상향(최종 2Gi), retention을 6h→1h로 단축해 replay 부담 자체를 줄임 |
| baseline dropped-iteration 집계가 실제와 9시간 어긋남 | k6 raw metric의 timestamp가 로컬시간(+09:00)인데 UTC로 잘못 취급 | timezone 변환 로직 수정, 기존 run 데이터 재생성 |

### 리뷰로 발견한 검증 로직 자체의 버그 (M1)
같은 사람이 코드도 짜고 검증도 하면 놓치는 게 생겨서, 별도 리뷰를 거쳤고 다음을 발견·수정함:
- timeout 감지가 k6의 실제 실패 방식(`res.status===0`+`error_code`)과 안 맞아서 항상 `false`로 기록되던 문제
- quality gate가 "이전 run이 남긴 stale metric"만으로도 통과할 수 있었던 문제 → 시간창을 run 시작 시각 이후로 제한
- 로그 검증이 "같은 배포의 아무 Pod"만 확인해서 재시작 후 다른 Pod의 로그로도 통과할 수 있었던 문제 → trace에서 실제 처리한 Pod 이름을 추출해 고정
- 빈 응답 본문이 business assertion을 통과하던 문제

**M1 결론**: 하나의 `run_id`로 workload 설정·실제 요청률·transaction 결과·metric·log·trace·Kubernetes event를 시간 기준으로 join 가능. no-fault 상태의 baseline(성공률/지연시간 분포)을 3개 부하 수준에서 확보.

---

## 3. Milestone 2 — 통제된 incident dataset

### 구축한 것
- **RE2-OB 스키마 실측 검증**: RCAEval 공개 데이터셋(HuggingFace)에서 실제 case 하나를 다운로드해 metrics/traces/logs의 정확한 컬럼명·단위·샘플링 주기를 확인 (문서만 보고 추측하지 않음)
- **3단계 데이터 뷰 파이프라인**: raw export(Tempo/Prometheus/Loki 원본 API 응답 보존) → canonical(identity 해석·정규화된 long-format) → RE2-compatible(RE2-OB와 같은 wide-format 1초 window로 투영, 손실 정보는 문서화)
- **Chaos Mesh 설치**: CPU saturation, network delay, packet loss, pod failure에 대한 fault 템플릿 작성 (traffic surge는 기존 workload profile 메커니즘 재사용)
- **Incident registry**: fault 유형·영향·복구를 분리 기록하는 스키마, 그리고 실험 전에 고정한 `evaluation-policy-v1`(정상/영향/복구 판정 기준)
- **CPU saturation pilot**: checkoutservice에 실제 CPU stress 주입 → Chaos Mesh 상태를 직접 폴링해 주입/제거 시각 확보 → 영향 확인 → 복구 여부 평가까지 end-to-end 1회 실행 성공

### 리뷰로 발견한 M2 파이프라인의 실제 버그 (전부 수정 완료)
1. **Trace export가 검색 상한(5000개)에 걸려 조용히 잘림** — `discovered==exported==5000`이 "다 받았다"는 뜻이 아니었음. 이번 transaction들의 trace_id를 직접 대조해서 발견 (2,279개 중 1,205개 누락). → 시간 구간을 나눠 검색 + transaction이 실제로 생성한 trace_id를 별도로 직접 조회하는 방식으로 수정
2. **일부 실패가 있어도 `complete: true`로 기록될 수 있었음** — 예외가 밖으로 안 나가면 내부 실패(개별 trace 실패, metric 오류, Loki 청크 실패)를 검사 안 하고 있었음 → 각 신호별 내부 실패를 명시적으로 검사하도록 수정
3. **recovery 판정이 이 run의 control이 아니라 예전 M1B 상수를 기준으로 사용** — 정책 문서는 "새 control run과 비교"를 요구했는데 코드는 안 그랬음. 실제로 fault 주입 직전 이미 이 run의 latency가 M1B 기준보다 높아서, "CPU stress 때문에 7분간 회복 안 됨"이라는 결론 자체가 근거 부족이었음 → 매 fault run마다 동일 환경/길이의 fresh control run을 요구하고 거기서 baseline을 동적으로 계산하도록 수정
4. **recovery의 "2분간 관찰" 조건이 실제로 2분치 데이터가 있는지 확인 안 함** — 데이터가 5초치만 있어도 통과할 수 있었음 → 실제 관측된 시간 구간 길이를 검증하도록 수정
5. **fault 제거 확인이 통신 오류를 "제거 완료"로 오인할 수 있었음** — kubectl 호출이 API 오류로 실패해도 "오브젝트가 없어졌다"로 처리 → NotFound(진짜 삭제됨)와 그 외 오류를 구분하도록 수정. 부수적으로 `fault.injected: false`가 항상 찍히던 metadata 버그, 잘못된 rate 계산 버그도 함께 수정

### 현재 상태 (진행 중)
위 5개를 고친 뒤 4분짜리 축소 버전으로 재검증까지는 마쳤으나, **작업 도중 노트북이 여러 차례 절전 모드로 들어가면서 19분짜리 실제 실험이 3번 정도 중간에 죽는 일이 반복됨** — 그때마다 Tempo(1h)/Prometheus(6h) retention이 만료돼 데이터 일부가 영구 손실됨. 이 문제를 근본적으로 해결하기 위해 **resumable export 아키텍처**(체크포인트, atomic write, host-suspend에도 안전한 watchdog timer, 실패 시 정확히 어디서 실패했는지 기록)로 export 계층을 재작성하는 작업이 진행 중이며, unit test 40개 이상으로 오프라인 검증도 함께 구축함. 이 작업의 4단계 감사 기록이 `audits/`에 있음.

**따라서 M2의 CPU saturation pilot은 "파이프라인이 end-to-end로 동작한다"는 것까지는 증명됐지만, 그 결과 수치(영향 정도, 복구 여부)를 공식 결론으로 아직 확정하지 않은 상태.** 신뢰성 강화 작업이 끝나는 대로 pilot을 다시 실행해 확정하고, 이후 나머지 4개 fault 유형(network delay, packet loss, pod failure, traffic surge)으로 확장할 계획.

---

## 4. 이번 단계에서 얻은 교훈

1. **"배포했다"와 "수집된다"는 다르다.** Collector를 띄운다고 trace/log가 자동으로 오지 않음 — 서비스별 OTLP 환경변수, DaemonSet의 filelog 설정, Loki의 structured-metadata vs stream-label 구분 등을 각각 개별적으로 검증해야 했음.
2. **검증 코드도 검증이 필요하다.** M1·M2 모두 "다 됐다"고 보고한 뒤 리뷰에서 실제 버그가 나왔음 — 특히 recovery 판정처럼 "이 정책이 맞는지" 자체가 데이터 품질에 의존하는 경우, 비교 기준(control run)이 최신인지가 결론의 타당성을 좌우함.
3. **로컬 파일럿 환경(Docker Desktop, 단일 노드, 개인 노트북)의 현실적 제약**이 연구 설계에 실제로 영향을 줌 — retention 정책, 메모리 예산, host suspend 같은 인프라 문제가 "며칠짜리 실험"의 신뢰도를 좌우한다는 걸 직접 겪음. 이는 상용 observability 플랫폼이 왜 항상-켜져 있는 인프라를 전제하는지에 대한 실질적 이해로 이어짐.

---

## 5. 다음 단계
1. resumable export 검증 완료 → CPU saturation pilot 재확정
2. network delay / packet loss / pod failure / traffic surge 순으로 확장
3. 각 fault마다 동일 조건 control run 페어링, incident registry에 누적
4. Milestone 3(RCA baseline)로 이관
