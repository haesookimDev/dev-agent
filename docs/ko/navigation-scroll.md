# 화면 이동 스크롤 검증

한국어 | [English](../en/navigation-scroll.md)

## 동작과 범위

전역 `html`의 `scroll-behavior: smooth`는 유지하되 Locale Layout에 `data-scroll-behavior="smooth"`를 명시했습니다. 설치된 Next.js 16.3.4의 안내에 따라 라우터가 화면 전환 중에만 즉시 스크롤을 적용한 뒤 원래 설정을 복원합니다. 기존 페이지 내 Anchor 이동과 `prefers-reduced-motion: reduce`의 `auto` 설정은 유지합니다. 레이아웃·색상·문구·API·권한·의존성·환경변수·CI Job·Timeout 변경은 없습니다.

## 자동 검증 — 2026-09-09

PR #42의 실제 CI 로그에서 설정 경고를 확인했고, 수정 전 코드 `b2c99fb`에서 회귀 테스트가 같은 경고로 두 번 실패하는 것을 확인했습니다. 초기 테스트의 잘못된 탐색 Label은 별도로 수정했으며 그 실패를 제품 회귀 재현으로 세지 않았습니다.

`navigation-scroll.spec.ts`는 실제 일회용 API·SQLite·Mock Worker와 Chromium을 사용하며 요청을 모의 응답으로 대체하지 않습니다. 한국어·영어 × 일반·모션 감소의 네 조합에서 다음을 검사합니다.

- 긴 작업 상세의 아래쪽에서 대시보드로 이동한 후 상단 복원, 경고 없음, HTML 계약, 임시 Inline 설정의 제거.
- 상세 재진입·뒤로 가기, 페이지 내 새 작업 Anchor, 390px 화면의 가로 넘침 없음.
- 건너뛰기 Link에 실제 브라우저 Focus와 Enter 입력을 적용한 후 본문 Focus, 원래 모션 설정 보존.

구현 `9f3153d`에서 `make test-web`(91개와 타입 검사), `make lint`, 프로덕션 Build, 전체 `npm --prefix apps/web run test:e2e`(33개, 1.9분)를 통과했습니다. 이후 Anchor·건너뛰기 검증을 보강한 네 조합도 통과했습니다(14.1초). 최종 PR의 필수 Web CI가 전체 Suite를 다시 실행합니다. 스크린샷은 기존 7일 보존 `browser-evidence`에 포함되므로 별도 Upload Job은 추가하지 않습니다.

이번 Web 전용 변경에서는 로컬 `make test-api`, `make test-runner`, `make test-worker`, `make test-gateway`, 전체 `make test`를 다시 실행하지 않았습니다. 해당 구현은 바뀌지 않았고 PR #42에서 전용 PostgreSQL 전체 테스트와 병합 후 CI를 통과했습니다. 새 PR의 기존 필수 Python·Go CI는 그대로 실행합니다.

## 실제 사용 확인과 한계

커밋 `9f3153d`의 프로덕션 Build를 실제 standalone 서버로 실행했습니다. `next start`의 standalone 경고를 확인한 뒤 그 Process를 종료하고, 생성된 정적 Asset을 배치하여 `server.js`로 재시작한 최종 구동만 인수 기록으로 사용합니다. API·Mock Worker는 소유한 일회용 Fixture이며 SCM 자격증명·실제 VM은 사용하지 않았습니다.

Orca 브라우저에서 영어 상세의 403px Scroll을 대시보드 이동 후 0으로 복원하고 `smooth`와 빈 Inline 설정을 확인했습니다. 한국어 390px 화면에서 가로 넘침 없이 새 작업 Anchor로 821px 이동했습니다. 최종 한국어 화면은 Computer-use 데스크톱 캡처로 직접 확인했습니다. 캡처된 요청 30개는 모두 200, 외부 요청·Console 메시지는 0개였습니다.

Orca의 건너뛰기 Link 입력은 목표 Hash/Focus 변경을 확인하지 못했으므로 성공으로 기록하지 않았습니다. 같은 기능은 위 실제 Chromium 키보드 테스트 네 조합에서 통과했습니다. OS Window Focus가 지원되지 않아 네이티브 키보드·마우스 입력은 검증하지 않았습니다. 다른 Workspace가 보인 캡처는 검증에서 제외했으며 올바른 Workspace를 선택한 후 최종 화면을 다시 확인했습니다.

합성 테스트 데이터만 담은 변경 전후 비교 HTML은 로컬에 보관했습니다. Orca 공개 Artifact 공유는 기기 설정에서 차단되어 한 번의 거부 후 중단했으며 권한을 변경하거나 재시도하지 않았습니다. PR에는 기존 GitHub CI의 변경 전후 검증 자료를 연결합니다. 데스크톱 전체 캡처는 공개하지 않습니다.

소유한 서비스·브라우저·터미널 탭을 종료하고 포트 13100·18100의 Listen 해제를 확인했습니다. Fixture가 소유한 DB·자격증명 파일·Mock 작업 경로를 정리했으며 기존 사용자 터미널·서비스는 변경하지 않았습니다.

## 유지보수와 롤백

라우팅·전역 Scroll·Anchor·접근성을 변경할 때 이 회귀를 유지하고 경고만 숨기거나 모션 감소 설정을 제거하지 않습니다. 전후 정적 화면은 디자인 보존의 증거이지 애니메이션 시간 자체의 증명은 아닙니다. 되돌릴 때는 Layout 속성과 관련 기대값을 함께 되돌리고 Web 검증·Build를 다시 실행합니다. 데이터 Migration이나 설정 변경은 필요하지 않습니다.
