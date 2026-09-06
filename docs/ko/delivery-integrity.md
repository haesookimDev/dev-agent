# 전달 패치의 실제 바이트 검증

한국어 | [English](../en/delivery-integrity.md)

## 보호하는 경계

DB의 SHA-256이 승인 기록과 같아도 저장된 파일은 유실되거나 바뀔 수 있습니다. 다운로드와 웹·서명된 Slack의 중앙 전달 승인에서는 실제 파일의 크기·SHA-256을 검증합니다. 전달 시도와 시작 시 복구도 Token 발급 전에 승인 Hash로 다시 검증합니다. 새 Git 작업에는 그때 읽은 바이트만 별도 사본으로 적용하므로, 원본 경로를 나중에 바꿔도 전달 내용이 바뀌지 않습니다.

- 기존 업로드 상한과 같은 20MiB, 1바이트 이상의 일반 파일만 허용합니다. 잘못된 Hash·크기, 읽는 도중 변경, 누락, 디렉터리·FIFO, 하위 심볼릭 링크와 `..`/Root 밖 경로를 거부합니다.
- 운영자 설정 `ARTIFACT_ROOT`를 연 후 각 하위 경로를 부모 파일 Descriptor 기준으로 `O_NOFOLLOW`와 함께 엽니다. 검사 후 원래 경로를 다시 여는 방식이 아닙니다. Root 자체의 운영자 관리 Alias는 허용하지만 하위 링크는 허용하지 않습니다. [Python 파일 Descriptor API](https://docs.python.org/3/library/os.html#os.open)
- 메모리에 검증한 바이트를 기존 비공개 임시 작업 폴더(0700)의 Checkout 밖 `approved.patch`(0600)에 배타적으로 생성합니다. Git은 이 사본만 읽고 기존 `finally` 정리 경로가 제거합니다. 변경된 원본을 자동 복구하거나 덮어쓰지는 않습니다.
- 기존 승인 신원·조직·저장소·버전·Hash 재검사, Worker 격리, 쓰기 잠금과 제한 시간은 유지합니다. Mock Worker의 중앙 전달 생략은 기존 동작이며 실제 Git 전달 검증이 아닙니다.

이 검증은 신뢰된 DB/승인과 현재 로컬 저장 경계에 대한 검증입니다. 같은 OS 계정이나 Root 권한의 악성 프로세스로부터 완전한 격리를 보장하지 않습니다. Hash는 암호화·작성자 서명이 아니며, 기존 원격 Branch/PR의 Tree Attestation도 아닙니다. 이미 발생한 외부 쓰기는 DB와 원자적으로 Rollback되지 않습니다.

## API·이벤트 호환성

요청 형태는 그대로입니다: `POST /api/work-items/{id}/approvals`에 `{"kind":"pull_request","decision":"approve"}`를 전송합니다. 실제 파일이 잘못되면 `409 {"detail":"delivery bundle is unavailable or invalid"}`를 반환하고 승인·작업 버전·이벤트·감사·전달 Job을 생성하거나 변경하지 않습니다. 원래의 권한·격리·상태·설정 검사가 우선합니다. 반려와 예산 승인의 의미는 바뀌지 않습니다.

`GET /api/work-items/{id}/delivery-bundle`는 검증된 Patch만 200으로 반환합니다. DB 행이 없으면 기존 404, 파일 누락·변조·허용되지 않는 경로이면 `410 {"detail":"delivery bundle is unavailable"}`입니다. 다른 조직은 여전히 404입니다. 원본 경로·파일 내용·실제 Hash·OS 오류를 응답하지 않습니다.

승인 후 파일이 손상되면 전달 Job/작업은 기존 실패 경로를 따릅니다. `delivery.failed` 이벤트와 감사에 `stage=bundle`, `error_code=bundle_unavailable` 또는 `bundle_integrity_failed`를 기록합니다. 후자는 크기·Hash·읽기 도중 변경을 뜻합니다. 감사의 `authorization=denied`에는 확인된 원래 승인 참조·Hash가 남을 수 있습니다. `delivery.started`는 승인 메타데이터 확인이지 이후 바이트 검증 성공의 증명이 아닙니다. `publication=not_started`는 이번 시도의 외부 호출 전 실패이지 과거 시도의 원격 쓰기 부재를 증명하지 않습니다.

Web은 기존 번역된 일반 승인 오류와 HTTP 409를 표시합니다. 코드/Schema·Worker·Runner·Gateway 계약, 환경변수·기본값·의존성·Migration은 추가하지 않습니다. 외부 이벤트 소비자의 코드 Allowlist에는 `bundle` 단계와 `bundle_integrity_failed`를 허용해야 합니다. [안전한 오류 계약](delivery-failure-safety.md), [승인 감사](delivery-audit.md)

## 복원·운영

1. [PostgreSQL 복원 Gate](postgres-restore.md)처럼 Writer·외부 전달을 중지·격리하고 DB와 대응 파일 Snapshot을 함께 보존합니다. DB만 복원해서 API를 재시작하지 않습니다.
2. 현재 `DeliveryBundle.object_path`는 저장 당시 경로입니다. API에 같은 `ARTIFACT_ROOT`와 저장 당시 경로의 정상 파일을 제공해야 합니다. 이전 Root의 파일이나 하위 심볼릭 링크로 자동 우회하지 않습니다. 경로 이전 자동 Migration과 일반 Artifact의 Hash 검증·Object Store 전체 복구는 이 변경에 포함되지 않습니다.
3. 승인 전 손상이면 신뢰된 Backup의 정확한 바이트를 복원하고 검증·사용자 승인을 다시 진행합니다. 검사에 맞추려고 DB Hash·감사·Job 링크를 고치거나 검사 코드를 끄지 않습니다. 승인 후 실패한 Job의 임의 상태 복원·자동 재시도는 지원하지 않습니다. 원격 결과를 대조한 뒤 새 검증 작업과 명시적 승인으로 진행합니다.
4. 보안 수정 전으로 Rollback하면 취약한 경로가 다시 열립니다. 문제가 있으면 전달을 중지한 상태에서 수정 버전을 배포하고, 이전 버전으로 전달을 재개하지 않습니다. 파일 읽기·Hash 비용은 요청당 최대 20MiB이며 대용량 동시 요청과 실제 저장소 장애의 운영 부하 검증은 별도입니다.

## 검증 · 2026-09-06

실행 코드·회귀 테스트 Commit은 `2d10f73`입니다.

- 수정 전 21개 회귀가 실패했습니다. 수정 후 유틸리티 23개, 웹·Slack 거부 10개, 전달/복구/사본 11개, 추가 실제 HTTP/Git 회귀 1개가 통과했습니다. 기존 실제 Git 실패의 비공개 경로 비노출 검증도 유지합니다.
- 전용 PostgreSQL 17 DB에서 `make test`: API 571개(Skip 없음), Runner 6개, Worker·Gateway, Web 52개·TypeScript 통과. `make lint`, 운영 Web 빌드와 `make test-monitoring`의 10개 Rule 검증도 통과했습니다. 기본 `make test-api`는 526개 통과·PostgreSQL 전용 45개 Skip입니다.
- 실제 Uvicorn·새 SQLite Migration·Git clone/apply/commit/push·루프백 SCM에서 손상 파일의 다운로드 410/승인 409와 원본 복원 후 승인을 확인합니다. Token 응답 시 원본을 다시 변조해도 원격 Git의 내용은 승인된 내용이고 PR은 한 번 생성되며 임시 사본은 정리됩니다. 실제 GitHub App·IdP·Slack·KVM 운영 검증은 아닙니다.
- 같은 Fixture의 API `:18490`와 운영 Web 빌드 `:13490`에서 Orca 브라우저의 한·영 승인 버튼으로 손상 거부를 확인했습니다. 복원 후 영어 승인 → 완료/100%/PR 링크/피드백 종료를 확인했고, 실제 원격 Git 내용·감사·Token/PR 횟수도 대조했습니다. 1035px에서 가로 넘침은 없었습니다. Computer Use로 실제 데스크톱의 한국어 거부·영어 완료 화면을 확인했습니다. OS Focus가 제공되지 않아 네이티브 키보드 검증은 주장하지 않습니다. UI 코드는 변경하지 않았습니다.

새 테스트는 기존 필수 `Python` CI에 포함되며 서비스·Matrix·중복 빌드를 추가하지 않습니다. 최종 Head CI와 Merge SHA는 PR에 기록합니다. 일반 Artifact 물리 복구, 보존/Janitor, 실행 중 VM 정리와 실제 KVM/네트워크/입력 격리는 계속 남아 있어 MVP 전체 완료로 표시하지 않습니다.
