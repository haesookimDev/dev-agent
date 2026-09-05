# 운영 가이드

한국어 | [English](../en/operations.md)

## 로컬 제어 플랫폼 Smoke Test

1. `.env.example`을 `.env`로 복사하고 모든 개발용 Secret을 교체합니다.
2. `docker compose --profile demo up --build`를 실행합니다.
3. `http://localhost:3000` 대시보드에서 작업을 등록합니다.
4. 작업이 `awaiting_approval`에 도달하는지 확인하고 승인한 뒤 `completed`가 되는지 확인합니다.

`demo` Compose 프로필은 Mock Worker를 함께 시작하며 GitHub에 실제 쓰기 작업을 하지 않습니다.

## Worker 자원 보고 순서

Worker는 heartbeat의 자원 Snapshot과 전송, Claim과 로컬 예약, Release와 로컬 반납을 같은 Process Lock으로 직렬화합니다. 따라서 완료된 작업의 API 반납 직후 오래된 heartbeat가 가용 자원을 덮어써 다음 작업을 주기적 heartbeat까지 대기시키지 않습니다. 실행 자체와 일반 작업 Event는 이 Lock을 점유하지 않습니다.

Release가 성공한 경우에만 해당 작업의 로컬 예약을 한 번 해제합니다. 실행 실패 후 상태 조회·종료 전환·반납을 확인하지 못하면 예약을 유지하므로 실행 슬롯이 계속 부족할 수 있습니다. API 연결과 임대 상태, 실제 VM의 생존 여부를 확인하고 복구하세요. 예약을 없애려고 Worker를 재시작하거나 DB의 자원 수치를 임의로 수정하지 않습니다.

이 순서 보장은 한 Worker Process 안의 동시 요청에 한정됩니다. 재시작 복구, 같은 신원을 공유하는 여러 Daemon, 응답을 잃은 Claim, VM 종료·보존 공간 회수의 확인은 별도 수명주기 작업입니다. API의 Release 응답만으로 물리적 VM 삭제가 검증된 것은 아닙니다.

API 형식·DB Schema·환경변수는 바뀌지 않습니다. 적용 시에는 해당 Worker의 새 작업 유입을 중지하고 활성 작업을 안전하게 정리한 후 새 Binary로 교체합니다. 롤백은 같은 조건에서 이전 Binary로 되돌릴 수 있지만 자원 보고 경쟁 조건이 다시 생기므로 원인 수정 후 재배포를 우선합니다.

## Database Migration

Compose 배포에서는 `api-migrate` 일회성 Service가 `alembic upgrade head`를 완료한 후에만 API가 시작됩니다. Compose 밖에서 배포할 때는 새 API를 Rollout하기 전에 동일한 `DATABASE_URL`을 사용하는 배포 환경에서 다음 명령을 실행합니다. API Container Image에서는 `alembic upgrade head`를 직접 실행할 수 있습니다.

```bash
make migrate-api
```

Migration은 PostgreSQL Transaction Advisory Lock을 획득하므로 여러 배포가 동시에 실행돼도 한 번에 하나만 Schema를 변경합니다. 실패한 Migration은 API Rollout을 중단하며 원인을 해결한 뒤 같은 명령을 다시 실행할 수 있습니다. `/healthz`는 Process 생존 여부를, `/readyz`는 Database 연결과 Alembic Head 일치 여부를 확인합니다.

준비 상태 검사는 Pool 대기·연결 확인·스키마 조회에 합산 2초 제한을 적용합니다. 시간 초과는 기존 `503 {"status":"not_ready","database_schema":"unreachable"}`로 반환하며 DB 주소·자격증명·예외 원문을 포함하지 않습니다. 기동 시 DB가 응답하지 않아도 이 검사가 끝난 뒤 `/healthz`로 프로세스를 확인할 수 있습니다. 일반 업무 쿼리나 Migration 전체의 제한 시간을 바꾸는 설정은 아닙니다. [장애·복구 검증과 운영상 제한](readiness-verification.md)을 참고하세요.

Migration 도입 전에 `create_all`로 만든 Database는 현재 Table과 Column이 Baseline과 모두 일치할 때 첫 Upgrade에서 데이터를 유지한 채 자동으로 채택됩니다. 일부 Table만 있거나 Column이 다르면 Migration이 실패하므로 Schema를 먼저 복구해야 합니다. `DATABASE_SCHEMA_MODE=bootstrap`은 비어 있는 일회성 개발 Database에서만 사용하고 운영 환경은 기본값인 `validate`를 유지합니다.

현재 Baseline 아래로의 Downgrade는 모든 데이터를 삭제하므로 명시적으로 차단됩니다. 향후 Revision을 Rollback할 때는 먼저 PostgreSQL과 Object Store Backup을 만들고 해당 Revision이 문서화한 안전 범위 안에서만 `alembic downgrade <revision>`을 실행합니다. Baseline으로 되돌려야 하는 장애는 새 Database에 검증된 Backup을 복원해 복구합니다.

`20260904_0002`는 기존 Work Item ID를 Correlation ID로 사용해 기존 Work Item과 Event를 역채움합니다. `20260904_0001`로 Downgrade하면 새 Correlation Column과 Index만 제거하고 기존 작업 데이터는 유지합니다. 롤백할 때는 API 트래픽을 중지하거나 Correlation 필드를 요구하지 않는 이전 API Version으로 먼저 전환한 뒤 Migration을 Downgrade해야 합니다.

`20260904_0003`은 일회성 OIDC 로그인 시도와 불투명 인증 Session Table을 추가합니다. `20260904_0002`로 Downgrade하면 두 Table과 활성 로그인 Session이 제거되지만 Work Item 데이터는 유지됩니다. 이전 API Version으로 먼저 전환한 뒤 Downgrade하고, 사용자가 다시 로그인해야 함을 안내합니다.

`20260905_0004`는 조직·Principal·Membership·Repository·Grant·Slack 연결 Table과 Work Item의 `organization_id`를 추가합니다. 기존 작업은 모두 신원 연결과 구성원이 없는 `legacy` 조직으로 배정되며 일반 사용자의 목록·상세에서 숨겨집니다. 같은 이름의 저장소를 등록해도 과거 작업의 소유권은 이전되지 않습니다. 기존 작업과 Event·Artifact 데이터는 보존하며, 역사적 데이터의 조직 재배정은 별도 검토가 필요한 후속 작업입니다. 진행 중인 Worker 임대는 기존 계약을 유지합니다.

RBAC Rollback 시에는 먼저 API 유입·Webhook과 Worker를 중지하고 DB 및 권한 정책을 Backup합니다. `alembic downgrade 20260904_0003`은 작업 데이터는 보존하지만 조직 구분과 권한 Table을 제거하므로, 이전 API를 여러 조직에 다시 공개하면 안 됩니다. 격리된 유지보수 환경에서만 이전 Version을 시작하고 RBAC Version 복구 후 검증된 Backup·정책으로 권한을 복구합니다.

## PostgreSQL 백업·복원

[백업·복원 절차와 실제 PostgreSQL 회귀](postgres-restore.md)는 새 DB에서 전체 행·권한·감사·Sequence 보존과 실패 시 원자적 Rollback을 검증합니다. 자동 전달 재개 위험, 외부 파일·Role·폐기 상태 대조와 운영 전환 Gate를 확인하세요. 예약 운영 Backup이나 Object Store·VM 복구 완료를 의미하지 않습니다.

## 시작 시 전달 복구

DB가 준비되지 않은 상태로 시작한 API도 연결·Schema 복구 후 [대기 중인 전달을 재개](delivery-recovery.md)합니다. 이 복구는 단일 API 프로세스 전용이며, 교체 시 이전 API를 완전히 종료한 뒤 새 API를 시작해야 합니다. `/readyz` 200을 전달 완료 신호로 사용하지 마세요.

## OIDC 인증

Secret 파일 주입·교체와 실패 복구는 [Secret 관리 가이드](secret-management.md)를 따릅니다. Worker는 [개별 자격증명 발급·전환 절차](worker-credentials.md)를 먼저 수행합니다.

운영 환경에서는 대시보드와 API의 `/auth`, `/api` 경로를 같은 HTTPS 공개 Origin으로 제공합니다. 대시보드 Server Component가 Browser의 Session Cookie를 내부 API 요청에 전달하며, Browser 요청과 Event Stream도 자격증명을 포함합니다. API를 별도 공개 Hostname으로 노출하거나 OIDC 신원 Header를 주입하지 않습니다.

Identity Provider에 Authorization Code Client를 등록하고 Callback URI를 `https://<control-host>/auth/callback`으로 지정합니다. PKCE S256은 Client 종류와 관계없이 항상 사용됩니다. Provider가 공개 Client의 `none` 인증을 Metadata에 선언하지 않는 한 Client Secret을 설정합니다. 조직 Claim은 비어 있지 않은 단일 문자열이어야 합니다.

```dotenv
AUTH_MODE=oidc
OIDC_ISSUER_URL=https://identity.example.com
OIDC_CLIENT_ID=kelpie-control
OIDC_CLIENT_SECRET=<secret manager에서 주입>
OIDC_REDIRECT_URI=https://control.example.com/auth/callback
OIDC_ORGANIZATION_CLAIM=organization
OIDC_SCOPES=openid,profile
OIDC_ALLOWED_ALGORITHMS=RS256
DASHBOARD_URL=https://control.example.com
```

Issuer, Redirect URI, 발견된 Authorization·Token·JWKS Endpoint는 HTTPS여야 합니다. 발견 문서의 Issuer는 설정값과 정확히 일치해야 하며, ID Token의 Signature, 허용 Algorithm, Audience, Expiry, Issued-at, Nonce, Authorized Party와 Organization Claim을 검증합니다. 허용 Algorithm에는 `none`이나 대칭 `HS*`를 지정할 수 없습니다.

로그인 시작은 `/auth/login`이며 `state`, `nonce`, PKCE Verifier는 5분짜리 일회성 DB Record로 관리합니다. 인증 완료 후 Browser에는 무작위 불투명 Token만 `Secure`, `HttpOnly`, `SameSite=Strict` Cookie로 전달하고 DB에는 SHA-256 Hash만 저장합니다. 기본 Session 수명은 8시간과 ID Token 만료 시점 중 이른 값입니다. `/auth/logout`은 서버 Session과 Cookie를 함께 제거합니다.

`trusted_headers` 인증 Mode는 제거되었습니다. `X-Kelpie-User`와 `X-Kelpie-Role`은 인증에 사용되지 않습니다. `development` Mode도 요청 Header를 무시하고 `DEVELOPMENT_SUBJECT`와 `DEVELOPMENT_ORGANIZATION`의 고정된 관리자 신원만 사용하므로 외부에 공개하지 않습니다.

OIDC 로그인에는 등록된 조직과 구성원이 필요합니다. `(Issuer, Organization Claim)`으로 조직을 찾고 `(Issuer, Subject)`로 Principal을 식별하며, ID Token의 임의 Role Claim은 사용하지 않습니다. 세션과 이벤트 스트림은 구성원·권한을 다시 확인하므로 회수된 권한은 다음 요청 또는 다음 이벤트 조회부터 적용됩니다. 작업을 변경하는 Cookie 인증 요청은 `DASHBOARD_URL`의 Origin과 일치하는 `Origin` Header가 필요합니다.

Preview Gateway는 OIDC 범위 Preview Grant가 구현되기 전까지 기본 `disabled` 상태로 503을 반환합니다. `KELPIE_GATEWAY_AUTH_MODE=development`는 격리된 로컬 Demo에서만 사용합니다.

## 조직·저장소 권한 설정

Migration 후 로그인 트래픽을 열기 전에 제어 서버 관리자가 [정책 예제](../../config/iam.example.json)를 복사해 실제 Issuer, Organization Claim, Subject, 저장소와 GitHub App 설치 ID, Slack Team/User ID를 작성합니다. 정책 파일은 조직 하나의 **전체 원하는 상태**입니다. 토큰·Client Secret을 넣지 않고 접근을 제한한 제어 서버에 보관합니다.

API Image 안에서 같은 `DATABASE_URL`을 사용하는 관리 Process로 실행합니다. 로컬 가상환경에서는 저장소 Root에서 `.venv/bin/python -m app.iam /path/to/organization.json`으로 실행할 수 있습니다.

```bash
python -m app.iam /run/config/organization.json
```

명령은 한 Transaction으로 조직의 Membership, Repository Grant, Slack 연결과 등록 저장소를 교체합니다. 생략한 항목은 회수되므로 항상 전체 정책을 제출합니다. 최소 한 명의 Administrator가 필요하며 조직 신원 재할당, 다른 조직에 등록된 저장소·Slack 연결, 구성원이 아닌 사용자에 대한 Grant는 거부합니다. 실패한 적용은 전체 Rollback됩니다. 정책 관리는 제어 서버 운영 권한으로만 가능하며 공개 Bootstrap·권한 관리 API는 제공하지 않습니다.

| 유효 역할 | 조회·Event·산출물 | 작업 생성·피드백·Console 인수 | PR·예산·Console 승인 | 미배정 대기 작업 취소 |
| --- | --- | --- | --- | --- |
| Viewer | 허용 | 거부 | 거부 | 거부 |
| Operator | 허용 | 허용 | 거부 | 거부 |
| Approver | 허용 | 허용 | 허용 | 거부 |
| Administrator | 허용 | 허용 | 허용 | 허용 |

조직 Membership의 역할이 소속 저장소 전체의 기본 권한입니다. 저장소 Grant는 해당 저장소에서만 역할을 승격하며 기본 역할을 낮추지 않습니다. 다른 조직에는 어떤 역할도 적용되지 않습니다. `/auth/session`의 `organization`은 내부 조직 ID, `role`은 조직 기본 역할을 반환하며 저장소별 승격은 포함하지 않습니다. 등록되지 않은 저장소·다른 조직의 작업은 404, 같은 조직에서 역할 부족은 403, 미등록 구성원은 403을 반환합니다. Work Item 응답 구조와 Worker 임대 계약은 유지되고 신규 저장소 이름은 소문자로 정규화됩니다.

GitHub Webhook은 서명 검증에 더해 등록 저장소와 설치 ID가 모두 일치해야 작업을 생성합니다. OIDC Mode의 Web 작업도 정책에 지정된 설치 ID를 사용합니다. Slack 명령은 서명된 `(team_id, user_id)` 연결의 Principal을 찾아 같은 권한 검사를 수행하며, `SLACK_APPROVER_USER_IDS`는 더 이상 승인 권한을 부여하지 않습니다. Slack 작업 기록의 Actor는 연결된 Principal ID입니다. Global Slack 알림 Channel은 여전히 배포 단위 설정이므로 그 Channel의 모든 사용자가 전송되는 작업 정보를 볼 수 있는 배포에서만 알림을 활성화합니다.

격리된 `development` Mode에서는 직접 작업 등록 시 전용 개발 조직과 저장소를 자동 등록합니다. 개발 조직은 OIDC 조직 및 `legacy`와 겹칠 수 없습니다. 조직·저장소 권한과 피드백·Console·승인·[미배정 대기 작업 취소](work-cancellation.md)·[승인에 연결된 전달](delivery-audit.md)의 추가 전용 감사를 구현했습니다. 전달 감사의 새 Background/Nullable Role 응답과 구형 Job 차단, Migration·전진 복구 절차를 배포 전에 확인하세요. 실행 중 관리자 취소는 IAM/OPS 후속 범위입니다.

## 관측성 및 Correlation

API는 모든 응답에 UUID 형식의 `X-Request-ID`와 `X-Kelpie-Correlation-ID`를 반환합니다. 유효한 UUID로 들어온 Header는 유지하고 잘못된 값은 새 ID로 교체합니다. 최초 요청에서 정한 Correlation ID는 Work Item과 Event에 영속화되고 Worker, VM Runner, Web 피드백, Slack 상태 Metadata, Background Delivery까지 전달됩니다. 이 값은 추적 전용이며 인증이나 권한 결정에 사용하지 않습니다.

Prometheus는 `GET /metrics`에서 다음과 같은 저카디널리티 Metric을 수집할 수 있습니다.

- HTTP 요청 수와 응답 시간: Method, Route Template, Status
- Worker Claim 결과와 Queue 대기 시간
- 작업 상태 전이 횟수와 각 상태 체류 시간
- 승인 결정, Delivery 최초 시도·재시도, 성공·실패
- [시작 시 전달 복구](delivery-recovery-metrics.md)의 단계, 대기를 포함한 경과 시간, 준비 검사·복구 시도 결과
- [Worker·Lease·Queued 작업 지속 관측](runtime-monitoring.md)의 최신 Snapshot, 신선도와 관측 가능 여부

작업 ID, 저장소 이름, 사용자, Correlation ID는 Metric Label에 포함하지 않습니다. `/metrics`는 외부에 공개하지 말고 내부 Prometheus Network에서만 접근할 수 있게 Reverse Proxy 또는 Network Policy로 제한합니다.

기본 Log 형식은 JSON이며 모든 요청 Log에 Request ID와 Correlation ID가 포함됩니다. OTLP HTTP Collector를 사용할 때는 Trace 수집 URL 전체를 설정합니다.

```dotenv
LOG_FORMAT=json
OTEL_SERVICE_NAME=kelpie-api
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://otel-collector:4318/v1/traces
```

OTLP Endpoint를 비워 두면 Application Span은 외부로 전송하지 않지만 Prometheus Metric과 구조화 Log는 계속 제공됩니다. [기본 장애 알림과 대응 안내](monitoring-alerts.md)는 수집 실패·누락, 시작 시 복구 지연과 관찰된 전달 실패를 다룹니다. [지속 관측 알림](runtime-monitoring.md)은 Worker Heartbeat 손실·활성 Lease 만료·오래 대기하는 Queued 작업을 감시하며, 관측 실패·오래된 값·누락을 정상으로 보지 않습니다. 외부 Object Store·SCM·Delivery Worker Readiness, 실행/DeliveryJob 전체의 정체 감시와 통합 운영 Dashboard는 OBS-001의 후속 범위입니다.

## GitHub App 전달

전달 실패는 원본 예외 대신 단계와 오류 코드로 진단합니다. 이벤트 계약, 과거 기록 처리와 검증 증거는 [안전한 전달 실패 진단](delivery-failure-safety.md)을 참고하세요.

저장소 Metadata 읽기와 Issues, Contents, Pull requests 읽기·쓰기 권한을 가진 GitHub App을 만들고 설치합니다. Webhook URL을 `https://<control-host>/webhooks/github`으로 설정하고 Issues Event를 구독합니다. App Webhook Secret과 `GITHUB_WEBHOOK_SECRET`에는 같은 무작위 값을 사용합니다. 다음 값은 API 프로세스에만 제공합니다.

```dotenv
GITHUB_APP_ID=<숫자 app id>
GITHUB_PRIVATE_KEY_PATH=/run/secrets/github-app-private-key.pem
AGENT_TRIGGER_LABEL=agent-ready
```

PEM 파일은 API 사용자만 읽을 수 있도록 설정한 경로에 Mount합니다. OIDC Mode에서는 조직 정책에 저장소의 App 설치 ID를 등록해야 하며 개발 Mode의 직접 요청만 설치 정보를 자동 조회합니다. GitHub 이슈 이벤트의 서명과 등록된 설치 ID가 일치할 때 `agent-ready` 라벨이 붙은 이슈를 대기열에 넣습니다. 권한 있는 사용자가 검증된 Patch를 승인하기 전에는 전달 토큰을 발급하지 않습니다.

## 운영 제어 플랫폼

- API와 대시보드를 같은 HTTPS 공개 Origin으로 제공하고 `AUTH_MODE=oidc`를 사용합니다. Reverse Proxy는 신원 Header를 만들지 않으며 내부 API 주소에 대한 직접 네트워크 접근을 차단합니다.
- 관리형 PostgreSQL 또는 시점 복구를 지원하는 암호화 Volume을 사용합니다. API Rollout 전에 Alembic Migration을 실행하고 `/readyz`가 성공하는지 확인합니다.
- Worker, GitHub, Slack, Object Store, DNS, OIDC 자격증명을 Secret Manager에 보관합니다. Compose 파일이나 작업 VM Image에는 넣지 않습니다.
- 범위가 제한된 OIDC Preview Grant가 구현되기 전에는 Preview Gateway를 외부에 공개하지 않습니다. 이후 전용 Gateway에서 Wildcard TLS를 종료하고 작업 대상을 해석하기 전에 Grant를 검증해야 합니다.
- `PREVIEW_ALLOWED_CIDRS`에는 전용 WireGuard/libvirt VM Subnet만 지정합니다. 제어 플랫폼, Metadata, 일반 사설 서비스 네트워크를 포함하지 않습니다.
- 작업 VM은 24시간, 검증 자료는 30일 보관하는 것을 기본값으로 사용합니다. 예약된 정리 작업은 활성 작업이 아님을 확인한 뒤 명시적인 UUID 이름의 Volume만 삭제해야 합니다.

## KVM Worker

대상 Architecture에 맞춰 Go Binary를 Build하고 `/usr/local/bin/kelpie-worker`로 복사합니다. 전용 Ubuntu Host에서 저장소 Root의 `infra/host/install-ubuntu.sh`를 실행합니다. Mode `0600`인 `/etc/kelpie/worker.env`를 작성합니다.

```dotenv
KELPIE_CONTROL_URL=https://control.example.com
KELPIE_WORKER_TOKEN_FILE=/run/secrets/kelpie/worker-1.token
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
