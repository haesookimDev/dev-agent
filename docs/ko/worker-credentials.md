# Worker별 자격증명 운영

한국어 | [English](../en/worker-credentials.md)

Worker 인증은 기본 `WORKER_AUTH_MODE=scoped`입니다. 등록·Heartbeat·Claim에는 제어 서버에서 발급한 자격증명이 필요하며 Worker 이름과 ID에 묶입니다. 다른 Worker 요청은 `403`, 잘못되거나 만료·폐기된 토큰은 `401`입니다. 작업 VM/Runner에는 Worker 토큰이 아닌 해당 작업의 Lease 토큰만 전달합니다.

## 발급과 최초 전환

1. 기존 Worker는 Drain 후 활성 작업과 VM 정리가 끝났음을 확인하고 유지보수 시간에 중지합니다. 발급 즉시 해당 Worker의 공유 토큰 접근이 차단되므로 최초 전환을 무중단으로 간주하지 않습니다.
2. DB를 백업하고 `alembic upgrade head`로 `20260905_0005`를 적용합니다. CLI는 제어 서버의 DB 접근 권한과 준비된 Schema가 필요하며 외부 HTTP 발급 Endpoint는 없습니다.
3. 접근이 제한된 기존 디렉터리를 지정해 저장소 Root에서 발급합니다. 아래는 경로 예시이며 실제 토큰을 인자에 넣지 않습니다.

```bash
.venv/bin/python -m app.worker_admin issue \
  --worker-name worker-1 --reason '신규 Worker 등록' \
  --output /secure/provisioning/worker-1.token
```

CLI는 원문을 새 `0600` 파일에만 기록하고 stdout에는 Worker ID, 자격증명 ID, 만료 시각만 출력합니다. 기존 파일·심볼릭 링크를 덮어쓰지 않습니다. 파일 저장 실패는 DB를 Commit하지 않고, DB Commit 오류 시 자신이 생성한 파일을 정리합니다. 강제 종료·불명확한 Commit 장애 후에는 Metadata를 조회하고 불필요한 자격증명을 폐기합니다. 파일시스템과 DB의 분산 원자성을 보장하지 않습니다.

4. 승인된 Secret 전달 경로로 대상 Host에만 파일을 배치합니다. Worker 서비스 계정에 읽기 권한을 부여하고 `0400`/`0600` 및 디렉터리 접근 제어를 적용합니다. Container에는 파일 하나가 아닌 제한된 디렉터리를 읽기 전용 Mount해 원자적 교체를 허용합니다. 배포 후 제어 서버의 임시 원문 사본은 보존 정책에 따라 제거합니다.
5. API는 `WORKER_AUTH_MODE=scoped`, Worker는 다음 설정으로 시작합니다. 이름은 발급 시 이름과 정확히 같아야 합니다. 임의로 만든 32자 토큰은 개별 자격증명이 아닙니다.

```dotenv
KELPIE_WORKER_NAME=worker-1
KELPIE_WORKER_TOKEN_FILE=/run/secrets/kelpie/worker-1.token
```

파일 설정은 `KELPIE_WORKER_TOKEN`보다 우선하고 오류 시 이전 값·환경변수로 돌아가지 않습니다. 등록·Heartbeat·Claim마다 다시 읽습니다. 일반 파일, 최대 64 KiB, 마지막 CR/LF를 제외한 32자 이상의 공백 없는 ASCII만 허용합니다. 누락·빈 파일·디렉터리·FIFO·읽기 오류는 요청 전에 차단하고 경로·원문을 로그 오류에 넣지 않습니다.

## 실행 중 교체와 개별 폐기

```bash
.venv/bin/python -m app.worker_admin list
.venv/bin/python -m app.worker_admin rotate \
  --credential-id <현재-자격증명-ID> --reason '정기 교체' \
  --overlap-seconds 600 --output /secure/provisioning/worker-1.next
```

새 파일을 대상 Host에 안전하게 전달한 뒤 동일 파일시스템에서 완성된 파일로 원자적으로 교체합니다. 다음 Heartbeat/Claim 성공과 `list`의 새 `last_used_at` 갱신을 확인합니다. Worker 재시작은 필요 없습니다. 확인 후 이전 자격증명을 폐기합니다.

```bash
.venv/bin/python -m app.worker_admin revoke \
  --credential-id <이전-자격증명-ID> --reason '새 자격증명 사용 확인'
```

- 수명: 기본 30일, `--lifetime-seconds`로 60초~90일. 교체 중첩: 기본 600초, 60~3600초. 이전 토큰의 원래 만료 시각은 연장하지 않습니다.
- 새 토큰은 같은 Worker에 발급합니다. `revoke`는 지정한 자격증명 하나만 폐기하며 반복 호출은 중복 Event를 남기지 않습니다. 다른 Worker와 새 토큰은 유지됩니다.
- 정상 교체·폐기는 활성 작업 Lease를 폐기하지 않습니다. **침해 Host 격리 명령이 아닙니다.** VM 종료, WireGuard 차단, Preview 연결 종료, 활성 Lease 일괄 폐기는 후속 사고 대응 작업입니다. 이 기능만으로 침해가 차단됐다고 판단하면 안 됩니다.
- DB에는 원문의 SHA-256 해시만 저장합니다. 관리 이력은 실행 OS UID, 사유, 시각을 기록합니다. DB 관리자에게도 변경 불가능한 감사 저장소는 아니며, 공유 OS 계정에서는 개인을 식별하지 못합니다.

## Gateway와 개발 호환성

Preview 해석은 Worker 토큰을 받지 않습니다. API의 `GATEWAY_SECRET` 또는 `GATEWAY_SECRET_FILE`과 Gateway의 `KELPIE_GATEWAY_TOKEN`에 별도의 같은 무작위 값을 공급합니다. 최소 32자, 공백 없는 ASCII이며 Worker의 `kwc_` 토큰을 재사용할 수 없습니다. API는 파일을 다시 읽지만 Gateway는 시작 시 환경변수를 읽으므로 교체에는 조율된 재시작이 필요합니다. Gateway의 기본 인증 Mode는 계속 `disabled`이며 OIDC Preview Grant가 구현되기 전에는 공개하지 않습니다.

공유 토큰은 `AUTH_MODE=development`와 `WORKER_AUTH_MODE=development`를 모두 명시한 격리된 로컬 Demo에서만 허용합니다. OIDC Mode에서 공유 인증 설정은 시작 시 거부됩니다. 한번 개별 자격증명을 발급받은 Worker는 모두 폐기해도 공유 토큰으로 돌아갈 수 없습니다. 기본 Compose는 이 명시적 Demo 예외를 사용하며 브라우저 E2E는 실제 관리 CLI와 개별 토큰 파일을 사용합니다.

## Migration과 Rollback

Migration은 기존 Worker와 자원 예약을 보존하고 자격증명·이력 테이블, 개별 인증 필수 플래그, 격리 시각을 추가합니다. 격리 시각은 인증 차단 기반 Metadata이며 운영용 일괄 격리는 아직 구현되지 않았습니다. Request/Response Body는 그대로지만 인증과 Gateway 환경변수 변경은 하위 호환되지 않습니다.

Rollback은 모든 외부 트래픽·Worker를 차단한 유지보수 환경에서 수행합니다. DB 백업 후 `alembic downgrade 20260905_0004`를 실행하면 개별 자격증명·이력·격리 Metadata가 삭제됩니다. 다시 Upgrade해도 복구되지 않으므로 새로 발급해야 합니다. 이전 API가 개별 인증을 지원하지 않는 한 운영 트래픽을 열지 말고 수정 버전으로 Roll-forward합니다. 개발 공유 토큰으로 운영을 복구하지 않습니다.

## 검증

- `make test-api`: 실제 CLI, 출력 비노출, 권한, 실패 정리, Worker 범위, 교체·폐기·만료, Gateway 분리, 공유 토큰 우회 차단.
- `KELPIE_TEST_POSTGRES_URL`을 격리된 최신 Schema PostgreSQL DB로 지정하고 `pytest -q apps/api/tests/test_worker_postgres.py` 실행: 인증/폐기 양방향 경합과 다른 Worker의 진행. URL이 없으면 이 두 테스트만 Skip하며 CI는 별도 Step에서 반드시 실행합니다.
- `make test-worker`, `go test -race ./...`(Worker 디렉터리): 원자적 파일 교체, 누락 시 차단, 활성 작업 Lease 분리.
- `npm run test:e2e --prefix apps/web`: 실제 API·개별 인증 Mock Worker의 생성·피드백·재검증·승인. Mock 검증은 실제 KVM/브라우저 VM 2개의 격리 증거가 아닙니다.
