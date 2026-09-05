# 개발 및 검증 절차

한국어 | [English](../en/development.md)

## 완료 기준

개발은 작업 브랜치에서 수행하고, 논리적 단위마다 관련 테스트를 실행한 뒤 한국어 메시지로 커밋합니다. 자동 테스트와 실제 사용 검증이 통과하면 증거를 포함한 PR을 만들고 최신 커밋의 CI와 리뷰 상태를 확인합니다. 사용자가 머지를 위임한 작업은 에이전트가 Merge Commit으로 머지하고 `main`을 Fast-forward 동기화합니다. 필수 검증을 수행할 수 없으면 Draft로 유지합니다.

MVP 지속 개발에서는 [로드맵의 바로 다음 Release](roadmap-summary.md#바로-다음-release)를 완료 기준으로 사용합니다. P3 이후의 확장 기능을 자동으로 MVP에 포함하지 않습니다. 현재 MVP 작업에 대한 자동 머지 위임은 운영 배포, 유료 인프라, 브랜치 보호 우회, 제품 내부의 사용자 승인 제거까지 허용하지 않습니다.

## 검증

- `make test`: API, Runner, Worker, Gateway, Web 테스트와 Web 타입 검사
- `make lint`: Python Ruff, Go vet, Web ESLint
- `make test-monitoring PROMTOOL=/path/to/promtool`: Monitoring 설정과 PromQL 알림 규칙 검증. [설치·운영 안내](monitoring-alerts.md)
- `cd apps/web && npm run build`: 운영 Web 빌드
- DB 변경: 실제 PostgreSQL의 Upgrade, 스키마 일치, 안전한 범위의 Rollback
- 사용자 기능: 실제 서비스를 구동한 뒤 브라우저에서 관련 여정 수행, 한국어·영어 및 데스크톱·모바일 너비 확인

UI에서는 작업 현황과 다음 행동을 우선하고, 빈 상태·오류·키보드 탐색·포커스·대비·긴 콘텐츠를 확인합니다. 최종 코드의 화면과 콘솔·네트워크 오류를 Browser/Computer-use 도구로 직접 확인하고, 핵심 흐름은 재실행 가능한 E2E로 남깁니다. 네이티브 Console 입력 변경은 실제 데스크톱 조작도 검증합니다. 변경 전후 화면은 비밀정보 없이 PR에 첨부합니다.

테스트 환경과 자원은 격리하고 자신이 만든 자원만 정리합니다. Mock Worker의 성공을 실제 Linux/KVM Acceptance로 표현하지 않습니다. 구동 대상이 없는 문서 변경은 해당 없음과 이유를 기록합니다.

### 브라우저 회귀 테스트

저장소 루트에서 API 개발 환경(`.venv`), Go, Web 의존성을 설치한 뒤 실행합니다.

```sh
npm ci --prefix apps/web
npx --prefix apps/web playwright install chromium
npm run test:e2e --prefix apps/web
```

Playwright는 실제 Chromium·Next.js·API·Mock Worker를 실행합니다. 매번 임시 SQLite DB를 마이그레이션하고 무작위 테스트 Worker 자격증명을 사용하며 종료 시 자신이 만든 프로세스·데이터만 정리합니다. `13100`(Web), `18100`(API) 포트는 비워 두세요. 같은 체크아웃에서 실행 중인 `next dev`도 먼저 종료해야 합니다. `.venv/bin/python` 대신 다른 Python을 쓸 때만 `KELPIE_E2E_PYTHON`을 지정합니다. Linux에서는 `playwright install --with-deps chromium`으로 시스템 라이브러리도 설치합니다.

작업 생성·실시간 이벤트·피드백·재검증·승인·자원 해제, 검색·필터·언어·좁은 화면, 통신·권한 오류 후 재시도, 연결 복구, 잘못된 작업 주소를 검증합니다. 테스트 Worker는 단일 실행 슬롯으로 고정하고 각 테스트는 `e2e/fixtures`의 자동 자원 해제 Fixture를 사용합니다. 실패 시 `apps/web/test-results`에 스크린샷과 Trace가 남습니다. `apps/web/playwright-report`의 HTML 보고서는 생성물이므로 커밋하지 않습니다. E2E 이후 운영 빌드를 수행하면 개발용으로 자동 갱신된 `next-env.d.ts`도 운영 기준으로 재생성됩니다.

[미배정 대기 작업 취소](work-cancellation.md)는 확인·Esc·포커스, 중복 제출, 권한 거부, 버전 충돌, 성공 응답 유실과 SSE 변경을 추가로 검증합니다. 실행 이력 없이 취소된 작업은 임대가 없어야 하며 해제 이벤트를 만들지 않습니다. 실행된 작업의 기존 자원 해제 검증은 그대로 유지합니다.

Playwright는 단위 테스트가 다루지 못하는 실제 브라우저와 서비스 간 계약을 검증하기 위한 개발 의존성입니다. 런타임 의존성을 추가하지 않습니다. `apps/web/AGENTS.md`의 영어 관리 블록은 설치된 Next.js가 생성한 원문을 유지하며, 해당 버전의 로컬 문서를 우선 확인하도록 합니다.

## CI와 머지

모든 브라우저 회귀 테스트는 공통 Fixture에서 처리되지 않은 화면 오류를 검사합니다. 테스트 수가 모두 통과해도 CI 로그에 숨은 Hydration·런타임 오류가 없는지 확인합니다. [시간 표시 오류와 검증 사례](time-hydration.md)를 참고하세요.

필수 검사와 명령은 `.github/workflows`의 Workflow가 기준입니다. CI는 언어별 병렬 실행, 의존성 캐시, 이전 PR 실행 취소와 Timeout을 사용합니다. 검사 누락·실패·취소·대기는 통과가 아니며, 정확한 최신 Head SHA의 필수 검사와 미해결 리뷰를 확인한 뒤 머지합니다. 보호 규칙을 우회하거나 논리적 커밋을 Squash하지 않습니다.

현재 `CI` Workflow의 필수 검사는 `Python`, `Go`, `Web`입니다. Python은 API·Runner 테스트, Ruff, PostgreSQL 17 Upgrade/Check/Downgrade/Re-upgrade, Worker 잠금·격리와 [감사 기록 변경 방지](feedback-audit.md) 테스트를 실행합니다. Go는 Worker·Gateway 테스트와 vet를 실행합니다. Web은 테스트·타입 검사·ESLint·운영 빌드·Chromium E2E를 실행합니다. 브라우저 바이너리를 캐시하고 보고서와 실패 증거를 `browser-evidence` Artifact로 7일간 보존합니다. 각 Job의 제한은 8분이며, PR에서는 이전 실행을 취소합니다. 짧은 전체 검사를 유지하므로 현재는 경로 기반 생략이나 다중 Version Matrix를 사용하지 않습니다.

GitHub Actions 구성은 [Workflow 문법](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)과 [의존성 캐시 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)를 기준으로 합니다. Token은 읽기 전용으로 제한하고 외부 Action은 검증한 SHA에 고정합니다.

필수 `Go` 검사에는 `make test-monitoring`도 포함됩니다. 공식 Prometheus 3.14.0 Archive를 캐시하고 매번 SHA-256을 검증한 뒤 설정·알림 테스트를 실행합니다. 가상 시계열을 사용하므로 실제 알림의 대기 시간을 CI에서 기다리지 않습니다. 기존 필수 검사 이름·8분 Timeout·애플리케이션 검증은 유지합니다.

필수 `Python` 검사는 `python -m pytest -q apps/api/tests/test_cancellation_postgres.py`로 취소와 Claim의 실제 PostgreSQL 경쟁도 검증합니다. 로컬에서는 `KELPIE_TEST_POSTGRES_URL`에 전용 테스트 DB URL을 지정하세요. 테스트마다 임의 이름의 전용 스키마를 만들고 그 스키마만 정리하며, 기존 데이터나 감사 기록을 삭제하지 않습니다. URL이 없으면 이 네 테스트는 Skip되므로 기본 SQLite 테스트 통과만으로 경쟁 검증을 완료 처리하지 않습니다.

작업 보고에는 커밋별 목적, 테스트와 실제 사용 결과, PR·CI·머지 상태, 미커밋 변경, 남은 MVP 항목과 필요한 외부 환경을 기록합니다. 상세 저장소 규칙은 [AGENTS.md](../../AGENTS.md)를 따릅니다.

[전달 감사](delivery-audit.md)의 실제 Git·API·루프백 SCM 회귀 테스트는 기존 `make test-api`와 필수 `Python` 검사에 포함됩니다. 외부 SCM 자격증명·추가 서비스 Job이 필요하지 않습니다. `test_audit_postgres.py`는 Background 감사 제약과 기존 행 보존 Migration도 검증합니다. 승인 출처를 감사와 함께 기록하고 외부 쓰기 직전에 재검사하는 경계를 향후 전달 변경에서도 유지합니다.
