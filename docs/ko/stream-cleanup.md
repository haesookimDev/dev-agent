# 실시간 스트림 종료와 DB 연결 정리

한국어 | [English](../en/stream-cleanup.md)

## 원인과 동작

PR #34 이후 `main` CI의 생성 실패·재시도 시나리오는 통과했지만 API 로그에 연결 종료 예외와 미반환 SQLAlchemy 연결 경고가 남았습니다. 실제 SQLite 조회 중 취소를 주입한 회귀 2개에서도 같은 오류를 재현했습니다. 테스트 개수뿐 아니라 실제 서비스 종료 로그까지 확인해야 합니다.

SSE의 각 배치에서 인증·권한 재검사, 이벤트 조회와 Session 정리를 AnyIO 취소 Shield 안에서 수행합니다. 외부 연결 종료가 정리 과정의 매 `await`를 반복 취소하지 않도록 보호하며, 내부 `asyncio.timeout`은 조회 단계에 2초 제한을 둡니다. 배치 후에는 즉시 취소를 다시 확인합니다. Shield 안에서 이벤트를 `yield`하거나 유휴 대기를 하지 않습니다. [AnyIO 취소와 Shield](https://anyio.readthedocs.io/en/stable/cancellation.html), [Python 비동기 Timeout](https://docs.python.org/3/library/asyncio-task.html)

- 초기 접근 검사와 매 배치의 OIDC Session·조직/저장소 권한 재검사는 유지합니다. 다른 조직은 기존처럼 404이며, 접근 폐기 시 스트림을 끝냅니다.
- 스트림이 시작된 뒤 조회 Timeout 또는 SQLAlchemy 오류가 발생하면 민감정보 없는 고정 Warning `event stream read failed; closing stream`을 기록하고 종료합니다. SQL·매개변수·DB 연결 정보나 가짜 성공 이벤트는 보내지 않습니다. 클라이언트는 기존 연결 복구 정책을 사용합니다.
- 정상 `GET /api/work-items/{work-id}/events?after=0`은 기존 `200 text/event-stream`, `id: <event-id>`와 `data: <EventView JSON>`을 유지합니다. 이벤트 형식·정렬·100개 배치·Keepalive·Cursor 계약은 바꾸지 않습니다. 이미 전송한 200을 뒤늦게 5xx로 바꾸지는 않습니다.
- 2초는 조회의 취소 시점이며, 드라이버 정리 시간이 더해질 수 있습니다. 응답하지 않는 드라이버까지 포함한 강제 종료 시간 보장은 아닙니다. 초기 요청 Session의 활성 스트림 중 점유나 Cursor 범위 검증은 별도 범위이며, 이번 변경은 연결 종료 뒤 반환을 검증합니다.

## 운영과 CI

새 환경변수·DB Migration은 없습니다. `anyio>=4,<5`를 API의 직접 의존성으로 선언합니다. 이미 FastAPI/Starlette가 사용하는 라이브러리이며, 표준 `asyncio.shield`만으로 해결되지 않는 AnyIO의 반복 취소 경계를 직접 사용하기 때문입니다. API 배포 시 의존성을 함께 설치합니다. 롤백이 필요하면 논리적 커밋을 Revert하고 API를 재시작하되 기존 연결 누수 위험이 돌아옴을 고려해야 합니다.

`make test-api`에는 SQLite/실제 HTTP 검증이 포함됩니다. PostgreSQL 6개는 전용 `KELPIE_TEST_POSTGRES_URL`을 설정하고 다음 명령으로 실행합니다. URL이 없으면 Skip되므로 PostgreSQL 검증으로 간주하지 않습니다. 각 테스트가 만든 UUID Schema만 정리합니다.

```sh
.venv/bin/python -m pytest -q apps/api/tests/test_stream_cleanup.py -k postgres
npm run test:e2e --prefix apps/web -- stream-cleanup.spec.ts
.venv/bin/python -m ruff check apps/web/e2e/stream-runtime.py
```

기존 필수 `Python` CI에 PostgreSQL 단계 하나를 추가하고 기존 `Web` E2E에서 실제 Chromium `EventSource` 사례를 실행합니다. 새로운 Job·Matrix·Timeout 증가는 없습니다. 테스트용 조회 지연/연결 계수 Route는 `apps/api/tests/stream_runtime_app.py`를 명시적으로 실행할 때만 존재하며 운영 `app.main:app`에는 추가하지 않습니다. 임시 Scoped 인증 Header가 남는 Trace만 새 브라우저 사례에서 제외하고, 화면 오류·실패 스크린샷·공통 자원 해제·서버 종료 로그 검사는 유지합니다.

## 검증 · 2026-09-06

실행 코드·테스트·CI Commit: `35a29a2`.

- SQLite·PostgreSQL 각각 인증 조회/이벤트 조회/Rollback 중 취소, 조회 Timeout/DB 오류와 재연결, 유휴 종료: 총 12개 통과. 실수로 로거가 비활성화된 전체 Suite 환경에서도 로그 검사가 실제 동작하도록 테스트 Fixture에서 복구합니다.
- 실제 Uvicorn/SQLite 쿼리를 잠시 잡고 HTTP 연결을 8회 종료한 뒤 매회 활성 스트림·Checked-out 연결 0을 검증했습니다. 이후 정상 이벤트와 다른 조직의 404, 프로세스 종료 로그까지 검사합니다.
- 실제 Chromium도 같은 조회 중 `EventSource.close()`를 4회 수행하고 매회 연결 반환, 다시 연결해 실제 `work.created` 수신, 최종 연결 반환과 종료 로그를 검증했습니다. 실제 쿼리가 아직 진행 중인지도 확인합니다.
- 전용 PostgreSQL 17의 `make test`: API 687개(Skip 없음, 약 88초), Runner 6개, Worker·Gateway, Web 52개·TypeScript 통과. `make lint`, Python 브라우저 Helper Ruff, `pip check`, 운영 Web 빌드, Monitoring 10개 Rule 통과. 전체 Chromium 20개는 약 1.6분에 통과했으며 연결 누수·Traceback은 없었습니다. 기존 Next.js Smooth-scroll 설정 Warning은 별도 UI 후속 작업입니다.
- 실제 Uvicorn `:18530`과 Next.js Standalone 운영 빌드 `:13530`에서 Orca로 한·영 상세 화면, 새 Lease 인증 이벤트 수신, 목록 이동·재접속·새로고침을 수행했습니다. 최종 스트림 시작/종료 5/5, 활성 스트림·Checked-out 연결 0을 확인했습니다. 정리 전 Console 메시지 0, 캡처한 65개 요청의 HTTP 오류 0입니다.
- Computer Use로 실제 한·영 1035px 화면과 이벤트 내용을 확인했습니다. 기기 에뮬레이션 명령은 390px를 반환했지만 실제 너비는 1035px로 유지돼 모바일 수동 증거로 사용하지 않습니다. 좁은 화면 검증은 전체 Chromium 회귀에 포함됩니다. 실제 IdP·VM·SCM 또는 네이티브 OS 입력 검증은 아닙니다.

이번 변경은 제품 UI 코드를 바꾸지 않아 전후 스크린샷 첨부 대상이 아닙니다. 다른 프로젝트가 보이는 데스크톱 캡처는 공개하지 않습니다. 최종 Head CI와 Merge SHA는 PR에 기록합니다. 캐시 보호·산출물 열람 UI·파일 복구/보존과 실제 KVM 검증이 남아 있어 전체 MVP는 미완료입니다.
