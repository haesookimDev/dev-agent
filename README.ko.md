# Kelpie

한국어 | [English](README.en.md)

Kelpie는 자율 소프트웨어 개발 에이전트를 위한 셀프 호스팅 제어 플랫폼입니다. GitHub 이슈나 사용자의 직접 요구사항을 격리된 작업으로 변환하고, 모든 실행 과정을 대시보드에 실시간으로 전달하며, 사용자 피드백을 수집하고, 명시적인 승인 이후에만 커밋과 Pull Request를 생성합니다.

이 저장소에는 실행 가능한 첫 번째 버티컬 슬라이스가 포함되어 있습니다.

- `apps/api`: PostgreSQL 상태 머신, GitHub App 수집·전달, Worker 임대, SSE 이벤트, Slack 피드백, RBAC, 승인 게이트를 제공하는 FastAPI 제어 플랫폼
- `apps/web`: 한국어·영어 로케일 경로, 실시간 작업 상태와 이벤트 스트리밍을 제공하는 Next.js 운영 대시보드
- `apps/worker`: 자원 보고, 작업 할당, Mock/libvirt 실행기 경계를 제공하는 Go 호스트 데몬
- `apps/runner`: 안정적인 stdio JSON-RPC 전송을 사용하는 VM 내부 Codex App Server 어댑터
- `apps/gateway`: 독점 콘솔 임대를 지원하는 인증 기반 와일드카드 Preview·Console Reverse Proxy
- `infra`: Ubuntu/KVM 호스트 설치, 네트워크 송신 정책, systemd 유닛

## 빠른 시작

```bash
cp .env.example .env
docker compose --profile demo up --build
```

<http://localhost:3000>을 엽니다. 첫 방문 시 브라우저 언어를 감지하며 대시보드에서 `/ko`와 `/en`을 전환할 수 있습니다. 개발 인증 모드는 요청을 관리자로 취급합니다. 공개 환경에서는 절대로 `AUTH_MODE=development`를 사용하지 마세요.

직접 요구사항 등록:

```bash
curl -X POST http://localhost:8000/api/work-items \
  -H 'content-type: application/json' \
  -d '{"title":"상태 확인 API 추가","requirement":"GET /health를 구현하고 테스트한다","repository":"owner/repo"}'
```

로컬에서 실행 가능한 모든 검사는 `make test`로 실행합니다. KVM Worker를 배포하기 전에 [아키텍처](docs/ko/architecture.md), [운영 가이드](docs/ko/operations.md), [보안 모델](docs/ko/security.md)을 읽어주세요.

`demo` 프로필은 Mock 실행기를 사용하므로 Linux/KVM 또는 GitHub 자격증명 없이도 대기 → 승인 → 완료 전체 흐름을 확인할 수 있습니다. 실제 실행에는 Ubuntu KVM Worker와 GitHub App이 필요합니다. 독립 검증이 끝나면 VM은 바이너리 Git 패치를 업로드하며, 중앙 제어 플랫폼만 설치 토큰을 발급하고 패치를 커밋하고 결정적인 에이전트 브랜치를 Push하고 Pull Request를 생성할 수 있습니다.

현재 구현은 첫 번째 배포 가능한 버티컬 슬라이스이며 최종 자율 보안 분석 제품은 아닙니다. 남은 운영 수준 개발 범위는 [로드맵 요약](docs/ko/roadmap-summary.md)과 [상세 계획](docs/ko/roadmap-detailed.md)에 정의되어 있습니다.

## 보안 경계

에이전트는 작업 VM 내부에서만 root 권한을 가지며 Worker 호스트 권한은 받지 않습니다. Worker 자격증명과 저장소 쓰기 토큰은 VM에 주입하지 않습니다. 작업별 임대 토큰은 해당 작업의 이벤트 및 상태 갱신만 허용하며, 사용자가 검증 결과를 승인한 이후에만 쓰기 자격증명을 발급합니다.
