# 일반 산출물 보존 기간 정리

한국어 | [English](../en/artifact-retention.md)

## 범위와 적용 전 확인

제어 호스트의 로컬 `ARTIFACT_ROOT/<work UUID>/artifacts/...`에 저장된 일반 파일만 대상으로 합니다. VM 디스크, Delivery Bundle, Event, Preview, Console, 감사 기록, 외부 Object Store, 이전 백업은 삭제하지 않습니다. 물리적 VM 종료·격리나 전체 OPS-001 완료를 뜻하지 않습니다.

운영자가 보존 기간과 저장소를 확인한 뒤 제어 호스트에서 실행하는 제한된 CLI입니다. 자동 타이머, API 정리 엔드포인트, 기본 보존 기간은 추가하지 않습니다. 기존 `DATABASE_URL`과 `ARTIFACT_ROOT`를 사용하며 새로운 환경변수나 의존성은 없습니다. 대상 파일을 unlink하므로 승인된 보존 정책 없이 운영에 적용하지 마세요.

1. DB·파일 복구 지점을 [백업 절차](artifact-backup.md)에 따라 확보합니다.
2. Migration `20260906_0010`을 적용하고 **모든 API 인스턴스와 정리·백업 CLI를 함께 업그레이드**합니다. 이전 API가 실행 중이면 만료 의도를 해석하지 못할 수 있으므로 정리를 시작하지 않습니다.
3. 정확한 저장소 Root가 연결되어 있고 파일을 쓰는 주체가 제어 플랫폼의 임대 검증 경계를 따르는지 확인합니다. 같은 OS 계정의 악의적인 파일 교체까지 격리하는 샌드박스는 아닙니다.
4. 실제 `--work-id`를 확인한 dry run부터 수행합니다. 아래 UUID는 예시이며 운영 대상이 아닙니다.

```sh
python -m app.artifact_retention_admin --retain-days 30 \
  --work-id 00000000-0000-0000-0000-000000000000 --limit 100

# 검토한 동일 범위에만 실제 적용
python -m app.artifact_retention_admin --retain-days 30 \
  --work-id 00000000-0000-0000-0000-000000000000 --limit 100 --apply
```

`--retain-days`는 필수 정수 `1..36500`입니다. `--apply`가 없으면 DB·감사·파일을 변경하지 않습니다. `--work-id` 생략은 **모든 작업**을 스캔하므로 범위를 넓히기 전에 승인된 정책을 확인합니다. `--limit`은 한 번에 살펴볼 메타데이터 수이며 기본 100, 최대 1000입니다. `next_cursor`가 있으면 동일한 정책·작업 범위에 `--after-artifact-id <next_cursor>`를 넣어 다음 페이지를 처리하고 `null`까지 확인합니다. 새 순회는 커서를 비우고 시작합니다. 첫 페이지만 반복하면 보호된 초기 항목 뒤의 자료를 놓칠 수 있습니다.

출력은 `dry_run`, `scanned`, `counts`, 고정 사유별 `reasons`, `next_cursor`만 포함합니다. `eligible`은 dry run에서 적격, `protected`는 안전 검사에 의한 보존, `purged`는 삭제 완료 기록, `already_purged`는 동시 처리로 이미 완료된 대상입니다. 같은 파일의 여러 메타데이터는 한꺼번에 처리될 수 있어 `purged_aliases`는 `scanned`보다 클 수 있습니다. `bytes_removed`는 이번 실행에서 unlink한 바이트만 셉니다. 실패는 종료 코드 2이며, 그 전에 성공한 대상은 되돌리지 않습니다. 경로·파일명·DB 오류 원문은 출력하지 않습니다.

## 보호 조건과 두 단계 처리

다음 조건을 **두 트랜잭션에서 각각 잠그고 다시 확인**합니다.

- 작업이 `completed` 또는 `cancelled`이고 마지막 변경 이후 보존 기간이 지났습니다. 재시도 가능한 `failed`는 제외합니다.
- 할당 Worker가 격리되지 않았으며 소유자가 일치하는 임대가 명시적으로 `released`입니다. 시간만 지난 `active` 임대는 보호합니다. 한 번도 할당되지 않은 최종 작업은 Worker와 임대가 모두 없어야 합니다.
- DeliveryJob이 없거나 `completed`이고 유효한 Preview·Console 임대가 없습니다.
- 같은 키의 모든 메타데이터가 같은 작업에 속하며 크기·만료 상태가 일치하고 모두 보존 기간이 지났습니다. 별칭은 최대 10,000개입니다. 다른 작업의 별칭이나 최근 자료가 섞이면 삭제하지 않습니다.
- UUID·작업별 경로·일반 파일·최대 10 MiB·크기·SHA-256이 검증됩니다. 하위 심볼릭 링크, 특수 파일, 변경된 내용이나 도달할 수 없는 Root/부모 디렉터리는 실패합니다. 재귀 삭제는 없습니다.

첫 트랜잭션은 `expired_at`, `retention_days`, `retention_sha256`과 `artifact.expiration_requested` 감사를 함께 커밋합니다. 이때부터 새 다운로드는 차단됩니다. 두 번째 트랜잭션은 보호 조건과 동일한 만료 의도를 재확인하고 정확한 파일만 unlink·디렉터리 fsync한 뒤 `purged_at`과 `artifact.purged` 감사를 커밋합니다. 파일명·크기 등 메타데이터와 기존 감사는 유지합니다. 서비스 감사 주체는 `artifact:retention`, 제공자는 `urn:kelpie:service`, 전송은 `background`이며 조직·작업·요청 ID와 보존 정책·내용 해시를 기록합니다.

중간 실패는 만료 의도를 남기므로 **같은 보존 기간과 범위로 재실행**합니다. 이미 unlink된 파일은 부모 디렉터리가 실제로 도달 가능한 경우에만 완료 기록을 이어갑니다. 삭제 후 DB 커밋 실패도 바이트를 복원하거나 감사를 중복 생성하지 않습니다. 새 격리·전달·별칭 변경이 발견되면 보류하고 원인을 조사합니다. 보존 기간을 바꾸거나 만료 필드를 지워 보류를 우회하지 않습니다.

PostgreSQL 잠금 순서는 Worker → ResourceLease → Work → DeliveryJob → Preview/Console → Artifact이며 잠금 대기는 2초, SQL 문장은 15초로 제한합니다. SQLite는 `BEGIN IMMEDIATE`로 쓰기를 직렬화합니다. 파일 IO는 CLI 안에서 제한된 크기로 동기 실행하여 취소된 비동기 스레드가 DB 잠금 해제 후 삭제를 계속하지 않게 합니다. 느린 스토리지는 여전히 처리 지연을 만들 수 있으므로 작은 배치부터 운영합니다.

## API·화면·복원 호환성

기존 목록 응답에 `expired_at: null | timestamp`만 추가합니다. 저장소 키·해시·정리 정책은 공개하지 않습니다. 권한 검사 이후 만료된 파일은 HTTP 410과 `{"detail":"artifact retention period has expired"}`를 반환하고 `Cache-Control: no-store`를 유지합니다. 다른 조직·작업은 여전히 404, 미인증은 401입니다. 이미 사용자에게 전달된 사본을 원격 삭제하는 기능은 아닙니다.

한국어·영어 목록은 만료 배지·메타데이터·안내를 남기고 파일 열기 버튼을 제거합니다. 오래 열린 목록에서 410을 받아도 만료 상태로 전환하며 복구·재업로드를 유도하지 않습니다. 모달을 닫을 때 원래 버튼이 사라졌다면 검증 자료 제목으로 키보드 포커스를 돌려줍니다. 별도 만료 SSE 이벤트나 주기적 UI 새로고침은 추가하지 않으며, 다음 조회·열기에서 상태를 확인합니다.

[백업 V2](artifact-backup.md)는 완료된 만료 기록은 보존하되 파일 바이트를 복원하지 않고, 미완료 만료 의도가 있으면 백업을 거부합니다. V1은 대응하는 DB에 만료 기록이 없는 경우에만 호환됩니다. 만료 이전의 DB·파일 백업에는 이후 만료 사실이 없으므로 최신 만료·권한 기록 대조 없이 운영에 재공개하지 않습니다.

롤백은 우선 정리 실행을 중지하고 만료 읽기 차단·0010·감사 기록을 유지하는 전진 수정으로 진행합니다. 만료 기록이 있으면 0010 downgrade가 거부됩니다. 만료 필드 삭제, 이전 API로의 단순 롤백, 기존 Root 위 덮어쓰기, 오래된 파일의 무조건 복원은 안전한 롤백이 아닙니다.

## 검증 증거

검증 대상 구현은 `923425c`이며 아래 문서·증거 정리에서 실행 코드는 변경하지 않았습니다. 격리된 macOS 로컬 환경의 PostgreSQL 17, Chromium과 Orca 창을 사용했습니다.

- 실제 PostgreSQL을 포함한 `make test`에서 API 983개·Runner 6개·Web 91개와 Worker/Gateway Go 테스트가 통과했고 `make lint`도 통과했습니다.
- 파일 안전 검사 26개, 정리 상태·실패·재시도 52개, 실제 CLI·입력 경계 19개, 권한별 API 2개, 실제 PostgreSQL 정리·임대·격리 경합 16개를 검증했습니다.
- 실제 PostgreSQL dump/새 DB restore와 읽기 전용 파일 백업 CLI로 만료 기록·감사·ACL 보존, 최신 파일 복원, 만료 바이트 미복원을 확인했습니다. 별도 UUID DB·Schema·파일만 사용했습니다.
- `make test-web` 91개, Web lint·production build, Chromium 29개가 통과했습니다. 첫 전체 실행의 기존 404 화면에서 개발 모드 `Performance.measure` 오류가 한 번 관측됐고, 오류 검사 변경 없이 해당 테스트 3회와 전체 29개 재실행이 통과했습니다. 원인이 수정됐다고 주장하지 않으며 CI에서도 기존 오류 검사를 유지합니다.
- macOS Orca 실제 창에서 정리 전 파일 열기 → CLI 실행 → 새로고침 없는 재열기의 만료 안내 → Escape → 제목 포커스 복귀 → 정상 자료 열기 → 언어 전환을 확인했습니다. 1280px·390px에서 가로 넘침이 없고 배지/안내 명암 대비는 각각 6.34/5.70이었습니다. Computer-use로 실제 창 렌더링을 확인했지만 OS 포커스 제공자가 지원되지 않아 OS 입력·실제 VM 검증으로 간주하지 않습니다.
- 테스트용 일반 파일 35바이트만 정리했고 새 파일·다른 조직 자료는 유지됐습니다. 검증 후 쿠키·탭·전용 서비스와 임시 DB를 정리했습니다. 운영 데이터에는 적용하지 않았습니다.

[정리 전](../assets/artifact-retention/before.png) · [한국어 만료 목록](../assets/artifact-retention/after-ko.png) · [영어 목록](../assets/artifact-retention/after-en.png) · [모바일](../assets/artifact-retention/mobile-ko.png) · [재열기 안내](../assets/artifact-retention/expired-dialog-ko.png)
