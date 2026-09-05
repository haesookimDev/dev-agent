# 시작 시 전달 복구

한국어 | [English](../en/delivery-recovery.md)

## 동작과 운영 경계

API가 시작할 때 DB가 내려가 있거나 Schema가 준비되지 않았다면 전달 복구를 백그라운드에서 기다립니다. 준비 검사 또는 복구 시도가 끝난 뒤 5초 간격으로 재시도하고, 준비된 DB의 전달 Job을 한 번 성공적으로 처리하면 종료합니다. API 기동은 전달 완료를 기다리지 않습니다.

- `pending`, `retry`, `running` Job을 조회합니다. 실행 중인 작업은 첫 비동기 대기 전에 프로세스 내부에 등록하여, 같은 프로세스의 승인 요청과 시작 시 복구가 같은 Job을 동시에 실행하지 않게 합니다.
- 현재 프로세스에서 실행하지 않는 `running` Job만 기존 방식대로 `retry`로 바꿉니다. Worker → Work → DeliveryJob 잠금과 기존 승인·검증 Bundle·격리 검사를 유지합니다. 완료·실패·격리된 Job을 자동으로 다시 승인하거나 실행하지 않습니다.
- 조회와 재개 상태 변경에 각각 2초의 비동기 Deadline을 적용합니다. 준비 검사도 별도의 2초 제한을 사용합니다. 실제 SCM 전달 전체나 일반 업무 쿼리에 새 전역 제한을 걸지는 않습니다.
- 복구는 하위 전달 작업을 기다리고 예외를 회수합니다. 취소 시 하위 작업도 취소하고 종료를 기다립니다. DB 처리 오류 등으로 복구가 끝나지 않으면 고정된 경고 `delivery recovery failed; retrying`을 남기고 재시도합니다. 원본 연결 문자열·오류를 이 경고에 넣지 않습니다.

이 중복 실행 방지는 **단일 API 프로세스** 안에서만 유효합니다. 같은 DB를 사용하는 여러 Uvicorn Worker나 API Replica, 겹치는 구·신 API의 전달 소유권은 보장하지 않습니다. 현재 Compose/Dockerfile의 단일 프로세스 구성을 유지하고, 교체 시 이전 API의 전달 작업과 프로세스가 종료된 뒤 새 API를 시작하세요. 다중 프로세스 운영에는 분산 Lease/Fencing이 선행되어야 합니다.

시작 시 복구이지 상시 Queue Worker가 아닙니다. 첫 복구가 성공한 뒤 새롭게 고립된 Job을 주기적으로 찾아내는 기능, SCM 실패 자동 재시도, 처리량·지연 SLO, 복구 완료 Metric/Alert는 별도 작업입니다. `/readyz`의 200은 Schema만 검증하며 전달 Queue의 완료나 외부 SCM의 건강 상태를 증명하지 않습니다. `delivery_jobs.state/attempts`, 작업 상태와 Event, 기존 전달 Metric을 함께 확인하세요.

API 응답·Schema·환경변수·의존성은 바뀌지 않습니다. `DELIVERY_RECOVERY_RETRY_SECONDS=5`, `DELIVERY_RECOVERY_DB_SECONDS=2`는 내부 코드 상수입니다. Rollback은 이전 API 배포로 가능하지만 DB가 준비되지 않은 기동에서 전달 재개를 놓치는 동작과 추적되지 않는 복구 Task가 다시 생깁니다. Migration/Downgrade는 필요하지 않습니다.

## 검증

구현 `1e3db7a`, 실제 프로세스 테스트 `c166d09` 기준입니다. 이후 변경은 이 문서의 기록뿐입니다.

- 회귀 테스트 8개가 수정 전 실패했습니다. 복구 후 재개, 같은 프로세스에서의 중복 실행 방지, 정상 전달, 오류 재시도·고정 경고, 종료 시 하위 작업 취소를 검증합니다.
- `KELPIE_TEST_POSTGRES_URL=<격리된 PostgreSQL> make test-api`: 357개 통과(약 37초). `make lint` 통과. 관련 전달 테스트 35개도 별도로 통과했습니다.
- 실제 Uvicorn/SQLite 테스트는 Schema 불일치 동안 시도 0회, Schema 복구 후 `pending/retry/running` Job 각각 1회 실행을 확인합니다. 외부 GitHub 설정이 없는 실패 경로를 의도적으로 사용하며, 격리 Job은 시도 0회·격리 상태를 유지합니다. 정상 종료와 미회수 Task 예외가 없는지도 확인합니다. 기본 API CI에 포함되며 별도 서비스는 필요하지 않습니다.
- 직접 사용 검증은 별도 PostgreSQL 17 DB와 TCP 장애 프록시, 실제 Uvicorn, Production Build Web, 로컬 GitHub HTTP Fixture를 사용했습니다. Worker는 API Protocol Fixture이며 실제 VM이 아닙니다. GitHub 외부 쓰기를 하지 않았고 이미 존재하는 Branch 경로를 사용했으므로 이 검증은 실제 Git Push 성공 증거가 아닙니다.

| 직접 실행한 여정 | 관찰 결과 |
| --- | --- |
| 검증 Bundle 업로드 후 브라우저 승인 | 승인 전 Token/PR 요청 0회, 승인 후 `committing` |
| 전달 중 API 종료, DB 무응답 상태로 재기동 | Job `running`, 시도 1회 유지; 브라우저 `/healthz` 200, `/readyz` 503(2.008초) |
| DB 연결만 복구 | API 추가 재시작·재승인 없이 `completed`; 총 시도 2회, PR 생성 요청 1회, 승인 Audit 1개 |
| 같은 브라우저의 Event Stream 복구 | 한국어 `완료`와 영어 `Completed`, 100%, PR 링크 확인 |
| 최종 상태 | `/readyz` 200; 테스트 DB 비밀번호·SCM Token의 API/Web 로그 비노출 |

Orca 브라우저로 승인·언어 전환을 직접 조작하고 화면을 확인했습니다. 콘솔 메시지는 없었고, 의도한 장애 중 Stream 연결 중단과 503 이후 조회·Stream 200을 확인했습니다. Computer-use로 데스크톱 창을 관찰했으나 네이티브 입력 검증은 하지 않았습니다. Web UI·네이티브 입력 코드는 이번 변경 대상이 아닙니다.

검증 후 자체 API/Web/Fixture/프록시·브라우저 탭·키·Token·임시 파일과 일회성 DB를 정리합니다. 사용자 데이터와 다른 프로세스는 건드리지 않습니다. [MVP](roadmap-summary.md)의 외부 의존성 건강 상태·Alert·보존 정책·실제 KVM 격리는 아직 별도 검증이 필요합니다.
