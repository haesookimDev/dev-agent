# 운영 가이드

한국어 | [English](../en/operations.md)

## 로컬 제어 플랫폼 Smoke Test

1. `.env.example`을 `.env`로 복사하고 모든 개발용 Secret을 교체합니다.
2. `docker compose --profile demo up --build`를 실행합니다.
3. `http://localhost:3000` 대시보드에서 작업을 등록합니다.
4. 작업이 `awaiting_approval`에 도달하는지 확인하고 승인한 뒤 `completed`가 되는지 확인합니다.

`demo` Compose 프로필은 Mock Worker를 함께 시작하며 GitHub에 실제 쓰기 작업을 하지 않습니다.

## GitHub App 전달

저장소 Metadata 읽기와 Issues, Contents, Pull requests 읽기·쓰기 권한을 가진 GitHub App을 만들고 설치합니다. Webhook URL을 `https://<control-host>/webhooks/github`으로 설정하고 Issues Event를 구독합니다. App Webhook Secret과 `GITHUB_WEBHOOK_SECRET`에는 같은 무작위 값을 사용합니다. 다음 값은 API 프로세스에만 제공합니다.

```dotenv
GITHUB_APP_ID=<숫자 app id>
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app-private-key.pem
AGENT_TRIGGER_LABEL=agent-ready
```

PEM 파일은 API 사용자만 읽을 수 있도록 설정한 경로에 Mount합니다. 저장소에 App을 설치하면 Web에서 직접 만든 요청도 설치 정보를 자동으로 찾을 수 있습니다. GitHub 이슈 이벤트는 서명된 Webhook에 설치 ID를 포함합니다. `agent-ready` 라벨을 적용하면 이슈가 대기열에 들어가며, 사용자가 검증된 Patch를 승인하기 전에는 전달 토큰을 발급하지 않습니다.

## 운영 제어 플랫폼

- API와 대시보드를 OIDC 인식 Reverse Proxy 뒤에 배치합니다. `AUTH_MODE=trusted_headers`를 설정하고 Client가 신원 Header를 위조할 수 없도록 API 직접 네트워크 접근을 차단합니다.
- 관리형 PostgreSQL 또는 시점 복구를 지원하는 암호화 Volume을 사용합니다. 배포된 Schema를 변경하기 전에 Alembic Migration을 도입합니다.
- Worker, GitHub, Slack, Object Store, DNS, OIDC 자격증명을 Secret Manager에 보관합니다. Compose 파일이나 작업 VM Image에는 넣지 않습니다.
- 전용 Preview Gateway에서 Wildcard TLS를 종료하고, 작업 대상을 해석하기 전에 사용자 인증을 요구합니다.
- `PREVIEW_ALLOWED_CIDRS`에는 전용 WireGuard/libvirt VM Subnet만 지정합니다. 제어 플랫폼, Metadata, 일반 사설 서비스 네트워크를 포함하지 않습니다.
- 작업 VM은 24시간, 검증 자료는 30일 보관하는 것을 기본값으로 사용합니다. 예약된 정리 작업은 활성 작업이 아님을 확인한 뒤 명시적인 UUID 이름의 Volume만 삭제해야 합니다.

## KVM Worker

대상 Architecture에 맞춰 Go Binary를 Build하고 `/usr/local/bin/kelpie-worker`로 복사합니다. 전용 Ubuntu Host에서 저장소 Root의 `infra/host/install-ubuntu.sh`를 실행합니다. Mode `0600`인 `/etc/kelpie/worker.env`를 작성합니다.

```dotenv
KELPIE_CONTROL_URL=https://control.example.com
KELPIE_WORKER_TOKEN=<32자 이상 무작위 값>
KELPIE_WORKER_NAME=worker-1
KELPIE_EXECUTOR=libvirt
KELPIE_CPU_TOTAL=16
KELPIE_MEMORY_MB_TOTAL=49152
KELPIE_DISK_GB_TOTAL=500
KELPIE_BASE_IMAGE=/var/lib/kelpie/images/ubuntu-desktop.qcow2
KELPIE_WORK_ROOT=/var/lib/kelpie/runs
```

Golden Image에는 `kelpie` 사용자, Codex, `kelpie-runner`, Git, 언어 Toolchain, Desktop/Browser Stack, qemu-guest-agent, Runner systemd Unit이 있어야 합니다. 봉인된 Template에서 ChatGPT Device Login을 수행하고 인증 자료는 Boot 시 VM별 tmpfs에 복사합니다. Screenshot, cloud-init Log, 보존 Artifact에 인증 자료가 남지 않도록 확인합니다.

## 사고 대응

- 유지보수 전 Worker 상태를 `draining`으로 변경합니다. 실행 중인 VM을 무조건 종료하지 않습니다.
- Host가 침해되면 Worker Secret을 교체하고 WireGuard Peer를 폐기하며 해당 Worker에 배정된 활성 임대를 무효화합니다.
- 작업 임대 토큰이 유출돼도 하나의 작업에만 접근할 수 있습니다. 해당 임대를 폐기하고 이벤트는 조사 목적으로 보존합니다.
- 예상하지 못한 외부 통신, Host Service 접근 시도, Event Payload의 Secret 유사 문자열을 보안 사고로 취급합니다.
