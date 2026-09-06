# PostgreSQL 백업·복원 검증

한국어 | [English](../en/postgres-restore.md)

## 범위와 안전 경계

OPS-001의 논리 백업·새 Database 복원 절차와 반복 검증입니다. 운영 데이터나 Object Store·VM을 복구·배포한 결과가 아닙니다. 예약 Backup, PITR, 암호화 저장소 배치, 실제 규모의 RPO/RTO 측정은 별도 운영 작업입니다.

`pg_dump`는 Database 하나의 일관된 Snapshot을 만들지만 Cluster Role·Tablespace와 외부 파일은 포함하지 않습니다. `pg_restore --single-transaction --exit-on-error`를 사용해 오류 시 부분 복원을 Rollback합니다. [SQL Dump](https://www.postgresql.org/docs/17/backup-dump.html), [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html), [pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)

- Backup에는 요구사항, 감사 신원, OIDC 로그인 Verifier, Session·Worker·Lease Hash 등 민감한 데이터가 들어 있습니다. Custom Format과 SHA-256은 암호화나 작성자 인증이 아닙니다. 승인된 암호화 저장소·접근 제한·보존 정책을 사용하며 Dump·로그·자격증명을 Git, PR, CI Artifact에 올리지 않습니다.
- 신뢰된 출처의 Backup만 복원합니다. Function 등 Dump 내용은 복원 중 SQL을 실행할 수 있습니다. TLS 검증·원래 소유권·ACL·감사 Trigger를 없애서 검증을 통과시키지 않습니다.
- 원본을 보존하고 Cluster·접속 Role·새 DB 이름을 운영자가 확인합니다. `--clean`, 기존 DB 덮어쓰기, `--disable-triggers`, `--no-owner`, `--no-acl`로 오류를 회피하지 않습니다.
- 복원 환경을 운영 API/Worker/Webhook/Preview와 외부 SCM·알림에서 격리합니다. API는 준비 상태가 회복되면 저장된 `pending`/`retry`/`running` 전달을 재개하므로, 검증 전에 정상 쓰기 계정으로 시작하지 않습니다. [전달 복구](delivery-recovery.md)

## 백업

1. 작업 유입을 닫고 API·Worker·파일 Writer를 조정해 DB/파일의 복구 지점을 기록합니다. 진행 중 VM과 외부 전송의 종료 여부·실제 원격 결과를 확인합니다. DB Snapshot은 이들을 정지시키지 않습니다.
2. 배포된 API Commit/Image Digest, Alembic Revision, PostgreSQL/Client Version, UTC 시간, Cluster 식별자, DB Owner·Role·DB별 설정/접속 ACL·Tablespace 정책, 대응 Object Store Snapshot/Version을 암호화 Inventory에 기록합니다. 앱의 조직 역할과 PostgreSQL Cluster Role은 다릅니다.
3. 서버와 호환되는 같은 Major의 `pg_dump`/`pg_restore`를 사용합니다. 회귀는 PostgreSQL 17 도구/서버를 사용합니다. `PGSERVICEFILE`·`PGPASSFILE`은 Secret Manager가 주입한 제한된 파일을 가리키게 하며 비밀번호/DSN을 명령 인수에 넣지 않습니다. 운영 Service는 `sslmode=verify-full`과 검증한 CA를 사용합니다.
4. 아래 Service·Mount는 운영자가 미리 승인·구성할 예입니다. 자동 생성되지 않으며 실제 대상 대신 그대로 사용하지 않습니다. 각 명령의 종료 상태를 확인합니다.

```sh
set -eu
umask 077
backup_dir=$(mktemp -d /run/secure-backups/kelpie.XXXXXX)
pg_dump --no-password --dbname='service=kelpie-backup' --format=custom \
  --file="$backup_dir/control.dump" 2>"$backup_dir/dump.log"
pg_restore --list "$backup_dir/control.dump" >"$backup_dir/catalog.txt"
(cd "$backup_dir" && sha256sum control.dump >control.dump.sha256)
```

권한 부족이나 Dump 오류는 실패입니다. Table/Schema 선택으로 데이터를 조용히 제외하지 않습니다. Hash·크기·복구 지점·성공 결과를 Inventory에 연결하고 승인된 저장소에서 다시 읽어 검증합니다. macOS에서는 `sha256sum` 대신 `shasum -a 256`을 사용합니다.

## 새 Database에 복원

원본과 분리된 Cluster에 Inventory의 Role·Tablespace를 먼저 구성합니다. Role 정의/자격증명, DB별 설정·접속 ACL은 단일 DB Dump만으로 복구되지 않습니다. 원래보다 넓은 권한을 부여하지 않습니다. Backup을 받아 신뢰된 Inventory의 Hash와 대조합니다.

아래 `restore_database`와 Owner는 운영자가 확인한 **아직 없는** DB 이름과 기존 Role로 바꿉니다. `createdb` 실패 시 즉시 멈추며 기존 DB를 삭제하거나 실패한 이름을 재사용하지 않습니다.

```sh
set -eu
umask 077
restore_database=kelpie_restore_incident_001
(cd "$backup_dir" && sha256sum --check control.dump.sha256)
createdb --no-password --maintenance-db='service=kelpie-restore-admin' \
  --template=template0 --owner=kelpie_owner "$restore_database"
pg_restore --no-password \
  --dbname="service=kelpie-restore-admin dbname=$restore_database" \
  --single-transaction --exit-on-error "$backup_dir/control.dump" \
  2>"$backup_dir/restore.log"
```

DB별 설정·접속 ACL을 복원하고 외부 접근을 계속 차단합니다. 실패하면 새 DB를 격리한 채 원인을 해결하고 또 다른 새 DB에서 재시도합니다. 단일 Transaction은 DB 생성 자체를 되돌리지 않습니다. 자동 삭제나 운영 연결 종료는 하지 않습니다.

## 검증·서비스 재개 Gate

1. 복원 시점의 API Version으로 Alembic Head 일치와 `alembic check`를 확인합니다. 이 검사만으로 Trigger·권한·데이터를 보장하지 않습니다. 필요하면 검증 후 별도 [Migration 절차](operations.md#database-migration)로 Upgrade합니다.
2. 전체 Table 건수·비공개 Fingerprint, PK/FK·Column/기본값·Index, Sequence의 다음 ID, 조직·저장소·Grant, 감사 상세와 승인-DeliveryJob 연결을 비교합니다. 원본 행을 로그에 출력하지 않습니다.
3. Owner/ACL·격리·폐기 상태를 확인합니다. 일회성 복원 사본의 Transaction에서 감사 UPDATE/DELETE/TRUNCATE/Upsert·서비스 Actor 위조의 거부와 충돌 없는 새 감사 INSERT를 검증합니다. 기존 감사는 삭제·재작성하지 않습니다.
4. Artifact/Delivery Bundle의 실제 바이트·크기·Hash를 대응 Snapshot과 비교합니다. DB 행만 있고 Artifact 바이트가 없으면 현재 API는 `410 artifact content is unavailable`을 반환합니다. 일반 파일은 [Artifact 백업·새 Root 복원·재검증](artifact-backup.md)을 수행합니다. [Delivery Bundle 검증](delivery-integrity.md)은 별도로 실제 크기·Hash·경로를 확인하고 손상된 다운로드·승인·전달을 거부합니다. Preview·VM·Lease의 실제 생존 상태도 대조합니다.
5. Backup 이후의 회원·Grant 회수, Session 폐기, Worker 회전·격리, 승인 변경과 이미 생성된 원격 Commit/PR을 신뢰된 최신 기록과 대조합니다. 과거 Session·Token·승인이 되살아난 채 트래픽을 열지 않습니다. 불확실한 전달의 승인 연결을 추측하거나 무조건 재시도하지 않습니다.
6. Gate 증거, 데이터 손실 구간(RPO), 실제 경과 시간(RTO), Rollback 대상과 운영 승인을 기록합니다. 이전 API가 완전히 중지됐는지 확인하고 단일 API·Worker·유입을 단계적으로 재개합니다. `/readyz` 200은 전달·파일·VM 복구 완료 신호가 아닙니다. 필요하면 격리 상태에서 `ANALYZE`를 실행합니다.

전환 전 실패는 원본을 유지한 채 복원 후보를 포기할 수 있습니다. 전환 후 새 쓰기가 생겼다면 무조건 원본으로 되돌리는 것도 데이터 손실·중복 전달을 만드므로 다시 유입을 멈추고 변경을 대조합니다. 저장소의 자율 PR 머지 위임은 운영 DB 전환·삭제를 허용하지 않습니다.

## 반복 검증과 CI

같은 전용 PostgreSQL 17 서버를 가리키는 `KELPIE_TEST_POSTGRES_URL`·`KELPIE_TEST_POSTGRES_CONTAINER`를 설정합니다. **테스트 전용** DB/Role 생성 권한과 Container 내부 로컬 인증이 필요합니다. 운영 대상을 사용하지 않습니다.

```sh
.venv/bin/python -m pytest -q apps/api/tests/test_postgres_restore.py \
  apps/api/tests/test_postgres_restore_runtime.py
```

6개 테스트는 성공적으로 만든 `kelpie_restore_<UUID>` DB와 `kelpie_reader_<UUID>` Role만 사용합니다. 입력 URL의 DB는 관리 접속에만 사용하며 Migration·Dump·Restore·삭제 대상이 아닙니다. 기존 이름의 충돌은 소유권을 부여하지 않습니다. Cleanup은 활성 연결을 강제 종료하지 않고 자기 DB·Role·0600 Archive·로그만 제거합니다.

- 모든 Model Table에 합성 행을 넣고 실제 Alembic Upgrade → Custom Dump → `template0` 새 DB Restore → 전체 행/Schema·Owner·ACL·Sequence 비교와 Alembic Check를 수행합니다.
- PostgreSQL이 다시 파싱하는 리터럴 배열의 동등한 형변환과 기본 ACL의 명시/암시 표현만 정규화합니다. 다른 제약 표현은 보존하며 감사 거부 동작도 실제 SQL로 검사합니다.
- 충돌 Table·잘린 Archive는 실패하며 앞서 실행한 DDL/데이터가 남지 않아야 합니다.
- 실제 Uvicorn을 SELECT-only PostgreSQL Login으로 실행합니다. 복원한 합성 OIDC Session의 자기 조직 조회, 타 조직 404, 관리자 감사 403, 무인증 401, 파일 없는 Artifact 410을 검사합니다. 시작 시 전달 복구는 실제 실행되지만 쓰기 Lock이 거부되며 원본/복원 Fingerprint는 유지됩니다. 제품의 유지보수 Mode나 쓰기 API 공개 방법은 아닙니다.

환경변수가 없으면 6개가 Skip됩니다. 필수 `Python` CI는 기존 PostgreSQL Service의 [Container ID](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#job-context)로 Skip 없이 실행합니다. 새 Job·Matrix·외부 Secret·의존성 없이 기존 8분 제한·읽기 전용 Workflow 권한을 유지합니다.

[일반 파일 복원 회귀](artifact-backup.md)는 같은 Step에 `test_artifact_restore_runtime.py` 2개를 추가해 총 8개를 실행합니다. DB만 복원한 410 상태에서 실제 CLI 파일 복원 후 200과 권한 경계를 확인합니다.

## 실제 검증 기록

2026-09-06 KST, `a47856c`의 테스트/Workflow와 변경 없는 앱에서 실제 PostgreSQL 복원 6개(3.40초), 전용 PostgreSQL `make test`(API 526개/70.99초, Runner 6개, Web 52개·타입 검사, Worker/Gateway), `make lint`가 통과했습니다. 기본 SQLite 실행의 PostgreSQL Skip은 실제 복원 검증을 대체하지 않습니다.

브라우저 증거와 최종 Commit·CI·Merge 결과는 PR에 기록합니다. 이 테스트는 같은 Test Cluster의 새 DB와 미리 구성한 테스트 Role을 사용합니다. 다른 Cluster의 Role/설정 이관, 실제 IdP 로그인, 운영 복구, Object Store·VM 복원이나 전체 MVP 완료의 증거가 아닙니다.
