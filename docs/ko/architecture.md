# 아키텍처

한국어 | [English](../en/architecture.md)

Kelpie는 제어 권한과 신뢰할 수 없는 개발 실행 환경을 분리합니다.

```text
GitHub / Web / Slack
          │ HTTPS Webhook 및 피드백
          ▼
┌──────────────── 중앙 VPS ───────────────────┐
│ Next.js UI ─ FastAPI ─ PostgreSQL           │
│                    └ 이벤트 / 임대           │
└──────────────────────┬──────────────────────┘
                       │ 외부 연결 WireGuard + TLS
                       ▼
┌──────────── 전용 Ubuntu Worker ─────────────┐
│ Go 데몬 ─ libvirt/QEMU/KVM                  │
│            ├ 작업 VM A: Codex + 브라우저    │
│            └ 작업 VM B: Codex + 브라우저    │
└─────────────────────────────────────────────┘
```

## 신뢰 경계

제어 플랫폼은 사용자 신원, 승인, 저장소 설치 정보, 쓰기 자격증명을 소유합니다. Worker는 VM 생명주기를 관리하지만 Worker 등록과 작업 할당에 사용하는 전역 자격증명만 받습니다. 작업 VM은 하나의 작업에만 한정된 무작위·해시·갱신 가능 임대 토큰을 받습니다. 이 토큰은 이벤트 추가, 피드백 조회, 유효한 상태 전이를 요청할 수 있지만 작업 할당, 다른 작업 조회, GitHub 토큰 발급, 자기 승인은 할 수 없습니다.

이슈 내용과 저장소 코드는 신뢰할 수 없는 입력으로 취급하고 작업 VM 안에서만 실행합니다. 에이전트는 해당 VM에서 root 권한을 가지지만 호스트 Socket, 호스트 Mount, Worker 자격증명, 사설 네트워크 직접 경로는 받지 않습니다. 인터넷 송신은 기록하며 RFC1918, Link-local Metadata, 제어 플랫폼 관리 주소 접근을 차단합니다.

## 영속 상태

PostgreSQL을 단일 진실 공급원으로 사용합니다. 각 작업은 단조 증가하는 버전을 가집니다. 상태 전이 요청은 예상 버전을 전달하고 상태 변경과 이벤트를 같은 트랜잭션에 기록합니다. 중복 Webhook, 재시도, Worker 재시작, 동시 피드백은 하나의 성공한 전이나 명시적인 `409` 충돌로 처리됩니다.

초기 Worker 통신은 외부 방향 HTTPS Polling입니다. API 전송 DTO와 도메인 타입을 분리했으므로 작업 의미를 바꾸지 않고 mTLS gRPC Stream으로 교체할 수 있습니다.

## 에이전트 어댑터

VM Runner는 `codex app-server`를 자식 프로세스로 시작하고 stdio의 줄 단위 JSON-RPC로 통신합니다. Thread, Turn, Item, Command, Tool 알림을 정규화된 이벤트로 기록합니다. 격리된 VM 내부 작업에 대한 승인 요청은 세션 동안 허용할 수 있지만 Commit, Push, Pull Request, 예산 확장, Console 제어권 인수는 에이전트가 답할 수 없는 플랫폼 승인으로 유지합니다.

`.kelpie/artifacts` 아래의 검증 자료는 지원하는 이미지·텍스트 형식으로 필터링하고 파일당 10 MiB로 제한한 뒤 작업 임대 토큰으로 업로드해 대시보드에 표시합니다. Slack이 설정되어 있으면 이미지를 외부 업로드 흐름으로 전송합니다. VM 밖으로 콘텐츠를 내보내거나 제어 플랫폼에서 제공하기 전에 파일명, MIME Signature, 경로, Symbolic Link를 검증합니다.

모델 이름은 코드에 고정하지 않습니다. Codex 설치 환경의 기본 설정을 사용하므로 제어 플랫폼 배포와 독립적으로 모델을 통제하며 업그레이드할 수 있습니다.

## 통제된 전달

VM은 저장소 쓰기 토큰을 받지 않습니다. 검사가 통과하면 크기가 제한된 Binary Git Patch를 제어 플랫폼에 업로드하고 `awaiting_approval` 상태로 전환합니다. 승인자는 실행 결과를 검토해 승인하거나 피드백을 돌려보냅니다. 승인은 영속적인 전달 작업을 생성합니다. 제어 플랫폼은 수명이 짧은 GitHub App 설치 토큰을 발급하고 기본 Branch를 새로 Clone하고 Patch를 적용하고 Bot 사용자로 Commit하고 `agent/<work-id>-<slug>` Branch를 Push한 뒤 PR을 생성합니다. 결정적인 Branch 이름과 GitHub Branch/PR 조회를 사용하므로 중단된 전달을 중복 PR 없이 재개할 수 있습니다.

## Preview 및 Console Routing

Runner는 만료 시간이 있는 Preview 대상과 선택적인 noVNC 대상을 등록할 수 있습니다. 대상은 `PREVIEW_ALLOWED_CIDRS` 안의 Literal IP여야 하므로 Gateway가 임의 SSRF Proxy가 되는 것을 방지합니다. Wildcard Gateway는 제어 플랫폼에서 Hostname을 해석하고 HTTP/WebSocket 요청을 전달합니다. Console 소유권은 버전이 있는 낙관적 임대입니다. 기본적으로 에이전트가 입력을 소유하며 사용자의 제어권 인수는 독점적이고 감사 이벤트로 기록됩니다. 현재 Gateway는 읽기 전용 상태를 Console Upstream에 전달하므로 noVNC 배포 환경에서 입력 경계가 이 신호를 반드시 강제해야 합니다.

## 현재 구현 경계

현재 버티컬 슬라이스에는 GitHub 이슈·직접 요구사항 수집, 영속 오케스트레이션, Mock/libvirt 실행기 경계, Codex 실행, 독립 명령 검증, 실시간 관찰, Web/Slack 피드백, 통제된 GitHub 전달, Wildcard Preview Routing, Version이 있는 Database Migration, 작업 전 구간 Correlation ID, 기본 Trace·Metric·구조화 Log, OIDC Authorization Code 인증이 포함됩니다. 운영 배포를 위해 조직·저장소 권한과 불변 감사 기록, 외부 의존성 Readiness와 Alert, OIDC Preview Grant, VM Preview용 WireGuard 경로, 강화된 noVNC 입력 필터, 자동 Golden Image Build, 보존 기간 Worker가 추가로 필요합니다. GitLab 전달과 자율 이슈 탐색은 이후 Provider Adapter로 개발합니다.
