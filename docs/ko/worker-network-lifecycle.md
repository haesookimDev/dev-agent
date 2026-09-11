# 작업별 네트워크 영속 정리

한국어 | [English](../en/worker-network-lifecycle.md) · [이전 스키마 전용 증거](worker-network-foundation.md)

2026-09-11 소스 `26476bebcc385cedbbc8259ec239803680d68246`에서 검증했습니다. 이 후속 작업은 명시적인 Fixture의 Network·Filter·Bridge를 소유하고 정리합니다. **운영 Executor는 아직 `network=default`를 사용하며 이번 검증에는 Guest를 연결하지 않았습니다.** 패킷 격리나 전체 Executor 인수 증거가 아닙니다.

## 변경과 복구 계약

- `fe336092b780e3c76c340ec2f663ced7ddf1c91e`: Run Store 생성·조회·상태 변경을 직렬화합니다. 동시 조회가 불완전한 기록을 읽지 못하며 동일 상태 전이는 멱등적입니다.
- `75450595619a252ccc42ca88efa8a6de6eb6b103`: `CreateNetworked`가 서브넷 할당과 Schema 3 영속 생성을 직렬화합니다. Host 부수 효과 전에 불변 Network 식별자를 임대 UUID에 연결합니다. 미확인 예약과 미해결 이전 기록은 재사용을 막습니다. 호출자가 전달한 제외 목록만으로 실제 Host 재고 수집을 입증하지는 않습니다.
- 정리는 현재·비활성 Network와 다른 Domain XML, 정확한 UUID·이름·소유권·설정, Filter 참조·Binding·Network Port를 검사합니다. Domain 부재 후 소유 Network만 중지·정의 해제하고 Filter 제거·Bridge 부재를 확인한 뒤 Run 산출물을 삭제합니다. 불명확하거나 재등장한 자원은 기록·예약을 보존하고 반환을 막습니다. 고아 Bridge는 이름만으로 삭제하지 않습니다.
- Schema 3는 기존 API 임대 연결을 유지합니다. 합성 HTTP 복구 테스트는 등록·조정·새 작업 수락 이전 정리를 검증합니다. 실제 Network Fixture의 `released` 표시는 로컬 테스트 확인이며 **API/PostgreSQL 반환이 아닙니다**.

Schema 1/2의 기존 바이트는 계속 읽으며 필드를 덧붙여 Network 권한을 획득할 수 없습니다. 운영 `Create`는 여전히 Schema 2를 기록하고 기존 기록을 재작성하지 않습니다. Schema 3 사용 후 이전 바이너리는 해당 기록을 거부하여 복구를 차단합니다. 호환 Worker가 물리 정리와 API 확인을 완료한 뒤에만 롤백하고, 기록을 보존하며 작업 수락을 우회하려고 삭제하지 않습니다.

## 실제 Linux 검증

Mac M4 Pro/macOS 15.7.3의 전용 Lima Ubuntu 24.04 ARM64와 libvirt 10.0.0을 사용했습니다. 비특권 계정으로 최종 소스의 정의·활성 Network 두 경우가 **3.17초**에 통과했습니다. Network·Filter·Bridge 부재, 기존 Network 목록 보존과 영속 확인을 검사했습니다. 최종 읽기 전용 확인에서도 Domain·소유 Filter/Binding/Bridge·dnsmasq 프로세스는 없고 기존 default Network는 inactive였습니다.

```sh
make test-worker
make lint
cd apps/worker
go test -race ./internal/daemon -run 'TestRunNetwork|TestRestartReconcilesTerminalLeaseBeforeAdmission|TestRunStoreConcurrent' -count=1
GOOS=linux GOARCH=arm64 go vet -tags libvirt_integration ./internal/daemon
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network-cleanup.test ./internal/daemon
# Copy to the dedicated Linux host and run as its unprivileged Worker user:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network-cleanup.test -test.run '^TestDedicatedLibvirtNetworkCleanup$' -test.v
```

모든 명령이 통과했습니다. 최종 전체 Worker daemon은 8.193초, 집중 Race는 2.728초입니다. Worker/Host 패키지 변경이므로 `make test` 전체는 재실행하지 않았고 API·Runner·Gateway·Web 동작을 바꾸지 않았습니다. 브라우저 검사는 해당하지 않습니다. CI Job을 추가하지 않았고 명시적 Host 검사는 일반 CI에서 실행하지 않습니다.

최종 바이너리 SHA256 `4ec90de4a95c33b57ec3059b6fd283cf4bbb7b5830a8b010dc63d58594e82472`는 Mac/Linux에서 일치했고 커밋된 소스의 재빌드도 바이트 단위로 일치했습니다. [편집하지 않은 최종 로그](../assets/worker-lifecycle/network-cleanup.log) SHA256은 `26052f725efe8bf530600e5976df896db14119f815affda605a707e676a80e0a`입니다.

실제 실패는 통과로 처리하지 않고 보존·수정했습니다.

- libvirt는 요소 순서와 기본 IPv6 속성을 정규화하고 활성 NAT에 기본 포트 범위를 추가합니다. `c111212bfb9e801f8b63cc10a626b4c20693dd5e`는 지원하는 정확한 기본값만 정규화하면서 전체 XML 구조를 비교합니다. 추가 주소·옵션, 다른 범위와 변경된 규칙은 계속 거부합니다. [Network XML](https://libvirt.org/formatnetwork.html), [virsh](https://libvirt.org/manpages/virsh.html)
- 전용 Host에 `dnsmasq`가 없었습니다. `d947e396d273ce714a89e32aa5b27a703903aba4`는 libvirt DHCP에 필요한 `dnsmasq-base`를 Ubuntu Host 설치기에 추가합니다. Lab에는 해당 패키지(2.91-0ubuntu0.24.04.1, 설치 893kB)만 추가했고 전역 DNS 서비스를 활성화하지 않았습니다. 기존 Host도 Network 활성화 전에 패키지가 필요합니다. Go 의존성이나 환경변수 계약은 바꾸지 않았습니다.
- 앞서 보존한 비활성·활성 Fixture는 별도 복구 테스트 프로세스로 정리했습니다(0.90/1.43초). 이전 복구 검사는 최종 두 경우의 로그와 구분하며 전체 Worker/API 복구나 VM 패킷 필터링을 입증하지 않습니다.

## 후속: Host 재고와 검증된 생성

소스 `ebb3d8e9f0c58195d075ed1fef614ce6982f607f`의 실제 Linux 읽기 전용 재고 검사가 0.07초에 통과했습니다. 인터페이스 2개, 제외 IPv4 Prefix 8개, 현재·비활성 XML 2개를 확인했습니다. 모든 IPv4 라우팅 테이블·기본 경로 Gateway·Host 주소와 비활성 libvirt Network/Domain 식별자 충돌을 포함합니다. 누락·잘못된 형식·미지원 재고는 거부하고 수집하지 않은 초기값을 사용 가능 증거로 인정하지 않습니다. IPv6 재고 검증이 IPv6 패킷 차단을 의미하지는 않습니다.

소스 `c53fea9ecf6d48d2473550ff0e343311845bfa46`의 Network 생성기는 비공개 `filter.xml`/`network.xml`을 배타적으로 영속 생성하고, 전면 차단 Filter 확인·충돌 재조회·정확한 Network 정의/시작·실제 Bridge MAC/주소 확인을 수행합니다. 기존 자원·파일을 덮어쓰거나 인수하지 않습니다. 호출자는 영속 Run을 수명주기에 연결하고 생성 종료를 기다린 뒤 정리해야 합니다. 이 조회가 외부 관리자의 변경까지 원자적으로 만들지는 않으며 동시 Host 재설정은 지원하지 않습니다.

실제 정상 생성과 활성화 성공 직후 취소 두 경우가 **4.61초**에 통과했습니다. 각각 별도 OS 프로세스가 남은 기록을 열어 자원·XML을 정리하고 합성 로컬 반환을 기록했습니다. 최종 조회에서 소유 Network/Filter/Bridge/Binding/dnsmasq 누수가 없고 기존 inactive default Network를 보존했습니다. 여전히 **Guest NIC·실제 패킷 검사·API/PostgreSQL 확인은 없습니다**. 운영 Executor는 아직 생성기를 호출하지 않습니다.

```sh
cd apps/worker
go test -race ./internal/daemon -run '^TestRunNetwork' -count=1
GOOS=linux GOARCH=arm64 go vet -tags libvirt_integration ./internal/daemon
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network-provision.test ./internal/daemon
# Copy to the dedicated Linux host and run as its unprivileged Worker user:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network-provision.test -test.run '^TestDedicatedLibvirtHostNetworkInventory$' -test.v
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network-provision.test -test.run '^TestDedicatedLibvirtNetworkProvisioning$' -test.v
```

최근 전체 `make test-worker`가 통과했고(daemon 8.195초), 이후 최종 불변 기록 Guard와 집중 Race(1.989초)를 검증했습니다. `make lint`와 최종 Linux Tag 포함 Vet도 통과했습니다. API/Web 변경, 새 Go 의존성·CI Job·운영 환경변수는 없습니다.

재고 바이너리 SHA256은 `db5785ab442fb54f2a002aabbbac2cd3d06d196bb94f37fc58f4ddb9507997ca`, [재고 로그](../assets/worker-lifecycle/network-inventory.log)는 `fd50c4787085f8ef176892e84b8a1c621211796ad6b1f8e860add7e125cb56cc`입니다. 생성/복구 바이너리는 `d51015e87a2f7dc83fc2c93b73f9bd62ebb7283d2a6d8eea625d35e78218b459`, [생성 로그](../assets/worker-lifecycle/network-provision.log)는 `03c8a3748dafd9e8b8ebf7392c76c64857f0876332290e94b17ccf028f231fe3`입니다. 두 바이너리는 Mac/Linux와 커밋 소스 재빌드에서 바이트 단위로 일치했습니다. 로그는 각 실행을 보존하며 하나의 최종 바이너리 호출에서 두 검사를 함께 수행했다고 주장하지 않습니다.

## 남은 조건

Executor 연결, Host/Metadata/다른 VM 차단, 허용 송신과 기존 연결 격리가 남았습니다. 실제 동시 VM으로 확인한 뒤 릴리즈합니다. 이 검증 기록은 PR 제출 이전으로 정확한 Head의 GitHub CI 결과가 없으며, 미병합 상태를 유지하고 이후 CI는 PR에 기록합니다. [MVP 완료율](mvp-progress.md)은 1/7(14.3%)입니다.
