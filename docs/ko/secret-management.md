# Secret 주입과 교체

한국어 | [English](../en/secret-management.md)

API는 `SecretProvider` 인터페이스의 환경변수·파일 구현을 사용합니다. 파일 경로를 설정하면 해당 환경변수 값보다 우선하며 **파일 오류 시 환경변수로 돌아가지 않습니다**. Worker 인증은 별도의 [개별 자격증명 전환 절차](worker-credentials.md)가 필요합니다. 개발용 기본 Secret을 운영에 사용하면 안 됩니다.

## 설정 계약

| API 환경변수 | 파일 경로 환경변수 | 읽는 시점 |
| --- | --- | --- |
| `OIDC_CLIENT_SECRET` | `OIDC_CLIENT_SECRET_FILE` | Authorization Code 교환마다 |
| `WORKER_SHARED_SECRET` | `WORKER_SHARED_SECRET_FILE` | 명시적 개발 Mode의 공유 Worker 인증 요청마다 |
| `GATEWAY_SECRET` | `GATEWAY_SECRET_FILE` | Gateway 인증 요청마다 |
| `GITHUB_WEBHOOK_SECRET` | `GITHUB_WEBHOOK_SECRET_FILE` | Webhook 서명 검증마다 |
| `SLACK_SIGNING_SECRET` | `SLACK_SIGNING_SECRET_FILE` | Slack 명령 서명 검증마다 |
| `SLACK_BOT_TOKEN` | `SLACK_BOT_TOKEN_FILE` | 알림·파일 업로드 작업마다 |
| 환경변수로 키 본문을 받지 않음 | 기존 `GITHUB_PRIVATE_KEY_PATH` | GitHub App JWT 서명마다 |

새 `_FILE` 설정의 기본값은 빈 문자열입니다. 실제 값이 아닌 절대 파일 경로를 설정하고 API 프로세스에 읽기 권한만 부여합니다. 파일은 UTF-8, 최대 64 KiB이며 마지막 CR/LF만 제거합니다. PEM 내부 줄바꿈은 보존합니다. 빈 파일, NUL, 잘못된 UTF-8, 일반 파일이 아닌 경로, 접근 오류는 거부합니다. Secret을 명령 인자, 이미지 레이어, Git 저장소, Artifact에 넣지 않습니다.

파일 내용은 캐시하지 않고 매번 다시 엽니다. 같은 파일에 쓰는 대신 동일 파일시스템의 임시 파일을 완성한 뒤 원자적으로 교체합니다. 심볼릭 링크 교체도 다음 읽기에 반영됩니다. 한 Slack 업로드의 여러 HTTP 요청에는 시작 시 읽은 동일 토큰을 사용합니다.

## 배포 방법

로컬/VM에서는 접근이 제한된 디렉터리에 Secret Manager가 파일을 만들게 하고, 전용 서비스 계정에 소유권과 `0400` 또는 `0600` 권한을 부여합니다. Container에는 디렉터리를 읽기 전용으로 Mount합니다. 예를 들어 배포 전용 Compose Override의 API 설정은 다음과 같습니다. 저장소의 기본 Compose는 여전히 격리된 개발 데모용입니다.

```yaml
services:
  api:
    environment:
      OIDC_CLIENT_SECRET_FILE: /run/secrets/kelpie/oidc-client
      GITHUB_WEBHOOK_SECRET_FILE: /run/secrets/kelpie/github-webhook
    volumes:
      - /secure/kelpie-secrets:/run/secrets/kelpie:ro
```

이 예시는 Secret 전달 부분만 보여줍니다. 운영 배포에는 별도로 `AUTH_MODE=oidc`, HTTPS, 권한 정책과 나머지 [운영 설정](operations.md)이 필요합니다. 실제 Secret 파일의 생성·배포는 승인된 Secret Manager에서 수행하고 이전 평문 환경변수는 제거합니다.

- Kubernetes: Secret Volume 디렉터리를 Mount하고 위 경로를 지정합니다. `subPath` Mount는 자동 갱신을 받지 않으며 Volume 갱신도 즉시 적용이 보장되지는 않습니다. [Kubernetes Secret 문서](https://kubernetes.io/docs/concepts/configuration/secret/)
- Vault: Vault Agent Template이 전용 파일을 렌더링하게 하고 해당 경로를 지정합니다. 갱신 주기·Vault 인증·Lease 수명 관리는 Agent/운영 환경의 책임입니다. [Vault Agent Template 문서](https://developer.hashicorp.com/vault/docs/agent-and-proxy/agent/template)

이 구현은 두 플랫폼이 주입한 파일을 읽는 어댑터입니다. Kubernetes/Vault API에 직접 연결하거나 외부 플랫폼을 구축하지 않습니다. 실제 클러스터·Vault에서의 운영 검증은 별도로 필요합니다.

## 교체, 실패 및 복구

1. 공급자 쪽에서 새 자격증명을 준비합니다. 진행 중인 요청이 끝날 시간을 고려해 이전 자격증명의 폐기 시점을 정합니다.
2. Secret Manager가 파일을 원자적으로 교체하게 합니다. API 재시작은 필요하지 않습니다.
3. 테스트 요청으로 새 값의 수용·이전 값의 거부를 확인한 뒤 공급자 쪽 이전 값을 폐기합니다. 값을 화면·로그에 출력하지 않습니다.
4. 파일을 읽을 수 없으면 관련 API 요청은 `503`, `Cache-Control: no-store`, `{"detail":"configured secret is unavailable"}`를 반환합니다. 경로와 파일 내용은 응답에 포함하지 않습니다. OIDC Secret 파일이 없어져도 공개 Client 인증으로 낮추지 않습니다.
5. 복구는 유효한 파일을 다시 배치하고 같은 프로세스에서 요청을 재확인합니다. 설정을 지워 개발용 기본 Secret으로 돌아가는 방식은 사용하지 않습니다.

Webhook과 Gateway는 현재 Secret 하나만 검증하므로 교체에는 소비자와의 조율이 필요합니다. Gateway는 환경변수를 시작 시 읽고 Worker는 토큰 파일을 매 요청 다시 읽습니다. Worker별 중첩 교체·개별 폐기는 [Worker 관리 가이드](worker-credentials.md)를 따릅니다. 사용자 Session과 작업 Lease는 일반 Secret 교체로 폐기되지 않습니다.

파일 Provider 자체에는 Schema 변경이 없지만 Worker별 인증에는 `20260905_0005` Migration이 필요합니다. [Worker Rollback 제한](worker-credentials.md#migration과-rollback)을 따릅니다. 파일·개별 인증을 지원하지 않는 이전 버전을 개발 기본값과 함께 운영에 노출하면 안 됩니다.

## 검증과 남은 범위

- `make test-api`: 파일 우선순위, 원자적·Projected Volume 방식 교체, 오류 차단, OIDC 캐시를 거친 교체, Webhook·Slack·GitHub 소비자를 검증합니다.
- `test_secret_runtime.py`: 실제 Uvicorn/SQLite/HTTP 프로세스에서 교체 → 이전 값 401 → 새 값 성공 → 파일 누락 503 → 복구 성공을 재시작 없이 검증합니다. 생성한 테스트 토큰이 API 로그와 DB에 없는지도 검사합니다.
- 지원하는 여섯 평문 Secret 설정은 Settings의 기본 repr/직렬화에서 제외합니다. 이것은 전체 로그·프로세스 메모리·Crash Dump의 Secret 비노출을 보장하는 기능이 아닙니다.
- OIDC/Slack 네트워크 소비자 테스트는 모의 공급자를 사용합니다. [Worker 제어 영역 격리](worker-quarantine.md)는 구현했지만 실제 Host/VM/네트워크·기존 연결 차단, 실제 외부 계정, 전체 Event/Artifact/Crash Dump/cloud-init 스캔 및 운영 Secret 정책은 남아 있습니다. SEC-001 전체 완료로 표시하지 않습니다.
