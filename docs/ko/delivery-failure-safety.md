# 전달 실패의 안전한 진단

한국어 | [English](../en/delivery-failure-safety.md)

Git 명령의 출력·인자·파일 경로와 외부 서비스 예외에는 토큰이나 저장소의 비공개 내용이 섞일 수 있습니다. API의 전달 실패 처리에서는 예외 원문을 `DeliveryJob.error`, `delivery.failed` 이벤트, `delivery.run` Trace에 복사하지 않습니다. 문자열 패턴을 찾아 가리는 대신 코드가 정한 단계와 오류 분류만 기록합니다.

## 이벤트 계약

승인 요청 `POST /api/work-items/{id}/approvals`의 형식과 응답은 바뀌지 않습니다. 승인 성공은 결정이 저장됐다는 뜻이며 비동기 전달 성공을 보장하지 않습니다. 이후 실패하면 작업과 전달 Job은 `failed`가 되고 기존 권한 검사를 거친 SSE `GET /api/work-items/{id}/events`에서 다음 이벤트를 받습니다.

```json
{
  "event_type": "delivery.failed",
  "source": "delivery:github",
  "level": "error",
  "message": "GitHub delivery failed at apply (command_failed)",
  "payload": {"stage": "apply", "error_code": "command_failed"}
}
```

예시는 ID·시간 등 기존 공통 필드를 생략했습니다. `message`는 같은 고정 문구로 DB에도 저장됩니다. API 이벤트 문구·코드 식별자는 한국어·영어 대시보드에서 동일하게 표시되며 UI 번역 키가 아닙니다. 이벤트 형식은 유지하고 Payload 필드 두 개를 추가합니다. 원래의 자유 형식 예외 문구를 파싱하던 외부 소비자는 `payload.stage`와 `payload.error_code`로 전환해야 합니다. 기존 이벤트의 Payload에는 이 필드가 없을 수 있습니다.

| 오류 코드 | 의미 |
| --- | --- |
| `command_failed` | Git 하위 프로세스 시작 또는 실행 실패 |
| `timeout` | 전달 제한 시간 또는 HTTP Client 제한 시간 초과 |
| `upstream_error` | 그 외 HTTP Client 오류 |
| `filesystem_error` | 그 외 OS/파일시스템 오류 |
| `internal_error` | 분류되지 않은 오류 |
| `bundle_unavailable` | 전달 Bundle 누락 또는 안전하게 읽을 수 없는 파일·경로 |
| `bundle_integrity_failed` | 전달 Bundle 크기·Hash 불일치 또는 읽기 도중 변경 |

[실제 바이트 검증](delivery-integrity.md)은 Token 발급 전 `bundle` 단계를 추가합니다. 승인 출처 검사의 `authorization` 단계와 기존 코드 `approval_unavailable`, `approval_mismatch`는 [전달 감사](delivery-audit.md)를 따릅니다.

단계는 `authorization`, `configuration`, `bundle`, `token`, `metadata`, `existing_pull_request`, `existing_branch`, `workspace`, `clone`, `checkout`, `apply`, `commit`, `push`, `pull_request`, `finalize` 중 진행 중이던 작업입니다. 임시 폴더 정리 오류는 마지막 Git 단계에 포함됩니다. Trace에는 `kelpie.delivery.stage`, `kelpie.delivery.error_code`, 안전한 오류 상태와 대체 예외를 기록하고 원본 예외의 연결된 원인·Stack Trace는 내보내지 않습니다. Work/Correlation ID와 기존 시도·결과 Metric은 유지합니다.

## 운영과 호환성

- Work/Correlation ID, 단계, 오류 코드로 먼저 범위를 좁힙니다. 예를 들어 `apply` 실패는 승인된 Patch와 기준 브랜치의 일치 여부를 접근이 제한된 환경에서 확인합니다. 원본 토큰·명령 출력·비공개 Patch를 이슈나 공유 로그에 붙여 넣지 않습니다.
- `run_command`의 성공 출력은 유지합니다. 실패 예외에는 종료 코드 또는 시작 실패 고정 문구만 남기며 인자·stdout·stderr는 포함하지 않습니다. 취소 시 해당 프로세스 그룹을 종료하는 기존 동작은 유지합니다.
- 승인 게이트, Worker 격리, 쓰기 제한 시간과 중복 PR 방지는 변경하지 않습니다. 이미 격리된 Job을 일반 실패 상태로 덮어쓰지 않습니다.
- 새 환경변수·의존성·Schema Migration은 없습니다. API 코드를 배포해야 이후 실패 기록에 적용됩니다. 과거 DB·이벤트·외부 Trace를 자동 수정하거나 삭제하지 않습니다. 과거 노출이 의심되면 별도 사고 대응 절차에 따라 접근 범위 확인과 자격증명 폐기를 수행합니다.
- 이전 코드로 Rollback하면 원문 노출이 다시 발생합니다. 문제가 있으면 전달 처리를 중지하고 수정 버전을 배포하며, 취약한 버전의 전달 처리를 공개 운영 상태로 재개하지 않습니다.

## 검증 증거

`6ea6a7f` 구현에서 다음을 확인했습니다.

- 수정 전 8개 회귀 테스트가 실패했습니다. 수정 후 실제 하위 프로세스 출력, 경로, 연결된 예외 원인, HTTP 오류·Timeout이 DB·이벤트·메모리 Trace에 남지 않는지 검증했습니다.
- 실제 Uvicorn·마이그레이션한 임시 SQLite·운영 Next.js 빌드·OTLP HTTP 수집기를 실행했습니다. 모의 GitHub HTTP 서버와 로컬 Git 저장소를 사용했으며 실제 GitHub 계정·VM은 사용하지 않았습니다.
- Scoped Worker 프로토콜로 작업을 승인 대기까지 진행하고 Patch를 업로드했습니다. 승인 전 전달 토큰 발급은 0회였습니다. Orca 브라우저의 승인 버튼을 직접 눌렀고 실제 `git clone`·`checkout` 후 `git apply` 실패가 발생했습니다. 승인 감사는 1건, 전달 실패 이벤트는 1건, PR 생성은 0건이었습니다.
- Git 원본 오류에 합성 비공개 경로가 포함됨을 먼저 확인했습니다. 실제 실패 처리 후 DB 오류·이벤트·전송된 OTLP·API 로그에 그 값과 테스트 전달 토큰이 없었고 임시 Git 폴더도 정리됐습니다.
- 한국어·영어 화면에서 안전한 오류와 실패 상태를 직접 확인했습니다. 1035px 브라우저 너비에서 가로 넘침과 콘솔 오류가 없었고 승인·상태 조회 요청은 200이었습니다. 데스크톱 도구에는 표시 가능한 창이 없어 네이티브 조작은 수행하지 못했습니다. 네이티브/UI 코드는 변경하지 않았습니다.
- 재실행: `make test-api`와 `make lint`. `test_delivery_disclosure.py`에는 외부 네트워크 없이 실제 Git 복제·Patch 실패를 검증하는 회귀 테스트도 포함합니다. PostgreSQL 전용 테스트는 `KELPIE_TEST_POSTGRES_URL`에 격리된 테스트 DB를 지정합니다.
- 실제 Git 회귀 테스트를 추가한 `da49e80`에서 PostgreSQL을 포함한 API 342개 테스트(26초)와 전체 `make lint`가 통과했습니다. 운영 Web 빌드도 통과했습니다.

검증 후 직접 만든 서버·브라우저 탭·임시 DB·키·토큰·저장소를 정리했습니다. 이 변경은 전달 실패 기록이라는 한 경계를 보호합니다. 모든 로그·Artifact·Crash Dump·cloud-init·DB 장애 경로의 비노출이나 SEC-001 전체 완료를 보장하지 않습니다. 남은 범위는 [Secret 관리](secret-management.md)와 [MVP 로드맵](roadmap-summary.md)을 따릅니다.
