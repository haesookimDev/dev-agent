# Worker 상태 전환의 사용자 승인 경계

한국어 | [English](../en/worker-transition-authority.md)

## 계약

작업 임대는 실행 진행을 보고할 권한이며 사용자 승인 권한이 아닙니다. `POST /api/runs/{id}/transition`은 검증한 임대의 Worker ID와 현재 배정을 확인하고 공통 상태 그래프보다 좁은 명시적 허용 목록을 적용합니다. Payload·Message의 승인 주장이나 Actor 문자열은 권한의 근거가 아닙니다. 공통 그래프에 새 전환을 추가해도 Worker 권한은 자동으로 늘어나지 않습니다.

| 전환 종류 | 권한 경계 |
| --- | --- |
| 분석·개발·검증 진행, 입력·피드백·승인·예산 대기 요청 | 허용 목록에 있는 Worker 전환만 가능 |
| 입력·피드백·승인 대기에서 개발 재개 | 권한 있는 사용자의 피드백 또는 해당 승인 결정 필요 |
| `budget_exhausted → implementing` | 사용자의 예산 승인 필요; Worker의 연장 주장은 불가 |
| `awaiting_approval → committing` | 사용자의 PR 승인 필요 |
| `committing → pr_created → completed` | 승인된 Mock만 아래 추가 조건으로 가능; 중앙 전달은 Control Plane 소유 |
| 실패 작업의 재대기·배정 등 플랫폼 전환 | Worker 임대만으로 수행 불가 |

각 상태에서 허용한 실패·취소 보고는 유지합니다. 예를 들어 `awaiting_feedback → awaiting_approval`은 승인 **요청**이지 승인 실행이 아닙니다. 전체 Worker 전환 목록은 `apps/api/app/worker_transitions.py`가 기준입니다.

Mock 완료에는 `virtualization=mock`, 중앙 `DeliveryJob` 부재와 최신 PR 승인 감사가 필요합니다. 추가 전용 감사의 조직·작업·저장소·Correlation ID, Web/Slack 전송, 승인자 역할, 승인 결정, 전달 미예약, Bundle 해시 부재, 승인 전후 상태·정수 Version이 현재 진행과 일치해야 합니다. 무관한 Console 승인은 유효한 PR 승인을 가리지 않습니다. 중앙 Job이 있으면 Worker Label을 Mock으로 바꾸어도 완료를 넘겨받을 수 없습니다. 실제 SCM 전달의 기존 승인·Bundle·Job 검증은 그대로 유지합니다.

## API 예시와 호환성

현재 작업이 `awaiting_approval`, Version 6일 때 유효한 작업 임대로 보내는 요청:

```json
{"status":"committing","expected_version":6,"payload":{"approved":true}}
```

응답은 `403`과 `{"detail":"worker cannot perform this transition"}`이며 상태·Version·이벤트·승인·감사·Job을 바꾸지 않습니다. 요청 중 임대 연장도 Rollback합니다. 임대 누락·잘못된 임대의 `401`, 오래된 Version·그래프상 불가능한 전환의 `409`를 먼저 유지하고, 그래프상 가능하지만 Worker 권한이 없는 전환은 `403`으로 거부합니다.

권한 있는 사용자는 기존 `POST /api/work-items/{id}/approvals`에 `{"kind":"pull_request","decision":"approve"}`를 보내 `200`/`committing` 작업을 받습니다. 정상 Mock Client는 이 상태를 관측한 뒤 `pr_created`, `completed`를 보고할 수 있습니다. 실제 전달은 중앙 Job이 진행합니다.

기존에 임대만으로 사용자 전환을 수행하던 Client는 이제 실패하므로 해당 동작을 사용자 API와 기존 권한·Origin 검사로 옮겨야 합니다. 요청·응답 Schema, 환경변수, 의존성, DB Migration 변경은 없습니다. 이전에 생성한 감사 없는 Mock 중간 작업도 완료를 거부합니다. 감사를 소급 생성·수정하거나 이미 `committing`인 작업을 자동 재승인하지 않습니다. 필요한 경우 승인된 운영 복구 절차 또는 새 Mock 작업을 사용합니다.

## 검증 — 2026-09-09

- 수정 전 `cb6c561`에서 최초 명세 74개 중 39개가 의도한 `403` 대신 `200`을 받아 실패하고 35개가 통과했습니다. Fixture 설정 오류를 수정한 뒤 동일한 이전 코드에서 다시 확인한 결과입니다.
- 최종 명세 77개는 공통 그래프 37개 전환, 권한·Version 검사 순서, 거부 시 전체 DB 불변성, 정상 사용자 재개·Mock 완료, 잘못된 감사·중앙 Job·다른 배정·미래 그래프 확장을 검증합니다.
- 실제 Uvicorn/SQLite·일회용 OIDC Session·두 조직·Scoped 임대로 피드백 재개, PR 승인 후 Mock 완료, 예산 승인 후 개발 재개를 확인합니다. 먼저 연 SSE의 승인자·실제 상태, 추가 전용 감사, Operator의 승인·감사 조회 거부, 다른 조직 404·잘못된 Origin 403도 검사합니다. 외부 IdP 로그인이나 실제 VM 실행은 아닙니다. 합성 자격증명이 API 로그·DB 바이트에 없고 실패 표현에 노출되지 않는지도 확인합니다. 이 테스트와 실패 표현 테스트를 합친 집중 실행은 79개 통과했습니다.
- PostgreSQL의 실제 JSON 감사 조회·잠금에서 정상/누락/오래됨/거절/중앙 Job/다른 저장소 6개를 추가 검증합니다. 최종 전용 PostgreSQL DB의 `make test`: API 1,160개(215.24초, Skip 없음), Runner 28개, Worker/Gateway, Web 91개·타입 검사 통과. `make test-api`, `make lint`, 운영 Web Build와 Chromium E2E 33개(2.0분)도 통과했습니다.

새 테스트는 기존 `Python` CI와 `test_worker_postgres.py` Step에 포함합니다. 로컬에서 `KELPIE_TEST_POSTGRES_URL`이 없으면 PostgreSQL 6개는 Skip되므로 별도로 검증합니다. CI Job·8분 제한·의존성·보안 설정은 변경하지 않습니다.

## 실제 화면과 한계

격리된 실제 API·Next.js·Scoped Mock Worker를 구동하고 내장 브라우저에서 작업 생성 → 승인 대기 유지 → 사용자 피드백 → 재검증 → 사용자 승인 → 완료·자원 해제를 직접 수행했습니다. 이 화면 환경은 명시적 `AUTH_MODE=development`이며 위 OIDC HTTP 검증과 구분합니다. 한국어·영어 390px 화면은 문서 폭 375px로 넘침 없이 완료 100%·Live·닫힌 피드백을 표시했습니다. Computer-use 스크린샷에서도 실제 대상 창의 한국어 완료·자원 해제 이벤트를 확인했습니다. OS 포커스 지원이 없어 네이티브 키보드·마우스 입력은 미검증입니다. UI 소스 변경은 없습니다.

관측 구간 네트워크 38개는 200 36개·생성 201 1개·정적 캐시 304 1개였고 외부 요청·브라우저 Console 메시지는 없었습니다. 그 이전 개발 서버 시작 로그의 Favicon 404·알 수 없는 Origin의 개발 CSS 요청 차단은 별도 제한으로 남깁니다. 보안 설정을 낮추지 않았습니다. 전체 데스크톱은 비공개로 유지하고 공개 공유 권한을 변경하지 않았습니다. 소유한 화면·터미널·서비스와 E2E 종료 후 13100·18100 포트 해제를 확인했습니다.

이 변경은 Control Plane 상태의 권한 경계입니다. 임의 `/events`의 출처 증명, 신뢰할 수 없는 VM의 물리적 실행 중단·시간 예산 강제, 실제 SCM/HTTPS OIDC 브라우저/두 KVM 작업 격리를 증명하지 않습니다. 기존 SCM 전달 우회가 재현됐다는 의미도 아닙니다. 해당 경계와 MVP 잔여 항목은 별도로 검증해야 합니다.

롤백은 정상 Revert로 수행하며 Migration은 없습니다. 이전 API는 Worker의 사용자 상태 전환 우회를 다시 허용하므로 위험을 평가해야 합니다. 인증·감사·승인 검사를 끄거나 과거 감사·이벤트를 바꾸지 않습니다.
