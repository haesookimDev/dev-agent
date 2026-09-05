# 다음 개발 요약

한국어 | [English](../en/roadmap-summary.md) · [상세 계획](roadmap-detailed.md)

## 목표

현재 배포 가능한 버티컬 슬라이스를 여러 Worker에서 운영할 수 있는 자율 개발 플랫폼으로 발전시킵니다. 작업 에이전트에 저장소 또는 Host 권한을 주지 않으면서 개선점을 탐색하고 구현하고 검증하고 전달하는 전체 과정을 안전하게 자동화하는 것이 목표입니다.

## 권장 개발 순서

| 우선순위 | 마일스톤 | 결과 | Release 조건 |
| --- | --- | --- | --- |
| P0 | 운영 기반 | Migration, OIDC/RBAC, Secret, 감사, Metric, 보존 정책 | 운영 환경에서 개발 인증과 암묵적 Schema 생성 제거 |
| P1 | 실제 KVM 실행 | 재현 가능한 Golden Image, VM Network, WireGuard Preview, Console 소유권 강제 | 한 Host에서 실제 저장소의 격리된 작업 2개 동시 완료 |
| P2 | 검증 및 증거 | 정책 기반 Test, Browser/Computer-use 증거, Artifact 저장, 평가 Gate | PR 승인마다 재현 가능한 명령·UI 증거 제공 |
| P3 | Provider 및 Messenger Adapter | GitLab 동등 기능, GitHub Checks, Slack/Messenger 결과·피드백 | 동일 작업 생명주기를 두 SCM Provider에서 실행 |
| P4 | Scheduling 및 Orchestration | 자원·시간 예측, 공정 Routing, Sub-agent DAG, Checkpoint 복구 | 다중 Worker 부하·장애 테스트가 Scheduling SLO 충족 |
| P5 | 자율 탐색 | 보안·Dependency·Bug·품질 탐색과 중복 제거 | 에이전트는 이슈를 제안할 수 있지만 자기 라벨·승인·전달은 불가능 |
| P6 | 확장 및 운영 | 고가용성, Quota, 비용 계산, Backup/Restore, Upgrade 전략 | 재해 복구 및 Tenant 격리 테스트 통과 |

## 바로 다음 Release

다음 Release는 P0와 P1의 최소 End-to-End 경로만 포함하는 것을 권장합니다.

1. **완료:** `create_all`을 Alembic으로 교체하고 Upgrade/Downgrade를 테스트합니다.
2. **부분 완료:** OIDC 인증과 조직·저장소 권한, [피드백](feedback-audit.md), [Console·승인](control-action-audit.md), [미배정 대기 작업 취소](work-cancellation.md)의 추가 전용 감사를 추가했습니다. 실행 중 관리자 취소·전달 감사와 OIDC Preview Grant는 남아 있습니다.
3. **부분 완료:** 환경변수·파일 Secret Provider, Worker별 발급·중첩 교체·개별 폐기·재읽기, [제어 영역 격리](worker-quarantine.md)를 구현했습니다. 자격증명·활성 Lease·사용자 조작·새 Preview 해석·후속 전송은 함께 차단됩니다. 실제 Host/VM/네트워크·기존 연결 격리와 전체 Secret 비노출 검증은 남아 있습니다. [자격증명 운영](worker-credentials.md)
4. **부분 완료:** OpenTelemetry Trace, Prometheus Metric, 구조화된 Correlation ID, [DB 준비 검사 제한](readiness-verification.md), [DB 복구 후 시작 시 전달 재개](delivery-recovery.md), [복구 상태 지표](delivery-recovery-metrics.md)와 [기본 장애 알림](monitoring-alerts.md)을 추가했습니다. 외부 의존성 Readiness·Worker/Lease 알림, 지속적인 Queue 건강 상태와 보존 기간 정리 작업은 남아 있습니다.
5. Version이 고정된 Ubuntu Desktop Golden Image 하나를 만들고 실제 libvirt 실행을 완료합니다.
6. WireGuard Preview Routing을 구축하고 Gateway 경계에서 noVNC 읽기 전용·입력 소유권을 강제합니다.
7. 브라우저를 사용하는 작업 2개를 동시에 실행해 Display, 입력, Network, Disk, 자격증명이 서로 격리됨을 증명합니다.

GitLab, 고급 Routing, 자율 이슈 탐색은 이 보안·실행 기준이 검증된 이후에 진행해야 합니다.
