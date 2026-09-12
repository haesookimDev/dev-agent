# 작업별 네트워크 접속 기록

한국어 | [English](../en/worker-network-logging.md) · [Guest 제어 연결](guest-control-bootstrap.md) · [MVP 현황](mvp-progress.md)

## 범위와 보안 경계

Root 소유 `kelpie-network-hook`은 정확히 재구성한 Kelpie 네트워크의 새 송신 연결을 기록합니다. 인터넷 송신을 허용하거나 기존 제어 IP:Port 예외를 넓히지 않으며, Guest의 기본 차단 NWFilter를 대체하지 않습니다. 일반 저장소/모델 송신은 별도 [명시적 공개 송신 기능](worker-public-egress.md)입니다.

[libvirt Network Hook](https://libvirt.org/hooks.html)은 네트워크 UUID·Bridge·사설 대역·소유 Metadata를 검증합니다. 활성화 전에 Journal 준비 확인, 영속 `pending` 기록, 작업별 nftables Table 생성, 실제 정책/Handle 대조, `active` 기록을 수행합니다. 네트워크 활성화와 NIC 연결/갱신 때 실제 식별자를 재검증하며, Table 유실·변경·재생성을 거부합니다. Hook 안에서 libvirt를 재호출하거나 Worker 자격증명을 읽거나 입력 명령을 실행하지 않습니다.

Forward Chain은 Priority `-10`에서 소유 Bridge/Source의 `ct state new`를 기록합니다. [nftables Log](https://netfilter.org/projects/nftables/manpage.html)는 처리를 종료하지 않으므로 다른 필터의 검사를 면제하지 않습니다. Prefix는 `kelpie-egress:<run UUID> `입니다. 기본 Packet Header의 Bridge·출발/목적지·Protocol·Port를 사용하며 Payload·URL·DNS 질문이나 선택적인 TCP/IP Log Flag는 요청하지 않습니다. 앞선 NWFilter에서 차단된 Packet까지 Journal에 남는다고 보장하지 않습니다.

`/var/lib/kelpie-network-guard`의 불변 `pending`·`active`·`stopped` 기록은 Root만 작성합니다. Root 소유·안전한 상위 경로·단일 Link·엄격한 Encoding·Lock·Boot ID를 확인합니다. 종료 시에는 일치하는 실제 Kernel Handle만 제거하며, 교체된 Table은 조사할 수 있게 보존합니다. 안전한 물리적 제거가 확인되면 Journal 장애가 종료를 막지는 않습니다. 정리 후에도 기록을 남기며, 불확실한 소유권 검사를 우회하려고 기록을 삭제하지 않습니다.

이는 **무손실 감사 저장소가 아닙니다**. 실행 중 Journal 유실/과부하 감지, 보존 정책, Kernel 생성과 `active` 기록 사이 장애 복구는 남은 운영 출시 조건입니다. 불확실한 상태는 안전하게 거부하며 조용히 채택하거나 정리 완료로 처리하지 않습니다. Hook 표준 오류는 특권 명령 출력을 포함하지 않는 고정 문구를 사용합니다.

## 최초 설치와 유지보수

[설치 스크립트](../../infra/host/install-network-hook.sh)는 libvirt·systemd-journald·nftables가 있는 유휴 전용 Ubuntu Host의 명시적인 **최초 설치 전용**입니다. 기존 Network Hook이나 Guard 기록이 있으면 덮어쓰지 않고 거부합니다. 새 Worker 환경변수 기본값이나 API/Schema Migration은 없습니다.

검토한 설치기와 실행 파일은 전체 상위 경로가 Group/World 쓰기를 허용하지 않는 정규 Root 소유 경로에 둡니다. 소스는 단일 Link의 일반 실행 파일이며 Symlink와 Group/World 쓰기를 허용하지 않습니다. 별도로 승인한 소문자 SHA256을 두 번째 인자로 전달합니다. 설치기는 Root 전용 임시 경로로 복사한 뒤 승인 Hash를 검사하고, 그 보호된 복사본을 설치하며 libvirt 재시작 전 설치된 Bytes도 대조합니다. 원본 변경으로 승인 Hash가 바뀌지는 않습니다. 새 Python/Go Module 의존성 없이 Ubuntu Bash/Coreutils와 기존 Host 의존성을 사용합니다.

호출 전에 작업 접수를 중지하고 정리를 완료한 뒤 `kelpie-worker.service`에 **실제로 유효한 Runtime Mask**를 설정합니다. systemd가 Masked·Inactive·Runtime-Masked로 보고하는지 확인하며, Unit 부재·`disable`·효력 없는 Mask는 충분하지 않습니다. 설치기는 빈 Domain/활성 Network 재고의 앞뒤, Hook 작성 직전, libvirt 재시작 직전에 Worker 상태를 재검사합니다. 유지보수 동안 다른 특권 libvirt 조작도 중지해야 합니다. systemd를 우회하는 신뢰된 관리자를 막는 Lock은 아닙니다.

현재 저장소의 Unit 설치 위치는 `/etc/systemd/system`이며 `/run`보다 우선하므로 `systemctl mask --runtime`만으로 차단되지 않을 수 있습니다. 기존 Unit과 Metadata를 로드 경로 밖에 그대로 보존하고, 유효한 Runtime Mask를 설정하며, 검증 후 원본을 복원하는 검토된 유지보수 절차를 따릅니다. 강제 덮어쓰기나 Unit 삭제로 해결하지 않습니다. [systemd Mask 의미](https://github.com/systemd/systemd/blob/v255/man/systemctl.xml)를 참고합니다. 로컬 Unit이 없는 신규 Host에서는 Runtime Mask를 직접 사용할 수 있지만 명령 성공만 믿지 말고 실제 상태를 확인합니다.

고정 시스템 경로·Locale·`KELPIE_NETWORK_HOOK_INSTALL_ACK=dedicated-idle-host-only`만 넣은 깨끗한 환경에서 검토한 설치기를 호출하고, 절대 경로의 검토된 바이너리와 승인 Hash를 인자로 제공합니다. Root가 필요하며 비특권 테스트가 Worker에 이 권한을 주지는 않습니다. 설치기가 작업을 자동 시작하지 않고 Mask를 유지합니다. 설치 Hash·Daemon 상태·한정된 수명주기 검사 후 기존 Unit/Mask 상태를 복원하고 작업 접수를 명시적으로 재개합니다.

설치/재시작 실패 시 접수 중지를 유지하고 부분 설치와 기록을 보존해 검토된 유지보수로 해결합니다. 검사를 완화해 다시 실행하지 않습니다. Rollback/Upgrade 전 소유 VM·활성 Network·불확실한 Kernel Table이 없음을 확인하고 Hook·Guard 기록·원본 Metadata를 보존합니다. 검증한 이전 버전을 복원하고 유휴 상태에서 libvirt를 재시작해 확인한 뒤 접수를 재개합니다. 최초 설치 스크립트는 이 파괴적인 교체 경계를 자동화하지 않습니다.

## 실제 검증 — 2026-09-13

[JSON 증거](../assets/worker-network-logging/macos-acceptance.json)에 생산 소스 `f24cf2d`, 테스트 소스 `16613e2`, 바이너리/Fixture Hash와 검증 한계를 기록했습니다. 승인된 Mac 로컬 Lima Ubuntu ARM64 KVM Lab에서 실제 VM으로 검증했습니다. 두 Fixture 파일은 빌드 뒤 변경 없이 커밋했으며 이 Checkout 차이를 명시했습니다.

- 새 Network Namespace의 Kernel 검사 2개가 각각 0.03초에 통과했습니다. 생성 정책·Snapshot·중복 거부·Handle 제거, 이후 기록기 유실/재생성 거부를 확인했습니다. 이 검사에서 Journal 준비 Callback은 주입하므로 실제 접속 기록의 증거는 아닙니다.
- 정상 네트워크 생성과 활성화 직후 취소, 각각 별도 OS 프로세스의 복구가 총 4.86초에 통과했습니다. 읽기 전용 후속 검사로 Root 수명주기 기록 6개와 해당 Table 부재를 확인했습니다. 실제 API 반환이 아닌 네트워크 검사입니다.
- 실제 Golden Image Guest의 비밀정보 없는 공개 테스트 Endpoint TLS 연결, 금지 TCP 8개와 TAP DROP `0 → 8`이 63.96초에 통과했습니다. 실제 Kernel Journal에서 이 작업의 UUID·Bridge·출발/목적지·TCP·Port가 일치했습니다. 실제 Table과 Root 기록이 일치했고, `stopped` 기록과 물리적 Table 부재 확인 후에만 Fixture의 합성 로컬 반환을 수행했습니다.
- VM·Network·Filter·Bridge·Disk를 제거하고 기존 네트워크 재고와 원본 이미지 Hash를 보존했습니다. 이미지 ACL·상위 경로 4개 ACL·기존 Hook Inode/Hash/소유자/Mode를 복원했습니다. 비공개 증거와 테스트한 Hook 바이너리는 보존했습니다.

HTTPS 검사에는 전송 정상 대조군으로 `example.com`을 사용했습니다. 전용 운영 API Listener나 Runner/API Acceptance가 아닙니다. 이 검사는 정확하고 복원 가능한 Hook 교체로 실행했으며 아래 후속 최초 설치 검증과 구분합니다. UI 코드 변경이 없어 새 브라우저/데스크톱 화면 증거는 해당하지 않습니다. 전체 GUI·봉인 이미지/Runner·amd64 VM·동시 두 작업 검증은 남아 있습니다.

최종 설치기 `502b32b`로 유휴 Lab에서 별도 **실제 Root 최초 설치와 libvirtd 재시작**을 통과했습니다. 유효한 Runtime Mask가 실제 Worker 시작 요청을 거부했습니다. 설치 바이너리의 승인 SHA256과 Root:Root 0755가 일치했고 새로 인식된 Hook에서 네트워크 생성/취소/복구 두 사례도 4.86초에 통과했습니다. 새 Root 수명주기 기록 6개를 대조했습니다. 원래 Hook/Guard Directory와 모든 파일의 Inode·Bytes·소유자·Mode를 보존한 Manifest와 비교해 복원했고, 기존 Unit 부재/Mask 없음 상태도 복원했습니다. 새 설치 증거는 삭제하지 않고 별도 보관했습니다. 증거에는 설치기/테스트의 정확한 Hash를 기록하며, 이 유지보수 검증을 무관한 운영 Host의 최초 구성과 혼동하지 않습니다.

Linux에서 설치 테스트 9그룹이 통과했습니다. 소스 검증 0.10초·원본 변경 경합 0.56초, 정확한 Inactive/Runtime-Mask/Zero-PID 해석, 재고 경합과 설치/재시작 직전의 독립 Gate를 포함합니다. PID 누락과 명령 실패 전파 3개의 회귀는 해당 보호를 제거하면 실패하고 복원 후 통과했습니다. Directory 생성·바이너리 복사·재시작 실패가 후속 동작으로 진행하거나 기존 Daemon이 살아 있다는 이유로 성공 처리되지 않습니다. Mac Worker 테스트·Vet·정적 검사도 통과했고 Mac에서 Skip한 Linux 전용 검사는 Lab에서 실행했습니다.

## 반복 검증과 CI

`make test-worker`, `make lint`를 실행하고, `apps/worker`에서 `go test -race ./internal/daemon -run '^TestNetworkLog' -count=1`을 수행합니다. Linux ARM64·amd64의 `libvirt_integration` Fixture 빌드와 Vet도 별도로 확인합니다. 일반 단위 테스트에는 Root나 libvirt가 필요하지 않습니다.

기존 GitHub Go Job에서 Ubuntu `nftables` Package를 설치하고 Tag가 있는 Fixture를 한 번 빌드하며 Tagged Vet를 유지합니다. `unshare --net`에서 `^TestIsolatedKernelNetworkLog`만 30초 제한으로 실행합니다. 테스트는 `KELPIE_NETWORK_LOG_TEST_ACK=disposable-isolated-netns-only`, Root와 PID 1이 아닌 Namespace를 요구합니다. 추가 Job·VM·운영 Secret·Host 방화벽 변경 없이 기존 Job의 8분 제한을 유지합니다. CI Kernel 검사는 실제 VM/Journal 검증을 대체하지 않습니다.

별도 `TestDedicatedLibvirtLoggedControlPackets`는 Hook이 설치된 승인된 빈 Linux Lab, `KELPIE_LIBVIRT_TEST_ACK=disposable-host-only`, 명시적으로 준비한 이미지·HTTPS Origin·고정 IPv4가 필요합니다. 외부 테스트 제한 6분으로 실행하고 성공/실패 모두 Journal을 보존하며 물리적 정리와 임시 권한 복원을 확인합니다. 공개 CI에 운영 libvirt Socket이나 자격증명을 제공하지 않습니다.
