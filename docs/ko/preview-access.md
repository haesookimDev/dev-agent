# OIDC HTTP Preview 접근

한국어 | [English](../en/preview-access.md)

## 동작과 보안 경계

작업 상세 화면의 **앱 미리보기**는 현재 OIDC 세션에 연결된 일회용 허가를 받아 실행 중인 웹 앱을 새 탭에 엽니다. Viewer 이상의 현재 조직·저장소 권한이 필요합니다. 로딩, 준비되지 않은 상태, 통신·권한 오류, 팝업 차단과 재시도를 한국어·영어로 제공합니다. 기능을 구성하지 않은 환경에는 버튼을 표시하지 않습니다.

- Launch Code는 최대 30초 동안 한 번만 교환할 수 있습니다. URL·브라우저 저장소에 넣지 않고 HTTPS Form POST Body로 전달합니다.
- 접근 Token은 작업 Hostname과 로그인 세션에 한정되며 최대 5분, 부모 세션·Preview 만료 중 가장 이른 시점까지 유효합니다. `__Host-kelpie_preview` Cookie는 Secure, HttpOnly, SameSite=Strict, Path=/, Domain 없음으로 설정합니다. 새 탭의 `opener`는 제거합니다.
- DB에는 무작위 Code·Token의 SHA-256 Hash만 저장합니다. `preview.granted` 감사에는 Actor·현재 Role 결정·작업·Hostname만 기록하며 자격증명은 기록하지 않습니다.
- 모든 요청에서 현재 세션, Issuer, Membership, 저장소 소유권, Worker 격리, Endpoint 만료와 허용 CIDR을 다시 확인합니다. PostgreSQL에서 Worker 잠금과 조건부 UPDATE로 격리 경합·동시 교환을 검증합니다.
- 기존 HTTP Stream/WebSocket은 2초마다 권한을 다시 확인합니다. 확인 요청의 2초 Timeout까지 포함해 정상적인 스케줄링에서 약 4초 이내에 권한 회수·대상 변경·제어 API 장애로 연결을 닫으며, 허가 만료 시에도 닫습니다. 이미 내려받은 콘텐츠는 회수할 수 없습니다.
- Gateway는 플랫폼 인증·Cookie·제어 Header를 대상 앱에 전달하지 않습니다. 예약 Cookie 이름을 차단하고 앱 Cookie의 Domain을 제거하며 Secure를 설정합니다. 외부 Origin으로의 HTTP Redirect, Cross-Origin 요청, Service Worker·Web Worker를 차단합니다.

이 기능은 **HTTP Preview와 그 웹 앱의 WebSocket**만 제공합니다. 앱 자체의 쓰기 동작을 읽기 전용으로 바꾸는 기능이 아닙니다. `/console`은 OIDC Gateway에서 차단됩니다. 실제 KVM·WireGuard 격리 및 noVNC 입력 소유권은 별도 MVP 항목입니다.

## 설정과 적용 순서

기본값은 API `PREVIEW_ACCESS_ENABLED=false`, Gateway `KELPIE_GATEWAY_AUTH_MODE=disabled`입니다. 기존 개발용 Compose Profile은 그대로 개발용이며 운영 OIDC 설정으로 자동 전환하지 않습니다.

1. DB Backup 후 `alembic -c apps/api/alembic.ini upgrade head`와 `check`를 실행합니다. Revision `20260906_0008`이 세션 FK와 만료·일회용 교환 인덱스를 가진 `preview_grants` Table을 추가합니다.
2. [OIDC·IAM](operations.md#oidc-인증)을 구성합니다. 대시보드/API는 같은 공개 HTTPS Origin을 사용하고 Preview에는 **다른 Site의 새 전용 Domain**을 할당합니다. 예: `control.example.com`과 `preview.example.net`. 이전 개발 Preview에서 Service Worker가 설치된 Domain은 재사용하지 않습니다.
3. Wildcard DNS·인증서, Gateway에서만 접근할 수 있는 사설 Target Network를 구성합니다. `PREVIEW_ALLOWED_CIDRS`는 필요한 VM Subnet만 허용합니다. 테스트의 Loopback CIDR을 운영에 복사하지 않습니다.
4. API에는 아래 설정을 적용합니다. 인증서와 Gateway Token은 Secret Manager/보호된 Mount로 주입합니다. 실제 값은 문서·저장소·화면에 넣지 않습니다.

| 설정 | 기본값 | 용도 |
| --- | --- | --- |
| `PREVIEW_ACCESS_ENABLED` | `false` | OIDC Grant 발급·해석 활성화 |
| `PREVIEW_DOMAIN` | `preview.localhost` | 작업별 Hostname의 전용 접미사 |
| `PREVIEW_HTTPS_PORT` | `443` | Browser가 접근하는 외부 TLS Port, 1–65535 |
| `PREVIEW_ALLOWED_CIDRS` | `10.0.0.0/8` | IP Literal HTTP Target의 허용 Network; 운영에서는 축소 |
| `KELPIE_GATEWAY_AUTH_MODE` | `disabled` | `oidc`로 설정해야 인증 Gateway 활성화 |
| `KELPIE_GATEWAY_TLS_CERT_FILE` / `KELPIE_GATEWAY_TLS_KEY_FILE` | 빈 값 | `oidc`에서 둘 다 필수 |
| `KELPIE_GATEWAY_LISTEN` | `:8080` | Gateway의 TLS Listen 주소; 외부 Port와의 매핑은 배포에서 구성 |
| `GATEWAY_SECRET` 또는 `GATEWAY_SECRET_FILE` / `KELPIE_GATEWAY_TOKEN` | 빈 값 | API와 Gateway 전용의 동일한 32자 이상 Secret |

TLS는 Gateway에서 직접 종료하거나 TCP Pass-through를 사용합니다. HTTP TLS Offload와 `X-Forwarded-Proto`는 허용하지 않습니다. `KELPIE_CONTROL_URL`은 신뢰된 내부 API 경로로 제한하고 네트워크를 격리합니다. API Redirect를 따라 인증 정보를 보내지 않습니다.

Domain 검사는 보수적으로 마지막 두 DNS Label이 대시보드/API와 같은 경우도 거부합니다. Public Suffix List가 아니므로 `co.uk` 같은 공통 접미사는 서로 다른 등록 Domain도 거부할 수 있습니다. 조건을 만족하는 별도 Domain을 사용하며 검사를 우회하지 않습니다. Origin 또는 Fetch Metadata를 제공하는 최신 브라우저가 필요합니다. Worker·예약 Cookie·외부 Redirect 제한 때문에 모든 웹 앱과 완전히 호환되지는 않습니다.

## API 계약

현재 OIDC Cookie로 `GET /api/work-items/{id}/preview-access`를 호출하면 `{ "available": true, "expires_at": "<UTC expiry>" }`, 또는 `{ "available": false, "reason": "not_configured" | "unavailable" }`를 반환합니다. 사설 Target 주소는 노출하지 않습니다.

대시보드 Origin에서 `POST /api/work-items/{id}/preview-grants`를 호출하면 HTTP 201과 아래 형태를 반환합니다. 아래는 비밀정보가 없는 자리표시자 예시입니다.

```json
{
  "launch_code": "<one-time code>",
  "exchange_url": "https://<work-id>.preview.example.net/_kelpie/authorize",
  "expires_at": "<launch expiry>"
}
```

Gateway의 `/_kelpie/authorize`에 `application/x-www-form-urlencoded`의 단일 `code` 필드로 POST합니다. 실제 대시보드 Origin이 필요하며 Query Parameter는 거부합니다. Gateway가 내부 `/internal/previews/exchange`와 `/internal/previews/authorize`를 서비스 인증으로 호출합니다. 브라우저에 서비스 Secret을 전달하거나 내부 API를 직접 공개하지 않습니다. 허가가 없거나 만료되면 401, 권한·Origin 불일치는 403/404, Endpoint 소멸·격리는 410, 비활성·Console은 503으로 차단될 수 있습니다. 새 계약은 추가형이며 기존 개발 Preview 경로는 유지합니다.

## 검증과 제한

- `make test`: PostgreSQL을 포함한 API 409개, Runner 6개, Worker·Gateway, Web 49개와 타입 검사 통과. `make lint`, Gateway `go test -race ./...`, Web 운영 빌드 통과.
- API Preview 56개, PostgreSQL Preview 24개: Viewer 접근, 세션·Membership·Endpoint·CIDR 회수, TTL 상한, 재사용/동시 교환, 실제 PostgreSQL 양방향 잠금 경합.
- `npm run test:e2e:preview --prefix apps/web`: 실제 OIDC Discovery/JWKS/RS256·PKCE와 TLS API·Gateway·Next.js·HTTP/WebSocket Fixture를 매번 실행합니다. 개발 서버 4개 여정 약 22초, `99bae1f` 운영 빌드의 별도 실행 약 15초에 통과했습니다.
- 한국어·영어, 1440px·390px, 키보드 포커스, 가로 넘침, 로딩·팝업·권한 오류·복구, 새 탭의 Opener 제거, URL/Storage/Cookie 비노출, 실제 WebSocket 연결 및 로그아웃 후 종료/401을 확인했습니다. 정상 UI 여정의 처리되지 않은 브라우저 오류는 없었습니다.

검증 Fixture는 일회용 인증서·Secret을 임시 디렉터리에 생성하고 정리합니다. OS 신뢰 저장소는 변경하지 않습니다. `13443/13530`, `18443/18530`, `19443/19330`, `14443`, `16330`을 비워 두고 같은 체크아웃의 Next 서버를 종료한 후 실행합니다. CI는 이 테스트를 기존 Web Job에 추가하며 OIDC 자격증명이 들어갈 수 있는 Trace는 저장하지 않습니다. 자체 실행한 격리 환경을 재사용할 때만 로컬에서 `KELPIE_PREVIEW_REUSE=1`을 지정할 수 있고 CI에서는 무시합니다.

실제 기업 IdP, 공개 DNS·인증서, VM/KVM, WireGuard, noVNC, 서로 격리된 실제 작업 2개 완료는 이 합성 HTTP Fixture로 증명하지 않습니다. 해당 환경 검증은 남은 MVP 완료 조건입니다.

직접 조작 검증은 아직 대기 중입니다. `computer-use`에서 Chrome 창이 사라지고 Orca 입력이 `window_not_focused`(포커스된 창 없음)로 차단되었습니다. 한 차례 복원을 재시도했지만 해소되지 않아 추가 입력을 중단했습니다. 내장 브라우저는 자체 서명 인증서 확인 화면까지 관찰했으며 예외 승인은 수행되지 않았습니다. 아래 제품 화면은 독립 Chromium 실행에서 캡처해 직접 시각 검토한 증거이고, 별도의 Browser/Computer-use 상호작용 완료를 의미하지 않습니다. Mac 잠금 해제·Orca 활성화 후 최종 여정을 수행하기 전까지 PR은 Draft로 유지하며 머지하지 않습니다.

## 화면

변경 전은 `5321043` 이전 Web(1280px), 변경 후는 `99bae1f` 운영 빌드(1440px)입니다. 같은 합성 작업 제목을 사용했으며 서로 다른 격리 실행의 데이터입니다. 390px 화면은 키보드 포커스도 표시합니다.

![변경 전](../assets/preview-access/before-en.png)

![변경 후](../assets/preview-access/after-en.png)

[한국어](../assets/preview-access/after-ko.png) · [390px 한국어](../assets/preview-access/mobile-ko.png) · [390px 영어](../assets/preview-access/mobile-en.png)

## 롤백

API Flag와 Gateway를 비활성화하고 기존 Gateway 연결을 종료한 뒤 이전 애플리케이션을 배포합니다. DB Downgrade가 필요하면 `20260906_0007`로 내립니다. 이는 일회용 Preview Grant만 제거해 접근을 회수하며 Auth Session·작업·추가 전용 감사 기록은 유지합니다. 일반 Rollback에서 감사 Table을 삭제하지 않습니다.
