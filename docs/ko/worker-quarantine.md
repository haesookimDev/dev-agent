# Worker 제어 영역 격리

한국어 | [English](../en/worker-quarantine.md) · [자격증명 운영](worker-credentials.md)

이 명령은 침해 의심 Worker의 **제어 API 접근과 후속 전송**을 차단합니다. VM 종료·Host 네트워크 차단·이미 열린 Preview/WebSocket 종료를 수행하는 물리적 격리 명령은 아닙니다. 완전한 사고 대응 또는 SEC-001 전체 완료로 간주하지 않습니다.

## 실행과 확인

제어 서버의 DB 관리 권한으로 실행합니다. 조직 관리자의 일반 HTTP 권한에는 전역 Worker 격리 권한을 추가하지 않습니다. 최신 API와 Schema `20260905_0005`가 필요하며 추가 Migration·환경변수는 없습니다.

```bash
.venv/bin/python -m app.worker_admin list
.venv/bin/python -m app.worker_admin quarantine \
  --worker-id <대상-Worker-ID> --reason '침해 의심 사고 대응'
```

대상을 먼저 확인하고 사유에 토큰·개인정보를 넣지 않습니다. 결과는 다음 형태의 Metadata이며 원문 토큰은 출력하지 않습니다.

```json
{"worker_id":"<Worker-ID>","already_quarantined":false,"revoked_credentials":2,"invalidated_leases":1,"affected_work_ids":["<Work-ID>"],"physical_cleanup_required":true}
```

한 DB Transaction에서 다음을 처리합니다.

- Worker를 `offline`으로 전환하고 모든 미폐기 자격증명과 활성 Lease를 폐기·격리합니다. 공유 개발 토큰으로 돌아가지 않습니다.
- 진행 중 작업은 `cancelled`, `committing`·`pr_created`는 `failed`로 전환합니다. 기존 `completed`·`failed`·`cancelled` 기록은 유지합니다.
- 대기·재시도·실행 중 Delivery Job을 `quarantined`로 표시하고 Preview·Console 임대를 만료시킵니다. 관리 이력에는 UID·사유, 작업 Event에는 일반적인 격리 안내와 Worker ID만 기록합니다.
- **자원 예약을 반환하지 않습니다.** VM 종료를 확인하지 않은 상태에서 CPU·메모리·Disk를 재사용 가능으로 표시하면 안 됩니다.

반복 실행은 성공하지만 중복 격리 Event를 만들지 않습니다. 격리 Worker의 재발급·Rotation도 거부합니다. 정상 교체용 `revoke`와 달리 작업 Lease도 무효화됩니다.

## API와 전송 경계

| 요청 | 격리 완료 후 결과 |
| --- | --- |
| Worker 등록·Heartbeat·Claim, 작업 Lease 요청 | `401` — 기존 자격증명으로 접근 불가 |
| 작업 Feedback·Approval·Console 인수/반환, 연결된 Slack 명령 | `409`, `{"detail":"work's worker is quarantined"}` |
| Gateway의 새 Preview/Console 해석 | `410` — 대상 주소를 반환하지 않음 |
| 권한 있는 사용자의 작업·Event 조회 | 기존 조회 가능 — 사고 기록 확인 |
| 다른 Worker의 실행·전송 | 유지 |

PostgreSQL Row Lock 순서를 Worker → Lease/Work → Delivery Job/Preview/Console로 맞춥니다. 승인 이후에도 GitHub Token 발급, Git Push, PR 생성 직전에 상태를 다시 검사하고 해당 외부 요청 동안 Worker Lock을 유지합니다. 이미 시작한 요청이 있으면 격리가 기다리며, 각 외부 쓰기는 Lock 대기를 포함해 45초로 제한합니다. Git 취소 시 생성한 프로세스 그룹을 종료합니다. 격리 Commit 이후 새 전송은 시작하지 않으며 늦은 실패 처리·서버 재시작도 격리 상태를 덮어쓰지 않습니다.

이미 SCM에 도달한 요청은 Timeout 후에도 원격에서 성공했을 수 있습니다. 격리 이전 Push/PR과 이미 발급한 외부 Token을 자동 삭제·폐기하지 않으므로 SCM 감사 기록을 확인하고 필요한 별도 대응을 수행합니다. 기존 Preview 연결은 이 API 검사만으로 끊기지 않습니다. SQLite는 Demo·기능 테스트용이며 PostgreSQL의 경쟁 조건 보장을 대신하지 않습니다.

## 물리적 대응과 복구

승인된 운영 절차로 대상 Host의 네트워크·WireGuard 접근을 차단하고, 해당 VM·열린 Preview 연결을 종료하며, 유출 가능 자격증명을 폐기합니다. 관련 로그와 작업 산출물은 증거 보존 정책에 따라 취급합니다. CLI는 Host 파일이나 VM을 삭제하지 않습니다.

격리 해제·강제 자원 반환 명령은 제공하지 않습니다. 원인 제거와 실제 정리를 확인한 뒤 깨끗한 Host를 새 Worker Identity로 등록하고, 산출물을 검토해 새 작업으로 재시도합니다. DB 플래그를 직접 되돌리거나 개발 공유 인증으로 복구하지 않습니다. 구버전은 전송 차단을 지원하지 않으므로 Rollback 시 외부 트래픽·Worker·전송 프로세스를 중지하고 격리 Metadata를 보존한 채 수정 버전으로 Roll-forward합니다.

## 검증

- `make test-api`: 모든 상태의 격리·반복 실행·CLI·9개 Lease Endpoint·사용자/Slack/Preview 차단, 전송 단계별 격리·정상 전달·늦은 실패·Deadline, 실제 자식 프로세스 취소.
- 격리된 최신 Schema PostgreSQL에 `KELPIE_TEST_POSTGRES_URL`을 설정하고 `pytest -q apps/api/tests/test_worker_postgres.py`: 임대·Preview·전송과 격리의 양방향 경합, 다른 Worker 진행, 전송 Timeout의 Lock 해제. CI의 기존 PostgreSQL Step에서도 실행합니다.
- 실제 사용은 별도 API·개별 인증 Mock Worker 두 대·대시보드에서 대상 작업 중단과 정상 Worker 완료를 확인합니다. 실제 Linux/KVM·WireGuard·noVNC 격리 검증은 별도 P1 완료 조건입니다.
