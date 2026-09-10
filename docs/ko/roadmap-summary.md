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
2. **부분 완료:** OIDC 인증과 조직·저장소 권한, [피드백](feedback-audit.md), [Console·승인](control-action-audit.md), [미배정 대기 작업 취소](work-cancellation.md), [승인에 연결된 전달](delivery-audit.md)의 추가 전용 감사를 추가했습니다. 실행 중 관리자 취소와 OIDC Preview Grant는 남아 있습니다.
3. **부분 완료:** 환경변수·파일 Secret Provider, Worker별 발급·중첩 교체·개별 폐기·재읽기, [제어 영역 격리](worker-quarantine.md)를 구현했습니다. 자격증명·활성 Lease·사용자 조작·새 Preview 해석·후속 전송은 함께 차단됩니다. 실제 Host/VM/네트워크·기존 연결 격리와 전체 Secret 비노출 검증은 남아 있습니다. [자격증명 운영](worker-credentials.md)
4. **부분 완료:** OpenTelemetry Trace, Prometheus Metric, 구조화된 Correlation ID, [DB 준비 검사 제한](readiness-verification.md), [DB 복구 후 시작 시 전달 재개](delivery-recovery.md), [복구 상태 지표](delivery-recovery-metrics.md), [기본 장애 알림](monitoring-alerts.md)과 [Worker·Lease·Queued 작업의 지속 관측](runtime-monitoring.md)을 추가했습니다. [실행 단계·DeliveryJob 메타데이터 경과 시간 관측·정체 알림](execution-monitoring.md)도 추가했습니다(2026-09-09). 외부 의존성 Readiness, 실제 진행 기반 지연 지표, 통합 운영 Dashboard와 일반 파일 외의 보존 정책·예약 정리는 남아 있습니다.
5. Version이 고정된 Ubuntu Desktop Golden Image 하나를 만들고 실제 libvirt 실행을 완료합니다.
6. WireGuard Preview Routing을 구축하고 Gateway 경계에서 noVNC 읽기 전용·입력 소유권을 강제합니다.
7. 브라우저를 사용하는 작업 2개를 동시에 실행해 Display, 입력, Network, Disk, 자격증명이 서로 격리됨을 증명합니다.

P0 OPS-001에는 [PostgreSQL 백업·새 DB 복원의 데이터·권한·감사 검증](postgres-restore.md)과 [일반 산출물의 활성 작업 보호 정리 CLI·만료 UI·백업 V2](artifact-retention.md)도 추가했습니다. Object Store 복구, 다른 데이터 종류의 보존 정책·예약 Janitor와 실제 운영 복구 검증은 남아 있습니다.

[일반 산출물 예약 Worker](scheduled-retention.md)는 제한된 페이지 진행·재시작 복구·동시 갱신 보호와 명시적 Linux 서비스 템플릿을 추가합니다(2026-09-09). API/파일의 실제 만료와 보호된 작업의 유지, PostgreSQL 경합, 양 언어 브라우저 회귀를 검증합니다. 서비스는 운영에 자동 설치하지 않으며 다른 데이터 종류와 전체 OPS-001 완료를 뜻하지 않습니다.

SEC-001의 [Runner 전송](runner-event-redaction.md)과 [API 이벤트 수신](api-event-redaction.md) 경계의 임대·명시적 자격증명 필드 가림을 실제 명령·직접 HTTP·SSE·DB·브라우저로 검증했습니다(2026-09-09). 범용 Secret Scan, 모든 API·Artifact·Crash Dump·cloud-init 전체 검증은 여전히 남아 있습니다.

[Worker 오류 진단](worker-private-diagnostics.md)은 HTTP 원문·명령 출력·임의 실행 오류의 로그/이벤트 노출을 제한합니다(2026-09-10). 실제 Worker 프로세스와 API 저장·임대 해제·양 언어 표시를 검증했으며 실제 VM·범용 Secret Scan 완료는 아닙니다.

공통 상태 전환이 생성하는 이력의 [실제 `from`·`to` 보존](transition-metadata.md)과 [Worker 임대의 사용자 승인 상태 전환 차단](worker-transition-authority.md)을 검증했습니다. 후자는 사용자 피드백·예산/PR 승인과 승인된 Mock 완료를 구분하며 중앙 전달은 기존 Control Plane 권한을 유지합니다. 임의 이벤트의 출처 증명과 신뢰할 수 없는 VM의 실제 실행·시간 예산 강제는 남아 있습니다.

GitLab, 고급 Routing, 자율 이슈 탐색은 이 보안·실행 기준이 검증된 이후에 진행해야 합니다.

[사용자 결정 이후 Runner 재개](runner-resumption.md)는 예산 승인 전 피드백 실행을 차단하고 피드백 없는 예산 승인·PR 거절 후 재검증을 실제 프로세스·HTTP로 확인했습니다. 후속 [예산 연장 UI](budget-approval-ui.md)는 추가 시간·총예산 확인과 안전한 재검토를 제공합니다. 명령 조회 이후 실행 경쟁과 실제 VM의 시간 예산 강제는 남아 있습니다.

[예산 승인 버전 검사](budget-approval-version.md)는 승인·거절에 사용자가 확인한 `expected_version`을 요구하며 재소진 이후 오래된 확인의 재사용을 차단합니다. 실제 HTTP 및 PostgreSQL 동시 요청·Rollback을 검증했습니다. Web 연장 UI는 이 계약을 사용하며 외부 예산 호출부도 호환성 전환이 필요합니다.
