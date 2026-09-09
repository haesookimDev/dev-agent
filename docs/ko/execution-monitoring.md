# 실행·전달 메타데이터 경과 시간 관측

한국어 | [English](../en/execution-monitoring.md)

## 범위와 지표 계약

API의 [읽기 전용 실행 상태 Snapshot](runtime-monitoring.md)에 실행 단계와 영속 `DeliveryJob`의 수·경과 시간을 추가합니다. 같은 집계 SQL, 시도 완료 후 10초 주기, 총 2초 제한, 불변 캐시와 30초 신선도 경계를 사용합니다. `/metrics`는 DB를 조회하지 않습니다. 새 의존성·환경변수·Schema Migration·수명주기 쓰기·자동 복구는 없습니다.

모든 이름은 `kelpie_runtime_` 접두사와 고정된 `state` Label만 사용합니다.

| 접미사 | 상태와 의미 |
| --- | --- |
| `execution_work{state}` | `provisioning`, `analyzing`, `implementing`, `verifying`, `committing`, `pr_created`의 작업 수 |
| `execution_oldest_update_age_seconds{state}` | 각 상태의 가장 오래된 `WorkItem.updated_at` 이후 시간 |
| `delivery_jobs{state}` | `pending`, `retry`, `running`, `completed`, `failed`, `unknown`의 전달 작업 수 |
| `delivery_oldest_update_age_seconds{state}` | 각 상태의 가장 오래된 `DeliveryJob.updated_at` 이후 시간 |

API Instance마다 24개 시계열이 추가되며 조직·저장소·작업/Worker 식별자·원본 오류·자격증명은 포함하지 않습니다. 알 수 없는 전달 상태는 `unknown`으로 묶고 원본 상태 문자열을 Label로 내보내지 않습니다. 승인·피드백·입력·예산 대기, Queued·종료 작업은 실행 수치에서 제외합니다. 완료·실패 전달 이력은 관측하지만 진행 중 작업의 정체 알림을 계속 발생시키지는 않습니다.

경과 시간은 **메타데이터 갱신 이후 시간**이지 마지막 AgentEvent·단계 진입·실제 진행 이후 시간이나 VM 종료 증명이 아닙니다. 다른 메타데이터 갱신으로 초기화될 수 있으며 정상적인 긴 단계도 경고할 수 있습니다. 기존 Queue는 별도로 **생성 시각** 기준을 유지합니다. 성공적으로 조회한 빈 상태는 수·경과 시간 모두 명시적 0이며 미래 갱신 시각의 나이는 0으로 제한합니다. 첫 성공 전에는 모든 도메인 지표가 없습니다. 조회 실패 시 이전 값을 유지하되 `snapshot_available=0`이며 오래되거나 실패한 관측은 정상이 아닙니다.

`/metrics`와 Prometheus UI/API는 승인된 내부 네트워크에만 노출합니다. 전역 관측이므로 여러 API Instance의 동일 Snapshot을 합산하지 않습니다. 큰 테이블 조회도 기존 제한 시간을 공유하며 Timeout을 빈 Fleet으로 취급하지 않습니다. 기존 단일 API Process 전달 배포 경계는 유지합니다.

## 알림과 안전한 대응

[설치 안내](monitoring-alerts.md)에 따라 API와 일치하는 [규칙](../../infra/monitoring/alerts.yml)을 함께 적용합니다. 15초 수집·30초 평가를 유지합니다. 세 알림 모두 Warning이며 조건이 **2분 연속 관찰**되어야 발생하고 조건 해소 후 다음 성공한 평가에서 해제됩니다. 이번 변경은 외부 Alertmanager 수신자나 운영 배포를 구성하지 않습니다.

`kelpie:execution_snapshot_usable` Recording Rule은 기존 Runtime Gate와 새 네 지표 집합의 모든 필수 상태에 유한한 0 이상 값이 정확히 하나씩 있는지 확인합니다. 누락·Stale·음수·NaN·무한대·중복 상태를 0으로 대신하지 않습니다. 다른 Job/Target이 빠진 상태를 채울 수도 없습니다. 새 지표만 유실돼도 유효한 Worker·Lease·Queue 알림은 유지하도록 Gate를 분리했습니다.

<a id="observation"></a>

### `KelpieExecutionObservationUnavailable`

기본 Runtime 관측은 유효하지만 확장 실행 지표 계약을 충족하지 못합니다. API·규칙 Version 정합성과 Metric Relabeling, 해당 Target의 네 집합에 각각 여섯 상태가 모두 있는지 확인합니다. 0 시계열을 만들거나 Gate를 끄지 말고 의도한 수집을 복원합니다. 전체 관측이나 Scrape 실패는 기존 Runtime/Scrape 알림을 사용하며 이때 정체 알림이 없다고 복구된 것은 아닙니다. `/readyz` 성공만으로 관측 대상 테이블의 조회 성공을 증명할 수 없습니다.

<a id="execution"></a>

### `KelpieExecutionPhaseStalled`

표시된 단계에 작업이 있고 가장 오래된 메타데이터 경과 시간이 다음 기준을 초과합니다.

| 단계 | 2분 연속 대기 전 임계값 |
| --- | --- |
| `provisioning`, `committing`, `pr_created` | 600초 초과 |
| `analyzing`, `implementing`, `verifying` | 1,800초 초과 |

권한이 있는 작업 상세·Event, Worker Heartbeat·격리, Lease 유효성과 실제 Host 관측으로 정상적인 긴 작업인지 진행 손실인지 구분합니다. 이 기준은 조정 가능한 시작점이지 작업 Timeout·SLO가 아니며 조정 시 규칙 Fixture도 갱신합니다. 사람의 대기는 의도된 것이므로 우회하지 않습니다. 이 알림만으로 실행 작업을 취소하거나 Lease 해제·VM 재시작을 수행하지 않습니다. 별도로 감사되고 실제 Host를 검증하는 복구 절차가 필요합니다.

<a id="delivery"></a>

### `KelpieDeliveryJobStalled`

`pending`, `retry`, `running`, `unknown` 전달 작업의 메타데이터 경과 시간이 600초를 초과합니다. 권한이 있는 작업 상태, [승인 연결 감사](delivery-audit.md), [안전한 실패 코드](delivery-failure-safety.md), Bundle 무결성, 시작 시 복구와 SCM 접근을 확인합니다. 알 수 없는 상태는 원본 값을 내보내지 말고 지원 API Version과 비교합니다. `completed`·`failed` 이력은 이 알림을 발생시키지 않으며 관찰된 실패는 [별도 Counter 기반 알림](monitoring-alerts.md#delivery-failures)으로 다룹니다.

승인을 만들거나 Job 상태를 수정하고, 두 번째 API 전달 Process를 띄우거나 격리된 작업을 재시도하고 자원 Gate를 우회하지 않습니다. 인수 테스트의 합성 상태 변경은 제품 복구 API가 아닙니다. 알림은 실제 SCM 발행·VM 건강 상태를 증명하지 않습니다.

## 검증과 롤백

`make test-api`, `make lint`를 실행합니다. `test_runtime_health.py`는 SQLite와 전용 테스트 DB의 `KELPIE_TEST_POSTGRES_URL`을 지정한 PostgreSQL을 검증하며 URL이 없으면 DB 사례 다섯 개가 Skip됩니다. 기존 필수 `Python` CI는 PostgreSQL Service를 사용합니다. `test_execution_health_http.py`는 실제 마이그레이션된 SQLite·Uvicorn·HTTP로 전달 테이블 조회 장애, 이전 값 유지와 API 재시작 없는 복구를 검증합니다. 원래 10초 주기(약 31초)를 유지하며 `/metrics` 응답과 Worker·Lease·감사 무변경도 확인합니다.

`make test-monitoring PROMTOOL=/path/to/promtool`은 두 Fixture 파일을 실행하며 규칙 14개(알림 12개·Recording Rule 2개), 시나리오 44개·시점별 검증 174개를 다룹니다. 새 검증은 모든 단계 임계값, 사람 대기·종료 상태 제외, Pending/Firing/복구, 누락·비정상·중복 상태, 부분 수집 손실 시 기존 알림 유지를 포함합니다. 필수 `Go` CI는 Checksum을 검증한 캐시 Prometheus와 가상 시계를 재사용하므로 추가 Job·자격증명·Timeout 증가는 없습니다.

새 API 지표와 그에 의존하는 실행 규칙을 함께 되돌린 후 설정·Fixture를 검증하고 승인된 재시작/Reload를 수행합니다. 이전 API에 새 규칙을 남겨 두면 의도적으로 관측 누락 경고가 발생합니다. 데이터 Migration·감사 재작성·TSDB 삭제·자원 조작은 필요하지 않습니다.

## 실제 구동 검증 기록 — 2026-09-09

구현 `72186d3`, 규칙 `1e61809`와 중복 판정 회귀 수정 `440e208`, 실제 HTTP 회귀 `3d5e689`를 기준으로 검증했습니다. 전달 장애 주입 테스트의 제한 시간 범위를 분리한 PR #41을 정상 Merge한 `de0440c`에서 전용 임시 PostgreSQL DB를 사용하는 `make test`가 API 989개(193.01초, Skip 없음), Runner 6개, Worker·Gateway Go 테스트, Web 91개·타입 검사를 통과했습니다. `make lint`와 위 44개 규칙 시나리오도 통과했습니다. 검증을 빠르게 만들기 위한 제품 Timeout 변경은 없습니다.

마이그레이션된 일회용 SQLite·실제 Uvicorn/API, 루프백 수집 Proxy와 Checksum을 재확인한 Prometheus 3.14.0을 사용했습니다. 제품의 10초 관측 주기·15초 수집·30초 평가·2분 대기를 단축하지 않았습니다. `committing` 두 작업·`implementing` 한 작업, `pending`·`running` 전달 각 한 개는 시작 시 자동 복구 완료 후 합성 행으로 만들었으며 실제 SCM 전달이나 VM 실행은 하지 않았습니다.

2026-09-06 선행 구동에서는 당시 규칙 `1e61809`의 전달 `pending` 지표만 누락시켰을 때 관측 불가 Pending→Firing, 새 정체 알림 억제와 기존 세 경고 유지를 확인했습니다. 최종 중복 판정 수정의 검증과는 구분합니다.

최종 구동의 UTC 관측 기록은 다음과 같습니다.

| 시각 | 확인한 상태 |
| --- | --- |
| 00:41:23부터 Pending, 00:43:34 확인 | 실행 두 단계·전달 두 상태의 네 Instance 모두 Firing |
| 00:47:53부터 Pending, 00:49:55 확인 | 정상 `pending` 사본을 남긴 채 NaN 중복을 주입하자 확장 Gate가 사라지고 관측 불가 Firing; 새 정체 알림은 억제되고 기존 세 경고는 유지 |
| 00:50:23 평가 → 00:50:53 평가 이후 확인 | 중복 제거·합성 상태 복구 후 Gate 1 복원. 첫 평가에서는 이전 Snapshot으로 정체가 잠시 Pending이었다가 다음 평가에서 새 알림 세 종류 모두 해제; 기존 Worker·Lease·Queue 세 경고만 Firing |

복구는 소유한 합성 Work를 승인 대기로, 전달을 완료로 바꾼 테스트 조작입니다. Worker·Lease·감사 행이 전후 동일함을 확인했으며 만료된 활성 Lease는 남아 있습니다. 전체 시스템 정상·자원 회수·실제 VM 복구를 주장하지 않습니다.

Orca 브라우저에서 규칙을 펼쳐 한국어·영어 요약·Runbook·2분 대기를 확인하고 새로고침한 상태 및 실제 데스크톱 화면을 Computer-use로 검사했습니다. 캡처된 브라우저 요청 36개는 모두 200, 외부 요청과 Console 메시지는 0개였습니다. 로컬 경로·다른 프로젝트 정보가 보이는 스크린샷은 공개하지 않았습니다. OS Focus가 지원되지 않아 네이티브 키보드·마우스 입력은 검증하지 않았습니다. 외부 알림 수신·운영 배포·실제 Worker/KVM 검증도 수행하지 않았습니다.

소유한 API·Proxy·Prometheus를 종료하고 브라우저·터미널 탭을 닫은 뒤 루프백 포트 18570·18571·19570의 Listen이 없음을 확인했습니다. 합성 DB는 Fixture 수명주기에 따라 정리했으며 기존 사용자 터미널과 서비스에는 종료·변경 명령을 보내지 않았습니다.
