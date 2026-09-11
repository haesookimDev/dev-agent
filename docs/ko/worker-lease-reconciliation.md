# 종료된 Worker 임대의 복구 확인

한국어 | [English](../en/worker-lease-reconciliation.md) · [자격증명](worker-credentials.md) · [진행 현황](mvp-progress.md)

## 범위와 신뢰 경계

API는 Worker 재시작으로 작업별 Lease Token을 잃었거나 반환 응답을 받지 못했을 때, **이미 종료된 작업**의 임대를 개별 Worker 자격증명으로 조회·확인할 수 있습니다. 이 변경만으로 Worker의 자동 재시작 복구나 VM 정리가 구현된 것은 아닙니다.

- `lease_id`는 Claim 시 저장한 `ResourceLease.id`이며 공개 식별자이지 인증 수단이 아닙니다. Worker는 `LeaseID`와 `LeaseToken`을 별도로 보관합니다. VM/Runner에 Worker 자격증명을 전달하지 않습니다.
- 등록한 Worker ID, 임대 소유 Worker, 작업 ID·할당 Worker, 현재 작업 Version이 모두 일치해야 합니다. `completed`·`failed`·`cancelled`만 허용하며 작업 상태나 Version을 바꾸지 않습니다.
- 임대 상태는 `active` 또는 `released`여야 합니다. 관련 Delivery Job은 없거나 `completed`·`failed`여야 합니다. 실행 중·격리·알 수 없는 상태는 허용하지 않습니다. 승인·전달 권한을 부여하지 않습니다.
- `cleanup_confirmed: true`는 신뢰된 Worker가 물리 정리를 끝냈다는 **선언**입니다. API는 원격 디스크나 VM 종료를 독립적으로 증명하지 않습니다. 호출자는 영속 소유권, 정확한 Domain 부재, 다른 VM의 디스크 참조 부재, 파일 정리를 먼저 확인해야 합니다. 정리가 불확실하면 호출하지 않고 자원 예약을 유지합니다.
- Lease 만료는 필수가 아닙니다. 재시작은 만료 전에도 발생하며, 만료 그 자체도 VM 종료의 증거가 아니기 때문입니다. 만료 시각을 연장하거나 작업 Token을 재발급하지 않습니다.
- 폐기·만료된 Worker 자격증명과 격리된 Worker는 거부합니다. 개발용 공유 Token도 이 경로에서는 사용할 수 없습니다. 새 환경변수나 인증 완화는 없습니다.

후속 Worker 연동은 API가 발급한 임대 UUID를 VM Run UUID로 사용하고, VM 생성 전에 작업 ID·자원과 함께 영속화해야 합니다. 현재 [Draft PR #58](https://github.com/haesookimDev/dev-agent/pull/58)의 독립 Run UUID 기록을 이 계약에 자동으로 맞는다고 가정하지 않습니다. 기존·불명확한 기록의 자동 채택, 실행 중 작업의 종료 정책, Claim 응답 자체를 잃은 경우의 복구는 별도 작업입니다.

## API 계약

`POST /api/workers/{worker_id}/claim` 응답에 `lease_id`가 추가됩니다. 기존 `work_item`, `lease_token`, `lease_expires_at`은 유지하며 빈 Claim은 계속 `null`입니다. 다음 예제의 UUID와 자격증명 표시는 설명용입니다.

```http
GET /api/workers/33333333-3333-4333-8333-333333333333/leases/11111111-1111-4111-8111-111111111111
Authorization: Bearer <individual-worker-credential>
```

```json
{
  "lease_id": "11111111-1111-4111-8111-111111111111",
  "worker_id": "33333333-3333-4333-8333-333333333333",
  "work_item_id": "22222222-2222-4222-8222-222222222222",
  "state": "active",
  "work_status": "failed",
  "work_version": 3,
  "cpu": 2,
  "memory_mb": 4096,
  "disk_gb": 30
}
```

조회는 Token·해시·사용자 요구사항·저장소 내용을 반환하지 않습니다. 조회한 ID·자원·Version을 영속 기록과 대조하고 실제 정리를 끝낸 뒤에만 다음 요청을 보냅니다.

```http
POST /api/workers/33333333-3333-4333-8333-333333333333/leases/11111111-1111-4111-8111-111111111111/reconcile
Authorization: Bearer <individual-worker-credential>
Content-Type: application/json

{"work_item_id":"22222222-2222-4222-8222-222222222222","expected_version":3,"cleanup_confirmed":true}
```

성공은 본문 없는 `204`입니다. 응답 유실 시 동일 요청을 재시도할 수 있습니다. `expected_version`은 정수, `cleanup_confirmed`는 JSON Boolean `true`여야 하며 문자열·숫자 대체, 누락, 추가 필드는 `422`입니다. 인증 실패 `401`, Worker 범위/개별 인증 위반 `403`, 소유 임대 없음 `404`, Version·할당·상태 불일치 `409`를 반환합니다. `409`를 무시하고 로컬 자원을 가용으로 표시하지 않습니다.

## 원자성과 감사

인증의 Worker→Credential 잠금 뒤 Lease→Work→Delivery Job 순서로 잠급니다. 기존 반환·Claim·Heartbeat·자격증명 폐기·격리와 같은 Worker 잠금을 공유합니다. 성공한 트랜잭션만 예약을 반환하며 중복 요청은 자원·이벤트를 중복 가산하지 않습니다.

최초 복구 확인은 추가 전용 `lease.reconciled` 감사에 Worker 신원, 작업·임대 ID, Correlation/Request ID, 작업 상태·Version, 반환 자원, 정리 선언을 기록합니다. 기계 작업이므로 `transport=background`이고 사람 Role·Actor ID·Source IP는 비워 둡니다. 기존 반환이 이미 성공했다면 자원을 다시 가산하지 않고 `lease_state_before=released`로 선언만 한 번 기록합니다. 이미 감사된 임대가 다시 `active`인 모순도 거부합니다. 자원 갱신과 감사 기록은 함께 Commit/Rollback됩니다.

DB Schema·Migration Head(`20260909_0011`)는 바뀌지 않으며 새 의존성도 없습니다. 기존 작업 Token 기반 반환은 유지됩니다. Rollback 시 이 API를 사용하는 Worker를 먼저 Drain/중지하고 API 코드를 되돌립니다. 이미 반환한 임대를 다시 활성화하거나 감사 기록을 삭제하지 않습니다. DB Downgrade는 필요하지 않습니다.

## 검증 증거 — 2026-09-11

구현 기준: `f5d9da32c6500c413c4340d914997cda53b4e479` (`aff410b7781740736f6e8970b6306959c5a2c4c4`의 Claim ID 계약 포함).

[수정하지 않은 PostgreSQL 검증 로그](../assets/worker-lease-reconciliation/postgres.log)의 SHA-256은 `86e9fc9a884662a0149ec47a23072427188a7cfefbc42e98271f1ad98a34d348`입니다.

- [SQLite API 명세](../../apps/api/tests/test_worker_lease_reconciliation.py): 27개 통과. 미구현 경로의 `404` 실패를 먼저 확인했습니다.
- [PostgreSQL 실제 경합·HTTP 검사](../../apps/api/tests/test_worker_lease_reconciliation_postgres.py): Mac 로컬 Docker의 PostgreSQL 17에서 46개 모두 통과(12.19초). 독립 임시 DB·계정과 UUID Schema만 사용했고 검사 후 제거했습니다. 기존 개발 데이터와 감사 Guard를 변경하지 않았습니다.
- 동일 소유 임대 중복 요청, 다른 Worker 진행, 폐기·격리 뒤 대기 요청 거부, 기존 반환과의 경쟁/실패 Rollback, Claim과의 자원 집계, 알려지지 않은 Delivery 상태 거부를 확인했습니다. 독립 리뷰의 감사 누락·알 수 없는 상태 허용 문제를 실제 DB에서 실패시킨 뒤 수정했습니다.
- Uvicorn을 임시 Loopback TCP 포트로 실제 실행해 생성→등록→Claim→작업 실패 전이→임대 만료→조회→잘못된 인증/Version 거부→동시 복구 요청 두 번→단일 반환·감사를 확인했습니다. 이 검사는 격리된 PostgreSQL Schema를 사용하며 Lifespan/백그라운드 작업은 꺼 두었습니다. 운영 시작·VM 종료·Browser/Console 검증은 아닙니다. UI 변경이 없어 화면·키보드 검사는 해당하지 않습니다.
- GitHub Actions는 기존 PostgreSQL Worker 검사 단계에서 이 회귀도 실행합니다. 새 Job·Matrix·VM 빌드는 추가하지 않습니다. 로컬 전체 검사와 정확한 최종 Head의 CI 결과는 PR에 기록합니다.
- 최종 코드의 `make test`는 API 1195 통과/152 Skip, Runner 45, Worker/Gateway, Web 125와 타입 검사, Lab 18 통과였으며 `make lint`도 통과했습니다. 별도 PostgreSQL CI 명령은 기존 16개+복구 46개, 총 62개 통과(15.34초). 일반 API 실행의 Skip은 전용 PostgreSQL 환경 미설정 및 Mac의 Linux 전용 검사이며, 새 복구 46개는 위 실제 DB 검사로 따로 확인했습니다. 다른 PostgreSQL Gate의 최종 검증은 CI에서 확인합니다.

재현 시 `KELPIE_TEST_POSTGRES_URL`은 운영 DB가 아닌 전용 최신 Schema 테스트 DB에 안전하게 주입합니다. 원문 URL이나 Token을 명령 기록·문서에 쓰지 않습니다.

```bash
make test-api
.venv/bin/python -m pytest -q apps/api/tests/test_worker_postgres.py apps/api/tests/test_worker_lease_reconciliation_postgres.py
make test
make lint
```

URL이 없으면 PostgreSQL 검사는 Skip하며 통과로 계산하지 않습니다. Worker의 실제 재시작/VM 정리 통합, 작업별 네트워크·시간 예산, Preview/Console와 동시 두 작업 Acceptance는 여전히 남아 있습니다.
