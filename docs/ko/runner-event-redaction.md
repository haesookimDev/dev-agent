# Runner 이벤트 자격증명 가림

한국어 | [English](../en/runner-event-redaction.md)

## 전송 경계와 호환성

Runner의 `ControlClient.event`는 HTTP 요청 본문을 만들기 전에 메시지·Source·Event Type·중첩 Payload의 사본을 가립니다. `transition`의 메시지도 같은 처리를 합니다. 호출자가 준 객체, 실제 명령, 인증용 `X-Kelpie-Lease`, Correlation ID, 상태·Version·승인 정책은 변경하지 않습니다. API Schema·DB Migration·환경변수·의존성·CI Job 변경은 없습니다.

- 해당 Client가 보유한 **완전한 평문 임대 토큰**을 문자열과 JSON 키에서 `[REDACTED]`로 바꿉니다. Client 간 토큰 목록을 공유하거나 환경 전체를 읽지 않습니다.
- 대소문자·`_`·`-` 차이를 정규화한 다음 Token, Access/Refresh/ID/Session Token, Authorization/Proxy Authorization, Cookie/Set Cookie, API Key, Secret/Client Secret, Password/Passwd, Private Key, Lease Token/X-Kelpie-Lease/KELPIE_LEASE_TOKEN 필드의 값을 재귀적으로 가립니다. `token_usage`, `secret_count`, 정상 숫자·Boolean·Null은 유지합니다.
- Codex 표시 메시지·종료 Stderr와 검증 명령 출력은 가림 이후 기존 길이 제한을 적용합니다. 절단 경계에 토큰이 걸쳐 원문 일부가 남는 회귀를 검사합니다. 구조화된 시작 실패의 오류 값도 가립니다.

예를 들어 Runner 메시지 `검증 결과: <현재 임대 토큰>`은 저장·조회 시 `검증 결과: [REDACTED]`가 됩니다. Payload의 `{"access_token":"<합성 값>","token_usage":17}`은 `{"access_token":"[REDACTED]","token_usage":17}`이 됩니다. 인증 헤더는 계속 유효해야 하며 잘못된 임대의 HTTP 오류를 성공으로 바꾸지 않습니다.

## 검증 — 2026-09-09

수정 전 `e51062f`에서 자격증명 필드·이벤트 전송 회귀 13개가 실패했습니다. 구현 `5ac234c`는 `make test-runner` 28개와 `make lint`를 통과했습니다. `51c74a2`의 실제 API 연동 회귀는 다음을 검증합니다.

- 일회용 OIDC 조직·Session·개별 Worker·작업 임대를 발급한 실제 Uvicorn/SQLite에 Runner Client로 연결합니다. 외부 IdP·Codex 계정·실제 VM은 사용하지 않습니다.
- 실제 자식 프로세스가 소유한 합성 임대 파일을 읽어 긴 출력을 만들고 실패(1)·성공(0)합니다. Runner가 종료 코드와 안전한 출력은 유지하고 임대 값은 가리는지 확인합니다. 임시 파일은 `0600`이며 해당 Probe 경로만 정리합니다.
- 실제 이벤트 조회·SSE·DB Row·종료 후 SQLite 바이트·API 로그에서 합성 자격증명 비노출을 검사합니다. 잘못된 임대 401, 다른 조직 조회 404, 거부된 요청의 이벤트 미저장과 Correlation ID 보존을 함께 확인합니다.

전용 PostgreSQL DB를 생성한 전체 `make test`는 API 990개(193.16초, Skip 없음), Runner 28개, Worker/Gateway, Web 91개와 타입 검사를 통과했습니다. 새 Runner HTTP 회귀 자체는 SQLite를 사용하며 PostgreSQL 전체 실행에 포함됐다는 이유로 그 회귀의 저장소를 PostgreSQL이라고 표현하지 않습니다. 정적 검사와 실제 사용을 위한 Web 프로덕션 Build도 통과했습니다. 새 테스트는 기존 Python CI에 포함되며 Job·8분 Timeout·검증 수준은 유지합니다.

## 실제 사용 확인

구현 `5ac234c`의 Runner와 위 일회용 API, standalone Web을 구동했습니다. 최초 브라우저 진입은 Web 준비 전 연결 오류였으며 Ready 확인 후 재진입한 결과만 인수 증거로 사용했습니다. 저장된 12개 이벤트를 조회한 뒤 새 이벤트를 추가해 새로고침 없이 `실시간 검증 / Live check: [REDACTED]`가 표시되는 것을 확인했습니다. 한국어·영어 전환, 1155px 화면과 양 언어 390px 화면(문서 폭 375px), Live 연결과 가독성을 확인했습니다. 실제 Computer-use 데스크톱 캡처에서도 한국어 이벤트와 가림 표시를 확인했습니다. OS Focus 미지원으로 네이티브 키보드·마우스 입력의 검증을 주장하지 않습니다.

캡처된 요청은 HTTP 200인 30개와 상태가 기록되지 않은 로그인 Redirect 진입 1개이며 외부 요청·Console 메시지는 없었습니다. 로그인 Cookie 값은 표시하지 않았습니다. 앱 UI 코드·디자인은 바꾸지 않았고 비밀정보가 포함된 변경 전 화면이나 데스크톱 전체 화면을 공개하지 않았습니다. Orca 공개 Artifact 권한도 변경·재시도하지 않았습니다.

소유한 브라우저·터미널·API·Web·로그인 Helper를 종료하고 포트 13200·18200·18201의 Listen 해제를 확인했습니다. 마지막 API 로그·DB 스캔 후 일회용 Fixture를 정리했으며 기존 사용자 세션과 데이터는 건드리지 않았습니다.

## 제한과 후속 작업

이것은 Runner의 특정 전송 경계 보강이지 범용 Secret Scanner나 적대적인 VM의 유출 방지 장치가 아닙니다. 알려지지 않은 자유 문장 속 토큰, 인코딩·조각·이미지 속 비밀, 다른 예외의 사전 절단·로컬 Traceback/Crash Dump, Artifact/Delivery Bundle 바이트·cloud-init, Runner를 거치지 않은 직접 API 전송은 아직 포괄하지 않습니다. 기존에 보존된 Event를 재작성하지도 않습니다. SEC-001·실제 VM 격리·MVP 전체 완료로 표시하지 않습니다.

오탐은 안전한 필드의 회귀 테스트를 추가해 조정하고 가림 전체를 끄거나 인증 검사를 우회하지 않습니다. Schema Rollback은 필요 없지만 이전 Runner로 되돌리면 기존 노출 경로가 다시 생기므로 해당 경로의 증거 전송을 안전하게 중단한 승인된 환경에서만 Rollback해야 합니다. 운영 자격증명이 노출됐다고 의심되면 관련 자격증명을 폐기·교체하고 보존 증거는 승인된 사고 대응 절차로 조사합니다.
