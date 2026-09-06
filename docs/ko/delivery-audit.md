# 승인에 연결된 전달 감사

한국어 | [English](../en/delivery-audit.md)

## 기록하는 것과 기록하지 않는 것

IAM-001의 중앙 GitHub 전달 감사입니다. 웹·Slack PR 승인을 예약할 때 `DeliveryJob.approval_audit_id`를 당시의 추가 전용 `approval.decided` 행에 연결합니다. 승인·상태·이벤트·감사·전달 예약은 같은 트랜잭션이며 하나라도 저장하지 못하면 Background Task를 시작하지 않습니다.

실행 주체는 사람의 세션이 아닌 `delivery:github` 서비스입니다. 따라서 서비스 행은 `transport=background`, `identity_provider=urn:kelpie:service`이고 Principal ID·Role·Source IP는 `null`입니다. 사람의 실제 신원, Provider, 당시 Role 결정, Request ID와 ASGI Peer IP는 참조된 승인 행에 그대로 남습니다. 사람의 권한을 서비스에 복사하거나 서비스가 그 사람의 세션으로 실행됐다고 표시하지 않습니다. 기존 웹·Slack 행의 필수 Role 제약은 유지합니다.

전달 시작, Token 발급·Push·PR 생성 직전의 잠금 구간, 완료 트랜잭션에서 승인 출처의 조직·저장소·작업·Correlation ID, PR 승인 결정, Approver 이상 Role, 예약 여부, 승인 후 작업 버전과 Bundle 메타데이터 Hash를 확인합니다. 누락·불일치는 외부 쓰기 전에 차단합니다. 당시 승인에 대한 위임이므로 이후 Membership 변경을 소급 적용하는 새 정책은 아닙니다. Worker 격리의 후속 쓰기 차단은 유지됩니다.

Hash는 **승인된 Bundle 메타데이터의 식별자**입니다. 후속 [실제 바이트 검증](delivery-integrity.md)은 승인·다운로드와 Token 발급 전 파일을 다시 Hash하고 고정 사본을 Git에 적용합니다. Git 원격 Tree의 Artifact Attestation은 추가하지 않습니다. 외부 PR 생성과 DB 커밋은 분산 트랜잭션이 아니며, 실패가 외부 쓰기 부재를 뜻하지도 않습니다.

| `action` | 의미 |
| --- | --- |
| `delivery.started` | 검증된 승인으로 새 시도를 시작함. 외부 호출 전에 저장 |
| `delivery.completed` | 전달 결과·작업 완료·Job 완료와 함께 저장 |
| `delivery.failed` | 승인 출처 거부 또는 전달 오류. 실패 상태·이벤트와 함께 저장 |
| `delivery.stopped` | 시작된 시도가 격리 등으로 중단된 당시 상태. 격리·자원 상태를 덮어쓰지 않음 |
| `delivery.interrupted` | 시작 시 복구가 `running` Job을 발견함. 재시도 전의 불확실한 외부 결과 |

시도마다 새 UUID Request ID를 만들고 시작·종료 행이 공유합니다. 복구 중단 기록은 별도 Request ID와 이전 시도 번호를 가집니다. `details`에는 검증된 승인 행 ID, 승인 Hash·버전, 시도 번호, Worker ID, 현재 작업/Job 상태·버전, 제한된 단계·오류 코드·전달 경로, 검증 가능한 PR 번호만 기록합니다. 승인 출처가 확인되지 않으면 참조·Hash는 `null`이고 `authorization`은 `denied` 또는 `unavailable`입니다. `publication`의 `new_branch`, `existing_branch`, `existing_pull_request`는 실행 경로이지 쓰기 성공 증명이 아닙니다. 복구 중에는 `unknown`일 수 있습니다. 토큰·Patch 경로·임의 URL·Upstream 오류 원문을 감사에 복사하지 않습니다. DB 오류도 Trace에 원문을 내보내지 않습니다.

## API와 호환성

기존 승인 요청 `POST /api/work-items/{id}/approvals`의 `{"kind":"pull_request","decision":"approve"}`와 WorkItem 응답 형식은 유지합니다. 응답의 `committing`은 승인 접수이며 최종 전달 완료가 아닙니다. 최종 상태는 기존 조회·SSE로 확인합니다.

Administrator의 `GET /api/work-items/{id}/audit-log`는 기존 조직·저장소 격리, 커서·Limit, `Cache-Control: no-store`를 유지합니다. 새 Background 행의 주요 응답 예시는 다음과 같습니다. 값은 합성 예시입니다.

```json
{
  "action": "delivery.completed", "actor_id": null,
  "actor_subject": "delivery:github", "identity_provider": "urn:kelpie:service",
  "transport": "background", "source_ip": null,
  "organization_role": null, "repository_role": null,
  "effective_role": null, "required_role": null,
  "details": {
    "approval_audit_id": 1, "authorization": "verified",
    "approved_work_version": 11, "attempt": 1,
    "work_status": "completed", "work_version": 13, "job_state": "completed",
    "stage": "finalize", "error_code": null,
    "publication": "new_branch", "pull_request_number": 42
  }
}
```

새 Transport와 Nullable Role을 처리하지 못하는 외부 감사 Client는 배포 전에 갱신해야 합니다. 기존 사람 행의 Role 값은 바뀌지 않습니다. 저장소 내 감사 응답 소비자는 API Schema·테스트뿐이며 Web·Worker·Gateway·Runner에 동일 타입의 별도 계약은 없습니다. 새 Endpoint·환경변수·의존성·감사 조회 UI는 추가하지 않습니다.

## Migration과 복구

새 승인을 멈추고 기존 전달을 정리한 뒤 API Writer를 중지하고 백업합니다. API 재시작 전에 `make migrate-api`와 `.venv/bin/python -m alembic -c apps/api/alembic.ini check`로 `20260906_0009`를 적용합니다. `0008`은 미병합 Preview PR #21이 사용하는 번호이므로 현재 Main 체인은 `0007 → 0009`입니다. Preview PR을 통합할 때 부모 Revision을 새 Main에 맞춰 조정하고 재검증해야 합니다.

PostgreSQL은 두 테이블에 배타 잠금을 잡습니다. SQLite는 쓰기 잠금을 먼저 잡고 Batch 재생성 후 추가 전용 Trigger를 다시 설치합니다. 기존 감사와 Job을 보존하며 예전 Job의 새 참조는 `null`입니다. 과거 승인을 추측해 소급 연결하지 않습니다. 남겨진 구형 Pending/Running Job은 재개 시 `approval_unavailable`로 실패하므로 새 작업의 검증·명시적 승인으로 다시 요청해야 합니다. DB를 수동 조작해 승인 링크를 만들지 않습니다. [Alembic Batch 동작](https://alembic.sqlalchemy.org/en/latest/batch.html)

Online Downgrade는 감사 테이블이 비어 있고 승인 참조가 하나도 없을 때만 잠금 아래 허용합니다. 보존 행·참조가 있거나 Offline이면 거부하고 데이터·Revision을 유지합니다. 삭제·Guard 해제·수동 Stamp로 우회하지 않습니다. 이전 Binary는 새 Schema의 Readiness를 통과하지 못하므로 보존 데이터가 있는 배포는 Schema를 유지한 전진 수정으로 복구합니다.

시작 감사 저장 실패는 Pending/이전 시도 수로 Rollback하고 Token을 발급하지 않습니다. 완료 감사 저장 실패는 완료 상태를 Rollback하고 `running`을 남깁니다. DB 복구 후 API 재시작의 기존 복구 절차가 `interrupted`를 기록하고 새 시도에서 기존 PR/Branch를 조회합니다. 실행 중 항상 재검색하는 Queue Worker나 일반 실패 Job의 자동 재시도 UI는 아닙니다. Job/작업 자체가 삭제된 경우 중단 감사는 새로 만들지 못합니다. 실제 VM 정리·Lease 반환 보장은 이 감사 변경에 포함되지 않습니다.

## 검증 · 2026-09-06

실행 코드 `3c3797e`, 재현 Fixture·테스트 `e5942a4` 기준입니다. 뒤의 문서·이미지는 실행 코드를 바꾸지 않습니다.

- PostgreSQL URL을 지정한 `make test`: API 502개, Runner 6개, Worker·Gateway, Web 52개·TypeScript 통과. `make lint`와 운영 Web Build 통과. PostgreSQL 17 Upgrade/Check/빈 Downgrade/Re-upgrade, 보존 행 Migration과 비어 있지 않은 Downgrade 거부, Background/사람 행의 제약·추가 전용 Guard를 검사했습니다.
- 신규 DB 오류 Trace 회귀 테스트는 수정 전 2개 실패 → 수정 후 통과했습니다. 전달·오류 비노출 47개는 출처 위조·누락, 버전 변경, 감사 INSERT 실패 Rollback, 격리, 동시 중복 시도, PR 생성 후 완료 감사 실패·재시작 시 중복 쓰기 방지를 포함합니다.
- 웹·서명된 Slack 통합 테스트는 저장된 OIDC Principal 승인이 실제 전달 코드로 이어지고, Approver의 감사 조회 403·다른 조직 Admin의 404·해당 조직 Admin의 200을 확인합니다. 외부 IdP·Slack 서비스를 호출한 검증은 아닙니다.
- `test_delivery_http_runtime.py`는 새 SQLite Migration, 실제 Uvicorn HTTP·Git clone/apply/commit/push, 루프백 SCM HTTP 서버를 실행합니다. 생성된 로컬 원격 Branch의 파일과 Commit 수, PR 생성 1회, 성공·실패 감사, 중복 승인 409와 감사 불변을 확인합니다. 외부 GitHub 호출은 차단한 시험 환경이며 실제 GitHub App 설치·KVM 검증으로 표현하지 않습니다.
- 같은 Fixture의 API `:18460`와 운영 Web `:13460`을 띄워 Orca 브라우저에서 한국어 승인 → 완료/100%/PR 링크, 영어 390px 승인 → 실패/오류 단계/피드백 마감을 직접 확인했습니다. 성공·실패 각각 감사 3행, 사람/서비스 Request ID 분리, 승인 참조·Hash·버전, `no-store`, 중복 승인 409 후 불변, 로컬 PR 쓰기 1회를 확인했습니다. KO/EN의 1035px·390px 상태/언어 전환과 가로 넘침 없음, 콘솔 오류 없음, 예상한 중복 승인 409 외 네트워크 오류 없음을 확인했습니다.
- Computer-use로 Orca 데스크톱 창의 실제 좁은 화면을 확인했습니다. OS 포커스는 제공되지 않았고 브라우저 Tab 입력도 포커스 이동을 증명하지 못해 수동 키보드 검증 완료로 표시하지 않습니다. 이번 변경은 API 감사이며 UI·네이티브 입력을 수정하지 않습니다. 모바일 캡처의 반복 렌더링 이미지는 증거에서 제외하고 아래 검토한 데스크톱 캡처만 첨부합니다.

![한국어 전달 완료](../assets/delivery-audit/ko-completed.png)
![영어 전달 실패](../assets/delivery-audit/en-failed.png)

기존 필수 CI `Python`의 전체 테스트와 PostgreSQL Guard 검사가 새 테스트·Migration을 자동 실행합니다. 별도 Job·Matrix나 중복 빌드를 추가하지 않아 기존 언어별 병렬 실행·캐시·8분 제한을 유지합니다. 최종 Head의 CI·브라우저 E2E 결과와 Merge SHA는 PR에 기록합니다.

남은 MVP는 실행 중 관리자 취소·VM 정리, 재시도·보존/백업·복구, OIDC Preview Grant, 외부 의존성/Worker/Lease 운영 검사, 전체 Secret 비노출, 실제 KVM·WireGuard·noVNC 입력 격리입니다. 승인된 Linux/KVM Host가 아직 없으며 PR #21의 직접 TLS 브라우저 검증도 별도 미완료입니다. 전체 MVP를 완료 처리하지 않습니다.
