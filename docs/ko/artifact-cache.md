# 산출물 HTTP 캐시 보호

한국어 | [English](../en/artifact-cache.md)

## 동작과 호환성

실제 Chromium에서 누락 파일의 410을 직접 연 뒤 파일만 복구하고 대시보드 Origin에서 같은 URL을 조회하면 `Failed to fetch`가 발생했습니다. 기존 응답에는 저장 금지와 Origin 변형 구분이 없었습니다. Header 정책 변경 후 같은 회귀가 통과했습니다. Disk-cache 플래그를 직접 측정한 결과로 표현하지 않습니다.

`/api/work-items/{work-id}/artifacts`와 `/api/work-items/{work-id}/artifacts/{artifact-id}`의 목록·파일·처리된 오류에 `Cache-Control: no-store`와 `Vary: Origin`을 적용합니다. Origin이 없는 직접 탐색도 포함합니다. 기존 Vary를 보존하며 허용 Origin·인증·권한을 늘리지 않습니다. 준수하는 개인·공유 HTTP 캐시는 이 응답을 저장·재사용할 수 없습니다. [RFC 9111](https://www.rfc-editor.org/rfc/rfc9111.html)

- 정상 200, 로그인 필요 401, 권한 폐기 403, 다른 조직/없는 메타데이터 404, 제공 불가 파일 410의 의미·본문은 유지합니다. HEAD 405와 뒤 슬래시 307도 보호합니다.
- 파일 바이트, UTF-8 파일명, `Content-Security-Policy: sandbox`, `X-Content-Type-Options: nosniff`는 유지합니다. 업로드·SSE·이벤트 목록·Health·WebSocket은 대상이 아닙니다.
- 순수 ASGI에서 응답 시작 Header만 변경합니다. Body 버퍼링·예외 처리는 추가하지 않습니다. 바깥 `ServerErrorMiddleware`가 생성하는 미처리 500까지 포괄하는 전역 오류 처리 변경은 아닙니다. [Starlette Middleware](https://starlette.dev/middleware/)

## API 예시와 운영

인증된 소유자의 `GET /api/work-items/{work-id}/artifacts/{artifact-id}`:

```http
HTTP/1.1 200 OK
Cache-Control: no-store
Vary: Origin
Content-Type: text/plain; charset=utf-8
Content-Disposition: inline; filename="owned-evidence.txt"; filename*=UTF-8''owned-evidence.txt
Content-Security-Policy: sandbox
X-Content-Type-Options: nosniff

Owned artifact acceptance evidence
```

파일 누락은 `410 {"detail":"artifact content is unavailable"}`, 권한 폐기는 `403 {"detail":"organization membership required"}`이며 저장 금지 Header를 유지합니다. 목록 200의 JSON 배열도 유지합니다. Schema·환경변수·기본값·의존성·DB Migration은 추가하지 않습니다.

배포 시 API와 앞단 Proxy/CDN이 두 경로의 Header를 보존하고 강제 캐시하지 않는지 확인합니다. 이전 버전이 이미 저장한 응답이나 사용자가 다운로드한 파일을 소급 삭제할 수는 없습니다. 관리 가능한 캐시는 해당 산출물 경로의 기존 항목만 배포 절차에 따라 만료시키고 새 요청을 검증합니다. 전체 사용자 캐시 삭제나 캐시 비활성화로 회귀를 숨기지 않습니다. 롤백은 민감한 응답 저장을 재개하지 말고 제공 경로를 중지한 채 수정 버전으로 복구합니다.

## 검증 · 2026-09-06

실행 코드·테스트 Commit: `b69656f`.

- 수정 전 API 15개가 Header 누락으로 실패했고 실제 Chromium의 직접 탐색 410 → 복구 → 같은 URL Fetch도 실패했습니다. 수정 후 API 정책 50개·실제 HTTP 1개·실제 Chromium 1개 회귀가 통과했습니다.
- 브라우저 기본 캐시를 유지하고 URL Nonce·Route Mock·캐시 우회를 사용하지 않습니다. 410 → 200, 200 → 410 → 200, 조직 전환 404·로그아웃 401·권한 폐기 403, 목록·파일의 새 Request ID와 거부 응답의 비공개 내용 부재를 검증합니다.
- 전용 PostgreSQL 17의 `make test`: API 738개(Skip 없음, 91초), Runner 6개, Worker·Gateway, Web 52개·TypeScript 통과. `make lint`, 브라우저 Python Helper Ruff, `pip check`, 운영 Web 빌드, Monitoring 10개 Rule 통과. Chromium 전체 21개는 약 1.6분, 새 사례는 2초였습니다. 연결 누수·Traceback은 없고 기존 Next.js Smooth-scroll Warning은 별도 UI 후속 작업입니다.
- 실제 Uvicorn `:18540`과 Next.js Standalone `:13540`을 Orca에서 실행해 한·영 1035px 상세 화면과 링크를 확인했습니다. 직접 연 누락 파일 복구 후 같은 URL의 기본 Fetch 200·정확한 내용·새 Request ID, 조직 전환 404·로그아웃 401·권한 폐기 403과 `no-store`를 확인했습니다. Computer Use로 한·영 화면과 라이트 모드 파일 내용도 확인했습니다. 파일의 불투명 Origin·스크립트 없음, HTTP의 CSP/nosniff가 유지됐습니다.
- 정리 전 Console 메시지는 0개였습니다. 캡처한 Dashboard 요청 86개에는 의도한 접근 거부, 권한 폐기 후 SSE 403, 실제 IdP가 없는 테스트 로그인 경로의 503이 포함됩니다. 정상 로그인이나 HTTP 오류 0으로 표현하지 않습니다. 테스트 Session·탭·서버·임시 파일/DB를 정리하고 API 종료 로그 검사를 통과했습니다. 전용 전체 검증 DB도 작업 행·접속 0 확인 후 제거했으며 기존 DB와 사용자 프로세스는 보존했습니다.

새 회귀는 기존 필수 `Python`·`Web` CI에 자동 포함되며 Job·Matrix·Timeout·의존성은 늘리지 않습니다. 최종 Head CI와 Merge SHA는 PR에 기록합니다. 제품 UI 코드는 바꾸지 않아 전후 스크린샷 대상이 아니며 다른 프로젝트가 보이는 캡처는 공개하지 않습니다.

실제 IdP·VM·SCM·네이티브 OS 입력 검증은 아닙니다. 산출물 열람 UI의 다크 모드 대비/오류 안내, 파일 Backup/Restore·보존, 실제 KVM/네트워크/입력 격리가 남아 있어 전체 MVP는 미완료입니다.
