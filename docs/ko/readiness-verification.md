# DB 준비 상태 장애·복구 검증

한국어 | [English](../en/readiness-verification.md)

## 계약과 범위

`inspect_schema`는 DB 연결 Pool 대기·Pre-ping·연결 수립·Alembic Revision 조회를 하나의 2초 Deadline으로 제한합니다. 기동 검사와 `GET /readyz`가 같은 구현을 사용합니다. Python의 비동기 취소 방식이므로 연결 정리와 스케줄링 비용까지 포함한 응답이 정확히 2.000초 이하라는 보장은 아닙니다.

| 상황 | HTTP 응답 | 본문 |
| --- | --- | --- |
| 현재 Schema | 200 | `{"status":"ok","database_schema":"current"}` |
| DB 무응답 또는 검사 Timeout | 503 | `{"status":"not_ready","database_schema":"unreachable"}` |
| Revision 불일치 | 503 | 기존 `outdated` 또는 `unversioned` 상태 |
| `GET /healthz` | 200 | `{"status":"ok"}` — 프로세스 생존만 확인 |

API 필드·상태·권한·승인 정책은 변경하지 않습니다. `SCHEMA_READINESS_SECONDS`는 코드의 내부 상수이며 새 환경변수·의존성·Schema Migration은 없습니다. 일반 업무 쿼리와 전체 Migration, 명시적 개발용 Bootstrap에 전역 제한을 걸지 않습니다. 호출자에 의한 `CancelledError`는 `unreachable`로 바꾸지 않고 전파합니다.

DB가 느려 2초를 넘으면 Revision이 올바르더라도 해당 검사는 준비되지 않은 상태입니다. 연결 Pool 고갈, 네트워크, DB 부하와 Schema 잠금을 확인하세요. `unreachable`은 Timeout 전용 코드가 아니며 다른 연결 실패도 포함합니다. 응답에는 DB 주소·비밀번호·원본 오류를 넣지 않습니다.

연결 복구 후 다음 `/readyz` 요청은 다시 검사합니다. 기동 당시 Schema가 준비되지 않았다면 [시작 시 전달 복구](delivery-recovery.md)가 백그라운드에서 재시도합니다. 이후 `/readyz`의 200만으로 전달 Queue도 완료됐다고 판단하면 안 됩니다. 지속적인 Delivery Worker 건강 상태는 별도 OBS-001/OPS-001 범위입니다. DB 준비 상태는 Object Store·SCM·실제 VM 건강 상태도 증명하지 않습니다.

## 검증 결과

구현 Commit `e951870`에서 검증했습니다.

- 수정 전 연결 대기·Schema 조회·실제 PostgreSQL 무응답 Peer 회귀 테스트 3개가 실패했고 수정 후 통과했습니다. 호출자 취소 2개와 실제 Uvicorn 프로세스 검증도 포함합니다.
- `KELPIE_TEST_POSTGRES_URL=<격리된 테스트 DB> make test-api`: PostgreSQL 포함 348개 통과(약 31초). `make lint` 통과.
- 실제 API 프로세스 테스트는 무응답 PostgreSQL Handshake 서버를 사용합니다. 시작 검사가 끝난 뒤 `/healthz` 200, 동시에 대기 중인 `/readyz` 약 2초 후 503, 테스트 비밀번호의 로그 비노출을 검증합니다. 매번 자체 포트·프로세스를 정리합니다.
- 직접 구동 검증은 별도 PostgreSQL 17 DB, 마이그레이션한 Schema, 제어 가능한 로컬 TCP 프록시, 실제 Uvicorn을 사용했습니다. 운영 DB나 사용자 데이터에는 접근하지 않았습니다.

| 직접 실행한 상황 | 관찰 결과 |
| --- | --- |
| DB 무응답으로 기동 | `/healthz` 200(0.001초), `/readyz` 503(2.005초) |
| 같은 프로세스에서 연결 복구 | `/readyz` 200, `current`(0.011초) |
| `alembic_version` 테이블을 별도 트랜잭션으로 잠금 | `/readyz` 503(2.034초), `/healthz` 200 |
| 잠금 Rollback | 다음 `/readyz` 200 |
| 크기 1인 Pool의 연결을 점유 | 검사 `unreachable`(2.005초) |
| Pool 연결 반환 | 다음 검사 `current` |
| 연결을 다시 무응답으로 변경 | 실제 브라우저 Fetch 503(2.008초), 남은 프록시 연결 0개 |

Orca 브라우저에서 `/readyz`를 열고 Reload·JSON 표시를 직접 조작해 `unreachable → current → unreachable` 응답을 확인했습니다. 브라우저 화면을 직접 확인했고 Computer-use로 데스크톱 창도 관찰했습니다. 네이티브 입력·Web UI 코드는 변경하지 않았으므로 해당 기능의 새 검증 자료로 사용하지 않습니다. Endpoint 식별자는 언어와 무관하며 이 가이드는 한국어·영어를 동기화합니다.

검증 후 자체 API·프록시·탭·로그와 일회성 DB를 정리합니다. Rollback은 이전 API 코드 배포로 가능하지만 무응답 대기 제한도 사라집니다. 이 변경만으로 [MVP](roadmap-summary.md)의 외부 의존성 검사·Alert·보존 및 복구가 완료되는 것은 아닙니다.
