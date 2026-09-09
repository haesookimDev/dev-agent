# API 이벤트 수신 자격증명 가림

한국어 | [English](../en/api-event-redaction.md)

## 수신 경계와 호환성

[Runner 전송 가림](runner-event-redaction.md)에만 의존하면 다른 Worker나 직접 HTTP Client가 원문을 저장할 수 있습니다. API는 유효한 임대를 확인한 다음 `POST /api/runs/{id}/events`의 Event Type·Source·Message·중첩 Payload와 `POST /api/runs/{id}/transition`의 Message·Payload 사본을 저장 전에 가립니다.

- 현재 요청에서 인증한 **완전한 평문 임대 토큰**은 문자열·JSON 키에서 `[REDACTED]`가 됩니다. 다른 작업의 토큰 목록이나 전체 환경은 읽지 않습니다.
- 대소문자·`_`·`-`를 정규화해 [Runner와 같은 18개 자격증명 필드](runner-event-redaction.md)의 값을 재귀적으로 가립니다. `token_usage`, `secret_count`, 정상 숫자·Boolean·Null은 유지합니다. 계약 테스트가 두 구현의 정책 차이를 검사하며 API에 Runner 런타임 의존성을 추가하지 않습니다.
- 임대 검증, 조직·저장소 권한, 상태·Version·승인 정책, Correlation ID는 유지합니다. API Schema·DB Migration·환경변수·의존성·CI Job 변경은 없습니다. 기존 Client는 수정 없이 사용할 수 있지만 보존되는 자격증명 값은 의도적으로 달라집니다.

유효한 `X-Kelpie-Lease`로 보내는 요청 예시(표시 값은 자리표시자이며 실제 자격증명이 아님):

```json
{"event_type":"worker.output","message":"Result: <current lease>","payload":{"access_token":"<synthetic value>","token_usage":42}}
```

200 응답과 이후 Event Log·SSE의 Message는 `Result: [REDACTED]`, Payload는 `{"access_token":"[REDACTED]","token_usage":42}`입니다. 누락·잘못된·다른 작업·만료·격리 임대는 401, 오래된 Version·금지된 상태 전환은 409이며 상태와 이벤트를 변경하지 않습니다.

## 검증 — 2026-09-09

수정 전 `4c5534e`에서 직접 이벤트 저장·상태 전환 회귀 2개가 실패했습니다. 구현 `6bcf140`의 API 명세·계약 테스트 33개, Runner 28개와 정적 검사가 통과했습니다. 독립 검증 `90cad52`는 Runner 가림 함수를 사용하지 않고 실제 Uvicorn/SQLite로 원문을 전송합니다.

- 일회용 OIDC 조직·Session·개별 Worker·임대로 인증하고, 이벤트와 상태 전환 응답·저장 Row·조회 결과를 검사합니다. SSE 연결을 먼저 연 뒤 새 이벤트를 보내 실시간 전달도 확인합니다.
- 임대 누락·위조·다른 작업 임대의 6개 쓰기를 거부하고 전후 전체 작업 상태·이벤트 목록이 동일한지 확인합니다. 다른 조직의 조회·SSE는 404입니다.
- 합성 자격증명 원문이 최종 API 로그와 종료 후 SQLite 바이트에 없는지 검사합니다. 시간대 표기가 다른 POST와 GET을 원문 비교한 초기 테스트 오류는 동일 조회 경계의 전체 상태 비교로 정정했습니다. 상태·Version·Correlation ID 비교와 거부 전후 불변 검증은 유지합니다.

전용 PostgreSQL DB를 생성한 최종 `make test`는 API 1,024개(199.81초, Skip 없음), Runner 28개, Worker/Gateway, Web 91개와 타입 검사를 통과했습니다. 새 실제 HTTP 회귀 자체는 SQLite를 사용합니다. 집중 회귀 62개(새 API 34개와 Runner 28개), `make lint`, 실제 사용을 위한 Web 프로덕션 Build와 문서 상대 링크 108개 검사도 통과했습니다. 기존 Python CI에 포함되며 Job·8분 Timeout·검증 수준을 유지합니다. 변경하지 않은 Web의 로컬 전체 E2E는 재실행하지 않았고 PR의 기존 Web CI로 확인합니다.

## 실제 사용 확인

구현 `6bcf140`과 검증 `90cad52`의 동작으로 일회용 API와 프로덕션 standalone Web을 실행했습니다. 저장된 이벤트 6개를 조회한 뒤 직접 HTTP로 7번째 이벤트를 추가해 새로고침 없이 `브라우저 직접 전송 / Browser direct HTTP: [REDACTED]`가 나타났습니다. 한국어·영어, 1155px 화면과 양 언어 390px 화면(문서 폭 375px), Live 연결과 가독성을 확인했습니다. Computer-use로 실제 데스크톱의 가림 표시도 확인했습니다. OS Focus 미지원으로 네이티브 키보드·마우스 입력은 검증하지 않았습니다.

캡처 시작 이후 요청 10개는 모두 HTTP 200이며 외부 요청·Console 메시지는 없었습니다. 로그인 Cookie는 HttpOnly이고 값을 표시하지 않았습니다. UI 코드·디자인 변경은 없으며 비밀이 노출되는 변경 전 화면이나 전체 데스크톱을 공개하지 않았습니다. Orca 공개 Artifact 권한은 변경·재시도하지 않았습니다.

소유한 브라우저·터미널·API·Web·로그인 Helper를 종료하고 포트 13300·18300·18301의 Listen 해제를 확인했습니다. 최종 로그·DB 스캔 후 소유한 일회용 Fixture만 정리했습니다. 외부 IdP·SCM·실제 VM은 사용하지 않았습니다.

## 제한과 롤백

인증 후 수락한 두 Telemetry 경로만 다룹니다. 유효성 검증 단계의 422 응답, 다른 API, 알려지지 않은 자유 문장 속 비밀, 인코딩·조각·이미지·Artifact/Delivery Bundle 바이트, 로컬 로그·Crash Dump·cloud-init 전체 검증은 남아 있습니다. 과거 이벤트를 정화하거나 적대적 VM의 유출을 차단하는 범용 Secret Scanner가 아닙니다. 이벤트 출처의 진위나 Payload 상태 메타데이터의 무결성도 이번 가림 검증과 별도입니다. SEC-001·MVP 전체 완료로 표시하지 않습니다.

Schema Rollback은 필요 없지만 이전 API로 되돌리면 직접 전송 노출 경로가 다시 생깁니다. 승인된 환경에서 해당 증거 전송을 안전하게 중단한 뒤 롤백하고, 실제 노출이 의심되면 자격증명 폐기·교체와 승인된 사고 대응 절차를 따릅니다. 오탐은 회귀 테스트로 조정하며 인증·가림 전체를 끄지 않습니다.
