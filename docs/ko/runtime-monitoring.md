# Worker·Lease·대기열 지속 관측

한국어 | [English](../en/runtime-monitoring.md)

## 범위와 안전 경계

API는 시작 시 전달 복구와 독립적으로 DB의 Worker Heartbeat, 활성 Lease 만료, `queued` 작업을 읽고 `/metrics`에 내보냅니다. 조회는 하나의 집계 SQL이며 개별 작업·조직·저장소·Worker 식별자나 자격증명을 지표 Label에 넣지 않습니다. 이는 DB 기록의 관측이지 실제 VM·네트워크·SCM의 정상 동작 증명이 아닙니다. 자동 취소, 재승인, 재시도, Lease 해제, VM 재시작·정리는 수행하지 않습니다.

시작 직후 첫 관측을 시도하고, 매 시도 완료 후 10초 대기합니다. Schema 검사·연결 확보·조회·연결 정리에 총 2초 제한을 적용합니다. 실패하면 고정된 경고만 기록하고 재시도하며, 전체 조회와 연결 정리가 성공해야 새 불변 Snapshot을 발행합니다. 미준비 Schema는 관측 불가입니다. `/metrics` 요청은 캐시만 읽으므로 DB 장애나 전달 복구 대기에 막히지 않습니다. 종료 시 감시 Task를 취소·회수하고 다음 Lifespan에서는 이전 관측을 초기화합니다.

DB 부담은 API Process마다 약 10~12초 간격의 Schema 검사와 집계 조회입니다. PostgreSQL에서는 한 SQL Statement의 일관된 Snapshot을 사용하며 행 잠금·쓰기·Migration·새 의존성을 추가하지 않습니다. 큰 테이블에서 제한 시간을 넘으면 정상 값으로 대체하지 않고 관측 불가로 알립니다. 기존 [단일 API Process 전달 경계](delivery-recovery.md)를 유지하세요. 여러 API Instance의 같은 전역 수치를 합산하면 중복 집계됩니다.

## 지표 계약

모든 이름은 `kelpie_runtime_` 접두사를 사용합니다. 외부 공개용 API가 아니며 `/metrics`와 Prometheus UI/API는 승인된 내부 네트워크에만 노출합니다.

| 접미사 | 의미 |
| --- | --- |
| `snapshot_available` | 마지막 시도가 성공했고 마지막 성공의 Monotonic 경과 시간이 30초 이하이면 1, 그 외 0 |
| `snapshot_age_seconds` | 마지막 성공 이후 Monotonic 경과 시간. 최초 성공 전 `NaN` |
| `snapshot_timestamp_seconds` | 마지막 성공 조회 시작의 Unix 시각. 최초 성공 전 0, 진단용 |
| `workers{state}` | 고정된 `online`, `draining`, `offline`, `quarantined`의 등록 Worker 수 |
| `leases{state}` | DB에서 `state=active`인 Lease 중 `active`(만료 시각이 관측 시각 이상)·`expired`(관측 시각 미만)의 수 |
| `queued_work` | 현재 `queued`인 작업 수. 승인·피드백·입력 대기는 제외 |
| `queue_oldest_age_seconds` | 현재 `queued`인 가장 오래된 작업의 생성 이후 시간. 관측 당시 값이며 빈 대기열은 0 |

Worker 분류는 **격리 → 오프라인 → Draining → Online** 순입니다. `quarantined_at`이 있으면 별도 집계합니다. 명시적 `OFFLINE`이거나 마지막 Heartbeat가 기존 `WORKER_OFFLINE_SECONDS`(기본 45초) 이상 지났으면 오프라인입니다. 이 설정은 관측 분류에 사용되며 자원을 회수하지 않습니다. Future Heartbeat는 시각이 따라잡을 때까지 오래된 것으로 판정하지 않으며, Future 작업 생성 시각의 나이는 0으로 제한합니다. 운영 Clock 동기화도 확인하세요.

Lease의 `expires_at < 관측 시각` 기준은 기존 Lease 검증과 같습니다. 해제·격리된 Lease는 두 수치 모두에서 제외합니다. Queue 나이는 마지막 상태 변경이나 재시도 이후 시간이 아니라 **생성 시각** 기준입니다. 실행 중인 작업과 `DeliveryJob` 대기·진행 상태 전체를 감시하지 않습니다.

최초 성공 전에는 Worker·Lease·Queue 지표가 **없습니다**. 성공적으로 조회한 빈 DB만 해당 지표에 0을 제공합니다. 이후 실패하면 이전 수치는 진단용으로 남지만 `snapshot_available=0`이며, 갱신이 끊기면 30초 이후에도 0입니다. 이전 수치나 시작 시 전달 복구 `completed=1`만 보고 시스템이 정상이라고 판단하지 마세요.

## 알림과 대응

[기본 설치 안내](monitoring-alerts.md)에 따라 최신 API와 [규칙](../../infra/monitoring/alerts.yml)을 함께 적용합니다. 기존 15초 Scrape·30초 평가와 최소 권한 CI를 유지합니다. 신규 환경변수나 운영 자동 배포는 없습니다.

`kelpie:runtime_snapshot_usable` Recording Rule은 성공·신선도·현재 Scrape 성공·필수 상태별 수치 존재를 함께 확인합니다. 일부 Metric Relabeling으로 데이터가 사라진 경우도 관측 불가입니다. 새 알림은 모두 Warning이며 조건이 **2분 연속 관찰**되면 Firing으로 전환합니다. 해소 후 다음 평가에서 해제됩니다. [공식 Alert Rule 의미](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)

| 알림 | 조건 |
| --- | --- |
| `KelpieRuntimeObservationUnavailable` | Scrape는 되지만 성공·신선도·필수 지표 계약을 충족하지 못함 |
| `KelpieWorkerHeartbeatLost` | 사용 가능한 관측의 오프라인 Worker 수 > 0 |
| `KelpieActiveLeaseExpired` | 사용 가능한 관측의 만료된 활성 Lease 수 > 0 |
| `KelpieWorkQueueStalled` | 사용 가능한 관측에 `queued` 작업이 있고 가장 오래된 나이 > 600초 |

관측 불가일 때 개별 이상 알림을 억제하고, Scrape까지 실패하면 기존 Scrape 장애 알림을 우선합니다. 이는 장애가 해결됐다는 뜻이 아닙니다. 값이 관측 사이에 정상화되거나 Prometheus가 재시작되면 연속 대기 시간이 초기화될 수 있습니다. 임계값·유지 시간 조정 시 규칙 테스트도 함께 갱신하세요. 외부 알림 수신은 별도 승인된 Alertmanager 설정과 수신 검증이 필요합니다.

<a id="observation"></a>

### 관측 불가

API Version, Metric Relabeling, `snapshot_age_seconds`, `/readyz`, 고정 관측 실패 경고, DB 연결·Pool·잠금·Migration을 확인합니다. `/readyz`는 Schema Revision 검사이므로 특정 테이블 조회 실패를 놓칠 수 있습니다. 관측 지표를 함께 확인하세요. DB 복구 후 감시는 자동으로 재개됩니다. 승인 재요청이나 두 번째 API 실행은 필요하지 않습니다. Raw SQL·DSN·Token을 알림이나 티켓에 복사하지 않습니다.

<a id="heartbeat"></a>

### Worker Heartbeat 손실

승인된 관리자 경로로 Worker 목록의 마지막 수신 시각·명시 상태·격리 여부를 확인하고, Worker Process·API 네트워크·개별 자격증명 유효성을 점검합니다. 계획된 Offline/Draining은 운영 Silence 정책으로 관리합니다. Heartbeat 수신만으로 실제 VM이나 기존 연결이 안전하다고 판단하지 않으며 공유 Secret으로 우회하지 않습니다.

<a id="lease"></a>

### 만료된 활성 Lease

권한이 있는 운영자가 Lease의 작업·Worker·최종 Event와 실제 Host 상태를 함께 점검합니다. 만료는 실제 VM 종료를 의미하지 않습니다. 현재 미구현인 실행 중 VM 종료·정리·강제 Lease 해제를 SQL 수정으로 대신하지 않습니다. 자원 이중 반환을 막기 위해 종료와 격리를 먼저 증명하는 별도 운영 절차가 필요합니다.

<a id="queue"></a>

### 오래 대기하는 작업

작업 목록과 요청 자원, Worker 용량·Draining·격리, Claim 오류, 조직·저장소 접근을 확인합니다. 10분 임계값은 운영 부하에 맞춰 조정할 수 있는 출발점입니다. 취소가 필요하면 [미배정 Queue의 감사되는 관리자 취소](work-cancellation.md)만 지원 범위에서 사용합니다. 승인 대기를 Queue 장애로 간주하거나 승인 Gate를 제거하지 않습니다.

## 검증 및 롤백

`make test-api`는 SQLite·지표 신선도·실패·전체 Timeout·종료·실제 Uvicorn/HTTP 복구 회귀를 실행합니다. `KELPIE_TEST_POSTGRES_URL=<전용 테스트 DB URL> .venv/bin/python -m pytest -q apps/api/tests/test_runtime_health.py`는 실제 PostgreSQL 집계도 검증하며, 테스트별 트랜잭션 안의 임의 Schema만 회수합니다. URL이 없으면 PostgreSQL 세 경우는 Skip됩니다. 필수 `Python` CI가 기존 PostgreSQL Service에서 이 명령을 실행합니다.

`make test-monitoring PROMTOOL=/path/to/promtool`은 가상 시계로 기존·신규 규칙, 경계값, 누락·Stale·NaN, 부분 Relabeling, 다른 Target/Job, 관측 실패 시 억제와 복구를 검증합니다. 실제 HTTP 회귀는 원래 10초 주기를 유지하여 약 21초가 걸리며 별도 CI Job이나 외부 자격증명은 필요하지 않습니다.

롤백은 API와 해당 관측 규칙을 함께 이전 Version으로 되돌린 후 `make test-monitoring`으로 검사하고 승인된 재시작/Reload를 수행합니다. 이전 API에는 새 지표가 없어 새 규칙만 남기면 관측 불가 알림이 발생합니다. 데이터 Migration·감사 삭제·TSDB 삭제는 필요하지 않습니다.

## 실제 검증 기록 — 2026-09-06

관측 구현 `49e0e03`·수명주기 `e88357a`, 규칙 `5045fbf`, HTTP 회귀 `d1e54a4`, CI `294d382`를 검증했습니다. 이후 변경은 한국어·영어 문서뿐입니다.

- 전용 PostgreSQL 17 URL을 지정한 `make test`: API 520개(66.92초), Runner 6개, Web 52개·타입 검사, Worker/Gateway 통과. `make lint` 통과.
- Prometheus 3.14.0 `make test-monitoring`: 규칙 10개(알림 9개·Recording Rule 1개), 시나리오 25개·시점별 검증 88개 통과.
- `npm run test:e2e --prefix apps/web`: Chromium 18개(1.5분), `npm run build --prefix apps/web` 통과. 기존 취소·피드백·권한 오류·다국어·시간대·Event Stream 여정을 재검증했습니다.
- 실제 검증은 마이그레이션한 임시 SQLite/Uvicorn `127.0.0.1:18470`과 Checksum을 확인한 macOS ARM64 Prometheus `127.0.0.1:19470`에서 수행했습니다. 예시의 Target Port와 규칙 파일 경로만 바꿨으며 15초 수집·30초 평가·2분 대기를 유지했습니다.

| 직접 수행한 여정 | 관찰 결과 |
| --- | --- |
| 오래된 Heartbeat·만료된 활성 Lease·20분 전 생성된 Queue 합성 행으로 시작 | 세 이상 알림이 Pending → Firing, 관측 불가 알림 없음 |
| 임시 DB의 Worker Table만 이름을 바꿔 실제 조회 실패 유발 | 이전 값은 남고 Available=0, 관측 불가 Pending → Firing, 세 개별 이상 알림 억제; `/metrics` 계속 응답 |
| Table 복원·개별 자격증명 Heartbeat HTTP 요청·관리자 Queue 취소 | API 재시작 없이 Available=1, Offline/Queue=0. HTTP 회귀에서 무관한 활성 Lease는 그대로 유지됨 |
| 별도의 화면 검증 Fixture에서만 합성 Lease를 Released로 바꿈 | 새 관측과 다음 평가 후 활성 알림 0, 규칙 9개 Inactive, Target Up·오류 없음 |

Orca 브라우저에서 각 규칙을 펼쳐 한국어·영어 요약과 Runbook·대기 시간을 확인하고, 새로고침한 실제 상태 및 최종 데스크톱 화면도 Computer-use로 확인했습니다. 확인한 브라우저 요청은 모두 200이고 Console 메시지는 없었습니다. 화면의 로컬 경로 때문에 스크린샷은 공개 업로드하지 않았습니다. Web·네이티브 UI 코드는 변경하지 않았으며 OS Focus를 확보할 수 없어 네이티브 키보드 입력은 검증하지 않았습니다.

합성 Worker/Lease 행과 실제 HTTP 경계를 사용한 관측 증거이며 실제 Worker Daemon·KVM·물리 네트워크 격리·VM 자원 회수 증거가 아닙니다. 외부 알림 수신과 운영 배포도 수행하지 않았습니다. 전체 MVP에는 외부 의존성 Readiness, 실행/DeliveryJob 정체 감시, 운영 Dashboard, 보존·복구, 승인된 실제 KVM 환경 검증이 남아 있습니다.
