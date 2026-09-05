# 개발 및 검증 절차

한국어 | [English](../en/development.md)

## 완료 기준

개발은 작업 브랜치에서 수행하고, 논리적 단위마다 관련 테스트를 실행한 뒤 한국어 메시지로 커밋합니다. 자동 테스트와 실제 사용 검증이 통과하면 증거를 포함한 PR을 만들고 최신 커밋의 CI와 리뷰 상태를 확인합니다. 사용자가 머지를 위임한 작업은 에이전트가 Merge Commit으로 머지하고 `main`을 Fast-forward 동기화합니다. 필수 검증을 수행할 수 없으면 Draft로 유지합니다.

MVP 지속 개발에서는 [로드맵의 바로 다음 Release](roadmap-summary.md#바로-다음-release)를 완료 기준으로 사용합니다. P3 이후의 확장 기능을 자동으로 MVP에 포함하지 않습니다. 현재 MVP 작업에 대한 자동 머지 위임은 운영 배포, 유료 인프라, 브랜치 보호 우회, 제품 내부의 사용자 승인 제거까지 허용하지 않습니다.

## 검증

- `make test`: API, Runner, Worker, Gateway, Web 테스트와 Web 타입 검사
- `make lint`: Python Ruff, Go vet, Web ESLint
- `cd apps/web && npm run build`: 운영 Web 빌드
- DB 변경: 실제 PostgreSQL의 Upgrade, 스키마 일치, 안전한 범위의 Rollback
- 사용자 기능: 실제 서비스를 구동한 뒤 브라우저에서 관련 여정 수행, 한국어·영어 및 데스크톱·모바일 너비 확인

UI에서는 작업 현황과 다음 행동을 우선하고, 빈 상태·오류·키보드 탐색·포커스·대비·긴 콘텐츠를 확인합니다. 최종 코드의 화면과 콘솔·네트워크 오류를 Browser/Computer-use 도구로 직접 확인하고, 핵심 흐름은 재실행 가능한 E2E로 남깁니다. 네이티브 Console 입력 변경은 실제 데스크톱 조작도 검증합니다. 변경 전후 화면은 비밀정보 없이 PR에 첨부합니다.

테스트 환경과 자원은 격리하고 자신이 만든 자원만 정리합니다. Mock Worker의 성공을 실제 Linux/KVM Acceptance로 표현하지 않습니다. 구동 대상이 없는 문서 변경은 해당 없음과 이유를 기록합니다.

## CI와 머지

필수 검사와 명령은 `.github/workflows`의 Workflow가 기준입니다. CI는 언어별 병렬 실행, 의존성 캐시, 이전 PR 실행 취소와 Timeout을 사용합니다. 검사 누락·실패·취소·대기는 통과가 아니며, 정확한 최신 Head SHA의 필수 검사와 미해결 리뷰를 확인한 뒤 머지합니다. 보호 규칙을 우회하거나 논리적 커밋을 Squash하지 않습니다.

현재 `CI` Workflow의 필수 검사는 `Python`, `Go`, `Web`입니다. Python은 API·Runner 테스트, Ruff, PostgreSQL 17 Upgrade/Check/Downgrade/Re-upgrade를 실행합니다. Go는 Worker·Gateway 테스트와 vet를 실행합니다. Web은 테스트·타입 검사·ESLint·운영 빌드를 실행합니다. 각 Job의 제한은 8분이며, PR에서는 이전 실행을 취소합니다. 짧은 전체 검사를 유지하므로 현재는 경로 기반 생략이나 다중 Version Matrix를 사용하지 않습니다.

GitHub Actions 구성은 [Workflow 문법](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)과 [의존성 캐시 문서](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching)를 기준으로 합니다. Token은 읽기 전용으로 제한하고 외부 Action은 검증한 SHA에 고정합니다.

작업 보고에는 커밋별 목적, 테스트와 실제 사용 결과, PR·CI·머지 상태, 미커밋 변경, 남은 MVP 항목과 필요한 외부 환경을 기록합니다. 상세 저장소 규칙은 [AGENTS.md](../../AGENTS.md)를 따릅니다.
