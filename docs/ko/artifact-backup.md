# 일반 산출물 파일 백업·복원

한국어 | [English](../en/artifact-backup.md)

## 범위와 복구 지점

OPS-001의 로컬 `ARTIFACT_ROOT` 일반 Artifact 파일을 대응 DB Dump와 연결하는 오프라인 운영 명령입니다. `pg_dump`에는 외부 파일이 없으므로 [DB 백업·복원](postgres-restore.md)과 함께 사용합니다. API·DB Schema·환경변수 기본값·조직 권한·다운로드 정책은 변경하지 않습니다. Delivery Bundle, VM Disk, Preview, 외부 Object Store, 예약 Backup·PITR·Janitor는 이 Snapshot에 포함되지 않습니다.

- 승인된 Control Host에서 배포된 API와 같은 Version으로 실행합니다. `DATABASE_URL`은 Secret Manager가 안전하게 주입한 검증 대상 DB를 가리켜야 합니다. CLI에는 Schema 조회와 Artifact SELECT 권한만 필요하며 API 시작·Migration·DB 쓰기·서비스 중지·재개·경로 전환을 수행하지 않습니다.
- 유입과 DB/파일 Writer를 운영자가 정지·조정한 상태에서 DB Dump → 파일 Snapshot을 만들고, 완료까지 같은 복구 지점을 유지합니다. `--writers-stopped`는 이 조정을 했다는 확인일 뿐 정지 명령·Lock·정지 증명이 아닙니다. 시작/종료의 메타데이터·Dump 해시 대조도 모든 동시 쓰기를 탐지하지는 못합니다.
- 복원 시 DB를 먼저 별도의 새 DB에 복원합니다. 파일 명령은 Dump의 실제 SHA-256과 연결된 Artifact 메타데이터를 대조하지만, 연결 DB가 그 Dump에서 왔거나 전체 ACL·감사가 올바르다는 증거는 아닙니다. DB 전체 검증과 최신 권한 회수 대조 Gate는 별도로 유지합니다.
- Backup에는 사용자 파일 내용·이름·작업 ID가 포함됩니다. SHA-256은 암호화·작성자 인증이 아닙니다. 승인된 암호화 저장소, 접근 제한, 보존 정책을 사용하고 Dump·Snapshot·Manifest·로그를 Git/PR/CI Artifact에 올리지 않습니다. Manifest 해시는 Backup과 독립적으로 보호하는 신뢰된 Inventory에 보관합니다. [Python 해시 함수](https://docs.python.org/3/library/hashlib.html)

## 생성 및 독립 검증

다음은 [DB 백업 절차](postgres-restore.md)의 `backup_dir`와 `control.dump`가 준비되고 모든 Writer 조정을 마친 뒤 실행할 예입니다. `ARTIFACT_ROOT`는 원본 파일 Root, `DATABASE_URL`은 같은 복구 지점의 원본 DB여야 합니다. 실제 경로·DB 선택은 승인된 운영 설정을 사용합니다.

```sh
set -eu
umask 077
.venv/bin/python -m app.artifact_backup_admin create \
  --database-dump "$backup_dir/control.dump" \
  --output "$backup_dir/artifacts" --writers-stopped
```

종료 코드 0과 `verified: true`를 확인하고 출력된 `manifest_sha256`을 신뢰된 Inventory에 연결합니다. stdout은 개수·검증 상태·Manifest 해시만 출력하며 실패는 비공개 값을 제외한 고정 오류와 종료 코드 2를 반환합니다. 파일 이름이나 DB 접속 정보를 로그로 출력하지 않습니다.

`trusted_manifest_sha256`은 Inventory에서 받아 설정한 값입니다. 검증하려는 Manifest에서 자동으로 계산해 신뢰 값으로 사용하지 않습니다. 승인된 저장소에 복사한 Snapshot도 아래 명령으로 다시 읽어 검증합니다.

```sh
.venv/bin/python -m app.artifact_backup_admin verify \
  --database-dump "$backup_dir/control.dump" --backup "$backup_dir/artifacts" \
  --manifest-sha256 "$trusted_manifest_sha256"
```

## 새 Root로 복원

DB 복원·격리 Gate를 먼저 수행하고 `DATABASE_URL`을 복원 후보 DB의 제한된 조회 계정으로 구성합니다. `restore_root`는 승인된 기존 부모 디렉터리 아래 **아직 없는 새 Root**여야 합니다. 원본·백업 Root 내부를 선택하거나 기존 후보를 재사용하지 않습니다. 아래 명령이 모두 성공하기 전에는 API에 복원 Root를 연결하지 않습니다.

```sh
set -eu
umask 077
.venv/bin/python -m app.artifact_backup_admin restore \
  --database-dump "$backup_dir/control.dump" --backup "$backup_dir/artifacts" \
  --manifest-sha256 "$trusted_manifest_sha256" \
  --output "$restore_root" --writers-stopped
ARTIFACT_ROOT="$restore_root" .venv/bin/python -m app.artifact_backup_admin verify-restored \
  --database-dump "$backup_dir/control.dump" \
  --manifest-sha256 "$trusted_manifest_sha256"
```

파일 복원은 원래 Object Key를 유지하며 DB 행을 만들거나 변경하지 않습니다. `.kelpie-artifact-restore.json`은 파일을 복사·다시 읽어 검증한 뒤 게시하는 Manifest 사본입니다. **표시 파일의 존재만으로 재개하지 않습니다.** 명령 성공, `verify-restored`, 전체 DB·권한·외부 전달·VM 생존 Gate를 모두 확인하고 운영 승인 아래 단일 API를 재개합니다. 일반 파일의 Hash 검증이 [제공 가능한 MIME/내용 검사](artifact-content.md)를 대체하지 않습니다.

실패한 새 디렉터리에는 일부 파일이 남을 수 있으며 자동 삭제하지 않습니다. 완료 표시가 있더라도 후속 DB 대조·동기화 실패나 이후 파일 변경이 가능하므로 격리한 채 원인을 조사하고 또 다른 새 경로에서 재시도합니다. 기존 파일·디렉터리·Symlink를 덮어쓰기 위해 권한 검사를 완화하지 않습니다. 전환 전에는 원본을 보존하고 후보를 포기할 수 있지만, 전환 후 새 쓰기가 있으면 단순히 원본으로 돌아가지 말고 [DB Rollback Gate](postgres-restore.md)를 따릅니다.

## 형식과 제한

- Version 1 `manifest.json`: `version`, `database_sha256`, `artifacts`. 각 항목에는 `artifact_id`, `work_item_id`, `object_key`, `kind`, `name`, `content_type`, `size_bytes`, `sha256`이 있습니다. 생성 시간 등 나머지 DB 행 정보는 DB Dump가 보존합니다.
- 실제 파일은 `blobs/<SHA-256>`에 중복 제거하여 저장합니다. Archive 경로를 추출하지 않습니다. Artifact ID 중복·다른 DB 메타데이터·일관되지 않은 동일 Key·잘못된 JSON/중복 필드·알 수 없는 Version/필드·누락/변조 파일을 거부합니다.
- 최대 10,000개 메타데이터, 파일당 10 MiB, Manifest 16 MiB이며 초과/잘못된 행을 조용히 제외하지 않고 전체 실패합니다. 빈 파일과 같은 Key의 별도 메타데이터는 보존합니다. 압축하지 않으며 원본·백업·복원 사본을 보관할 공간이 각각 필요합니다.
- 작업별 경로 검증, 하위 디렉터리와 최종 파일의 no-follow 열기, 일반 파일·크기·읽기 중 변경 검사를 사용합니다. Root/부모 자체는 운영자가 통제해야 합니다. 같은 OS 계정의 악성 프로세스로부터 Root를 보호하는 Sandbox가 아닙니다.
- 새 디렉터리 0700·파일 0600, 배타적 생성, 파일/디렉터리 동기화와 덮어쓰기 없는 최종 Manifest 게시를 사용합니다. 파일시스템은 해당 동작을 지원해야 하며 전원 장애 내구성·원격 저장소의 원자성을 보장하지 않습니다. 장애 뒤 반드시 다시 검증합니다.

## 회귀·CI·실제 확인

`make test-api`에 파일 경계 50개와 CLI 11개 테스트를 포함합니다. 같은 테스트용 PostgreSQL 서버를 지정한 `KELPIE_TEST_POSTGRES_URL`·`KELPIE_TEST_POSTGRES_CONTAINER`가 있으면 아래 실제 복원 8개도 실행합니다. 없으면 PostgreSQL 관련 검증은 Skip되며 성공 증거가 아닙니다.

```sh
.venv/bin/python -m pytest -q apps/api/tests/test_postgres_restore.py \
  apps/api/tests/test_postgres_restore_runtime.py apps/api/tests/test_artifact_restore_runtime.py
```

필수 `Python` CI의 기존 PostgreSQL 17 Service/복원 Step에 2개 파일 복원 테스트를 추가했습니다. 새 Job·의존성·Secret·Matrix 없이 8분 제한을 유지합니다. 테스트는 자신이 생성한 UUID DB·Reader Role만 정리하며 원본/복원 DB 전체 Fingerprint와 원본 파일 보존을 검사합니다.

2026-09-06, 앱 `e6f36a5`·회귀 `68e6d15`에서 실제 PostgreSQL 복원 8개/9.88초, 전용 PostgreSQL `make test`(API 801개/98.86초, Runner 6개, Web 83개·타입 검사, Worker/Gateway), `make lint`가 통과했습니다. 합성 OIDC Session과 SELECT-only DB 계정의 실제 Uvicorn·Next.js 브라우저에서 410 안내 → API 중지·CLI 복원·재개 → 같은 URL의 텍스트 200/재시도 포커스, 한국어 390px PNG 표시를 확인했습니다. HTTP 타 조직 404·감사 403·무인증 401도 검사합니다. 실제 IdP 로그인·운영 복구·VM 검증이 아닙니다. 최종 Head·브라우저 증거·CI/머지 결과는 PR에 기록합니다.
