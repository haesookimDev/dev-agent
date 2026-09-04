# 운영 가이드

한국어 | [English](../en/operations.md)

## 로컬 제어 플랫폼 Smoke Test

1. `.env.example`을 `.env`로 복사하고 모든 개발용 Secret을 교체합니다.
2. `docker compose --profile demo up --build`를 실행합니다.
3. `http://localhost:3000` 대시보드에서 작업을 등록합니다.
4. 작업이 `awaiting_approval`에 도달하는지 확인하고 승인한 뒤 `completed`가 되는지 확인합니다.

`demo` Compose 프로필은 Mock Worker를 함께 시작하며 GitHub에 실제 쓰기 작업을 하지 않습니다.

## Database Migration

Compose 배포에서는 `api-migrate` 일회성 Service가 `alembic upgrade head`를 완료한 후에만 API가 시작됩니다. Compose 밖에서 배포할 때는 새 API를 Rollout하기 전에 동일한 `DATABASE_URL`을 사용하는 배포 환경에서 다음 명령을 실행합니다. API Container Image에서는 `alembic upgrade head`를 직접 실행할 수 있습니다.

```bash
make migrate-api
```

Migration은 PostgreSQL Transaction Advisory Lock을 획득하므로 여러 배포가 동시에 실행돼도 한 번에 하나만 Schema를 변경합니다. 실패한 Migration은 API Rollout을 중단하며 원인을 해결한 뒤 같은 명령을 다시 실행할 수 있습니다. `/healthz`는 Process 생존 여부를, `/readyz`는 Database 연결과 Alembic Head 일치 여부를 확인합니다.

Migration 도입 전에 `create_all`로 만든 Database는 현재 Table과 Column이 Baseline과 모두 일치할 때 첫 Upgrade에서 데이터를 유지한 채 자동으로 채택됩니다. 일부 Table만 있거나 Column이 다르면 Migration이 실패하므로 Schema를 먼저 복구해야 합니다. `DATABASE_SCHEMA_MODE=bootstrap`은 비어 있는 일회성 개발 Database에서만 사용하고 운영 환경은 기본값인 `validate`를 유지합니다.

현재 Baseline 아래로의 Downgrade는 모든 데이터를 삭제하므로 명시적으로 차단됩니다. 향후 Revision을 Rollback할 때는 먼저 PostgreSQL과 Object Store Backup을 만들고 해당 Revision이 문서화한 안전 범위 안에서만 `alembic downgrade <revision>`을 실행합니다. Baseline으로 되돌려야 하는 장애는 새 Database에 검증된 Backup을 복원해 복구합니다.

`20260904_0002`는 기존 Work Item ID를 Correlation ID로 사용해 기존 Work Item과 Event를 역채움합니다. `20260904_0001`로 Downgrade하면 새 Correlation Column과 Index만 제거하고 기존 작업 데이터는 유지합니다. 롤백할 때는 API 트래픽을 중지하거나 Correlation 필드를 요구하지 않는 이전 API Version으로 먼저 전환한 뒤 Migration을 Downgrade해야 합니다.

## 관측성 및 Correlation

API는 모든 응답에 UUID 형식의 `X-Request-ID`와 `X-Kelpie-Correlation-ID`를 반환합니다. 유효한 UUID로 들어온 Header는 유지하고 잘못된 값은 새 ID로 교체합니다. 최초 요청에서 정한 Correlation ID는 Work Item과 Event에 영속화되고 Worker, VM Runner, Web 피드백, Slack 상태 Metadata, Background Delivery까지 전달됩니다. 이 값은 추적 전용이며 인증이나 권한 결정에 사용하지 않습니다.

Prometheus는 `GET /metrics`에서 다음과 같은 저카디널리티 Metric을 수집할 수 있습니다.

- HTTP 요청 수와 응답 시간: Method, Route Template, Status
- Worker Claim 결과와 Queue 대기 시간
- 작업 상태 전이 횟수와 각 상태 체류 시간
- 승인 결정, Delivery 최초 시도·재시도, 성공·실패

작업 ID, 저장소 이름, 사용자, Correlation ID는 Metric Label에 포함하지 않습니다. `/metrics`는 외부에 공개하지 말고 내부 Prometheus Network에서만 접근할 수 있게 Reverse Proxy 또는 Network Policy로 제한합니다.

기본 Log 형식은 JSON이며 모든 요청 Log에 Request ID와 Correlation ID가 포함됩니다. OTLP HTTP Collector를 사용할 때는 Trace 수집 URL 전체를 설정합니다.

```dotenv
LOG_FORMAT=json
OTEL_SERVICE_NAME=kelpie-api
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces
```

OTLP Endpoint를 비워 두면 Application Span은 외부로 전송하지 않지만 Prometheus Metric과 구조화 Log는 계속 제공됩니다. 외부 Object Store·SCM·Delivery Worker Readiness, Alert 기준, Dashboard는 OBS-001의 후속 범위입니다.

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
- 관리형 PostgreSQL 또는 시점 복구를 지원하는 암호화 Volume을 사용합니다. API Rollout 전에 Alembic Migration을 실행하고 `/readyz`가 성공하는지 확인합니다.
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
