# Runner 제어 API의 전용 CA — 2026-09-13

한국어 | [English](../en/runner-control-tls.md) · [운영](operations.md#kvm-worker) · [MVP 현황](mvp-progress.md)

## 범위와 설정

Runner의 `ControlClient`에 선택적인 `KELPIE_CONTROL_CA_FILE`을 추가했습니다. 신뢰할 CA를 명시해야 하는 HTTPS 제어 API를 위한 설정이며 인증서 체인·유효 기간·호스트명 검사를 끄지 않습니다. `verify=False`, 시스템 CA 설치, 모델 Provider의 신뢰 설정 변경은 없습니다.

- 기본값은 미설정/빈 값입니다. 이 경우 기존 HTTPX 기본 인증서 검증을 유지합니다.
- 설정하면 PEM CA 파일로 이 제어 API 클라이언트만을 위한 SSL Context를 만듭니다. 명시한 CA 대신 시스템 기본 CA로 재시도하지 않습니다. 파일이 없거나 올바른 PEM이 아니면 클라이언트 생성이 실패합니다.
- 운영자는 Runner 프로세스의 환경에 `KELPIE_CONTROL_CA_FILE=/run/kelpie/control-ca.pem`처럼 **Guest 내부 경로**를 설정하고, 신뢰할 인증서만 담긴 파일을 해당 사용자 전용 읽기 권한으로 공급해야 합니다. 개인 키, 주변 Host 인증 자료, 실제 인증서를 저장소에 넣지 않습니다.
- 이 PR은 Worker의 Host→Guest 파일 전달을 구현하지 않습니다. Worker의 환경에 같은 이름을 추가하는 것만으로 Guest가 설정되는 것은 아닙니다. Golden Image/Worker가 이 Runner 버전을 포함하고 실제 파일을 전달하는 경로는 후속 검증 대상입니다.
- API 경로·Payload·임대/Correlation Header 계약은 바뀌지 않습니다. 기존 로컬 HTTP Fixture는 유지하지만 운영 제어 통신은 HTTPS를 사용해야 합니다.

Rollback은 이 기능의 Merge Commit을 되돌리고 해당 설정을 제거하는 방식입니다. 사설 CA를 쓰는 실행은 그 전에 Drain해야 하며, 실패를 피하려고 인증서 검증을 비활성화하거나 HTTP로 전환하지 않습니다. DB Migration은 없습니다.

## 검증 증거

기준 `main`은 `d5fb7fc40e3f0d820db1539d5d47792a130707ff`이고, 구현·필수 회귀 소스는 `33c5ba87babfe76d410c00a81967c168214d6919`입니다. 기존 통합 브랜치의 `129f9b1`에서 필요한 6줄 구현만 검토해 옮겼고, 테스트는 실패 입력·신뢰 초기화 검사를 보강했습니다.

1. 구현 전 새 회귀 **3개 실패**: 명시 CA로도 정상 TLS 요청 실패, 없는/잘못된 CA 입력을 무시했습니다.
2. 구현 후 `make test-runner`: **48개 통과**, 0.29초. 실제 Loopback TLS 소켓에서 정상 연결, 미신뢰 인증서 거부, 호스트명 불일치 거부, 설정 제거 후 기본 신뢰 복원, 별도 기본 SSL Context의 미신뢰 거부를 검증합니다. 필요한 OpenSSL은 Fixture 인증서 생성에만 사용합니다. 새 Python 의존성은 없습니다.
3. `make lint`, `git diff --check`: 통과했습니다.
4. 테스트 러너 밖에서 Mac ARM64의 별도 `ControlClient` 프로세스 **6개**를 실제 임시 TLS 서버에 연결했습니다. 정상·미신뢰·잘못된 이름·없는 CA·잘못된 PEM·기본 신뢰 불변을 모두 통과했습니다. 정상 Event HTTP 요청 1개만 관찰했고 거부된 연결은 임대 Header를 전송하지 않았습니다. [자격증명이 없는 결과 기록](../assets/runner-control-tls/macos-acceptance.json)
5. 직접 만든 TLS 서버·스레드·포트와 임시 인증서/키 디렉터리의 정리를 확인했습니다. 개인 실험 스크립트는 `check-runner-control-tls.py`이며 저장소의 [반복 가능한 TLS 회귀](../../apps/runner/tests/test_control_tls.py)도 동일한 핵심 경계를 검증합니다.

이것은 실제 TLS **전송 계층** 검증입니다. 서버의 Event 응답은 Fixture이며 제품 API의 임대 승인, VM 부팅, Guest IP Pin/방화벽, Runner 전체 작업 수행의 증거는 아닙니다. UI 변경이 없어 Browser/Computer-use는 해당 없습니다. Worker/API 등 다른 컴포넌트의 로컬 전체 검사는 이 변경에 해당하지 않으며 PR의 기존 필수 CI는 그대로 실행합니다.

최종 Head의 CI·리뷰·병합 상태는 PR에 기록합니다. 후속 문서 변경은 위 구현·테스트의 동일성을 확인하며, 이 기능을 7개 MVP 릴리즈 단계의 완료로 가산하지 않습니다.
