# 산출물 파일명과 HTTP Header

한국어 | [English](../en/artifact-filenames.md)

## 동작과 호환성

한글·이모지 파일을 업로드한 뒤 다운로드할 때 발생하던 500을 수정합니다. 원본 이름을 Latin-1 Header에 직접 삽입하지 않고 ASCII Header의 `filename*`에 UTF-8 Percent Encoding으로 보존합니다. `filename`은 기존 클라이언트용 이름입니다. ASCII이면서 `%`가 없는 정상 이름은 그대로, 그 외에는 `artifact`를 사용해 일부 클라이언트의 Percent 재해석을 피합니다. [RFC 6266](https://www.rfc-editor.org/rfc/rfc6266.html), [RFC 8187](https://www.rfc-editor.org/rfc/rfc8187.html)

- 등록과 업로드는 1–255 Code Point의 이름을 받습니다. 빈 이름, 앞뒤 공백, `.`·`..`, `/`·`\`·`"`, Unicode `Cc` 제어 문자와 `Cs` Surrogate는 거부합니다. 내부 공백·한글·악센트·이모지·ZWJ·`%`는 허용합니다.
- 신규 거부는 422이며 행·이벤트·파일을 생성하지 않습니다. 이전에 허용되던 공백/제어 문자 등의 이름은 이제 거부되므로 생산자는 정상 Basename을 보내야 합니다. Runner의 파일 업로드와 Web의 기존 산출물 URL 계약은 유지합니다.
- 기존의 잘못된 이름은 DB에서 삭제·수정하지 않습니다. 소유권·경로·콘텐츠 검사가 통과한 파일만 이름을 `artifact`로 대체해 제공합니다. 잘못된 MIME/경로의 410, 다른 조직의 404, Lease 검사는 그대로입니다.
- Unicode 정규화, 모든 OS의 예약 파일명, 확장자와 MIME 일치, 악성 파일 검사를 보장하지 않습니다. `Cf` 전체를 금지하지 않으며 사용자 기기의 저장 이름은 브라우저/OS 정책에도 영향을 받습니다.

## API 예시와 운영

정상 Lease로 `POST /api/runs/{work-id}/artifacts/upload`에 UTF-8 텍스트 본문과 Query `name=검증 결과 ✅.txt&content_type=text/plain`을 URL Encoding해 보내면 201입니다. 메타데이터 등록도 동일한 이름 정책을 적용합니다. `GET /api/work-items/{work-id}/artifacts/{artifact-id}`는 원본 바이트와 다음 Header를 반환합니다.

```http
HTTP/1.1 200 OK
Content-Disposition: inline; filename="artifact"; filename*=UTF-8''%EA%B2%80%EC%A6%9D%20%EA%B2%B0%EA%B3%BC%20%E2%9C%85.txt
X-Content-Type-Options: nosniff
Content-Security-Policy: sandbox
```

`100%20 complete; v2.txt`는 `filename*=UTF-8''100%2520%20complete%3B%20v2.txt`로 전송하며 이름의 `%20`을 공백으로 바꾸지 않습니다. 범위 내 길이의 금지 문자 이름은 `422 {"detail":"invalid artifact name"}`입니다. 길이 등 기존 요청 Schema 오류는 기존 Validation 응답을 유지합니다. 보존된 잘못된 이름의 정상 파일은 `inline; filename="artifact"; filename*=UTF-8''artifact`입니다.

새 설정·기본값·의존성·DB Migration은 없습니다. 데이터 일괄 재작성은 하지 않습니다. 배포 시 등록 생산자의 이름을 점검하고 API를 갱신합니다. 롤백은 취약 Header 제공을 재개하지 말고 해당 제공 경로를 중지한 채 수정 버전으로 복구합니다. [기존 콘텐츠 보호](artifact-content.md)와 [작업별 저장 경계](artifact-isolation.md)를 약화하지 않습니다.

## 검증 · 2026-09-06

실행 코드·회귀 테스트 Commit: `77a77fc`.

- 수정 전 API 40개 중 37개 실패·3개 통과였으며 실제 Uvicorn에서도 정상 한글 업로드의 조회 500을 재현했습니다. 수정 후 파일명 회귀 43개와 실제 HTTP 회귀 1개를 추가해 통과했습니다. 무해한 Header Probe만 사용하며 자격증명/외부 통신은 사용하지 않습니다.
- 전용 PostgreSQL 17 DB의 `make test`: API 674개(Skip 없음, 약 85초), Runner 6개, Worker·Gateway, Web 52개·TypeScript 통과. `make lint`, 운영 Web 빌드, Monitoring 10개 Rule도 통과했습니다.
- Chromium E2E 19개가 약 91초에 통과했습니다. 기존 콘텐츠 사례를 확장해 실제 링크 Alt-click 다운로드에서 한글/이모지와 `%` 이름, 다운로드 성공과 원본 바이트 일치를 검사합니다. [Chrome 다운로드 단축키](https://support.google.com/chrome/answer/157179?hl=en), [Playwright의 브라우저 결정 파일명](https://playwright.dev/docs/api/class-download#download-suggested-filename)
- 실제 임시 SQLite·Scoped OIDC 테스트 Session·Worker 등록/Claim의 Uvicorn `:18520`과 Next.js Standalone 운영 빌드 `:13520`을 사용했습니다. 실제 등록에서 잘못된 이름 422, 한글/Percent/기존 잘못된 이름 조회 200과 안전한 Header·동일 바이트를 확인했습니다.
- Orca 브라우저의 한·영 상세 화면에서 산출물 링크를 열고, 세 파일 모두 문자 그대로의 텍스트·불투명 Origin·스크립트 없음, 1035px 대시보드의 가로 넘침 없음을 확인했습니다. 대시보드 Console 메시지와 캡처된 6개 요청의 오류는 없었습니다. Computer Use의 실제 화면으로 한·영 대시보드와 라이트 모드의 한글 파일 내용을 확인했습니다. 네이티브 OS 저장 대화상자, 실제 IdP 로그인·VM·SCM의 검증은 아닙니다.

새 테스트는 기존 필수 `Python`/`Web` CI에 포함되며 Job·Matrix·Timeout·의존성을 늘리지 않습니다. 최종 Head CI와 Merge SHA는 PR에 기록합니다. 제품 UI 코드 변경은 없어 UI 전후 스크린샷 첨부 대상은 아니며, 다른 프로젝트가 보이는 데스크톱 캡처는 공개하지 않습니다.

HTTP 캐시 정책과 열람 UI의 다크 모드 대비/오류 안내는 별도 후속 PR입니다. 물리 파일 복구·보존/Janitor와 실제 KVM/네트워크/입력 격리도 남아 있어 전체 MVP는 미완료입니다.
