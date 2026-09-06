# 작업 화면의 산출물 미리보기

한국어 | [English](../en/artifact-preview.md)

## 동작과 경계

검증 자료를 파일명·형식·크기가 있는 카드로 표시합니다. 작업 화면을 떠나지 않고 텍스트·JSON·이미지를 확인하며 원본 열기는 별도 동작으로 유지합니다. 완료된 작업에서도 읽기만 허용하며 피드백·승인 정책은 바꾸지 않습니다.

- UTF-8 `text/plain`, `application/json`, PNG·JPEG·WebP만 표시합니다. React가 텍스트를 이스케이프하며 HTML·Markdown을 실행하거나 iframe에 넣지 않습니다.
- Header뿐 아니라 실제 스트림 바이트에 10 MiB, 전체 읽기에 15초 제한을 적용합니다. API 검증을 대체하지 않습니다. 닫으면 요청을 취소하고 이미지 Object URL을 해제합니다. 다시 열기·재시도는 기존 인증·권한·`no-store` 정책을 따르는 새 요청입니다.
- 로그인 만료, 권한 거부, 없는 주소, 제공 불가 파일, 용량·형식·해석 오류, Timeout·연결·서버 오류를 한·영으로 구분합니다. 원시 오류 본문이나 내부 경로는 표시하지 않습니다.
- 네이티브 `dialog`에서 닫기·Escape, Tab/Shift+Tab 순환, 원래 버튼으로 복귀를 지원합니다. 재시도 버튼이 사라지면 포커스는 파일 내용 영역에 남습니다. 스크롤·키보드 탐색, 긴 텍스트·파일명의 줄바꿈을 지원합니다. [MDN dialog](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/dialog), [Object URL 해제](https://developer.mozilla.org/en-US/docs/Web/API/URL/revokeObjectURL_static)

밝은 표면과 명시적인 글자색으로 OS 다크 모드 선호에도 대비를 유지합니다. 사이트 전체의 다크 테마를 추가하는 변경은 아닙니다. 이미 내려받거나 열린 파일은 권한 회수 후 소급 삭제할 수 없습니다. 새 요청에서 서버 권한을 재검사하며 영구 미리보기 캐시는 만들지 않습니다.

## 검증 · 2026-09-06

최종 실행 코드·회귀 Commit: `c2efe0f`.

- 전용 PostgreSQL 17의 `make test`: API 738개(Skip 없음, 89초), Runner 6개, Worker·Gateway, Web 83개·TypeScript 통과. Web 변경 후 `make test-web`, `make lint`, 운영 빌드를 다시 통과했습니다. Monitoring 10개 Rule과 브라우저 Seed Helper Ruff도 통과했습니다.
- 단위 검증 31개를 추가했습니다. Header·실제 바이트 제한, Timeout·취소·UTF-8 오류, 허용 형식, 한·영 빈 상태·이름·크기를 검증합니다.
- Chromium 전체 26개는 약 1.7분, 새 미리보기 5개는 각각 1.4초 이내였습니다. 임시 API에 Scoped Worker로 실제 업로드·임대 해제를 수행합니다. 정상 파일·410·원본 다운로드는 실제 서비스이며, 느린 응답·해석 실패·401/403/404/500·크기 제한 등의 UI 장애 분기만 명시적인 응답 Fixture를 사용합니다.
- 실제 브라우저에서 발견한 Shift+Tab 순환과 재시도 후 포커스 유실을 수정했습니다. 후자는 수정 전 `toBeFocused` 실패, 수정 후 통과를 확인했습니다. 닫힌 파일의 늦은 응답, 이미지 URL 해제, 원본 UTF-8 파일명·바이트와 스크립트 비실행도 검증합니다.
- 완료 상태 테스트는 읽기 전용 미리보기 버튼 하나만 허용하며 피드백 입력·다른 버튼이 없음을 계속 검사합니다. 기존 회귀를 삭제하거나 변경 동작 금지를 완화하지 않습니다.

새 검사는 기존 필수 `Web` CI에 포함됩니다. 새 의존성·Job·Matrix·Timeout·환경변수·DB Migration은 없습니다. 정확한 Head CI와 병합 결과는 PR에 기록합니다.

### 실제 구동·화면 증거

Orca에서 실제 Uvicorn `:18550`, Next.js Standalone `:13550`을 실행했습니다. Scoped 세션의 실제 파일로 410 → 파일 복구 → 같은 미리보기 재시도 200·내용·포커스, 다른 조직 404 → 소유자 복귀 200, Membership 회수 후 403 안내를 확인했습니다. 한·영 텍스트와 32×32 PNG, 1280px 데스크톱·390×844px 좁은 화면의 줄바꿈·가로 넘침 없음, 닫기 복귀를 확인했습니다. Computer Use로 실제 Orca 창의 좁은 화면도 검사했습니다. 네이티브 OS 입력 검증을 의미하지 않습니다.

정리 직전 Console은 0개였고 관찰한 요청 116개에는 의도한 410/404/403과 권한 회수 뒤 SSE 403이 포함됐습니다. 기존 개발 모드 Smooth-scroll Warning은 별도 후속 항목입니다. 정상 HTTP 오류 0이나 실제 IdP 로그인을 검증했다고 표현하지 않습니다.

테스트 세션·탭·서버·합성 파일/DB를 정리하고 API 종료 로그 검사를 통과했습니다. 전용 PostgreSQL 검증 DB는 작업 행·접속 0 확인 후 제거했으며 기존 DB와 사용자 프로세스는 보존했습니다.

아래는 브라우저 영역만 캡처한 합성 자료입니다. 변경 전은 `75662b9`(1035px), 변경 후는 `c2efe0f`(1280px 또는 390px)입니다. 다른 프로젝트가 보이는 전체 데스크톱 캡처는 공개하지 않습니다.

| 변경 전 | 변경 후 |
| --- | --- |
| ![기존 원본 링크 목록](../assets/artifact-preview/before-en.png) | ![형식·크기·미리보기 카드](../assets/artifact-preview/after-en.png) |
| ![기존 누락 파일 응답](../assets/artifact-preview/before-error.png) | ![복구 안내와 재시도](../assets/artifact-preview/error-en.png) |

[복구 후 내용](../assets/artifact-preview/restored-en.png) · [한글 좁은 화면](../assets/artifact-preview/mobile-ko.png) · [이미지](../assets/artifact-preview/image-ko.png)

## 운영과 남은 범위

기존 Web 운영 빌드·배포 절차를 사용합니다. API 계약은 유지하며 Proxy가 CORS·인증·보안 Header를 보존해야 합니다. 문제가 생기면 Web 기능 Commit을 되돌려 원본 링크 UI로 복구합니다. API의 파일 격리·콘텐츠 검사·캐시 보호는 되돌리지 않습니다.

일반 산출물 Backup/Restore·보존 정책, 실제 IdP·Linux/KVM·네트워크·Console 입력 격리는 이번 변경으로 완료되지 않습니다. 실제 VM 실행 없이 만든 합성 검증 자료입니다. 전체 P0·최소 P1 MVP는 미완료입니다.
