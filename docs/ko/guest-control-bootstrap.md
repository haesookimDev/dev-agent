# 작업 VM의 제어 연결 Bootstrap

한국어 | [English](../en/guest-control-bootstrap.md) · [운영](operations.md#kvm-worker) · [MVP 현황](mvp-progress.md)

## 동작과 경계

Worker는 Host용 주소를 Guest에 복사하지 않습니다. `libvirt` 실행은 지정한 Guest HTTPS Origin과 고정 IPv4, 작업 소유 네트워크·MAC·필터, 정적 주소를 사용합니다. 공유 `network=default`로 대체하지 않습니다. 새 Schema 4/Network Version 2 기록은 제어 연결 예외를 저장하며, 기존 Schema 3/Version 1은 계속 전체 차단입니다.

허용되는 것은 고정 제어 IPv4의 TCP 포트와 해당 연결에 필요한 제한된 ARP/반환 트래픽입니다. DNS·DHCP·IPv6·일반 인터넷·Host 서비스·Metadata·다른 작업 주소로 접근을 허용하지 않습니다. 제어 IP가 Worker 인터페이스 주소이거나 작업 할당 풀 안에 있으면 거부합니다. 네트워크 정책은 **IP:포트 경계**이지 SNI/HTTP Host 검사기가 아닙니다. 반드시 전용 API-only Listener를 사용하고 공유 CDN·범용 Proxy·다른 서비스와 포트를 공유하지 않아야 합니다. Host 네트워크 재구성과 VM 생성의 동시 실행은 지원하지 않습니다.

시작 순서는 `소유 기록 → 네트워크/Seed → VM → Runner의 TLS·임대 인증 → analyzing → 복제`입니다. Runner는 API의 작업 ID·정수 버전·상태를 확인하고 직접 `provisioning → analyzing`을 요청합니다. Worker는 이 전이를 대신하지 않습니다. `virt-install` 성공만으로 준비 완료를 기록하지 않으며, 초기 OS 부팅과 제어 연결 관찰에 합쳐서 최대 180초를 허용합니다. 잘못된 상태·멈춘 VM·응답 실패·시간 초과는 실패 경로로 보냅니다. 빠른 실행 실패나 취소는 준비 완료 Event를 내지 않고 정리합니다.

복제는 기본 120초 제한이며, 정상 종료·오류·시간 초과·취소 모두 해당 복제가 만든 프로세스 그룹을 정리합니다. 출력에 포함될 수 있는 인증 URL은 보관하지 않고 종료 코드만 보고합니다. Runner는 실패 시 현재 실행 상태/버전을 다시 확인하고, 충돌은 한 번만 재조회합니다. 취소·승인 대기·전달 상태를 덮어쓰거나 임대를 직접 반환하지 않습니다. 물리 정리 확인 후 API 반환, 그 ACK 후 로컬 용량 반환 순서는 Worker 책임입니다.

## 설정 계약

| 위치 / 변수 | 기본값과 운영 적용 |
| --- | --- |
| Worker `KELPIE_CONTROL_URL` | 기존 Host용 주소. Guest 설정의 대체값이 아닙니다. |
| Worker `KELPIE_GUEST_CONTROL_URL` | 기본값 없음, libvirt 필수. 사용자 정보·경로·Query·Fragment 없는 HTTPS Origin. |
| Worker `KELPIE_GUEST_CONTROL_IPV4` | 기본값 없음, libvirt 필수. 전용 제어 Listener의 표준 IPv4. DNS 이름은 Guest `/etc/hosts`에 고정합니다. |
| Worker `KELPIE_NETWORK_POOL` | `10.240.0.0/16`. 검증된 전용 사설 풀에서 충돌 없는 `/30`을 예약합니다. |
| Worker `KELPIE_GUEST_CONTROL_CA_FILE` | 비어 있으면 Guest 기본 공개 PKI. 선택적 사설 CA는 절대 경로의 Worker 소유 일반 파일, 단일 링크, `0600`, 최대 64 KiB/8개 유효한 서명용 CA 인증서만 허용합니다. 심볼릭 링크·개인 키·기타 내용은 거부합니다. |
| 생성된 Guest `KELPIE_CONTROL_CA_FILE` | 사설 CA 사용 시 `/run/kelpie/control-ca.pem`. `kelpie` 소유 `0600`, 해당 ControlClient만 사용. 시스템/모델 CA는 바꾸지 않습니다. |
| 생성된 Guest `KELPIE_CONTROL_BOOTSTRAP` | 새 Worker가 항상 `1`로 생성. 비어 있으면 기존 독립 Runner 경로를 유지하고, 그 외 값은 거부합니다. 운영자가 새 libvirt 경로에서 이 값을 제거하는 우회는 지원하지 않습니다. |

비밀정보·인증서·개인 키를 Git에 넣지 않습니다. CA는 인증서만 명시적으로 전달하며 Host의 주변 신뢰/자격증명 디렉터리를 검색하거나 복사하지 않습니다. 기본 이미지의 QEMU 접근은 운영자가 읽기 전용으로 준비하고, 이미지 자체의 DAC 자동 재지정만 막습니다. VM/쓰기 Overlay의 일반 DAC·AppArmor 경계는 유지합니다. 이미지보다 작은 디스크 예약은 자원 생성 전에 거부합니다.

## 적용·호환성·Rollback

새 Worker와 Bootstrap Runner를 포함한 이미지는 **Drain 후 함께 적용**합니다. 기존 Runner 이미지는 새 Seed Flag를 처리하지 못하므로 성공한 연결로 간주하지 않으며, 제한 시간 후 실패할 수 있습니다. Mock Executor와 Bootstrap을 설정하지 않은 독립 Runner는 기존 동작을 유지합니다. API 스키마나 DB 마이그레이션은 없습니다.

구버전 Worker는 Schema 4를 복구할 수 없습니다. Rollback 전 새 Worker로 소유 VM·네트워크를 정리하고 API 반환/로컬 `released`를 확인해야 합니다. 기록을 삭제하거나 Schema 3으로 바꾸어 복구 검사를 우회하지 않습니다. 종료된 Schema 4 기록이 남아 있어도 구버전은 거부하므로, 검증된 비활성 기록은 안전한 별도 보관 위치에 유지하고 구버전에는 비어 있는 새 작업 Root를 지정합니다. 불확실한 자원이 있으면 Rollback하지 말고 새 Worker와 기록을 유지해 복구합니다.

## 검증 범위

반복 가능한 실제 테스트는 [Worker/VM](../../apps/worker/internal/daemon/libvirt_runner_api_integration_test.go)와 [API/PostgreSQL](../../apps/api/tests/test_guest_runner_postgres.py) Fixture입니다. 전용 Mac/Lima ARM64 환경에서 실제 생산 Executor의 NIC·Seed와 실제 API를 사용합니다. 현재 Runner 소스는 임시 Guest systemd Override로 실행하며, 봉인된 새 Golden Image 검증으로 표현하지 않습니다.

인증된 실행 취소 테스트는 임대 범위의 실제 전이 API를 사용합니다. 현재 관리자 취소 API는 미할당 대기 작업만 지원하며, **실행 중 작업의 사용자 UI 취소를 검증한 것이 아닙니다**. 이 별도 MVP 조건은 남습니다.

제어 연결 기능은 일반 저장소 복제·모델 실행·GUI·Preview/Console·전체 VM 예산·동시 두 작업 완료를 의미하지 않습니다. 공개 인터넷 송신은 이 정책에서 의도적으로 차단하며, 그 후속 기능은 별도 브랜치/검증/PR로 진행합니다.
