# 작업별 로컬 산출물 저장 경계

한국어 | [English](../en/artifact-isolation.md)

## 보호 범위

산출물 메타데이터가 작업에 속한다는 사실만으로 파일의 소유권을 증명할 수는 없습니다. 등록·다운로드·업로드 모두 정확히 `<work-id>/artifacts/<파일 또는 하위 경로>`만 허용합니다. 다른 작업, 비슷한 ID 접두사, 같은 작업의 `delivery.patch`, Root 파일, 빈 경로 요소, `.`, `..`, 역슬래시와 NUL은 허용하지 않습니다. 기존 조직·저장소 권한, 작업별 Lease와 Worker 격리 검사는 유지합니다.

- 운영자가 지정한 `ARTIFACT_ROOT`를 연 뒤 하위 디렉터리를 부모 File Descriptor 기준 `O_NOFOLLOW`로 순회합니다. Root 자체의 운영자 관리 Alias는 허용하지만 하위 심볼릭 링크는 거부합니다. 경로를 먼저 검사하고 원래 문자열로 다시 열지 않습니다.
- 쓰기는 같은 부모 Descriptor에 0600 임시 파일을 배타적으로 생성하고 원자적으로 교체합니다. 새 하위 디렉터리는 0700입니다. 실패 시 해당 임시 파일만 정리하고 기존 파일은 보존합니다. 최종 파일이 링크이면 대상 파일을 따라 쓰지 않고 링크 자체를 교체합니다. [Python Descriptor·원자적 교체 API](https://docs.python.org/3/library/os.html#os.replace)
- 읽기는 일반 파일만 허용하고 실제 크기를 최대 10MiB로 제한합니다. FIFO·디렉터리·링크·누락·읽는 중 크기/수정 정보 변경은 거부합니다. 기존 업로드의 비어 있지 않은 콘텐츠·10MiB·형식 검증은 유지합니다.

이 경계는 신뢰된 운영 Root와 API/DB 소유권을 전제로 합니다. 같은 OS 계정 또는 Root 권한의 악성 프로세스까지 완전히 격리하지 않습니다. 일반 Artifact의 Hash, 메타데이터 `size_bytes` 일치, 암호화, Slack 재전송 파일의 불변성, Object Store 물리 복원은 이번 범위가 아닙니다. MIME과 실행 문서 차단은 후속 [콘텐츠 정책](artifact-content.md)에서 다루며 파일명 표시와 HTTP 캐시는 별도 후속 작업입니다.

## API 호환성

요청·응답 Schema와 URL은 유지합니다. Runner는 기존 `/api/runs/{work-id}/artifacts/upload`를 사용하므로 키 생성 방식을 바꿀 필요가 없습니다. 새 설정·기본값·의존성·DB Migration은 없습니다.

- `POST /api/runs/{work-id}/artifacts`: 예를 들어 `{"kind":"evidence","name":"test.txt","content_type":"text/plain","object_key":"<work-id>/artifacts/test.txt","size_bytes":12}`는 기존처럼 201입니다. 유효한 Lease라도 다른 저장 범위의 키는 `422 {"detail":"artifact key must belong to this work"}`이며 메타데이터·이벤트를 만들지 않습니다. Schema 자체가 잘못된 경로를 거부하면 기존 422 검증 응답이 먼저 적용됩니다.
- `POST /api/runs/{work-id}/artifacts/upload`: 정상 업로드는 201입니다. 저장 경로의 링크·파일시스템 오류는 `503 {"detail":"artifact storage is unavailable"}`이며 메타데이터·이벤트를 발행하지 않습니다. 실제 경로·OS 오류·파일 내용은 응답하지 않습니다.
- `GET /api/work-items/{work-id}/artifacts/{artifact-id}`: 정상 파일은 200입니다. 보존된 메타데이터라도 범위 밖 키·링크·읽을 수 없는 파일이면 `410 {"detail":"artifact content is unavailable"}`입니다. 다른 조직·다른 작업의 메타데이터는 기존처럼 404입니다. 잘못된 기존 행을 자동 삭제하거나 고치지 않습니다.

## 운영·복원

기존 업로드 키는 호환됩니다. 직접 등록한 범위 밖 키나 하위 링크에 의존하던 데이터는 이제 조회되지 않습니다. 배포 전에 DB와 대응 파일을 함께 보존하고 키를 검토하세요. 정당한 파일의 소유 작업과 출처를 운영자가 확인한 뒤 해당 작업의 실제 `artifacts` 디렉터리에 복원해야 합니다. 다른 작업의 파일을 복사하거나 ID만 바꿔 검사를 통과시키지 않습니다. 자동 키 이전 도구는 제공하지 않습니다.

복원 시 [DB 복원 Gate](postgres-restore.md)를 유지하고 Writer·외부 전달을 격리합니다. DB만 복원한 상태를 성공으로 취급하지 않습니다. 기존 파일 권한을 일괄 변경하거나 자료를 자동 제거하지 않습니다. 이전 취약 버전으로 롤백해 산출물 제공을 재개하지 말고, 해당 제공 경로를 중지한 상태에서 수정 버전으로 복구합니다.

## 검증 · 2026-09-06

실행 코드·회귀 테스트 Commit: `f179313`.

- 수정 전 작업 간 참조·링크 공격 회귀 9개가 실패했고 수정 후 통과했습니다. 파일 저장 23개, API 격리 10개, 실제 HTTP 1개 회귀를 추가했습니다.
- 전용 PostgreSQL 17 DB에서 `make test`: API 605개(Skip 없음), Runner 6개, Worker·Gateway, Web 52개·TypeScript 통과. `make lint`, 운영 Web 빌드, `make test-monitoring`의 10개 Rule도 통과했습니다.
- 실제 Uvicorn·새 SQLite Migration·Scoped Worker 등록/Claim/업로드를 실행합니다. 두 조직의 테스트용 OIDC Session으로 정상 조회, 작업 간 등록 422, 보존된 잘못된 키·링크 조회 410, 링크를 통한 업로드 503을 검증합니다. 실제 IdP 로그인·VM·SCM·Slack 운영 검증은 아닙니다.
- 동일 Fixture의 API `:18500`와 Next.js Standalone 운영 빌드 `:13500`에서 Orca 브라우저의 산출물 링크를 클릭해 정상 텍스트와 고정된 거부 응답을 확인했습니다. 한·영 상세 화면, 조직 변경 후 목록 격리와 접근 불가 화면, 새 네트워크 요청의 404를 확인했습니다. 1035px 가로 넘침은 없었고 Computer Use로 한국어 상세·영어 접근 거부 데스크톱 화면을 확인했습니다. 네이티브 Focus가 없어 OS 키보드 검증은 주장하지 않습니다. UI 코드는 변경하지 않았습니다.
- 최초 브라우저 진입은 Web 준비 전이라 실패했고 준비 확인 후 정상 진입했습니다. 직접 열었던 410 응답의 캐시로 교차 Origin 재조회가 실패하는 현상은 새 요청에서 정상 410임을 대조했습니다. 캐시·콘텐츠 표시 보호는 독립된 후속 PR로 남깁니다. 화면 Console에는 추가 메시지가 없었습니다.

새 회귀는 기존 필수 `Python` CI에서 실행하며 별도 서비스·Matrix·중복 빌드를 추가하지 않습니다. 최종 Head CI·Merge SHA는 PR에 기록합니다. 일반 산출물 물리 복구, 보존/Janitor와 실제 KVM/네트워크/입력 격리는 남아 있으므로 전체 MVP는 미완료입니다.
