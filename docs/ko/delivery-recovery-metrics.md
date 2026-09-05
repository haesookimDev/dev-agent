# 시작 시 전달 복구 지표

한국어 | [English](../en/delivery-recovery-metrics.md)

## 수집 계약

`GET /metrics`는 기존 Metric에 아래 지표를 추가합니다. Scrape는 DB·SCM 요청을 하지 않으므로 DB 무응답 중에도 수집할 수 있습니다. 이 Endpoint를 외부에 공개하지 말고 [내부 Prometheus Network로 제한](operations.md#관측성-및-correlation)합니다.

| 이름 | 종류와 Label | 의미 |
| --- | --- | --- |
| `kelpie_delivery_startup_recovery_state` | Gauge; 같은 이름의 Label에 아래 6개 단계 | 현재 단계만 1, 나머지는 0 |
| `kelpie_delivery_startup_recovery_duration_seconds` | Gauge; Label 없음 | 복구 예약부터 완료·취소까지의 경과 시간 |
| `kelpie_delivery_startup_recovery_checks_total` | Counter; `outcome` = `database_unready`, `completed`, `error` | 끝난 준비 검사·복구 반복의 결과 횟수; 개별 Job 수가 아님 |

- `not_started`: 아직 복구를 예약하지 않음. 경과 시간은 0입니다.
- `waiting_for_database`: 복구 예약 후 DB·Schema 준비 검사 중이거나 준비되지 않아 대기 중입니다.
- `running`: 준비된 DB의 복구 조회·전달 처리를 기다립니다.
- `retrying`: 준비 검사 또는 복구 처리에서 예외가 발생하여 재시도를 기다립니다. 다음 검사 동안 이전 단계가 유지될 수 있습니다.
- `completed`: 복구 함수가 반환했습니다. 빈 Queue, 개별 전달 실패, 격리 또는 같은 프로세스에서 이미 실행 중인 Job의 건너뛰기도 가능합니다.
- `cancelled`: API 종료로 완료 전 복구가 취소됐습니다. 종료 후 Endpoint가 없어질 수 있으므로 이 단계를 반드시 Scrape할 수 있다고 가정하지 마세요.

경과 시간은 단조 시계를 사용하며 백그라운드 준비 검사·DB 대기·5초 재시도 대기를 포함합니다. 최초 기동의 동기적 Schema 검사 시간은 제외하므로 전체 API 기동 시간이 아닙니다. 완료·취소 후 값은 고정됩니다. 같은 프로세스에서 Lifespan을 다시 열면 단계·시간은 초기화되지만 Counter는 유지됩니다. 프로세스를 재시작하면 Counter도 초기화됩니다. 기존 Prometheus Client는 Counter 생성 시각을 `kelpie_delivery_startup_recovery_checks_created`로 함께 내보냅니다.

예를 들어 복구가 한 번 완료된 경우 응답 일부는 다음과 같습니다.

```text
kelpie_delivery_startup_recovery_state{kelpie_delivery_startup_recovery_state="completed"} 1.0
kelpie_delivery_startup_recovery_checks_total{outcome="completed"} 1.0
```

`completed=1`을 SCM 전달 성공이나 상시 Queue 건강 상태로 사용하지 마세요. `/readyz`도 Schema만 검사합니다. 개별 결과는 기존 `kelpie_delivery_outcomes_total`, `delivery_jobs.state/attempts`, 작업 상태·Event와 함께 확인합니다. 수집 실패·누락, 오래 지속되는 미완료 복구와 관찰된 전달 실패는 [기본 알림](monitoring-alerts.md)으로 확인할 수 있습니다. Worker·Lease·Queued 작업은 [독립적인 지속 관측](runtime-monitoring.md)으로 확인하며, 처리량·지연 SLO와 실행/DeliveryJob 전체의 정체 감시는 후속 범위입니다.

지표는 [단일 API 프로세스](delivery-recovery.md)에 한정됩니다. 다중 Worker 집계·분산 복구를 지원하지 않습니다. Label은 고정된 단계·결과만 사용하며 Job·저장소·사용자·요청 ID와 원본 예외를 넣지 않습니다. 새 의존성·환경변수·Migration은 없고 기존 API 응답과 지표 계약은 유지합니다. 이전 API로 Rollback하면 새 지표만 사라지므로 이를 사용하는 수집 규칙·Dashboard를 함께 되돌립니다. 데이터 Downgrade는 필요하지 않습니다.

## 검증 기록

구현 `3e3a833`, 실제 프로세스 검증 `e7cf5cd` 기준이며 이후 이 PR의 변경은 문서뿐입니다.

- `KELPIE_TEST_POSTGRES_URL=<격리된 PostgreSQL> make test-api`: 367개 통과(37.62초). `make lint` 통과. 새 지표 테스트 10개와 실제 프로세스 테스트 2개도 별도로 통과했습니다.
- 단위 테스트는 초기 상태·고정 Label·단조 시간·완료·취소·재기동, DB 대기→오류→실행→완료, 실행 전 즉시 취소와 DB 접근 없는 `/metrics`를 검증합니다.
- 실제 Uvicorn/SQLite 테스트는 Schema 복구 후 개별 전달 3개가 실패해도 복구 완료는 1회·복구 오류는 0회임을 확인합니다. 실제 Uvicorn/무응답 TCP 테스트는 `/readyz` 대기 중 `/metrics`가 0.5초 Timeout 안에 200을 반환함을 확인합니다. 기본 API CI에 포함되며 추가 서비스는 필요하지 않습니다.

직접 사용 검증은 별도 PostgreSQL 17 DB, TCP 장애 프록시와 실제 Uvicorn을 Loopback에서 실행했습니다. 비어 있는 Queue로 복구 관측만 검증하며 외부 GitHub 쓰기, 실제 SCM 전달·Worker·VM 검증을 뜻하지 않습니다.

| 직접 만든 상황 | 브라우저에서 관찰한 결과 |
| --- | --- |
| DB 프록시 무응답 | `/metrics` 200, `waiting_for_database=1` |
| DB 연결 복구, `delivery_jobs` Table 잠금 유지 | `/readyz` 200이어도 `running`과 `retrying` 관찰; 재시도 중 Scrape 200(3ms) |
| 자체 잠금 해제 | API 재시작 없이 `completed=1`; 완료 1회, DB 미준비 11회, 복구 오류 34회 |
| 완료 후 다시 수집 | 경과 시간 약 317.138초 고정; 지연은 의도적으로 유지한 장애 시간을 포함 |

Orca 브라우저의 실제 응답·최종 화면과 Computer-use의 데스크톱 창을 직접 확인했습니다. 네이티브 포커스가 없어 입력은 검증하지 않았습니다. 이번 변경은 Web·네이티브 입력 UI를 수정하지 않습니다. 테스트 DB 비밀번호가 API 로그·지표에 없음을 확인했고 자체 API·프록시·잠금·탭·터미널·임시 로그·일회성 DB를 정리했습니다. DB는 Migration으로 다시 만들 수 있는 검증 전용 데이터이며 사용자 DB·프로세스는 변경하지 않았습니다.
