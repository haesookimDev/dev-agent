# 실제 VM의 전면 네트워크 격리 검증

한국어 | [English](../en/worker-network-packets.md) · [선행 수명주기](worker-network-lifecycle.md)

2026-09-11 소스 `071f95f7c96ec16bbf965cf4fcb3b6048122923f`에서 ARM64 Golden Image를 실제 libvirt/KVM으로 부팅하고, 기록된 NIC의 양방향 Ethernet 차단을 검증했습니다. **운영 Executor 연결·허용 송신 정책·Runner 실행 완료를 의미하지 않습니다.**

## 구현과 확인 범위

- `bc45f945b8d114dc04a55b41b6ba5c7f0fbc25a4`: NIC XML을 기록된 Network·MAC·Filter에 고정하고 IP를 명시하며 자동 IP 학습을 끕니다. 변조된 식별자는 거부합니다. 기존 Network Version 1의 전면 차단 의미와 Schema 3 형식은 바꾸지 않았습니다.
- 전용 Fixture가 운영 RunStore·Network Provisioner·NIC XML·물리적 Cleanup을 사용합니다. Domain XML과 QEMU Guest Agent 명령은 테스트 전용입니다. 운영 Executor는 여전히 공유 `network=default`를 사용하므로 이 경로를 운영에서 사용 가능한 격리 기능으로 보지 않습니다.
- 게스트 Loopback UDP 전송·수신을 정상 대조군으로 확인합니다. 실제 NIC에는 ARP/Route 성공 여부와 무관하게 Raw Ethernet 프레임을 전송하고, 정확한 TAP의 단일 무조건 DROP 규칙 카운터 증가를 확인합니다.
- 방향별 12개 프레임: IPv4 6개(게이트웨이/게스트, Metadata, RFC1918 세 범위, 문서용 주소), IPv6 3개(Link-local, ULA, 문서용 주소), ARP, VLAN, MAC/IP 위조 조합입니다. 외부 DNS 조회·실제 서비스 연결·의존성 다운로드는 없습니다.

libvirt의 NIC별 Filter 연결과 IP 학습 제어를 사용합니다. 설치한 libvirt 10의 실제 TAP별 ebtables PREROUTING/POSTROUTING DROP 카운터를 관찰했습니다. 이는 이후 TCP/HTTPS 허용 정책의 INPUT/FORWARD 동작까지 검증한 것은 아닙니다. [Network Filter 문서](https://libvirt.org/formatnwfilter.html), [Firewall 구조](https://libvirt.org/firewall.html)

## 실제 결과와 이미지 보존

Mac M4 Pro/macOS 15.7.3의 전용 Lima Ubuntu 24.04 ARM64, libvirt 10.0.0/QEMU 8.2.2/KVM에서 **1개 검사, 109.96초 통과**했습니다. 동일 이미지의 작은 Overlay를 사용했고 약 1.9GiB의 남은 공간에 5GB 이미지를 복제하지 않았습니다.

| 관찰 | 결과 |
| --- | --- |
| 게스트 → Host 방향 | 12개 전송, 정확한 TAP DROP 카운터 5 → 17 |
| Host의 소유 Bridge → 게스트 방향 | 12개 전송, 정확한 TAP DROP 카운터 6 → 18 |
| 물리적 정리 | VM·Network·Filter·Bridge·Disk/NVRAM/XML 제거 확인 후 로컬 모의 `released` |
| 기존 자원 | 기존 `default` Network는 비활성 상태 유지, Binding·소유 Bridge 잔여 없음 |
| 원본 이미지 | 실행 중·종료 후 동일 Inode/소유자/해시 확인, 마지막에 원래 0600 ACL 복원 |

첫 50.09초 패킷 검사 자체는 통과했지만 후속 검사에서 libvirt의 기본 DAC 처리가 원본 Backing Image 소유자를 QEMU로 바꾸어 Worker의 읽기를 막는 문제를 발견했습니다. 원본 바이트가 변하지 않았음과 VM 부재를 확인한 뒤 정확한 파일의 원래 소유자를 복원했습니다. 이 첫 실행은 최종 보존 검증의 통과 증거로 사용하지 않습니다.

최종 Fixture는 Backing Store에만 `model='dac' relabel='no'`를 지정하고, 열린 FD에 QEMU UID의 `r--` ACL만 잠시 부여합니다. 원본에 QEMU 쓰기를 허용하거나 Domain DAC/AppArmor를 끄지 않습니다. 원본 소유자·Inode·ACL·해시가 바뀌면 검사에 실패합니다. VM 부재 확인 후 원래 ACL을 복원합니다. [Backing Store·Security Label 형식](https://libvirt.org/formatdomain.html)

테스트 운영자는 필요한 원본 상위 디렉터리 네 곳에만 QEMU 탐색 ACL을 임시 부여했고 최종 확인 후 모두 원복했습니다. 다른 사용자/그룹/전체 공개 권한은 추가하지 않았습니다. 이는 **Lab 준비 절차이며 운영 이미지 배포 API가 아닙니다**. 원본 이미지 SHA256은 `b2de98bf1725ede0319b2e50c74d4dc9de3499846cdeb3b7ca880b0bd22963a6`입니다.

## 재현 조건과 검증 명령

빈 Domain 목록의 승인된 폐기 가능 ARM64 Host, 준비한 사설 Golden Image와 QEMU Guest Agent, 필요한 상위 경로의 좁은 탐색 권한이 필요합니다. 일반 Worker 사용자로 실행합니다. Opt-in Fixture에서만 `sudo -n`으로 정확한 TAP의 ebtables 카운터를 읽고 소유 Bridge에 생성한 프레임을 보냅니다. 운영 Worker의 `NoNewPrivileges`, 권한, 전역 방화벽 설정은 변경하지 않습니다. 운영 자격증명·실제 Assignment를 사용하지 마십시오.

```sh
make test-worker
make lint
cd apps/worker
GOOS=linux GOARCH=arm64 go vet -tags libvirt_integration ./internal/daemon
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network-packets.test ./internal/daemon
# Copy to the prepared dedicated Linux host; replace the image placeholder:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only \
KELPIE_LIBVIRT_PACKET_IMAGE=/approved/private/golden-image.qcow2 \
/tmp/worker-network-packets.test -test.run '^TestDedicatedLibvirtQuarantinePackets$' -test.v
```

모두 통과했습니다. NIC 구현 후 전체 Worker는 Daemon 8.846초였고 최종 일반 Worker 검사는 Cache 통과, 최종 Linux Tag Vet·컴파일·실제 검사는 별도로 통과했습니다. Worker 한정 변경이므로 `make test`는 다시 실행하지 않았습니다. Web/네이티브 UI 변경이 없어 Browser/Computer-use 검사는 해당하지 않으며, 이번 OS 부팅을 최종 GUI Acceptance로 계산하지 않습니다.

바이너리 SHA256 `bab5a1fcf355e355acf4f96310ae1e56ea7eb2592e7da5b0040350ae7f2ac5a4`는 Mac/Linux가 일치했고 커밋된 소스로 재빌드한 결과도 바이트 단위로 동일했습니다. [원본 최종 로그](../assets/worker-lifecycle/network-packets.log)의 SHA256은 `9265b3e144cb9a5742aecb5fbc27064dc8be3600ce5da635ce9d35f8aaab4fc4`입니다. 새 의존성·CI Job·운영 환경변수는 없으며 이 실제 Host 검사는 일반 CI에서 실행하지 않습니다.

## 2026-09-13 선행 병합 후 최종 이미지 재검증

선행 [PR #62](https://github.com/haesookimDev/dev-agent/pull/62)는 최종 Head `ff6036c0c1d9318db21eea25768cb400c194aad2`의 [CI 5개](https://github.com/haesookimDev/dev-agent/actions/runs/34713928147)·실제 검증·리뷰 확인 후 `2b7e76d84ea56416c39f00dde1ee2bfa298f3b27`로 병합됐습니다. 이 PR은 별도 NIC 기능 브랜치에서 선행 수정을 통합했고, 최종 실제 검증 소스는 `fdd2e08b0f21447f7d9b32cec063c82ce5d69a84`입니다. 생성 의도 기록을 요구하는 새 정리 로직과 NIC의 호환성을 이전 로그만으로 판단하지 않고 다시 실행했습니다.

같은 Mac/Lima/libvirt의 비특권 Worker에서 새 ARM64 이미지 후보로 실제 검사 **1개/79.80초 통과**입니다. 게스트 Loopback 대조군과 양방향 12개 프레임이 통과했고 정확한 TAP DROP 카운터는 다시 게스트→Host 5→17, 소유 Bridge→게스트 6→18이었습니다. 새 VM의 종료·정의 해제·Network/Filter/Bridge·Disk/NVRAM/XML 제거 후에만 합성 로컬 반환을 기록했습니다. 별도 최종 조회에서 Domain·Binding·소유 Bridge·dnsmasq 잔여 없음, 기존 비활성 `default`와 기본 Filter 목록 보존, 비공개 단일 링크 저널 5개만 남음을 확인했습니다.

이미지 SHA256 `2fc312a6335a5919f5a0d4e3cfe2e40eae6915cce47b79801f559afc251035d8`, 크기 5,095,489,536바이트입니다. 기존 이미지의 작은 Overlay만 만들었고 여유 공간 2.6GiB에서 시작했습니다. 원본 Inode/소유자/해시 불변과 0600 ACL 복원을 검사했고, VM 부재 확인 후 새 전용 Receipt에 기록한 네 상위 디렉터리 ACL도 원복했습니다. 이미지·바이너리·비공개 소유권 저널/Receipt는 보존하고 이번 테스트가 만든 재생성 가능한 VM 자원만 제거했습니다. 이미지 후보의 전체 GUI·출시 Gate를 통과한 증거는 아닙니다.

`make test-worker`(Daemon 8.898초), `make lint`, Linux ARM64 Tag Vet/빌드가 통과했습니다. 바이너리 SHA256 `1bbce2449440107abdc2570c5ed2694f253520197364dbeb2481fb4d0ca28a42`는 Mac/실행 Host/커밋 소스 재빌드가 일치합니다. [새 원본 로그](../assets/worker-lifecycle/network-packets-run09.log) SHA256은 `9563634028f6e98606af828b1a7509b8af97c28d0e706a4b60a74a5b204856be`입니다. 이전 이미지의 109.96초 증거는 위에 별도로 보존합니다. UI/전체 실행기/API 반환은 변경·실행하지 않았으며 전체 `make test`·브라우저 검사를 이번 Worker 한정 통합에서 반복하지 않았습니다.

## 남은 Gate

허용 HTTPS/DNS/DHCP와 Host·Metadata·다른 VM 거부의 공존, 기존 연결 차단, 실제 동시 두 VM, 운영 Executor의 Guest 접근 가능 Control URL·이미지 접근·Runner·전체 시간 예산, 실제 API/PostgreSQL 반환·장애 복구를 검증해야 합니다. Raw 전면 차단을 TCP 연결·허용 정책 또는 서비스별 격리 증거로 확대하지 않습니다. 기존 Version 1을 허용 정책으로 재해석하지 않습니다.

이 NIC 기반 PR은 정상·실패 경로, 실제 패킷 차단·정리·이미지 보존과 최종 Head CI·리뷰를 확인해 병합합니다. 범위 밖의 운영 실행기·허용 송신·전체 MVP 출시 조건 때문에 Draft를 유지하지 않으며, 해당 기능의 검증은 생략하지 않습니다. [MVP 진행률](mvp-progress.md)은 고정 기준 **1/7(14.3%)** 그대로입니다.
