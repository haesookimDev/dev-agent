# 작업 VM의 명시적 공개 IPv4 송신

한국어 | [English](../en/worker-public-egress.md) · [접속 기록 설치](worker-network-logging.md) · [Guest 제어 연결](guest-control-bootstrap.md) · [MVP 현황](mvp-progress.md)

## 동작과 적용 범위

기본값은 계속 제어 API의 정확한 HTTPS IP:Port만 허용합니다. 새 `logged-public-ipv4` 모드를 명시하면 공개 IPv4 TCP·UDP·ICMP 송신과 해당 연결의 응답을 허용합니다. 작업별 NWFilter의 MAC/IP 위조 방지·Gateway 전용 ARP·IPv6/VLAN 차단, 별도 NAT Network와 소유권 검증을 유지합니다. 공개 Repository/Model **전송 경로**이며 봉인 이미지의 전체 Runner·모델 실행이나 UI/GUI Acceptance 완료를 뜻하지 않습니다.

RFC1918·Loopback·Link-local/Metadata·CGNAT·문서/Benchmark·Multicast·예약 대역, 관리자가 지정한 관리 CIDR, 현재 Host IPv4와 제어 IP의 다른 Port를 차단합니다. [IANA IPv4 Registry](https://www.iana.org/assignments/iana-ipv4-special-registry/)를 기준으로 비공개 대역을 거부하며 `192.0.0.0/24`와 폐기된 Relay 대역은 보수적으로 전체 거부합니다. Registry의 모든 **공개** 특수 목적 서비스를 사설 주소로 분류하는 정책은 아닙니다. 관리자가 사용하는 공개 관리 주소도 반드시 추가 거부 목록에 포함해야 합니다.

제어 API의 정확한 HTTPS 예외만 관리/사설 거부보다 앞섭니다. 제어 IP는 Host나 현재/향후 작업 주소 Pool 안에 둘 수 없습니다. Listener는 전용 API여야 하며 공유 CDN·HTTP Proxy를 운영 제어 주소로 사용하지 않습니다. IP:Port 검사는 SNI/HTTP Host 인가를 대체하지 않습니다.

[libvirt 필터](https://libvirt.org/formatnwfilter.html)의 L2 거부를 Connection Tracking보다 앞에 두고, L3 방향성 검사와 NAT Network를 함께 사용합니다. 실제 검증에서 관찰한 차단 위치를 구분하며, 공유 Host 방화벽을 완화해 테스트하지 않습니다. Host 주소를 새로 수집해 두 번 대조하지만 **실행 중 Host 설정 변경 감시기는 아닙니다**. 관리자는 활성 작업 중 주소·Routing·방화벽을 변경하지 않아야 하며 변경 전 작업 접수를 중지하고 정리해야 합니다.

## 설정과 활성화

아래 값은 신뢰된 관리자가 보호된 `/etc/kelpie/worker.env`에 설정합니다. 작업 내용·저장소에서 값을 받지 않습니다. [.env.example](../../.env.example)은 기본 비활성화 예시입니다.

| 환경변수 | 기본값 | 조건 |
| --- | --- | --- |
| `KELPIE_GUEST_INTERNET` | 비어 있음 또는 `disabled` | 활성화 값은 정확히 `logged-public-ipv4`. 알 수 없는 값은 거부합니다. |
| `KELPIE_GUEST_DNS_IPV4` | 비어 있음 | 활성화 시 정규 공개 IPv4 DNS 주소가 필수입니다. Host·제어 주소·거부 CIDR에 포함되면 안 됩니다. |
| `KELPIE_GUEST_DENIED_IPV4` | 비어 있음 | 활성화 시 관리 IPv4 CIDR이 필수입니다. 정규 Network Prefix를 중복 없이 문자열 정렬해 쉼표로 구분합니다. `/0`, 공백, IPv6, Host Bit가 있는 Prefix를 거부합니다. |

비활성화 상태에서 DNS/거부 설정만 남겨 둔 부분 설정도 거부합니다. 관리자 CIDR에 모든 Host IPv4 `/32`와 제어 IP `/32`를 합친 결과가 최대 32개여야 합니다. 부족한 목록을 조용히 잘라 내지 않습니다. 정적 Cloud-init Network에 지정 DNS만 추가하고 DHCP·IPv6는 활성화하지 않습니다.

접수를 중지하고 기존 작업을 정리한 유휴 전용 Host에 검증한 [Root Hook](worker-network-logging.md)을 먼저 설치합니다. 생산 Worker의 libvirt/kvm 접근, `NoNewPrivileges`, 자격증명·TLS·승인 Gate는 변경하지 않습니다. 관리 주소 목록과 DNS, 제어 Endpoint를 검토한 뒤 새 Worker 설정을 적용하고 한정된 실제 검증을 통과한 후 접수를 재개합니다. 이 문서는 운영 Host에 자동 배포할 권한을 부여하지 않습니다.

## 영속 계약과 실패 처리

- 기존 Schema 1–4와 Network Version 1(전체 차단)/2(제어 전용)는 그대로 읽습니다. 새 모드만 Schema 5/Network Version 3에 DNS·합쳐진 거부 목록을 기록합니다. API/DB Schema 변경은 없습니다.
- Worker는 고정 Root 경로의 안전한 설치와 읽기 전용 기록을 확인합니다. Root Lock·nft 명령·설정 가능한 특권 RPC를 Worker에 추가하지 않습니다. Root Network Identity에는 Worker의 송신 정책 필드를 섞지 않습니다.
- 활성화 명령 전에 `network-start.json`에 Run/Boot ID를 영속 기록하고, 새 Root `pending`/`active` 확인 후에만 NIC 생성으로 진행합니다. 실제 NIC 연결/갱신 Hook은 Kernel 정책·Handle·Journal 준비를 다시 확인합니다.
- VM·Network·Filter·Bridge의 물리적 제거와 해당 Root `stopped` 증거를 확인한 후 임대를 반환합니다. 시작 의도는 일반 Artifact 정리 후에도 보존합니다. 기존 Private `network.xml`/`filter.xml` 생성 의도도 별도로 요구하며 Root 기록을 타인 자원의 소유권으로 사용하지 않습니다.
- 기록이 없거나 변경되었거나 Crash 경계가 불명확하면 접수/반환을 거부하고 예약·증거를 유지합니다. `intent` 직후나 `pending`/`active` 사이 장애의 자동 복구는 아직 제공하지 않습니다. 기록 삭제·Schema 수동 변경·강제 반환으로 우회하지 않습니다.

`stopped`는 물리적 정리 증거이며 **연속 무손실 감사 완료 증명은 아닙니다**. Root Hook은 새 연결 Header만 기록하고 Payload·URL·DNS 질문을 수집하지 않습니다. 실행 중 기록기/Journald 유실·과부하 감지, 보존/Janitor, 불완전 시작의 안전한 자동 정합성 복구는 [기록 기능의 운영 한계](worker-network-logging.md)와 동일한 남은 출시 조건입니다.

## Rollback

설정 변경은 이미 기록한 작업의 정책을 바꾸지 않습니다. 접수 중지 → Schema 5 작업의 물리적 정리·Root 종료 기록·임대 반환 확인 → 모드를 `disabled`로 하고 DNS/거부 값을 함께 비움 → Worker 재시작·제어 전용 검증 순서로 되돌립니다. 이전 Worker 바이너리는 Schema 5를 읽지 못하므로 기록을 임의 변환하거나 삭제하지 말고, 해당 계약을 읽을 수 있는 검증된 버전으로 복구합니다. Hook 교체는 기존 Inode/Bytes/기록을 보존하는 별도 유휴 유지보수 절차를 따릅니다.

## 반복 검증

### 실제 검증 — 2026-09-13

[고정 SHA JSON 증거](../assets/worker-public-egress/macos-acceptance.json)에 생산 코드 `727ae08`, Fixture `0596ba2`와 바이너리 Hash를 기록했습니다. Mac 내부 Lima/ARM64 KVM Guest에서 공개 DNS·실제 HTTPS Shallow Clone이 **47.83초**에 통과했습니다. 정확한 복제 목적지, DNS, 별도 제어 TLS의 Kernel Journal을 대조했습니다. 저장소 코드나 자격증명은 사용하지 않았습니다.

양방향 위조/금지 프레임 각 6개는 TAP DROP `0→6`, `4→10`, 금지 TCP 8개는 `6→14`, 정상 수신 대조군 이후 unsolicited UDP는 해당 NAT Bridge REJECT `0→1`을 확인했습니다. Root 수명주기 기록 3개·Handle/Fingerprint·종료 Table 부재를 별도 읽기 전용 검사로 확인했습니다. 다른 공개 HTTPS 로그를 복제 증거로 허용하면 실패하는 회귀를 재현하고 정확한 대조로 복원했습니다.

VM·Network·Filter·Bridge·Disk·임시 Veth/Namespace를 정리했고, 원본 이미지 SHA256과 0600 소유권, 상위 경로 4개 ACL, 기존 Hook Inode/Hash/소유권을 복원했습니다. 기존 Root 기록과 새 검증 증거는 보존했습니다. 테스트 프로세스만 UID/GID 1000과 기존 libvirt/kvm 그룹으로 실행했으며 영구 계정/Socket 권한·Worker Unit을 변경하지 않았습니다. UI 변경은 없어 새 화면 검증은 해당하지 않으며 전체 GUI·Runner/API Acceptance는 별도입니다.

Worker 전체·정적 검사와 집중 Race, Linux ARM64/amd64 Tag 빌드/Vet가 통과했습니다. 이 기능의 PR은 최신 Head CI·리뷰·실제 증거를 확인한 뒤 정상 병합하며, 전체 MVP의 남은 Gate와 구분합니다.

### 명령과 Fixture

`make test-worker`, `make lint`, 집중 Race 검사와 Linux ARM64/amd64 `libvirt_integration` 빌드/Vet를 실행합니다. 기본 비활성화·잘못된 설정·전체 Pool 제어 예외 거부·Private 생성 의도·Root 기록 누락/변조·시작 실패·정리 전 임대 반환 금지 회귀를 포함합니다.

`TestDedicatedLibvirtInternetClone`은 승인된 빈 ARM64 KVM Lab, 명시한 Golden Image와 Root Hook, `KELPIE_LIBVIRT_TEST_ACK=disposable-host-only`, 위 활성화 설정 및 공개 테스트용 HTTPS Origin/IP가 있어야 실행합니다. Guest에서 공개 DNS와 `octocat/Hello-World`의 HTTPS Shallow Clone을 확인합니다. 자격증명·사용자 Git 설정 없이 새 디렉터리로 복제하고 저장소 코드는 실행하지 않습니다. 이번 DNS 결과를 [Git의 연결 주소 설정](https://git-scm.com/docs/git-config#Documentation/git-config.txt-httpcurloptResolve)으로 고정하되 TLS Hostname 검증을 유지하여 정확한 복제 목적지의 Kernel Log를 대조합니다. 별도 제어 TLS 로그를 복제 증거로 인정하지 않습니다.

Raw IP/MAC/ARP/VLAN/IPv6 거부, TCP 관리/Metadata 거부, 별도 Network Namespace/Veth의 정상 수신 대조군과 unsolicited UDP 거부, Root 기록·물리적 정리도 확인합니다. 새 Veth는 충돌 검사 후 만들고 정확한 Ifindex/MAC/Alias를 확인해 제거합니다. 기존 Host Route·방화벽을 변경하지 않습니다. 이 Fixture의 Root 관측 권한은 생산 Worker 권한이 아니며 반환은 실제 API가 아닌 합성 로컬 반환입니다. CI에는 기존 Go Kernel 검사/Tag 빌드를 사용하며 추가 VM Job·운영 Secret은 없습니다.
