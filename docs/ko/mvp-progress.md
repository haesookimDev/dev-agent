# MVP 진행 현황 — 2026-09-10

한국어 | [English](../en/mvp-progress.md) · [개발 요약](roadmap-summary.md) · [상세 기준](roadmap-detailed.md)

## 기준과 진행률

제품 기준 코드: `main`의 `c231d6f0bf73d58505c160870a1919a07950f63c` ([PR #56](https://github.com/haesookimDev/dev-agent/pull/56) 병합). 로컬 개발 호스트 검증은 `feat/macos-kvm-lab`의 템플릿 `055a61e`·실제 부팅 Probe `a7413dd`·CI 연결 `847e0ce`에 근거하며 아래에 별도로 기록합니다. 아래 평가는 문서뿐 아니라 현재 코드·테스트·실제 구동 기록을 대조한 시점별 기록입니다. Draft 브랜치나 계획은 완료로 세지 않습니다.

[바로 다음 Release](roadmap-summary.md#바로-다음-release)에 명시된 7단계를 고정된 분모로 사용합니다. **검증 완료 1/7 = 14.3%**, 부분 완료 3/7, 미완료 3/7입니다. 부분 구현에는 임의 점수를 주지 않습니다. 이 수치는 릴리즈 단계의 검증 완료율이며 코드 작성량·투입 공수·남은 일정의 비율이 아닙니다. 전체 개발 공수의 정확한 완료율은 현재 근거로 산정하지 않습니다.

## 완료 기준별 현황

| 단계 | 현재 판정과 확인한 근거 | 완료까지 남은 내용 |
| --- | --- | --- |
| 1. Versioned DB Migration | **완료.** Alembic Head `20260909_0011`, PostgreSQL Migration Lock, 기본 `validate`, 빈 DB·기존 데이터 채택·안전한 Downgrade·실패 Rollback·실제 복원 회귀를 확인했습니다. [운영](operations.md#database-migration), [Migration 테스트](../../apps/api/tests/test_migrations.py), [원자성 회귀](../../apps/api/tests/test_migration_atomicity.py) | 이후 Schema 변경마다 동일 Gate를 다시 통과해야 합니다. 임의 운영 DB 전환까지 위임받은 것은 아닙니다. |
| 2. OIDC·조직/저장소 권한·감사 | **부분 완료.** OIDC/RBAC, 추가 전용 피드백·승인·대기 취소·전달 감사가 있습니다. [운영](operations.md#oidc-인증), [IAM 테스트](../../apps/api/tests/test_iam.py), [감사](control-action-audit.md) | OIDC Preview Grant의 통합·실제 TLS 경계 검증, 실행 중 관리자 취소와 VM 종료·정리 보장. |
| 3. Worker Secret·격리 | **부분 완료.** 파일 Provider, 개별 발급·중첩 교체·폐기, 제어 영역 격리, Runner/API/Worker 진단 보호를 검증했습니다. [자격증명](worker-credentials.md), [격리](worker-quarantine.md), [최신 진단 검증](worker-private-diagnostics.md) | 실제 Host/VM/네트워크와 기존 연결 격리, Event/Artifact/Crash Dump/cloud-init 전체 비노출·보존 정책. 제한된 가림을 범용 Secret Scan으로 계산하지 않습니다. |
| 4. 관측·복구·보존 운영 기반 | **부분 완료.** Correlation·Metric·DB 준비 검사·장애 알림, 실제 PostgreSQL 복원, 일반 산출물 백업·예약 정리가 있습니다. [관측](execution-monitoring.md), [복원](postgres-restore.md), [예약 정리](scheduled-retention.md) | 외부 의존성 Readiness, 실제 진행 기반 정체 관측·통합 운영 화면, 다른 데이터 종류의 보존/Janitor, 실행 중 취소·재시도·강제 해제의 물리적 안전성, 외부 저장소·실제 운영 복구 검증. |
| 5. Golden Image·실제 libvirt 실행 | **미완료.** [Worker 실행기](../../apps/worker/internal/daemon/executor.go)는 주어진 Base Image로 VM을 시작하는 초기 구현입니다. [Host 설치 스크립트](../../infra/host/install-ubuntu.sh)는 Image Builder가 아닙니다. | Version 고정 Desktop Image의 재현 가능한 생성·무결성/Boot 검사, Run Metadata·Timeout·Shutdown·재시작/Orphan 복구·정확한 자원 회수·작업별 네트워크·실제 시간 예산 강제. |
| 6. WireGuard Preview·Console 소유권 | **미완료.** [Gateway](../../apps/gateway/main.go)는 운영 인증이 없으면 503이며 Console 읽기 전용 값을 Header로 전달할 뿐입니다. | 실제 WireGuard Routing·Wildcard TLS·조직/작업/만료/대상 경계, noVNC 입력 필터와 에이전트 입력 중단·반환 Version·Timeout 복구. Header만으로 입력 차단을 증명하지 않습니다. |
| 7. 실제 Host 동시 2작업 Acceptance | **미완료.** 현재 Mock/HTTP/Chromium 회귀는 이 기준의 대체 증거가 아닙니다. | 서로 다른 저장소 2개를 동시에 Clone→분석→개발→Browser 검증→피드백→재검증→승인→전달→정리하고, Worker 재시작·네트워크 단절 후 복구와 Display/입력/프로필/네트워크/디스크/자격증명 격리를 증명해야 합니다. |

7단계는 상세 P0/P1 범위를 요약한 것이며 [보안 불변 조건](security.md)·승인 정책·실제 사용 Gate를 면제하지 않습니다. P2 전체, GitLab·고급 Routing·자율 탐색을 임의로 이번 MVP에 추가하거나, 반대로 남은 P0/P1 조건을 삭제하지 않습니다.

## 이미 구축한 개발 기반

- [작업 중심 UI/UX](dashboard-verification.md): 대시보드 탐색·검색·상태 필터·반응형·실패 복구와 이후 피드백·증거·예산 승인 개선. 실제 작업 2개의 KVM 실행 완료와는 별도입니다.
- [개발 지침](../../AGENTS.md): 개발 전 Branch, 목적별 한국어 Commit, 자동 테스트와 실제 사용, 증거 기반 PR·최신 SHA 검사·일반 Merge Commit·`main` 확인 절차가 있습니다. 운영 배포·유료 자원·보호 우회 권한은 없습니다.
- [GitHub CI](../../.github/workflows/ci.yml): API 2묶음, 후속 PostgreSQL/Runner Gate, Go, Web/Chromium을 실행합니다. Action SHA 고정·최소 권한·캐시·8분 Job 제한·동일 PR 이전 실행 취소·증거 보존을 유지합니다. [분할 정책](ci-partitions.md)
- [PR #53 CI](https://github.com/haesookimDev/dev-agent/actions/runs/34422241723)는 정확한 Head `9d9b35f`에서 첫 실행 4분 56초에 통과했습니다. API 503/663개와 별도 PostgreSQL 16/23/38/8/12/6/9개, Runner 45개, Web 125개·Chromium 41개와 Go/정적 검사입니다. API 단계의 PostgreSQL Skip은 후속 DB 검사로 검증합니다.

이 기반이 있다는 사실을 7개 제품 릴리즈 단계의 완료 수에 중복 가산하지 않습니다. 이번 작업의 실제 Worker/프로덕션 Web 검증과 네이티브 포커스 제한은 [진단 검증 기록](worker-private-diagnostics.md)에 구분해 적었습니다.

## 다음 진행 순서와 외부 의존성

2026-09-10 사용자의 Mac 로컬 개발 승인에 따라 **전용 Linux 머신 부재의 우회 경로를 실제로 검증했습니다**. [Mac KVM Lab](macos-kvm-lab.md)에서 M4 Pro/macOS 15.7.3 → Lima 2.2.0/Ubuntu ARM64 → 실제 KVM Linux 부팅·정상 종료(5.790초), KVM 활성, Timeout 후 프로세스 정리와 기존 증거 보존을 확인했습니다. 이 호스트 검증은 Golden Image/작업 VM의 완료가 아니므로 1/7을 유지합니다. ARM64 로컬 테스트 경로를 추가한 결정이며 기존 amd64 제품 검증을 면제한 결정은 아닙니다.

이번 로컬 전체 검증: 전용 PostgreSQL DB를 사용한 `make test`에서 API 1270 통과/1 Skip(systemd Parser는 Linux CI), Runner 45, Web 125, Go, Lab 18 통과. `make lint`, KO/EN 명령 일치·Shell 문법·문서 링크 검사도 통과했습니다. 새 Lab 검사는 기존 Python Job에서 실행하며 추가 VM 빌드나 CI Job은 없습니다. 최종 Head의 CI·리뷰·머지 상태는 해당 PR에 기록하고 확인합니다.

1. **P1 실행 경로를 우선합니다.** Golden Image 재현 → 영속 VM 수명주기·격리 → Preview/Console 강제 → 실제 두 작업·장애 복구 Acceptance의 의존 순서로 구현합니다. 실제 환경이 없어 검증하지 못한 기능 PR은 Draft로 유지합니다.
2. P0의 남은 Secret·관측·보존·관리자 제어는 위 실행 경로와 연결해 완료합니다. 단위 테스트가 쉬운 부수 개선만 반복해 P1 완료를 대신하지 않습니다.
3. 승인된 Mac 내부 ARM64 Linux/KVM 호스트를 확보했습니다. [Draft PR #55](https://github.com/haesookimDev/dev-agent/pull/55)의 Head `f96635c`는 amd64 입력 무결성·Builder·Guest/Boot 검사 경로이며 아직 실제 Golden Image Acceptance를 통과하지 않았습니다. ARM64 입력·Firmware·Browser·모든 생산자/소비자 계약을 함께 확장한 뒤 실제 Desktop 부팅을 검증합니다. 작업별 네트워크·TLS·Console 경계와 동시 2작업 Acceptance는 여전히 남아 있습니다. 기존 운영 Host나 주변 자격증명을 임의로 찾아 사용하지 않습니다.
4. [Draft PR #21](https://github.com/haesookimDev/dev-agent/pull/21)의 Preview 작업은 `main`에 포함되지 않았습니다. 오래된 Head의 CI는 현재 Migration·권한 계약과의 통합 검증을 대체하지 않습니다. 실제 TLS·브라우저/Console 검증 전에는 완료로 계산하거나 강제 병합하지 않습니다.

## 갱신 방법

각 후속 완료 보고에서 기준 SHA·PR·CI·실제 사용 증거와 남은 조건을 갱신합니다. 한 단계의 모든 조건이 입증됐을 때만 완료 수를 올립니다. 회귀가 확인되면 판정을 되돌리고 이유를 남깁니다. 분모/범위를 바꿔 수치를 높이지 않으며 범위 변경은 사용자 결정과 이전 기준의 차이를 기록합니다. 한국어·영어를 함께 갱신합니다.
