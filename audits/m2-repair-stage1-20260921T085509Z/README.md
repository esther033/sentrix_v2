# M2 복구 작업 — 1단계 완료

## 보존 및 점검 결과

- 수정 전 소스·설정·문서 46개를 source-snapshot/에 복사했습니다.
- run 9개, 파일 18,987개의 크기·수정 시각을 inventory.json에 기록했습니다.
- 소스와 주요 산출물의 SHA-256을 기록하고, 점검 종료 시 변경되지 않았음을 확인했습니다.
- 기존 소스, run, incident 기록은 수정·삭제하지 않았습니다.
- 보존 사본은 수정 전 미커밋 변경까지 포함합니다. Git HEAD와 별개입니다.

## 현재 실행 환경

- context: docker-desktop.
- 조회된 sentrix Pod 19개는 모두 Running입니다.
- 다수 Pod가 점검 약 7분 전에 함께 재시작했습니다. 원인과 장기 안정성은 아직 확인하지 않았습니다.
- StressChaos 3개는 모두 desiredPhase=Stop, AllInjected=False, AllRecovered=True입니다.
- 원본 응답: cluster-pods.json, cluster-chaos.json. 현재 상태의 증거이며 과거 run 당시의 실시간 관측을 대체하지 않습니다.
- 세션에서 조회 가능한 python/k6/kubectl 프로세스는 발견되지 않았습니다.

## 데이터 취급

| run | 다음 단계에서의 취급 |
|---|---|
| M1A/M1B 4개 | transaction 기준 자료 유지. 전체 telemetry archive로 간주하지 않음 |
| M2 dryrun | transaction trace 파일 1,166개 누락. 개발 자료로 보존 |
| CPU pilot-001 INVALID | transaction trace 파일 1,293개 누락. invalid 유지 |
| CPU pilot-002 | transaction trace 파일 1,205개 누락. 과거 complete=true를 신뢰하지 않고 공식 평가 제외 |
| fastcheck control | 알려진 transaction trace 479개 존재. raw 보존, 파생 view 재검증 필요 |
| fastcheck pilot | 알려진 transaction trace 473개 존재. 로그 4개 청크/80초 누락, complete=false |

이 표는 기존 판정을 덮어쓰지 않는 별도 감사 의견입니다. 파일 존재 검증은 span 전체 보존의 증명이 아닙니다.

## 다음 단계: export만 수정

1. 성공한 raw를 보존하는 청크별 재개·원자적 저장.
2. 검색 상한, trace 응답, 빈 metric, 시간 구간 누락의 완전성 검증.
3. 재시도 및 시간 제한, 진행 체크포인트와 실패 이유 기록.
4. backend 없이 실행하는 실패·재개 회귀 검증.

canonical/RE2, recovery, 새 부하 실험은 이후 단계로 분리합니다. PVC 초기화는 하지 않습니다.
