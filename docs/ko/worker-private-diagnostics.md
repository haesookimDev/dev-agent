# Worker의 비공개 오류 진단

한국어 | [English](../en/worker-private-diagnostics.md)

## 보호 경계와 호환성

HTTP 실패 응답·외부 명령 출력에는 자격증명, 임대, URL, 경로가 포함될 수 있습니다. [API 이벤트 가림](api-event-redaction.md)은 Worker의 로컬 로그를 보호하지 않으며 다른 종류의 자격증명을 모두 알 수도 없습니다. Worker는 다음 오류의 원문을 버리고 고정 분류·숫자 코드만 반환합니다.

- HTTP JSON 직렬화, 요청 생성, 전송, 응답·Claim 역직렬화. 실패 본문과 서버가 제공한 Status 문구를 제외하고 HTTP 코드·표준 상태 이름은 유지합니다.
- VM 준비 파일 접근·쓰기·할당 직렬화. 설정의 경로를 오류 문자열에 포함하지 않습니다.
- 외부 명령의 표준 출력·표준 오류. 성공·실패 모두 Null Device로 보내고 메모리에 수집하지 않습니다. 시작 실패, 종료 코드, Context 취소·기한 초과는 구분합니다.
- 실행기 오류를 받는 로그·`worker.failed` 이벤트. 알려진 안전한 오류만 분류하고 나머지는 `worker execution failed`로 표시합니다. 시작 로그에서 자유 입력인 작업 제목을 제외하고 작업·Correlation ID는 유지합니다.

진단 예: `control plane returned HTTP 403 Forbidden`, `control plane returned HTTP 409 Conflict`, `VM base image unavailable`, `VM command failed (exit 7)`.

`worker.failed`의 Type·Source·Level·Payload는 유지하고 Message만 제한합니다. API Schema, Migration, 환경변수, 의존성 변경은 없습니다. 승인·Version·인증·자격증명 재읽기·작업별 임대 경계를 유지합니다. 실패 처리도 현재 상태 조회→필요한 실패 전환→확인된 임대 해제 순서 그대로이며, 해제가 실패하면 로컬 예약을 유지합니다.

## 반복 검증

```sh
make test-worker
(cd apps/worker && go test -race ./...)
make lint
```

- `client_privacy_test.go`: HTTP 401/403/409/422/500/503, 임의 Status·본문, 직렬화·전송·응답 오류, 실제 TCP 임대 분리, 취소.
- `execution_privacy_test.go`: 실제 셸의 양쪽 출력·종료 코드·시작 실패, 임의 오류와 제목, 실패 이벤트, 해제 실패 시 예약 유지, 안전한 오류의 Wrapper.
- `cmd/kelpie-worker/privacy_test.go`: 새로 빌드한 실제 Worker 바이너리의 등록 거부·이미지 부재·명령 실패. HTTP 서버·실패용 `qemu-img`·빈 도메인 목록만 반환하는 `virsh`가 합성이며 VM은 만들지 않습니다. Header 분리, Correlation ID, 실패 전환 Version, 해제 1회, 원문 없는 로그·이벤트, SIGTERM 종료를 검사합니다. 실제 VM 정리 검증은 [수명주기 기록](worker-lifecycle.md)과 구분합니다.

수정 전 HTTP 경계 회귀 4개와 실행 경계 회귀 3개가 실패했습니다. 구현 `5c2b259`·`5a506c1`, 프로세스 회귀 `66a78ec`에서 위 검증이 통과했습니다. 전용 PostgreSQL DB의 전체 `make test`는 API 1,270개(237.51초), Runner 45개, Worker/Gateway, Web 125개와 타입 검사를 통과했습니다. Mac의 Linux 서비스 구문 검사 1개만 Skip이며 Linux CI가 검사합니다. 기존 Go CI가 새 회귀를 자동 실행하므로 Job·Matrix·8분 Timeout을 늘리지 않습니다.

## 실제 사용과 제한

2026-09-09, 일회용 Uvicorn/SQLite·개별 Worker 자격증명·실제 Worker 바이너리로 명령 실패와 이미지 부재를 각각 확인했습니다. 두 작업 모두 `failed`, Version 3, `released` 임대, 활성 작업 0이며 DB·API/Worker 로그에 해당 평문 Worker 자격증명이 없었습니다. 실패 명령은 일회용 Fixture이며 KVM 검증이 아닙니다.

Orca의 대기 화면에서 새로고침 없이 명령 실패·임대 해제 이력이 표시됐으며 한국어→영어 전환과 종료 후 피드백 마감을 확인했습니다. 초기 임시 스크립트의 잘못된 컬럼 조회와 중복 실행 입력은 제품 변경 없이 정정하고 새 환경에서 다시 검증했습니다.

개발 서버는 출처 없는 캡처용 CSS 요청을 차단했고 캡처 호출은 Orca 연결 종료로 실패했습니다. 네이티브 창은 포커스 미지원·접근성 Container만 노출했습니다. 시각·네이티브 입력 성공으로 표현하지 않으며 허용 목록이나 OS 권한을 우회하지 않습니다. UI 코드·레이아웃 변경은 없습니다.

2026-09-10에는 프로덕션 Build·standalone Web에서 Orca의 이미지 부재 오류 표시를 재확인했습니다. Orca Screenshot은 창 포커스 Timeout으로 실패했지만 별도의 일회용 Chromium은 한글 390px·영어 1280px에서 두 오류를 모두 검증했습니다. 네 장의 캡처를 직접 검토했고 가로 넘침·Page/Console 오류·HTTP 오류는 없었습니다. Orca의 최근 요청 25개도 모두 200이며 과거 404는 이미 제거한 앞선 두 Fixture의 SSE 재연결이었습니다. 네이티브 입력 미검증을 Chromium 결과로 대체하지 않습니다.

범용 Secret Scanner가 아닙니다. 과거 로그 정화, 성공 이벤트의 모든 메타데이터, 인코딩된 비밀, Artifact/Crash Dump/cloud-init 파일, VM·하위 프로세스 자체의 로그와 다른 컴포넌트의 모든 오류 경로는 별도입니다. SEC-001·실제 KVM·MVP 전체 완료를 뜻하지 않습니다.

## 운영과 롤백

원문 대신 작업/Correlation ID, HTTP 코드, 명령 종료 코드와 안전한 분류로 진단합니다. 추가 조사는 승인된 격리 환경에서 해당 단계만 재현하며 비밀이 포함된 원문을 공유 로그에 다시 출력하지 않습니다. 자동 원문 수집 Debug Mode를 추가하지 않습니다.

Schema 롤백은 필요 없지만 이전 Worker는 노출 경로를 다시 엽니다. 승인된 절차로 해당 Worker 실행을 중단한 뒤 롤백하고, 유출 의심 시 자격증명 폐기·교체와 사고 대응을 수행합니다. 인증·가림·승인 경계를 끄지 않습니다.
