# Mac 로컬 KVM 개발 환경

한국어 | [English](../en/macos-kvm-lab.md) · [MVP 진행률](mvp-progress.md)

## 범위와 경계

별도 Linux 머신 없이 지원되는 Apple Silicon Mac에서 **macOS → Lima/VZ ARM64 Linux → KVM ARM64 작업 VM** 경로를 검증합니다. 2026-09-10 사용자가 이 로컬 개발 방식을 승인했습니다. x86_64를 TCG로 실행하는 것은 KVM 검증의 대체가 아닙니다. 기존 amd64 이미지 계약을 몰래 ARM64로 바꾸거나 전체 MVP의 완료 기준을 낮추지 않습니다.

[템플릿](../../infra/lima/ubuntu-arm64.yaml)은 신뢰하는 개발 호스트입니다. 작업용 Golden Image가 아니며 외부 서비스를 향한 NAT 통신이 가능합니다. `networks: []`는 추가 네트워크가 없다는 뜻이지 인터넷 차단이 아닙니다. 향후 신뢰하지 않는 작업은 별도 내부 VM과 작업별 네트워크에서 실행해야 합니다.

- 기본 4 CPU, 8 GiB RAM, 40 GiB Sparse Disk. 실제 이미지 빌드·동시 작업 전에 여유 자원을 다시 확인합니다.
- Mac 홈·저장소·추가 디스크 Mount, 기존 SSH 공개키 가져오기, SSH Agent/X11 전달, Rosetta, containerd, Proxy 환경 전달을 끕니다.
- 모든 Guest IP·Port의 자동 TCP/UDP 전달을 끕니다. Lima 관리용 SSH는 별도이며 최초 부팅에서 vsock 대신 **Mac Loopback의 임의 Port**로 연결할 수 있습니다.
- 전용 `LIMA_HOME`에는 Lima가 만든 관리 키와 상태가 저장됩니다. 권한 0700으로 보호하고 커밋하지 않습니다. macOS 기본 Lima 다운로드 캐시는 `~/Library/Caches/lima`에 남을 수 있습니다. 공개 이미지 캐시이며 비밀정보 저장소로 쓰지 않습니다.
- `kelpie`의 passwordless sudo와 `kvm` 그룹은 이 **신뢰하는 개발 호스트 관리자**에만 해당합니다. 제품 작업 VM 사용자에게 부여하는 정책이 아닙니다.

## 처음 준비하기

macOS 15 이상과 Apple의 실제 중첩 가상화 지원 검사가 필요합니다. 모델 이름만으로 통과 처리하지 않습니다. 아래는 저장소 루트에서 실행하는 새 환경 예시입니다. 기존 Lab에는 다시 생성 명령을 실행하지 않습니다.

```sh
set -eu
test "$(uname -m)" = arm64
xcrun swift -e 'import Virtualization; import Darwin; if #available(macOS 15.0, *) { exit(VZGenericPlatformConfiguration.isNestedVirtualizationSupported ? 0 : 1) } else { exit(1) }'
LAB_ROOT="$(mktemp -d "$HOME/kelpie-lab.XXXXXX")"
mkdir -m 700 "$LAB_ROOT/tools" "$LAB_ROOT/lima"
curl --fail --location --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 180 \
  https://github.com/lima-vm/lima/releases/download/v2.2.0/lima-2.2.0-Darwin-arm64.tar.gz \
  --output "$LAB_ROOT/lima.tar.gz"
printf '%s  %s\n' bbdef91774885a0d05f7b048c4eb89ae2bcf3a0c252ae7ca7934e63df76d93c3 "$LAB_ROOT/lima.tar.gz" | shasum -a 256 -c -
tar -xzf "$LAB_ROOT/lima.tar.gz" -C "$LAB_ROOT/tools"
LIMA_BIN="$LAB_ROOT/tools/bin/limactl"
lab() {
  env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin LANG=C.UTF-8 \
    LIMA_HOME="$LAB_ROOT/lima" "$LIMA_BIN" --tty=false "$@"
}
lab --version
lab validate infra/lima/ubuntu-arm64.yaml
lab create --name=kelpie-kvm --mount-none infra/lima/ubuntu-arm64.yaml
lab list --json
lab start kelpie-kvm --timeout=10m
lab shell --workdir=/home/kelpie kelpie-kvm -- sh -c 'uname -srmo; test -c /dev/kvm'
```

명령 실패 시 다음 단계로 진행하지 않습니다. Archive Hash를 먼저 확인한 뒤 실행하며 Mac 전역 설치나 Homebrew 변경은 필요 없습니다. `HOME` 값은 원래 그대로 유지하고 주변 Token·SSH Agent·Proxy는 전달하지 않습니다. 새 Terminal에서는 기록한 **동일한 전용 경로**로 `LAB_ROOT`/`LIMA_BIN`과 함수를 복구합니다. `lab list --json`에서 Mount 없음, 중첩 가상화와 전체 Port 무시 설정을 확인하세요. Lima의 `READY`와 선택적 Readiness Probe만으로 실제 내부 KVM 부팅을 통과 처리하지 않습니다.

템플릿의 Ubuntu 24.04 ARM64 Cloud Image는 `release-20260826`, SHA-256 `afa139bac6f2629c1e1f2f8f34215f3a9ad9779801bcb945521ba1a45016743f`에 고정합니다. 실제 다운로드와 공식 `SHA256SUMS`를 대조했고 Lima의 Digest 검사도 통과했습니다. 별도 GPG 서명 검증을 했다고 주장하지 않습니다.

## 실제 중첩 부팅 검사

다음 변경은 새로 만든 전용 Linux 호스트 안에서만 수행합니다. APT의 서명·만료 검사를 끄지 않습니다. 이 Lab의 패키지 설치는 Golden Image의 재현 가능한 잠금 파일을 대신하지 않습니다.

```sh
set -eu
lab shell --workdir=/home/kelpie kelpie-kvm -- sudo -n apt-get update
lab shell --workdir=/home/kelpie kelpie-kvm -- sudo -n env DEBIAN_FRONTEND=noninteractive \
  apt-get install -y --no-install-recommends qemu-system-arm busybox-static
lab shell --workdir=/home/kelpie kelpie-kvm -- sudo -n usermod -aG kvm kelpie
lab shell --reconnect --workdir=/home/kelpie kelpie-kvm -- id
lab shell --workdir=/home/kelpie kelpie-kvm -- sh -c \
  'test ! -e /home/kelpie/lab-kernel && sudo -n install -o kelpie -g kelpie -m 0400 "/boot/vmlinuz-$(uname -r)" /home/kelpie/lab-kernel'
lab copy --backend=scp infra/lima/kvm_smoke.py kelpie-kvm:/home/kelpie/kvm_smoke.py
lab shell --workdir=/home/kelpie kelpie-kvm -- python3 /home/kelpie/kvm_smoke.py \
  --kernel /home/kelpie/lab-kernel --output /home/kelpie/kvm-smoke-01 --timeout 60
```

그룹 변경 뒤 `--reconnect`가 필요합니다. `/dev/kvm`을 누구나 쓰도록 `chmod`하거나 QEMU를 Root로 실행하지 않습니다. 재검사할 때는 새 Output 이름을 사용합니다. 기존 디렉터리는 덮어쓰지 않습니다. 성공 Receipt와 Serial/QEMU Log는 Guest Output에 남으며 필요한 파일만 `lab copy`로 가져옵니다. 공개 커널·BusyBox·Probe Hash는 Receipt에 기록합니다.

[검사기](../../infra/lima/kvm_smoke.py)는 디스크·NIC·Display 없이 1 CPU/512 MiB 내부 VM을 생성합니다. QMP에서 `present=true`와 `enabled=true`, Block Device 없음 → 현재 실행의 임의 Nonce가 포함된 ARM64 부팅 메시지 → 정상 Power Off와 QEMU 종료 0을 모두 요구합니다. TCG fallback은 없습니다. Timeout은 기본 60초, 허용 1–120초이며 실패 시 자신이 시작한 QEMU를 종료·회수하고 성공 Receipt를 쓰지 않습니다. 강제 종료가 필요하면 최대 5초씩 종료/회수 시간을 추가로 사용합니다. 임시 QMP Socket만 정리하고 증거 디렉터리는 보존합니다.

## 2026-09-10 실제 검증

검증 코드: 템플릿 `055a61e`, Probe `a7413dd`. 이후 CI·문서 변경은 해당 실행 코드를 변경하지 않습니다. PR에서는 최종 Head의 동일 파일 Hash와 CI를 함께 확인합니다.

| 항목 | 실제 관찰 |
| --- | --- |
| Mac | M4 Pro, RAM 24 GiB, macOS 15.7.3, Apple API `nested_virtualization_supported=true` |
| 외부 Linux | Lima 2.2.0/VZ, 4 CPU/8 GiB, Ubuntu ARM64, `6.8.0-138-generic`, 첫 부팅 약 12초 |
| 경계 | Guest의 공유 Mount 없음, 전체 자동 Port 전달 비활성 Log, 관리 SSH `127.0.0.1` Listener, Agent/기존 키 전달 없음 |
| 실행 패키지 | QEMU `1:8.2.2+ds-0ubuntu1.18`, busybox-static `1:1.36.1-6ubuntu3.1` |
| 정상 부팅 | 최종 Probe 실행 약 5.790초, KVM `present/enabled=true`, Block Device 없음, ARM64 Nonce 메시지, `Power down`, 종료 0 |
| 실패 경로 | KVM 그룹 적용 전 접근 거부, 1초 Timeout, 잘못된 커널의 10초 Timeout 모두 실패. Timeout 후 QEMU 잔여 프로세스·성공 Receipt 없음. 기존 Output 재사용 거부·기존 증거 보존 |
| Probe SHA-256 | `b34003d714878075dd2fe4c1b83ba13306b6a4fad94b30f67d5a8834aad4cc95` |
| 커널 SHA-256 | `a6c429cb79db29b987d138d1e8b2a6c9f0bbad28023145e2db7fe95505e96c9d` |
| Serial SHA-256 | `214d9b626e28a705e27d1059933a7f4220cdcf824bbbef2e2ecfcd3203f9c923` |

`make test-lab`의 18개 정책/프로토콜 테스트와 `make lint`를 사용합니다. 기존 `Python` CI와 `make test`에 포함하며 새 Job, Matrix, VM 빌드 또는 Timeout 증가는 없습니다. CI의 합성 QMP 응답 테스트는 위 실제 KVM 실행과 별도입니다. 이 변경은 웹 UI가 없는 호스트 구성/CLI이므로 브라우저·Computer Use 화면 검증 대상은 아닙니다.

Desktop Image·libvirt Worker·Browser/Console 입력·작업별 네트워크·동시 두 작업은 **아직 검증하지 않았습니다**. [Draft #55](https://github.com/haesookimDev/dev-agent/pull/55)의 amd64 이미지 경로를 ARM64 입력/펌웨어/브라우저 계약과 함께 확장하는 작업이 다음 단계입니다. 이 호스트 검사로 MVP 진행률을 올리지 않습니다.

## 중지와 복구

`lab stop kelpie-kvm`은 이 전용 VM만 중지하고 디스크·증거를 유지합니다. `lab start kelpie-kvm --timeout=10m`으로 재개합니다. Mac 재시작 후에는 실행 상태부터 확인합니다. 자동 시작 서비스를 설치하지 않습니다. 문제가 있으면 이 구성 PR을 되돌리고 Lab을 중지할 수 있으며 제품 DB/API 마이그레이션은 없습니다. 삭제·초기화가 필요하면 정확한 전용 인스턴스와 증거 백업을 먼저 확인하고 별도로 결정합니다. 홈·공유 Cache 전체를 삭제하지 않습니다.

근거: [Lima v2.2.0](https://github.com/lima-vm/lima/releases/tag/v2.2.0), [VZ Driver](https://lima-vm.io/docs/config/vmtype/vz/), [Apple 중첩 가상화 검사](https://developer.apple.com/documentation/virtualization/vzgenericplatformconfiguration/isnestedvirtualizationsupported), [QEMU ARM virt](https://www.qemu.org/docs/master/system/arm/virt.html), [Linux initramfs 형식](https://docs.kernel.org/driver-api/early-userspace/buffer-format.html).
