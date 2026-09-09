# 재시작 가능한 일반 산출물 예약 정리

한국어 | [English](../en/scheduled-retention.md)

## 범위와 시작 조건

OPS-001의 **로컬 일반 산출물**만 다룹니다. [기존 정리](artifact-retention.md)의 활성 임대·Worker 격리·작업 종료·Preview/Console·별칭·파일 무결성·두 단계 감사 검사를 그대로 사용합니다. API 요청이나 VM 안에서 자동 실행하지 않으며 새 HTTP API, 기본 보존 기간, 의존성을 추가하지 않습니다. Event·Delivery Bundle·VM 디스크·감사 기록·외부 Object Store·이전 백업의 정리는 여전히 범위 밖입니다.

승인된 정책, 정확한 `DATABASE_URL`/`ARTIFACT_ROOT`, [DB·파일 복구 지점](artifact-backup.md)을 먼저 확인합니다. 모든 API·백업/정리 도구를 같은 버전으로 올리고 `alembic upgrade head`로 `20260909_0011`까지 적용한 뒤 실행합니다. 각 배치 전에 현재 코드와 일치하는 DB 준비 상태를 검사합니다. 운영 배포·삭제 권한을 이 문서가 부여하지는 않습니다.

```sh
# 예시 정책 30일, 예시 UUID: 실제 운영 대상을 의미하지 않음
python -m app.artifact_retention_worker --retain-days 30 \
  --work-id 00000000-0000-0000-0000-000000000000 --limit 100 --once

# 검토한 범위에만 적용; --once를 빼면 배치 종료 후 300초 간격으로 반복
python -m app.artifact_retention_worker --retain-days 30 \
  --work-id 00000000-0000-0000-0000-000000000000 --limit 100 --mode apply --once
```

기본 `--mode dry-run`은 **정리 진행 위치만 DB에 기록**하고 산출물·감사·파일은 변경하지 않습니다. DB까지 완전히 읽기 전용이어야 하면 기존 `artifact_retention_admin`의 dry run을 사용합니다. `--retain-days`는 필수 정수 1..36500, `--limit`은 1..1000(기본 100), `--interval-seconds`는 1..86400(기본 300)입니다. `--work-id` 생략은 **전체 작업**이므로 별도 범위 승인이 필요합니다.

## 진행 위치와 실패 처리

`artifact_retention_jobs`는 정규화한 Root·보존 기간·작업 범위·dry-run/apply를 해시한 `scope_key`와 커서, 버전, 갱신/순회 종료 시각만 저장합니다. 경로·DB URL·자격증명·파일 내용은 저장하지 않습니다. 배치 크기와 간격 변경은 기존 진행 위치를 유지하며, 정책·Root·작업·모드 변경은 별도 진행 위치를 사용합니다. dry-run의 진행이 apply 대상을 건너뛰게 하지 않습니다.

성공한 페이지의 다음 커서를 저장하므로 보호된 첫 페이지 뒤의 자료까지 방문합니다. 끝에 도달하면 커서를 비워 다음 순회에서 새 자료와 보호되었던 작업을 다시 확인합니다. 삭제 자체는 기존 파일별 만료 의도/완료 기록으로 재시도 가능하며, 커서 저장 실패 뒤에도 감사가 중복되지 않습니다. 같은 범위는 한 인스턴스로 운영하세요. 동시 실행 시 버전 조건부 갱신으로 오래된 진행 위치의 덮어쓰기를 차단하지만 중복 스캔을 없애는 분산 리더 선출은 아닙니다.

배치마다 JSON 한 줄을 출력합니다. 예를 들어 첫 페이지의 활성 임대를 보호하면 다음과 같은 형태입니다(해시와 UUID는 예시).

```json
{"job_id":"<scope SHA-256>","checkpoint_saved":true,"checkpoint_version":2,"batch":{"dry_run":false,"scanned":1,"counts":{"protected":1},"reasons":{"lease_not_released":1},"next_cursor":"00000000-0000-0000-0000-000000000001"}}
```

`failed`가 있거나 진행 위치 갱신에 경쟁/실패가 있으면 종료 코드 2로 중단합니다. `checkpoint_saved=false`인 응답의 `batch.next_cursor`는 **저장된 진행 위치가 아닙니다**. 앞서 성공한 삭제는 되돌리지 않습니다. 원인을 조사하고 **같은 보존 정책과 범위**로 재시작합니다. Root 누락·변경 파일·보호 상태를 우회하거나 만료 필드를 지우지 마세요. 경로·파일명·원본 DB 오류는 로그에 내보내지 않습니다.

SIGINT/SIGTERM은 대기 중인 작업을 취소하고 종료를 기다린 뒤 DB Pool을 닫습니다. 신호 종료 코드 0은 전체 순회 완료를 뜻하지 않습니다. 느린 동기 파일 IO에는 강제적인 전체 실행 시간 보장이 없으며, 강제 종료는 만료 의도 커밋과 진행 위치 저장 사이에 발생할 수 있습니다. 재시작 시 같은 페이지를 안전하게 재검사합니다.

## 명시적으로 설치하는 Linux 서비스

[서비스 템플릿](../../infra/systemd/kelpie-artifact-retention.service)은 자동 설치/활성화되지 않습니다. 일반 제어 호스트용이며 KVM·libvirt 그룹이나 VM Host 권한을 요구하지 않습니다. 운영자가 다음을 준비하고 검토해야 합니다.

- API 패키지가 설치된 `/opt/kelpie/api/.venv/bin/python`, 작업 디렉터리 `/opt/kelpie/api`, 비특권 `kelpie-api` 계정/그룹. 실제 배포 경로가 다르면 템플릿을 명시적으로 조정합니다.
- 보호된 `/etc/kelpie/api.env`의 기존 API 설정과 PostgreSQL 연결. `ARTIFACT_ROOT`는 쓰기 허용 경로 `/var/lib/kelpie/artifacts`와 일치해야 합니다. 다른 Root나 SQLite DB 파일을 사용하면 별도의 경로·권한 검토가 필요하며 넓은 디렉터리를 통째로 쓰기 허용하지 않습니다.
- 운영자가 소유하고 읽기를 제한한 `/etc/kelpie/artifact-retention.env`. 서비스는 기본적으로 전체 작업을 스캔하므로 해당 범위까지 승인합니다. 한 작업만 필요하면 `ExecStart`에 확인한 `--work-id`를 명시합니다.

| 서비스 전용 변수 | 기본값 | 의미 |
| --- | --- | --- |
| `ARTIFACT_RETENTION_DAYS` | 없음, 필수 | 승인된 보존 기간 |
| `ARTIFACT_RETENTION_MODE` | `dry-run` | 검토 후에만 `apply` |
| `ARTIFACT_RETENTION_LIMIT` | `100` | 배치 후보 수 |
| `ARTIFACT_RETENTION_INTERVAL_SECONDS` | `300` | 배치 종료 후 대기 초 |

위 변수는 systemd가 CLI 인자로 전달하는 설정이며 API 전역 설정은 아닙니다. 정책 파일이나 기간이 없으면 실행되지 않습니다. 승인된 호스트에 템플릿을 설치한 뒤 `systemd-analyze verify`와 dry-run 로그를 확인하고, 그 이후에만 활성화합니다. 별도 timer는 필요하지 않습니다. 실패 시 `Restart=no`로 멈추므로 상태·종료 코드·배치 로그를 운영 모니터링에 연결하고 원인 해결 후 재시작합니다. 기본 SIGTERM 유예는 30초이며 이후 강제 종료될 수 있습니다. 현재 macOS 검증에서는 호스트 서비스를 설치하거나 시작하지 않았습니다.

## 복구와 검증

0011은 진행 위치 테이블만 추가합니다. DB 백업/새 DB 복원 검증에는 실제 진행 행과 읽기 전용 역할의 수정 거부도 포함됩니다. DB와 Root 복구 시 같은 실제 경로/정책을 사용하면 해당 복구 지점의 진행을 이어갑니다. Root를 바꾸면 새 순회이며 파일별 만료 기록은 그대로 존중합니다. 백업하는 동안 이 Worker도 파일/DB Writer로 중지해야 합니다.

롤백은 먼저 서비스를 중지하고 기존 만료 읽기 차단·0010·감사를 유지하는 전진 수정을 우선합니다. 0011만 되돌릴 때는 모든 새 Worker를 중지하고 0010과 호환되는 코드로 함께 전환해야 합니다. 진행 위치는 사라져 처음부터 스캔하지만 완료된 삭제·감사는 유지됩니다. 만료 바이트 복원이나 0010 삭제는 롤백이 아닙니다.

- 실행 코드 `d35be34`, 서비스 `21eb4a6`: 실제 PostgreSQL을 포함한 `make test`에서 API 1,268개, Runner 45개, Web 125개 및 Worker/Gateway 통과. `make lint` 통과. 이후 서비스 검사는 macOS에서 2개 통과, Linux systemd 구문 1개는 해당 CI 환경에서 확인합니다.
- 실제 프로세스 11개 검증은 재시작, 주기적 진행, 긴 대기 중 SIGTERM, 입력/설정 실패와 비밀정보 비출력을 포함합니다. PostgreSQL 7개는 버전 경쟁, 재시도, 삭제/감사 중복 방지, DB 잠금 대기 중 종료와 연결 반환을 검증합니다.
- 임시 API·DB·파일을 사용하는 Orca 브라우저에서 만료 전 내용, 두 번의 별도 Worker 실행 후 활성 파일 200/완료 파일 410, 한국어·영어 만료 안내와 열기 버튼 제거, 보호된 파일의 계속된 열람을 확인했습니다. 테스트용 완료 파일 29바이트만 정리했습니다. Fixture의 나이/상태는 합성 설정이며 실제 VM 실행 증거가 아닙니다.
- Orca 브라우저의 1155px DOM에 가로 넘침이 없었고 마지막 정상 서비스 구간의 완료 요청 72개에 HTTP 오류가 없었습니다. 콘솔에는 DevTools/HMR 안내만 있었습니다. 네이티브 화면은 작업 터미널을 표시했고 포커스 복원 뒤 클릭이 거부되었습니다. 브라우저 캡처도 시간 초과되어 **네이티브 입력·화면 캡처 성공으로 보고하지 않습니다**. 완료 후 모달 닫기/재열기 수동 결과는 확인되지 않았으며 기존 자동 회귀로 별도 검증합니다.
- `artifact-retention.spec.ts`는 기존 수동 CLI 경로를 유지하면서 예약 Worker로 실제 업로드 자료를 만료한 양 언어 경로를 추가합니다. `cfbf890`의 전체 Chromium 41개가 첫 실행에서 2.5분에 통과했으며 Web 테스트·타입 검사 125개, lint와 운영 빌드도 통과했습니다. 390px 한국어·데스크톱 영어의 실제 Chromium 스크린샷을 직접 검토했고 최종 CI의 `browser-evidence`에 보존합니다. 제품 UI 코드는 변경하지 않았습니다.

다른 데이터 종류의 보존 정책, Object Store 복구, 실제 운영 복구, KVM/네트워크/Console 격리 수용 검증은 남아 있습니다. 이 변경을 전체 OPS-001 또는 MVP 완료로 표시하지 않습니다.
