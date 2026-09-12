# MVP 진행 현황 — 2026-09-11

한국어 | [English](../en/mvp-progress.md) · [개발 요약](roadmap-summary.md) · [상세 기준](roadmap-detailed.md)

## 기준과 진행률

제품 기준 코드: `main`의 `95aac9d81d4ea5ed7ffb083995dd56ead157c3ac` ([PR #59](https://github.com/haesookimDev/dev-agent/pull/59) 병합). 임대 복구 API의 검증 소스는 `f5d9da32c6500c413c4340d914997cda53b4e479`입니다. 미병합 Worker 수명주기 PR #58의 후속 코드 `b9fdd484f4d7777345e89694c0c2a00b42f87816`에서 종료 임대의 실제 VM·새 Worker 프로세스·API·PostgreSQL 복구까지 검증했습니다. 전체 Executor와 실행 중 작업 복구는 남아 있습니다. 아래 평가는 코드·테스트·실제 구동 기록을 대조한 시점별 기록이며 Draft 브랜치나 계획은 완료로 세지 않습니다.

[바로 다음 Release](roadmap-summary.md#바로-다음-release)에 명시된 7단계를 고정된 분모로 사용합니다. **검증 완료 1/7 = 14.3%**, 부분 완료 3/7, 미완료 3/7입니다. 부분 구현에는 임의 점수를 주지 않습니다. 이 수치는 릴리즈 단계의 검증 완료율이며 코드 작성량·투입 공수·남은 일정의 비율이 아닙니다. 전체 개발 공수의 정확한 완료율은 현재 근거로 산정하지 않습니다.

## 완료 기준별 현황

| 단계 | 현재 판정과 확인한 근거 | 완료까지 남은 내용 |
| --- | --- | --- |
| 1. Versioned DB Migration | **완료.** Alembic Head `20260909_0011`, PostgreSQL Migration Lock, 기본 `validate`, 빈 DB·기존 데이터 채택·안전한 Downgrade·실패 Rollback·실제 복원 회귀를 확인했습니다. [운영](operations.md#database-migration), [Migration 테스트](../../apps/api/tests/test_migrations.py), [원자성 회귀](../../apps/api/tests/test_migration_atomicity.py) | 이후 Schema 변경마다 동일 Gate를 다시 통과해야 합니다. 임의 운영 DB 전환까지 위임받은 것은 아닙니다. |
| 2. OIDC·조직/저장소 권한·감사 | **부분 완료.** OIDC/RBAC, 추가 전용 피드백·승인·대기 취소·전달 감사가 있습니다. [운영](operations.md#oidc-인증), [IAM 테스트](../../apps/api/tests/test_iam.py), [감사](control-action-audit.md) | OIDC Preview Grant의 통합·실제 TLS 경계 검증, 실행 중 관리자 취소와 VM 종료·정리 보장. |
| 3. Worker Secret·격리 | **부분 완료.** 파일 Provider, 개별 발급·중첩 교체·폐기, 제어 영역 격리, Runner/API/Worker 진단 보호를 검증했습니다. [자격증명](worker-credentials.md), [격리](worker-quarantine.md), [최신 진단 검증](worker-private-diagnostics.md) | 실제 Host/VM/네트워크와 기존 연결 격리, Event/Artifact/Crash Dump/cloud-init 전체 비노출·보존 정책. 제한된 가림을 범용 Secret Scan으로 계산하지 않습니다. |
| 4. 관측·복구·보존 운영 기반 | **부분 완료.** Correlation·Metric·DB 준비 검사·장애 알림, 실제 PostgreSQL 복원, 일반 산출물 백업·예약 정리가 있습니다. 병합된 [종료 임대 복구 API](worker-lease-reconciliation.md)는 실제 DB·HTTP 경합을, 미병합 [Worker 재시작 연동](worker-restart-recovery.md)은 실제 VM 정리·단일 반환/감사를 검증했습니다. [관측](execution-monitoring.md), [복원](postgres-restore.md), [예약 정리](scheduled-retention.md) | 실행 중 작업·Claim 유실 복구, 외부 의존성 Readiness, 진행 기반 정체 관측·운영 화면, 다른 데이터의 보존/Janitor, 실행 중 취소·재시도·강제 해제의 물리적 안전성, 외부 저장소·운영 복구 검증. |
| 5. Golden Image·실제 libvirt 실행 | **미완료.** Draft 이미지 PR #55에 ARM64 실제 생성·두 번 부팅 증거가 있습니다. 미병합 [Worker 수명주기](worker-lifecycle.md)는 영속 소유권·취소 정리·ACL·실제 빈 VM 두 사례와 종료 임대의 실제 새 Worker/API 복구를 검증했습니다. | amd64 실제 이미지 검증과 남은 GUI Gate, 전체 실행기의 Golden Image·Runner 통합·정상 OS 종료, 실행 중 작업·완전한 Orphan 복구, 작업별 네트워크·실제 시간 예산 강제. |
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

이전 Host 단계의 전용 PostgreSQL 전체 검증은 API 1270 통과/1 Skip이었습니다. API PR #59 최종 코드의 `make test`는 API 1195 통과/152 Skip, Runner 45, Web 125·타입 검사, Go, Lab 18 통과였으며 `make lint`도 통과했습니다. 새 복구 검사는 일반 API 실행에서 DB 환경 미설정으로 Skip되지만 별도 실제 PostgreSQL에서 46/46, 기존 Worker 검사와 함께 실행한 CI 명령에서 62/62 통과했습니다. [API 증거·한계](worker-lease-reconciliation.md)를 참조합니다. 기존 Python Job에 연결하며 새 Job·VM 빌드는 없습니다. PR #59는 정확한 Head `e3521e1b78369da0a7bb58f609bc73b67f5b88ba`의 [CI 5개](https://github.com/haesookimDev/dev-agent/actions/runs/34563221182)가 첫 실행 5분 47초에 통과했고 미해결 리뷰 없이 일반 Merge Commit으로 병합했습니다. Worker 후속 통합의 증거와는 구분합니다.

Worker 후속 코드의 `make test`는 API 1195 통과/153 Skip(155.48초), Runner 45, Web 125·타입 검사, Worker/Gateway, Lab 18 통과이며 `make lint`와 집중 Race 검사도 통과했습니다. 새 실제 VM/API/PostgreSQL Opt-in 검사는 별도로 1개/22.88초 통과했습니다. 실제 Worker 바이너리의 Commit과 `vcs.modified=false`, 단일 반환 이벤트·감사, 정리된 임시 자원은 [재시작 증거](worker-restart-recovery.md)에 기록했습니다. 전체 Executor SIGKILL·OS/Runner·GUI·동시 두 작업의 증거는 아니며 PR #58의 최종 Head CI와 Draft 상태는 PR에 별도 기록합니다.

1. **P1 실행 경로를 우선합니다.** Golden Image 재현 → 영속 VM 수명주기·격리 → Preview/Console 강제 → 실제 두 작업·장애 복구 Acceptance의 의존 순서로 구현합니다. 실제 환경이 없어 검증하지 못한 기능 PR은 Draft로 유지합니다.
2. P0의 남은 Secret·관측·보존·관리자 제어는 위 실행 경로와 연결해 완료합니다. 단위 테스트가 쉬운 부수 개선만 반복해 P1 완료를 대신하지 않습니다.
3. [Draft PR #55](https://github.com/haesookimDev/dev-agent/pull/55), Head `b865954101a4becd37d23f4aadf39a9d9795585f`: 실제 ARM64 Desktop Image 생성, 두 번의 표준 부팅에서 각 8/8 검사, 기본 Image 불변, 실제 화면·키보드 입력 증거가 있습니다. [고정 SHA 증거](https://github.com/haesookimDev/dev-agent/blob/b865954101a4becd37d23f4aadf39a9d9795585f/docs/ko/golden-image-inputs.md), [CI 5개 통과](https://github.com/haesookimDev/dev-agent/actions/runs/34556143436)(첫 실행 7분 43초). 최종 후보의 전체 GUI 여정·amd64 실제 검증·Worker 통합은 미완료라 Draft를 유지합니다.
4. [Draft PR #58](https://github.com/haesookimDev/dev-agent/pull/58)의 이전 Head `2780f4d3d3f9ba6dc6647293c61fef64aff88403`: 영속 Run 기록·정리 전 자원 반환 금지, 실제 빈 ARM KVM VM의 종료/취소 두 사례에서 종료→정의 해제→파일 제거→모의 API 확인을 검증했습니다. [고정 SHA 증거](https://github.com/haesookimDev/dev-agent/blob/2780f4d3d3f9ba6dc6647293c61fef64aff88403/docs/ko/worker-lifecycle.md), [이전 CI 5개 통과](https://github.com/haesookimDev/dev-agent/actions/runs/34560620314)(첫 실행 약 5분 20초). 이번 후속에는 Schema 2 임대 연결과 실제 새 Worker/VM/API 복구가 추가됐습니다. 실제 Golden Image/Runner를 통한 전체 실행, 전용 ARM Firmware·작업별 네트워크·시간 예산은 남아 있습니다. 빈 VM의 순차 검사를 동시 두 작업 Acceptance로 계산하지 않습니다.
5. 다음은 전체 Executor의 Golden Image/Runner 경로와 소유 ARM Firmware·작업별 네트워크·시간 제한을 연결하는 단계입니다. 실행 중 작업의 복구 정책과 Claim 응답 유실도 별도 검증합니다. [Draft PR #21](https://github.com/haesookimDev/dev-agent/pull/21)의 Preview는 아직 `main`에 없습니다. 작업별 네트워크·TLS·Console·동시 두 작업이 입증되기 전에는 완료로 계산하거나 강제 병합하지 않습니다. 기존 운영 Host나 주변 자격증명을 임의로 찾아 사용하지 않습니다.

## 갱신 방법

각 후속 완료 보고에서 기준 SHA·PR·CI·실제 사용 증거와 남은 조건을 갱신합니다. 한 단계의 모든 조건이 입증됐을 때만 완료 수를 올립니다. 회귀가 확인되면 판정을 되돌리고 이유를 남깁니다. 분모/범위를 바꿔 수치를 높이지 않으며 범위 변경은 사용자 결정과 이전 기준의 차이를 기록합니다. 한국어·영어를 함께 갱신합니다.
