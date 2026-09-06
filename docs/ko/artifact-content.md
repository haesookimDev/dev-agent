# 산출물 콘텐츠와 브라우저 실행 경계

한국어 | [English](../en/artifact-content.md)

## 보호 범위

산출물을 API Origin에서 실행 가능한 문서로 제공하지 않습니다. 메타데이터 등록과 업로드 모두 기존 업로드 형식인 `image/png`, `image/jpeg`, `image/webp`, `text/plain`, `application/json`만 허용합니다. HTML·SVG·XHTML·PDF·JavaScript·임의 바이너리는 허용하지 않습니다. 파일 확장자를 신뢰하거나 `Content-Type`을 자동 추측하지 않습니다.

다운로드는 [작업별 저장 경계](artifact-isolation.md)의 최대 10MiB 읽기 후 같은 콘텐츠 검사를 다시 수행합니다. 이미지 검사는 기존 PNG/JPEG/WebP Signature 수준이며 전체 이미지 디코딩이나 악성 파일 검사를 뜻하지 않습니다. 텍스트는 UTF-8, JSON은 파싱 가능 여부를 검사하고 파서 재귀 오류도 고정된 거부 응답으로 처리합니다. JSON에 별도의 깊이 제한이나 새로운 Schema를 추가하지 않습니다.

정상 응답에는 기존 `X-Content-Type-Options: nosniff`와 `Content-Security-Policy: sandbox`를 적용합니다. `allow-scripts`·`allow-same-origin`을 추가하지 않습니다. Sandbox는 문서 스크립트 실행을 제한하고 Origin을 불투명하게 격리합니다. [MDN Sandbox 정책](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/sandbox), [MIME 추측 방지](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Content-Type-Options)

기존 조직·저장소 권한, 작업별 Lease, Worker 격리와 경로 검사는 유지합니다. 일반 산출물 Hash·`size_bytes` 일치·물리 Backup/Restore, 파일명 인코딩과 HTTP 캐시는 이번 변경이 아닙니다.

## API 호환성과 운영

URL과 요청·응답 필드는 유지하며 새 설정·기본값·의존성·DB Migration은 없습니다.

- `POST /api/runs/{work-id}/artifacts`: `{"kind":"evidence","name":"report.txt","content_type":"text/plain","object_key":"<work-id>/artifacts/report.txt","size_bytes":12}`는 정상 Lease·경로에서 기존처럼 201입니다. 같은 요청의 `content_type`이 `text/html`이면 `415 {"detail":"unsupported artifact type"}`이며 메타데이터·이벤트를 만들지 않습니다. 실제 파일 내용은 다운로드 때 검사합니다.
- `POST /api/runs/{work-id}/artifacts/upload?name=report.txt&content_type=text/plain`: 정상 UTF-8 본문은 201, 미지원 형식은 기존처럼 415, 선언 형식과 다른 바이트는 `422 {"detail":"artifact content does not match its declared type"}`입니다.
- `GET /api/work-items/{work-id}/artifacts/{artifact-id}`: 정상 데이터는 200과 위 보안 Header를 반환합니다. 보존된 미지원 MIME이나 형식이 달라진 실제 파일은 `410 {"detail":"artifact content is unavailable"}`입니다. 다른 조직의 작업은 기존처럼 404이며 잘못된 행을 자동 수정·삭제하지 않습니다.

직접 등록한 HTML 등의 기존 링크는 더 이상 제공되지 않으므로 호환성 영향이 있습니다. 배포 전 대응 DB·파일을 보존하고 사용 형식을 점검하세요. 필요한 문서는 소유권·출처를 확인한 뒤 안전한 이미지/텍스트/JSON 산출물로 별도 생성해야 합니다. MIME 값만 바꿔 검사를 우회하거나 기존 행을 일괄 덮어쓰지 않습니다. 롤백 시 취약 버전의 산출물 제공을 재개하지 말고 해당 제공 경로를 중지한 채 수정 버전으로 복구합니다.

## 검증 · 2026-09-06

실행 코드·회귀 테스트 Commit: `a2e18e5`.

- 수정 전 회귀 23개 중 22개가 실패했습니다. 무해한 DOM 문구 변경 Probe로 실제 API Origin의 HTML 스크립트 실행도 재현했습니다. 자격증명을 읽거나 외부로 통신하지 않습니다.
- API 콘텐츠 회귀 24개와 실제 Uvicorn HTTP 회귀 1개를 추가했습니다. 신규 등록 거부, 보존된 MIME, 정상 다섯 형식, 저장 후 변조, Lease/조직 경계, 파서 오류와 보안 Header를 검사합니다.
- 전용 PostgreSQL 17 DB의 `make test`: API 630개(Skip 없음), Runner 6개, Worker·Gateway, Web 52개·TypeScript 통과. `make lint`, 운영 Web 빌드, Monitoring 10개 Rule, Chromium E2E 19개도 통과했습니다. 새 브라우저 사례는 약 2초, 전체 E2E는 약 1.5분입니다.
- 새 Chromium 사례는 실제 임시 SQLite·OIDC 테스트 Session·Scoped Worker 등록/Claim/업로드를 사용합니다. 텍스트가 Markup으로 파싱되지 않음, 불투명 Origin, 32×32 PNG의 실제 로드, JSON 열람, 기존 HTML의 410을 검증합니다. 기존 공통 화면 오류·실행 작업 자원 해제 검증은 유지합니다. 헤더에 임시 인증값이 포함되는 Trace만 이 사례에서 저장하지 않으며 실패 스크린샷·보고서는 유지합니다.
- 같은 Fixture의 Uvicorn `:18510`과 Next.js Standalone 운영 빌드 `:13510`에서 Orca 브라우저의 산출물 링크를 직접 열었습니다. 정상 텍스트·PNG·JSON과 미지원 HTML 거부, 한·영 상세 화면, 1035px 가로 넘침 없음, 새 요청의 200/410을 확인했습니다. Console에는 추가 메시지가 없었습니다. Computer Use로 한·영 대시보드·PNG·라이트 모드의 텍스트와 거부 응답을 확인했습니다. 실제 IdP 로그인·VM·SCM 검증이나 OS 입력 성공을 주장하지 않습니다.

Orca 다크 모드의 기본 텍스트/JSON 문서는 흰 글자가 흰 배경에 표시되는 제한을 발견했습니다. 라이트 모드에서 검은 글자와 내용이 보임을 대조했습니다. 탭이 화면에 표시되지 않은 캡처와 구분하며, 네이티브 좌표 입력은 Focus 보호로 차단되어 재시도하지 않았습니다. [파일명 인코딩](artifact-filenames.md)은 후속 변경으로 검증했습니다. 산출물 열람 UI의 대비·오류 안내와 기존 410 캐시 문제는 별도 후속 PR로 남깁니다. 이 PR에는 제품 UI 코드 변경이 없습니다.

새 API/브라우저 회귀는 기존 필수 `Python`/`Web` CI에 포함됩니다. 추가 Job·Matrix·의존성은 없으며 최종 Head CI·Merge SHA는 PR에 기록합니다. 산출물 물리 복구·보존/Janitor와 실제 KVM/네트워크/입력 격리가 남아 있어 전체 MVP는 미완료입니다.
