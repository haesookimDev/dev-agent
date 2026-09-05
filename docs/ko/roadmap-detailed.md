# 다음 개발 상세 계획

한국어 | [English](../en/roadmap-detailed.md) · [요약](roadmap-summary.md)

## 1. 계획 원칙

- 신뢰 경계를 유지합니다. 작업 VM은 일회성이며 신뢰하지 않고, 제어 플랫폼만 승인과 쓰기 자격증명 발급을 할 수 있습니다.
- API에 Provider 전용 지름길을 추가하지 않고 Test와 운영 통제를 포함하는 수직 기능 단위로 배포합니다.
- 모든 마일스톤은 위험도에 비례한 Unit, Integration, 장애 복구, Browser/Computer-use 검증을 요구합니다.
- Provider, Executor, Artifact Store, Identity, Messenger 구현은 명시적인 Interface 뒤에 둡니다.
- 자율 제안과 그 제안의 실행을 서로 다른 주체로 취급합니다. 제안은 정책 또는 사용자 검토를 통과해야 실행 가능한 작업이 됩니다.

개발 규모는 상대값을 사용합니다. S는 좁은 변경, M은 여러 Component를 건드리는 기능, L은 전용 검증 환경이 필요한 Infrastructure 또는 Security 마일스톤입니다.

## 2. P0 — 운영 기반

### MIG-001 · Version이 있는 Database Migration — M

상태: 구현 완료 (2026-09-04)

범위:

- 현재 모든 Model의 Baseline Migration을 포함한 Alembic을 도입합니다.
- Test와 명시적인 Bootstrap Mode 외에는 Runtime `create_all`을 비활성화합니다.
- Migration Lock과 API Rollout 전에 실행하는 배포 명령을 추가합니다.
- 빈 설치, Baseline Upgrade, 안전한 범위의 Rollback, Migration 실패 중 재시작을 테스트합니다.

운영과 유사한 배포가 데이터 손실 없이 Upgrade되고 Schema가 맞지 않으면 API가 Ready 상태가 되지 않을 때 완료됩니다.

### IAM-001 · OIDC 신원 및 저장소 권한 — L

상태: 인증 Batch(2026-09-04)와 조직·저장소 RBAC Batch(2026-09-05) 구현 완료. 등록된 Membership과 저장소별 Role 승격, 조직 간 접근 차단, GitHub 설치 등록과 Slack Principal 연결, 권한 회수를 포함합니다. [추가 전용 피드백 감사 Batch](feedback-audit.md)(2026-09-06)를 구현했습니다. Console·승인·취소·전달 감사와 OIDC Preview Grant는 남아 있습니다.

범위:

- Issuer, Audience, Signature, Expiry, Nonce, Organization Claim을 검증합니다.
- 조직 Role과 저장소 단위 Viewer, Operator, Approver, Administrator 권한을 모델링합니다.
- 공개 경계에서 개발용 Header를 제거합니다. 신뢰된 신원 Header는 내부 Reverse Proxy에서만 전달할 수 있습니다.
- 피드백, Console 제어권 인수, 승인, 취소, 전달 시 Actor ID, Identity Provider, Role 결정, Request ID, Source IP를 기록합니다.

조직 간 접근, 위조 Header, 만료 Token, Viewer 승인이 Integration Test에서 실패할 때 완료됩니다.

### SEC-001 · Secret Provider 및 자격증명 교체 — M

범위:

- 배포 환경에 맞춰 File, Kubernetes, Vault 구현을 선택할 수 있는 Secret Provider Interface를 추가합니다.
- 전역 Worker Secret을 개별 식별·폐기 가능한 Worker 자격증명 또는 mTLS 인증서로 교체합니다.
- 활성 작업을 중단하지 않는 Rotation과 Worker 격리 시 자격증명 폐기를 정의합니다.
- Log, Event, Artifact, Crash Dump, cloud-init 출력에 Secret Scan을 적용합니다.

모든 Worker의 Secret을 바꾸지 않고 하나의 Worker만 폐기할 수 있고 보존된 증거에 Secret이 없을 때 완료됩니다.

### OBS-001 · Observability 및 Correlation — M

상태: 기본 Correlation ID·Trace·Metric·구조화 Log, DB 준비 검사 제한, 시작 시 전달 복구 지표와 [기본 장애 알림](monitoring-alerts.md)을 구현했습니다. 외부 의존성 Readiness, Worker Heartbeat·Lease·상시 Queue 알림과 통합 운영 Dashboard는 남아 있습니다.

범위:

- Webhook, Work Item, Claim, VM, Codex Turn, Artifact, Notification, Delivery 전체에 Correlation ID를 전달합니다.
- Queue 대기, Provisioning, 실행 시간, 재시도, Token·비용 추정, 승인, 실패에 대한 OpenTelemetry Trace와 Prometheus Metric을 내보냅니다.
- Database, Object Store, SCM, Background Delivery Worker의 구조화된 Health/Readiness 검사를 추가합니다.
- 멈춘 상태, 임대 만료, Worker Heartbeat 손실, Delivery 오류에 대한 Alert 기준과 Dashboard를 정의합니다.

하나의 작업을 처음부터 끝까지 Trace할 수 있고 의도적으로 멈춘 작업이 조치 가능한 Alert를 만들 때 완료됩니다.

### OPS-001 · 보존 및 복구 Worker — M

범위:

- Event, Artifact, Delivery Bundle, VM Disk, Preview, Audit Record의 명시적인 보존 정책을 구현합니다.
- 검증된 UUID 대상과 활성 작업 안전 검사를 사용하는 멱등 Janitor Job을 추가합니다.
- PostgreSQL Backup/Restore와 Object Store 복구를 문서화하고 테스트합니다.
- 감사 이벤트를 남기는 관리자 격리, 재시도, 취소, 강제 임대 해제 기능을 추가합니다.

활성 작업을 건드리지 않고 만료 데이터를 제거하며 깨끗한 환경에 Backup을 복구할 수 있을 때 완료됩니다.

## 3. P1 — 실제 KVM 실행 및 연결

### IMG-001 · 재현 가능한 Golden Image Pipeline — L

- Packer 또는 동등한 선언형 Builder로 Version이 고정된 Ubuntu Desktop Image를 만듭니다.
- Codex, Runner, Browser, Desktop, qemu-guest-agent, 일반 Toolchain, CA Root, Update Policy를 설치합니다.
- SBOM, 취약점 Report, Checksum, Signature, Image Version Metadata를 생성합니다.
- 모든 Image를 Boot Test하고 Health Probe가 실패하면 Worker Rollout을 자동 Rollback합니다.

### KVM-001 · 완전한 VM 생명주기 — L

- Domain, Overlay, Seed, IP, Boot 상태, Timestamp, Cleanup 상태를 영속적인 Run Metadata로 추적합니다.
- Boot Timeout, 정상 Shutdown, 강제 종료, Orphan 정합성 복구, Host 재시작 복구를 추가합니다.
- CPU, Memory, Disk IOPS, 동시 Browser 용량을 할당하고 자원을 정확히 한 번 반환합니다.
- 작업별 Network를 사용하고 Host, Metadata Endpoint, 다른 VM에 대한 직접 접근을 차단합니다.

### NET-001 · WireGuard Preview Network — L

- Worker의 외부 방향 Peer와 작업별 Routing 주소를 구성하며 Worker Inbound Port는 열지 않습니다.
- Preview Gateway의 Wildcard DNS와 인증서를 자동화합니다.
- 모든 HTTP/WebSocket 대상 해석에서 사용자, 조직, 작업, Target CIDR, 만료 시간을 검증합니다.
- Host Header Injection, Rebinding, 금지 Network로의 Redirect, 만료된 Preview Route를 테스트합니다.

### GUI-001 · 강제되는 Console 소유권 — M

- noVNC 앞에 강화된 입력 Filter를 배치해 읽기 전용 상태를 Header로 알리는 수준이 아니라 실제로 강제합니다.
- 사용자에게 소유권을 주기 전에 에이전트 Mouse/Keyboard 동작을 중단하고 반환 시 임대 Version을 검증합니다.
- 제어권 인수, 입력 중단, 반환, Timeout, 강제 복구 Event를 기록합니다.
- 동시에 실행하는 GUI 작업 2개가 Display, Clipboard, Browser Profile, 입력 Channel을 공유하지 않음을 증명합니다.

### E2E-001 · 실제 Host Acceptance Suite — M

서로 다른 저장소 2개를 동시에 Clone, Codex 분석, Browser 검증, 피드백, 재검증, 승인, 전달, 정리까지 실행합니다. 중간에 Worker 재시작과 Network 단절을 주입합니다. 자격증명 유출, 작업 간 입력 혼선, 중복 PR, 자원 누수가 없고 두 작업이 복구돼야 통과합니다.

## 4. P2 — 검증 및 증거

### VER-001 · 저장소 검증 정책 — M

- 필수 명령, 허용 가능한 Override, Timeout, 환경 Service, Browser Journey, 증거 요구사항을 정의하는 Versioned Repository Policy를 만듭니다.
- 변경 영역을 찾아 관련 Test를 선택하되 저장소가 요구하는 Gate는 유지합니다.
- Command, Exit Code, Duration, 환경 Fingerprint, Output Digest, Retry 사유를 기록합니다.
- 결정적 실패, Infrastructure 실패, Flaky 결과, Policy 실패를 구분합니다.

### BROWSER-001 · Browser/Computer-use Supervisor — L

- 작업별 고유 Browser Profile과 Display를 할당합니다.
- 선언된 Acceptance Journey를 실행하고 안정적인 Checkpoint마다 Screenshot을 캡처합니다.
- 업로드 전에 설정된 Selector와 Secret 형태의 값을 Redaction합니다.
- 필수 UI 증거가 없거나 만료됐거나 다른 작업에서 생성됐거나 최종 코드 변경 이전에 캡처됐다면 승인을 거부합니다.

### ART-001 · 운영 Artifact Store — M

- 로컬 Artifact 경로를 Object Store Interface, Content Hash, Encryption, Retention Tag, 수명이 짧은 Signed Download로 교체합니다.
- Malware·Content Scan, Quota 강제, Multipart Upload, 변경 불가능한 Evidence Manifest를 추가합니다.
- Binary는 기본 비공개로 두고 모든 Download를 기록합니다.

### EVAL-001 · 독립 결과 평가기 — M

- 구현 이후 별도 Principal로 평가기를 실행합니다.
- 요구사항 완료 기준, Code Diff, Test 결과, Browser 증거, Policy를 비교합니다.
- 구조화된 Pass/Fail Finding을 만들고 해결되지 않은 높은 심각도의 Finding이 있으면 승인을 차단합니다.
- 구현 에이전트가 평가 Policy 또는 결과 Record를 변경하지 못하게 합니다.

## 5. P3 — Provider 및 Messenger

### SCM-001 · SCM Provider Contract — M

Provider 중립적인 Repository, Installation, Issue, Branch, Commit, Check, Comment, Merge Request 작업을 정의합니다. 기존 Work Item을 깨뜨리지 않고 GitHub 전용 Field를 Provider Metadata로 이동합니다.

### GL-001 · GitLab Adapter — L

서명된 System Hook, Label Trigger, Project Access Token 또는 OAuth Application Credential, Branch/MR 전달, 멱등성, Comment, 승인, GitLab Status Check를 구현합니다. 동일한 Delivery Bundle과 승인 경계를 재사용합니다.

### SCM-002 · Review 동기화 — M

PR/MR Review Comment를 피드백으로 가져와 Work Item과 연결하고 구현·검증을 다시 수행한 뒤 Check를 갱신합니다. Provider 신원이 승인 권한을 가진 사용자와 매핑되지 않으면 Comment를 승인으로 취급하지 않습니다.

### MSG-001 · Messenger 전달 — M

Slack을 시작으로 추가 Provider를 지원하는 Messenger Interface를 만듭니다. 번역된 요약, 결과 이미지, 증거 링크, 승인 Control, 실패 진단을 전송합니다. 피드백이나 승인을 받기 전에 Signature를 검증하고 Messenger 신원을 플랫폼 신원과 매핑합니다.

## 6. P4 — Scheduling 및 다중 에이전트 Orchestration

### ROUTE-001 · 자원·시간 인식 Scheduler — L

- 저장소와 작업 분류별 Queue, Setup, Model, Test, Browser, Delivery 소요 이력을 수집합니다.
- CPU, Memory, Disk, Accelerator, Browser/Display, Network, 전체 시간을 신뢰 구간과 함께 추정합니다.
- 적합성, Cache Locality, 예상 완료 시각, 자원 파편화, 공정성, 장애율, 유지보수 상태로 실행 가능한 Worker를 평가합니다.
- 할당 전에 중앙에서 자원을 예약하고 Worker Local 예약과 정합성을 맞춥니다.
- Queue 대기와 Starvation SLO를 정의하고 Aging으로 크거나 드문 작업도 결국 실행되게 합니다.

### DAG-001 · Main Agent 및 Sub-agent 작업 Graph — L

- 분석, 구현, Review, Test, 조사를 영속적인 DAG로 표현합니다.
- Main Agent가 Repository Snapshot, 명시적인 Capability, Budget, 완료 기준을 가진 제한된 Child Task를 만들 수 있게 합니다.
- Child Worktree를 격리하고 Main Agent가 Conflict와 Test를 확인한 뒤에만 결과를 병합합니다.
- Graph 상태와 Agent별 Log를 Dashboard에 Streaming합니다.

### REC-001 · Checkpoint 및 복구 — M

Codex Thread ID, Repository Revision, Worktree Diff, Verification Manifest, Artifact Manifest, Budget 사용량, 대기 중인 질문을 저장합니다. Base Revision과 Policy가 호환될 때만 재개하며 그렇지 않으면 명시적으로 다시 계획합니다.

### COST-001 · Budget 정책 — M

Model Token, Compute Minute, Storage, 외부 Service 비용을 추적합니다. 작업·저장소·조직 단위 제한을 지원합니다. Soft Limit을 넘으면 승인을 요청하고 Hard Limit을 넘으면 Checkpoint를 잃지 않고 작업을 중단합니다.

## 7. P5 — 자율 탐색

### DISC-001 · 탐색 Scheduler — L

Dependency Update, SAST, Secret Scan, Test Gap, Flaky Test, 성능 회귀, Dead Code, 문서 불일치에 대한 읽기 전용 예약 작업을 실행합니다. 탐색 자격증명은 코드를 변경하거나 실행 라벨을 적용할 수 없어야 합니다.

### TRIAGE-001 · Finding 정규화 및 중복 제거 — M

Tool Finding을 Fingerprint, 영향 Revision, Severity, Confidence, Exploitability, Owner, Evidence로 정규화합니다. 중복을 억제하고 관련 Revision에서 다시 발생한 경우에만 다시 엽니다.

### ISSUE-001 · 통제된 Issue 생성 — M

증거, 영향, 개선 방향, 완료 기준, 위험을 포함한 제안 Issue를 생성하고 제안 전용 Label을 적용합니다. 별도 Policy Engine 또는 권한 있는 사용자가 실행 Label로 승격해야 합니다.

### SAFE-001 · 자율 동작 안전성 평가 — L

Prompt Injection, 오염된 Repository, 악성 Test, 유출 시도, 자기 승인, 범위 확장, 파괴적 Cleanup, False-positive Issue 폭증 시나리오를 유지합니다. 이 평가가 요구 수준으로 통과하기 전에는 자율 동작을 활성화하지 않습니다.

## 8. P6 — 확장 및 Release 운영

- Singleton-safe Background Job과 PostgreSQL Advisory Lock을 사용하는 고가용성 제어 플랫폼
- Multi-tenant Quota, Network 격리, Encryption Key, Audit Export, Data Residency Control
- Canary Worker 및 Golden Image Rollout, 호환성 Matrix, 자동 Rollback
- 용량 계획, 비용 배분 Report, SLO/Error Budget, 사고 대응 절차, 재해 복구 훈련

## 9. 모든 Issue의 완료 조건

1. 구현 전에 완료 기준과 Threat 고려사항을 기록합니다.
2. 적용 가능한 성공, 권한 실패, Timeout, Retry, 중복 전달을 Unit 및 Integration Test로 검증합니다.
3. 사용자 UI 문구를 한국어와 영어 Catalog에 모두 추가합니다.
4. 운영 문서를 두 언어로 갱신합니다.
5. Log와 Evidence가 Correlation ID를 포함하고 Redaction 검사를 통과합니다.
6. 사용자에게 보이는 변경에는 Browser/Computer-use 검증을 첨부합니다.
7. Migration, Rollback, 호환성, Cleanup 동작을 문서화합니다.
8. Commit, Push, PR/MR, Budget 증가, Console 제어권 인수가 승인 정책을 우회하지 않습니다.

## 10. 첫 번째 구현 Batch

다음 의존성 순서로 첫 번째 Batch를 별도 Issue로 만듭니다.

1. `MIG-001` Database Migration
2. **완료:** `OBS-001` Correlation ID 및 기본 Metric
3. `IAM-001` OIDC 및 권한 모델
4. `SEC-001` Worker별 자격증명 및 Secret Provider
5. `IMG-001` 재현 가능한 VM Image
6. `KVM-001` 생명주기 복구
7. `NET-001` WireGuard Preview
8. `GUI-001` 강제되는 Console 소유권
9. `E2E-001` 실제 Host 동시 실행 Acceptance

이 Batch와 P2 Evidence Gate가 완료되기 전에는 자율 탐색 개발을 시작하지 않습니다.
