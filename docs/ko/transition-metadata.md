# 상태 전환 이력의 실제 상태 보호

한국어 | [English](../en/transition-metadata.md)

## 계약과 영향

`transition_work_item`이 생성하는 `work.transitioned` 이벤트의 최상위 Payload `from`·`to`는 Control Plane의 실제 전환 전·후 상태입니다. 호출자가 같은 키를 보내도 덮어쓸 수 없습니다. 다른 필드와 중첩 `from`·`to`, 명시적 Message, Actor, Correlation ID는 유지하며 입력 사본을 사용합니다. [API 수신 가림](api-event-redaction.md)도 그대로 적용됩니다.

유효한 작업 임대로 `POST /api/runs/{id}/transition`에 보내는 예시:

```json
{"status":"analyzing","expected_version":2,"payload":{"from":"awaiting_approval","to":"completed","safe":17}}
```

현재 상태가 `provisioning`, Version이 2이면 응답 작업은 `analyzing`, Version 3입니다. 저장된 이벤트·Event Log·SSE의 Payload는 `{"from":"provisioning","to":"analyzing","safe":17}`입니다. 이 경로는 WorkItem을 응답하며 이벤트 자체를 응답하지 않습니다. 기존 Client 수정·Schema Migration·새 환경변수·의존성은 필요 없습니다. 충돌 키에 잘못된 값을 보존하던 동작만 의도적으로 달라집니다.

공통 함수를 사용하는 Worker 전환·Claim·피드백·승인·격리·전달 경로에 적용됩니다. 상태 그래프, Version 충돌, 임대 검증, 사용자 권한과 승인 정책은 변경하지 않습니다. Web은 이벤트 Payload 상태를 직접 신뢰하지 않고 실제 WorkItem을 다시 조회합니다. 이번 수정은 화면 상태 위조가 아니라 전환 이력의 기록 무결성 문제를 해결합니다.

## 검증 — 2026-09-09

- 수정 전 `6775ef3`에서 새 명세 50개 중 41개 실패·9개 통과를 재현했습니다. `6c68f22`는 허용된 상태 그래프 37개 전환, 선택 Payload·명시적 Message, 거부 시 불변성, 임대로 인증한 HTTP 전환을 모두 통과했습니다.
- `7303bd5`의 독립 회귀는 실제 Uvicorn/SQLite, 일회용 OIDC Session·조직·Worker·임대로 네 번 전환하고 사용자 피드백으로 개발을 재개합니다. 먼저 연 SSE의 새 이벤트, Event Log, DB Row에서 실제 상태와 가림 결과가 일치합니다. 임대 누락·다른 작업 임대는 401, 오래된 Version·금지 전환은 409이며 상태·전체 이벤트 목록이 유지됩니다. 다른 조직의 조회·SSE는 404입니다. 종료 후 API 로그·DB 바이트에 합성 자격증명이 없는지도 검사합니다.
- 전용 PostgreSQL DB의 최종 `make test`: API 1,075개(204.36초, Skip 없음), Runner 28개, Worker/Gateway, Web 91개·타입 검사 통과. 새 실제 HTTP 회귀 자체는 SQLite를 사용합니다. `make test-api`, `make lint`, 실제 사용용 Web 프로덕션 Build도 통과했습니다. CI Job·Timeout·검증 수준은 변경하지 않습니다.

기존 Chromium E2E도 로컬에서 재실행해 33개가 2.0분에 통과했습니다. 생성·실시간 이벤트·피드백 전송·재검증·승인·완료와 마감 후 피드백 거부/입력 보존을 확인합니다. 이 환경은 격리된 `AUTH_MODE=development`, `WORKER_AUTH_MODE=scoped`, Mock Worker이며 실제 OIDC HTTPS 브라우저·SCM 전달·KVM 실행 검증은 아닙니다. 운영 기본값은 변경하지 않았습니다. 자동 정리 후 13100·18100 포트 해제도 확인했습니다.

## 실제 사용과 검증 한계

최종 구현으로 일회용 OIDC API와 standalone Web을 구동했습니다. 기존 합성 산출물 Fixture를 재사용했고 외부 IdP·SCM·VM은 사용하지 않았습니다. 한국어 페이지에서 직접 HTTP 전환 후 새로고침 없이 `개발 중 → 검증 중 → 피드백 대기`와 최신 이벤트가 표시됐습니다. 브라우저의 인증된 조회에서도 실제 상태·중첩 문맥·가림 결과를 확인했습니다. 영어 전환과 390px 화면(문서 폭 375px)의 Live 표시·가독성도 확인했습니다.

브라우저 피드백은 성공하지 않았습니다. HTTP 로컬 페이지 Origin이 Fixture의 HTTPS `DASHBOARD_URL`과 달라 같은 출처 검사가 403으로 차단했고, 오류 안내와 입력 내용 보존을 확인했습니다. 실제 HTTP 회귀의 올바른 Origin 피드백 성공과 구분합니다. 인증 정책·TLS 검증·시스템 인증서 신뢰를 낮추지 않았습니다. 캡처 이후 17개 요청 중 16개는 200, 해당 POST만 403이며 외부 요청·Console 메시지는 없었습니다.

Computer-use 캡처도 시도했지만 대상 페이지가 네이티브 창에 표시된 것을 확인하지 못했습니다. 따라서 이번 변경의 증거는 내장 브라우저의 상호작용·스크린샷이며 네이티브 입력·화면 확인 성공으로 기록하지 않습니다. UI/네이티브 기능 변경은 없습니다. 전체 데스크톱·자격증명은 공개하지 않았고 Orca 공개 공유 권한도 변경하지 않았습니다. 소유한 브라우저·터미널·서비스를 종료하고 13400·18400·18401 포트 해제를 확인했습니다. 로그·DB 검사 후 소유한 일회용 Fixture만 삭제했습니다.

## 남은 경계와 롤백

과거 이벤트는 수정하지 않습니다. 별도 `/events` 수신 경로의 임의 Event Type·Source·Payload는 여전히 호출자 데이터입니다. `work.transitioned`라는 이름만으로 모든 이벤트를 신뢰해서는 안 됩니다. 임의 Message·중첩 상태의 사실성, 이벤트 출처 증명, 호출자별 상태 전환 권한 검증은 별도 범위입니다. 실제 OIDC HTTPS 브라우저 변경, KVM 동시 실행·격리, Preview 검증과 MVP 전체 완료를 대신하지 않습니다.

문제가 있으면 해당 API 변경을 정상 Revert합니다. Migration은 없지만 이전 버전에서는 충돌 Payload가 전환 이력을 다시 왜곡할 수 있습니다. 기존 자격증명 가림·인증·승인 검사는 끄지 않습니다.
