# Golden Image 후보 빌드 — Draft

한국어 | [English](../en/golden-image-inputs.md) · [MVP 현황](mvp-progress.md) · [IMG-001](roadmap-detailed.md#img-001--재현-가능한-golden-image-pipeline--l)

## 이번 범위

[`prepare.py`](../../infra/images/prepare.py)는 입력 검증·별도 사본 준비만 수행합니다. [`build.py`](../../infra/images/build.py)와 [Packer 설정](../../infra/images/ubuntu.pkr.hcl)은 승인한 전용 KVM Host에서 [`guest.py`](../../infra/images/guest.py)의 설치·봉인 후 [`boot.py`](../../infra/images/boot.py)의 두 차례 부팅 검사를 자동 실행하는 후보 Builder입니다. **이 경로의 실제 Image Build/Boot/Desktop 검증과 릴리즈 Gate는 미완료이며 IMG-001도 미완료입니다.** 현재 Worker의 Base Image 설정이나 실행 경로를 바꾸거나 자동 Rollout하지 않습니다.

검토한 Ubuntu 24.04 **amd64 또는 arm64** 이미지와 같은 Architecture의 Codex 플랫폼 패키지 또는 독립 실행 파일, Linux 브라우저 ZIP, Runner와 전체 Python 의존성 Wheel을 먼저 확보해야 합니다. 공개 배포 입력은 에이전트가 공식 출처에서 확보·검증할 수 있으며 사용자에게 파일 제공을 반드시 요구하지 않습니다. 기대 Hash는 검토한 공식 배포 자료/빌드 증거에서 확인하세요. 출처를 확인하지 않은 파일을 직접 Hash한 것만으로 신뢰할 수 있는 배포물이 되지는 않습니다. 저장소에는 실제 검증하지 않은 Version·Checksum을 릴리즈 Lock으로 넣지 않습니다.

## Version 1 입력 계약

Manifest는 UTF-8 JSON이며 최대 128 KiB입니다. 아래 Field만 허용하고 중복 JSON Key·알 수 없는 Field·가변 Alias(`latest`, `current`, `main` 등)를 거부합니다. 입력 준비 단계에서 Version은 선언 값이며 실제 설치 가능성 검사는 아닙니다.

| Field | 필수 내용 |
| --- | --- |
| `schema_version`, `architecture` | 정수 `1`, 문자열 `amd64` 또는 `arm64`; 혼합 Architecture·TCG 대체 불가 |
| `image_version` | 검토한 고정 이미지 Version 식별자 |
| `ubuntu_snapshot` | 유효한 UTC 날짜 `YYYYMMDDTHHMMSSZ`; 실제 가용성은 Guest의 APT 실행에서 확인 |
| `runner_source_commit` | Runner 소스의 소문자 40자리 Git SHA; Wheel과 소스 간 Provenance 증명은 후속 Gate |
| `apt_packages` | Package 이름 → 정확한 Version 문자열의 Object, 최대 256개 |
| `base_image`, `codex`, `browser` | 각각 `file`, `version`, `sha256`, `size_bytes`를 가진 Object |
| `runner_wheels` | 동일 4개 Field와 `name`을 가진 Wheel Object 목록, 1~64개. `kelpie-vm-runner` 필수, 정규화된 Package 이름 중복 금지 |

입력 준비의 필수 APT 이름은 `build-essential`, `ca-certificates`, `dbus-x11`, `git`, `nodejs`, `npm`, `python3.12-venv`, `qemu-guest-agent`, `xfce4`, `xorg`입니다. Builder는 추가로 `lightdm`, `libnss3`, `libgbm1`, `libasound2t64`, `fonts-liberation`, `x11-utils`의 정확한 Version을 요구합니다. `x11-utils`의 `xdpyinfo`는 실제 X Display 응답 검사에 사용합니다. 이 목록이 완전한 Desktop/Browser 의존성 집합임을 보장하지 않으며 실제 설치·사용 검증이 필요합니다.

`file`은 하위 경로 없는 ASCII 파일명이며 대소문자 Alias까지 중복을 금지합니다. Base Image 확장자는 `.qcow2`/`.img`(최대 64 GiB), Browser는 `.zip`(2 GiB), Wheel은 `.whl`(개당 256 MiB), Codex 입력은 최대 1 GiB입니다. `size_bytes`는 양의 정수, `sha256`은 소문자 64자리입니다. 확장자 검사는 내부 형식·Architecture·실행 안전성 검사가 아닙니다.

### Codex 전체 플랫폼 패키지

[공식 Codex CLI 안내](https://learn.chatgpt.com/docs/codex/cli)의 Linux 설치 경로를 참고하되, 정확한 플랫폼 Layout은 실제 배포 파일로 검증합니다. 검토한 `@openai/codex`의 `VERSION-linux-x64`(amd64) 또는 `VERSION-linux-arm64`(arm64) 플랫폼 Archive를 사용할 수 있습니다. 최상위 JavaScript Wrapper 패키지나 다른 Architecture의 Archive와 혼동하지 마세요. Manifest의 `codex.version`은 `VERSION`이며 파일 이름이 `.tgz`/`.tar.gz`이면 전체 패키지로 해석합니다. 다른 이름의 독립 ELF도 Manifest와 Architecture가 일치해야 합니다. Schema 1의 기존 amd64 호출은 유지하며 Worker 설정 변경이나 기존 이미지 자동 교체는 없습니다.

- [`codex_package.py`](../../infra/images/codex_package.py)는 `package/` 아래 Layout Version 1·플랫폼 Metadata와 5개 동일 Architecture ELF(Codex, `codex-code-mode-host`, `rg`, `bwrap`, `zsh`)를 검사합니다. amd64는 `vendor/x86_64-unknown-linux-musl/`·ELF Machine 62, arm64는 `vendor/aarch64-unknown-linux-musl/`·ELF Machine 183입니다. 전체 파일을 미리 검사한 뒤 `/opt/kelpie/codex`에 상대 배치를 보존해 추출하며 `/usr/local/bin/codex`는 그 안의 본체를 가리킵니다. npm 관리 설치라고 표시하거나 로그인하지 않습니다.
- 최대 100개 일반 파일·총 512 MiB·Metadata별 64 KiB입니다. 경로 이탈·대소문자 중복·부모/파일 충돌·링크·특수/Sparse 파일을 거부하고, 실행 파일은 `0755`, 나머지는 `0644`로 제한해 Setuid/Setgid를 보존하지 않습니다. 압축 해제 읽기에도 상한이 있습니다. 현재 지원 Layout과 다른 배포물은 검토·테스트 없이 수용하지 않습니다.
- 설치 시 파일 목록·크기·SHA-256·Mode를 `/opt/kelpie/codex-inventory.json`에 기록합니다. 부팅 검사는 보조 파일까지 동일한지와 실행 링크의 정확한 대상을 재검사한 뒤 Version을 확인합니다. 이 Inventory는 배포자 서명이나 완전한 SBOM을 대체하지 않습니다.

## 실행과 결과

POSIX 파일 시스템과 Python 3.12 이상을 사용합니다. 전용 입력 Root에 Manifest가 선언한 파일을 평평하게 배치하세요. 아래 `/approved/...`는 실제 승인한 경로로 바꿔야 합니다. 필요한 디스크 공간을 먼저 확인하고 운영 Image 디렉터리를 출력으로 사용하지 마세요.

```sh
image_workspace="$(mktemp -d /tmp/kelpie-image-inputs.XXXXXX)"
python3 infra/images/prepare.py \
  --manifest /approved/image-lock.json \
  --assets /approved/image-assets \
  --output "$image_workspace/bundle"
```

- 출력 부모는 현재 사용자 소유의 `0700` 디렉터리여야 합니다. 기존 출력은 빈 디렉터리·파일·링크·실패한 Bundle 모두 거부하며 자동 삭제·덮어쓰기하지 않습니다.
- 선언된 단일 Link의 일반 파일만 읽습니다. Manifest/입력 Root의 최종 Symlink와 입력 파일의 Symlink·Hardlink·FIFO·디렉터리를 거부합니다. 승인한 상위 경로와 동일 사용자 프로세스는 신뢰 경계 안에 있어야 합니다.
- 파일은 최대 1 MiB씩 새 `0600` 파일로 복사하며 정확한 크기·SHA-256·복사 전후 파일 상태를 확인합니다. 디렉터리는 `0700`이고, 선언하지 않은 파일은 읽거나 복사하지 않습니다.
- 성공하면 `files/`, 정규화한 `manifest.json`, `inputs-verified.json`을 남기고 JSON 결과와 Exit 0을 반환합니다. 마지막 기록의 상태는 `inputs_verified`, **`release_eligible`은 항상 `false`**입니다. 복사한 바이트가 입력과 일치했다는 뜻이지 Boot나 이미지 릴리즈 성공이 아닙니다.
- 오류는 민감한 경로나 원문을 포함하지 않는 분류와 Exit 1입니다. 복사 중 실패하면 성공 기록 없이 비공개 부분 디렉터리가 남을 수 있습니다. 이를 사용하지 말고 원인을 해결한 뒤 새 출력 경로로 재실행하세요. 기존 부분 파일 정리는 소유권·정확한 경로를 확인한 별도 작업입니다.
- 후속 소비자는 완전한 성공 기록과 Manifest Hash를 확인하고 **복사본의 Hash도 사용 직전에 다시 검증**해야 합니다. 정전·디스크 오류로 잘린 기록, 단순 파일 존재, 이전 성공 로그를 승인으로 보지 않습니다.

## 전용 Host의 후보 빌드

이 절차는 승인된 **Linux amd64/arm64 비 Root 사용자·접근 가능한 `/dev/kvm`**에서 Manifest와 네이티브 Architecture가 일치할 때만 실행합니다. macOS에서 Builder를 직접 실행하지 않으며, [Mac 로컬 KVM Lab](macos-kvm-lab.md)의 실제 KVM 검증을 통과한 Linux arm64 VM을 전용 Host로 사용할 수 있습니다. 일반 GitHub Runner나 에뮬레이션으로 실제 KVM 검증을 대체하지 않습니다. Host에 Architecture에 맞는 `qemu-system-x86_64` 또는 `qemu-system-aarch64`, `qemu-img`, `ssh-keygen`, `xorriso`와 **Packer 1.16.0**이 필요합니다. [공식 Archive의 SHA-256](https://releases.hashicorp.com/packer/1.16.0/packer_1.16.0_SHA256SUMS)을 확인해 별도로 설치하고 실행 경로를 지정합니다. Packer가 전용 Plugin 디렉터리에 설치하는 [QEMU Plugin](https://developer.hashicorp.com/packer/integrations/hashicorp/qemu/latest/components/builder/qemu)은 **1.1.6**으로 고정합니다. 승인되지 않은 Host 접속·환경 탐색·자동 의존성 설치는 하지 않습니다.

### ARM64의 추가 조건

- 모든 이미지·Codex·브라우저·네이티브 Wheel은 Linux arm64용이어야 합니다. Chrome ZIP Root는 `chrome-linux-arm64`이며 amd64의 `chrome-linux64`와 구분합니다. 순수 Python Wheel도 고정한 Version·Hash·Metadata를 검사합니다.
- ARM64 APT는 `https://snapshot.ubuntu.com/ubuntu/<ubuntu_snapshot>/`에 날짜를 직접 고정합니다. 실제 Ubuntu 24.04에서는 `ports.ubuntu.com`과 `Snapshot:`만 조합한 초기 설정이 지원 오류로 실패했습니다. 날짜가 URL에 포함된 경로는 같은 VM에서 서명·TLS·유효기간 검사를 유지한 메타데이터 갱신과 16개 필수 패키지 후보 확인을 통과했습니다. amd64의 기존 Archive/Snapshot 설정은 유지합니다.
- 최소 구성의 Ubuntu Host에는 `ipxe-qemu`도 필요합니다. 실제 첫 빌드에서 `efi-virtio.rom` 누락으로 Virtio NIC 초기화가 실패했고, 전용 Host에 `ipxe-qemu=1.21.1+git-20220113.fbbdc3926-0ubuntu2`를 설치한 뒤 동일 장치 초기화가 통과했습니다. 이 ROM은 Host의 QEMU 의존성이며 Guest APT Lock과 구분합니다. NIC·보안 검사·하드웨어 가속을 끄는 우회는 사용하지 않습니다.
- Ubuntu 24.04 Host의 `qemu-efi-aarch64=2024.02-2ubuntu0.9`에서 아래 두 파일이 필요합니다. Builder는 원본을 쓰지 않고 Hash 검증 후 비공개 사본을 만들어 Recipe에 기록합니다. 다른 Version은 자동 수용하지 않고 검토·테스트한 Pin 변경이 필요합니다.

| `/usr/share/AAVMF/` 파일 | 크기 | SHA-256 |
| --- | --- | --- |
| `AAVMF_CODE.no-secboot.fd` | 67,108,864 Bytes | `4a4cb7f6d8106bb2a7dd8c763fab14b1810152136fc4304e5b728f0043e84f12` |
| `AAVMF_VARS.fd` | 67,108,864 Bytes | `b3b855c5a80310168051164986855692d1bdb06e67619856177965cd87c6774f` |

ARM64는 `virt`·KVM·Host CPU/GIC, Virtio GPU와 SCSI Seed CD를 사용합니다. 최초 검사는 빌드 중 변경된 NVRAM 대신 고정된 빈 VARS 사본에서 시작하고, 두 Cold Boot는 같은 검사용 VARS와 Disk Overlay를 유지합니다. CODE는 읽기 전용이며 Host 펌웨어·원본 후보는 VM의 쓰기 대상으로 사용하지 않습니다. amd64는 기존 q35 경로를 유지합니다. Worker의 실제 ARM64 VM 수명주기·Console 연결은 별도 통합 검증 대상이며 이 Builder만으로 완료 처리하지 않습니다.

Packer의 `qemuargs`는 기본 `-device` 목록 전체를 교체하므로 ARM Seed CD의 컨트롤러·드라이브를 명시적으로 연결합니다. 전용 DMI 제품명을 사용하는 Builder/Probe는 Manufacturer `KVM`도 유지합니다. ARM64의 [systemd 판별](https://github.com/systemd/systemd/blob/v255/src/basic/virt.c)은 x86 CPUID 대신 DMI를 사용하기 때문입니다. DMI는 설명용 정보이며 가속 증명이 아닙니다. Host의 네이티브 Architecture·`/dev/kvm` 검사와 QEMU의 `accel=kvm` 강제, Guest의 `kvm`·전용 제품명 검사는 그대로 유지합니다. `qemu` 결과를 허용하거나 TCG로 대체하지 않습니다.

### 실행 명령

```sh
image_build_workspace="$(mktemp -d /tmp/kelpie-build.XXXXXX)"
python3 infra/images/build.py \
  --bundle /approved/prepared-bundle \
  --output "$image_build_workspace/run" \
  --packer /approved/tools/packer \
  --execute-on-dedicated-host
```

- 실행 Flag가 없거나 Host 조건이 다르면 파일 준비·VM 생성 전에 거부합니다. Flag는 Host 승인 사실에 대한 운영자 확인이며 권한을 발급하거나 Host를 격리하는 장치가 아닙니다. 운영 서비스가 없는 전용 테스트 Host와 검토한 입력만 사용하세요.
- 출력 부모는 소유한 `0700` 디렉터리, 정규화한 출력 전체 경로는 최대 69자의 ASCII 문자·숫자·`/_.-`여야 합니다. 자동 `boot-check` 하위 경로의 Socket 길이를 빌드 전에 확보합니다. 새로운 경로만 허용하며 입력 사본·Packer Cache·40 GiB Guest Disk·검사용 후보 사본/Overlay가 함께 존재할 디스크 공간이 필요합니다.
- 완전한 입력 성공 기록과 Manifest Hash를 확인한 뒤 모든 입력을 다시 검증·복사합니다. 실행 Recipe와 Runner Unit도 사본과 SHA-256을 기록합니다. Base/출력은 외부 Backing/Data File이나 암호화가 없는 독립 qcow2, 가상 크기 최대 40 GiB여야 합니다. `.img`도 실제 형식은 qcow2여야 합니다.
- 빌드마다 임시 SSH Key를 생성하고 SSH Agent·주변 API/SCM/Cloud 환경을 전달하지 않습니다. SSH Forward와 VNC는 `127.0.0.1`, Guest Agent Socket은 비공개 실행 디렉터리에 제한합니다. 빌드 NAT의 외부 접근은 Package 설치용이며 **작업별 운영 네트워크 격리를 구현한 것은 아닙니다.**
- Guest는 Ubuntu 24.04·Manifest와 일치하는 amd64/arm64·KVM·전용 DMI 표식을 확인한 뒤 설치합니다. [Ubuntu Snapshot](https://snapshot.ubuntu.com/)에 고정한 APT Version·설치 Inventory, Hash 고정 Offline Wheel·Metadata·`pip check`, Codex ELF/Version, Browser ZIP 경계/Version을 검사합니다. Xfce/LightDM과 배정 전 비활성 Runner를 구성하며 Browser의 `--no-sandbox` 우회는 사용하지 않습니다.
- 일반 자동 APT Update Timer를 Mask합니다. 이미지는 새 Snapshot·Lock으로 재빌드하고 검증 후 교체해야 하며 이 후보를 보안 업데이트 없이 운영하지 않습니다. 설치 Inventory는 Guest의 `/opt/kelpie/apt-inventory.json`, `python-inventory.json`에 보존합니다. 아직 완전한 SBOM은 아닙니다.
- 봉인은 Builder 계정 잠금·전용 sudo 권한/SSH Key 제거, SSH Host Key·cloud-init Seed/Machine ID 초기화 후 전원을 끄도록 구성했습니다. 이 코드의 존재를 실제 재부팅·Secret 비노출 증거로 계산하지 않습니다.
- Host 실행기는 명령별 Timeout과 최대 1시간의 Packer Build Timeout, 소유한 Process Group 종료를 사용합니다. 종료 시 임시 Key 쌍을 삭제하며 기존 Bundle/부분 실행 디렉터리는 삭제하지 않습니다. Host 강제 종료·전원 장애 후 실제 잔여 VM/Key/파일 정합성 복구는 별도 Gate입니다.
- 빌드 단계 기록은 `candidate.json`의 `image_built_unverified`, **`release_eligible=false`**, Image Hash·Recipe Hash입니다. 이것만으로 CLI 전체 성공이 아니며 아래 부팅 검사가 자동으로 이어집니다. 모두 통과한 CLI 결과는 `candidate`, `boot_smoke`, `release_eligible=false`를 가진 JSON입니다. 원문 도구 진단은 노출하지 않습니다. 실패한 디렉터리를 재사용하거나 후보를 Worker Base Image로 자동 지정하지 마세요.

## 자동 부팅 상태 검사

`build.py` CLI는 생략 Flag 없이 `boot-check/`에서 아래 절차를 수행합니다. 실패하면 Exit 1이며 빌드 후보가 남아도 승인하지 않습니다. [`health.py`](../../infra/images/health.py)는 이미지에 설치되는 Guest 검사, [`qga.py`](../../infra/images/qga.py)는 비공개 Guest Agent 통신입니다.

1. 후보·Recipe·Manifest 기록과 Hash를 다시 확인하고 후보의 독립 사본을 만든 뒤 검사용 qcow2 Overlay를 생성합니다. 원본 후보를 VM의 쓰기 Disk로 사용하지 않습니다.
2. 새 cloud-init Seed로 **NIC·VNC·TCP 관리 Port 없는 KVM VM**을 시작합니다. 2 vCPU/4 GiB이며 TCG 대체를 허용하지 않습니다. 전용 DMI 표식과 소유한 QEMU PID/UID에 연결된 Unix Socket을 확인합니다. 이 Offline Smoke는 운영 작업 네트워크 격리 검증이 아닙니다.
3. [QEMU Guest Agent 계약](https://www.qemu.org/docs/master/interop/qemu-ga-ref.html)에 따라 난수·구분자로 오래된 응답을 제거하고 크기·시간을 제한합니다. `guest-exec`, `guest-exec-status`, `guest-shutdown`이 허용되어야 하며 차단된 RPC를 임의로 활성화하지 않습니다.
4. Guest는 cloud-init 완료, 봉인 기록/Manifest, 초기화된 ID, Builder 잠금/접근 제거, 알려진 자격증명 Cache 부재, 잠금 APT/Wheel/설치 Inventory, Runner Import·배정 전 비활성, Codex/Browser Version, LightDM/Xfce/X Display를 검사합니다. 전체 Secret Scan이나 인증된 Codex 작업 실행은 아닙니다.
5. 비특권 `kelpie`의 일회성 Profile로 Chromium Headless를 실행해 `data:` 문서의 JavaScript 실행 후 DOM을 확인합니다. Sandbox 우회·외부 사이트·디버깅 Port를 사용하지 않고 Profile을 정리합니다. [DOM Smoke](https://developer.chrome.com/docs/automation-and-testing/headless-cli)는 **화면의 시각 검토·클릭/키보드·Console 입력 소유권·Computer Use Acceptance를 대체하지 않습니다.**
6. 각 부팅/검사는 300초의 같은 Deadline, 종료는 60초 한도입니다. 종료 RPC에 성공 응답이 없다는 계약에 따라 QEMU Process의 Exit 0을 기다립니다. 실패/Timeout에는 직접 생성한 Process Group만 종료하며 성공 기록을 남기지 않습니다.
7. 정상 전원 종료 후 같은 Overlay를 다시 시작합니다. 두 Boot ID Hash는 달라야 하고 Machine ID Hash는 같아야 합니다. 원본 ID는 기록하지 않습니다. 이는 두 번의 Cold Boot 검사이지 별도 두 VM 간 ID 격리·실행 중 Reboot·Host 장애 복구 증거가 아닙니다.
8. 후보 사본의 Hash/크기가 변하지 않았는지 재확인한 뒤 마지막으로 `boot-smoke.json`을 씁니다. 상태는 `boot_smoke_passed_unreleased`, **`release_eligible=false`**이고 두 Guest 보고서·Image/Recipe/검사 도구 Hash를 보존합니다. 서명·SBOM·취약점·실제 GUI·Canary Gate는 별도로 남습니다.

독립 재검사는 `python3 infra/images/boot.py --build-dir /approved/completed-build --output /approved/private/new-probe --execute-on-dedicated-host`를 사용합니다. 실제 승인한 경로로 바꾸고 소유한 `0700` 부모와 새 출력(정규화한 전체 경로 최대 80자)을 지정해야 합니다. 자동/독립 검사 모두 기존 부분 출력·Overlay·Seed를 지우지 않습니다. 원인 조사와 정확한 소유 자원 정리 후 새 경로로 재시도하며 Boot 결과가 없거나 잘렸으면 실패로 취급합니다.

## 보안·미완료 Gate

공용 이미지/입력 Bundle에 Codex 로그인 캐시, API Key, SCM·Worker 자격증명을 넣지 않습니다. [공식 Codex 인증 안내](https://learn.chatgpt.com/docs/auth)는 `auth.json`을 비밀번호처럼 취급하도록 명시합니다. 이 CLI는 승인한 파일의 일치 검증이지 범용 Secret Scan·서명 검증이 아닙니다. 운영 인증 방식이나 제품 승인 Gate를 바꾸지 않습니다.

다음 Gate는 남아 있습니다: 실제 잠금 입력으로 설치·재부팅·Desktop/Browser 사용과 Codex/Runner 호환성, Wheel의 소스 Provenance·Host QEMU Toolchain 고정/재현성, SBOM·취약점 보고서·서명, 전체 자격증명 비노출, 실제 KVM Boot/Health Probe·Canary Rollback. 승인된 Linux/KVM Host와 검토한 실제 입력이 필요하며, 관련 기능 PR은 그 Gate 전까지 Draft입니다. GitHub 일반 PR CI에서 무거운 VM 빌드나 운영 자원 접근을 시작하지 않습니다.

## 검증 기록

구현 `c2ec08e`, 파일 경계 보강 `ff368cb`, CI 연결 `85e639f`에서 입력 테스트 19개와 CI 제어 테스트 7개, `make lint`가 통과했습니다. `85e639f`의 전용 PostgreSQL DB 전체 `make test`는 API 1,270개 통과/1개 Skip, Runner 45개, Worker/Gateway, Web 125개·타입 검사, 이미지 입력 19개가 통과했습니다. Skip은 macOS에서 실행할 수 없는 Linux systemd 구문 검사이며 Linux CI가 검사합니다.

검토에서 JSON 파서가 UTF-16/32도 자동 수용하는 차이를 발견했습니다. 먼저 회귀가 두 인코딩에서 실패함을 확인한 뒤 `59fda72`에서 UTF-8을 강제했고 입력 테스트 **20개**가 통과했습니다. 입력 테스트는 로컬 약 0.2초이며 기존 `Python` Job에서 실행합니다. `make test-images`가 `make test`에도 포함되고, 새 Dependency·Job·Timeout 증가는 없습니다.

테스트는 실제 별도 CLI 프로세스, 정상 복사/권한, 같은 크기 변조·실제 복사 중 원본 변경, 중복 Key·경로 이탈·링크/FIFO, 쓰기 실패, 부분 출력 재사용 거부, 동시 실행, 큰 입력의 분할 복사를 확인합니다. 테스트 파일은 명시적인 합성 바이트이며 실제 Ubuntu·Codex·Browser·Wheel로 주장하지 않습니다. 실제 사용과 최종 PR CI 결과는 PR의 검증 기록에도 남깁니다. 이 변경에는 Web UI·네이티브 입력 동작이 없어 해당 화면 변경 전후 검증은 해당 없음이며, VM Desktop 검증은 미실시입니다.

`59fda72`에서 macOS arm64/Python 3.13.5의 전용 Orca 터미널로 실제 CLI를 별도 실행했습니다. 합성 입력 4개의 정상 복사·비공개 권한·미등록 파일 제외·`release_eligible=false`, 잘못된 Hash의 Exit 1/성공 기록 없음, 부분 출력의 재사용 거부를 순서대로 확인했습니다. 초기 셸 업데이트 안내가 첫 명령을 소비해 셸 준비 후 재전송했으며 앱/셸 설정은 변경하지 않았습니다. 실제 Guest GUI를 띄웠다는 증거는 아닙니다.

후속 `7b24b20`의 Guest 설치·봉인과 `ce5a6f5`의 Host/Packer 연결에서 `make test-images` **46개**(약 1.7초)와 `make lint`가 통과했습니다. 실제 별도 프로세스로 Host 실행 거부·환경 필터·실패 정리·Timeout 자식 프로세스 종료를 확인하고, 모의 빌드 명령 순서·각 단계 실패·Key 정리·승격 금지를 검증했습니다. 실제 Packer 1.16.0과 QEMU Plugin 1.1.6으로 **합성 입력의 전체 `packer validate`**도 통과했습니다. `/dev/null`을 Packer 설정으로 지정하면 EOF로 실패하는 것을 직접 발견해 비공개 `{}` JSON 설정으로 수정했습니다. 검증용 입력·임시 키·Plugin 디렉터리는 전용 임시 디렉터리와 함께 정리했으며 VM은 시작하지 않았습니다.

`make test-image-template PACKER=/path/to/packer`는 Format와 **구문만** 검사합니다. 기존 `Go` CI에서 공식 Packer Archive를 캐시하고 매번 고정 SHA-256을 확인한 뒤 실행합니다. Plugin/VM 설치가 없고 새 Job·Matrix·8분 Timeout 증가는 없습니다. 전체 Plugin 설정 검증·실제 Build/Boot를 이 구문 검사로 대체하지 않습니다. 이번 후속 변경의 전체 회귀·최종 SHA CI는 PR 검증 기록을 확인하세요.

후속 `c63be0e`(Guest 검사), `34a16ff`(Guest Agent 통신), `0379e48`(자동 두 차례 부팅 연결)은 `make test-images` **85개 중 84개 통과/1개 Skip**(macOS 약 2.3초), `make lint`, Packer Format/구문 및 합성 입력 전체 설정 검증을 통과했습니다. Skip은 Linux `SO_PEERCRED` PID 검사이며 Linux CI에서 실제 Unix Socket으로 실행합니다. Mac에서도 실제 Unix Socket의 동기화·분할/오래된 응답·중복 Key·크기/시간 제한을 검사했습니다. 두 Boot·Guest 상태·QEMU 종료는 모의 호출 검증이며 실제 VM을 구동하지 않았습니다. 새 Python 의존성·CI Job·VM CI는 추가하지 않았습니다.

`86369b4`의 플랫폼 패키지 검증과 `a73f600`의 Guest/Boot 연결은 이미지 테스트 **97개 중 96개 통과/1개 Linux 전용 Skip**(2.216초), `make lint`, Packer Format/구문·합성 입력 전체 설정 검증을 통과했습니다. 전체 파일 보존, Metadata·Architecture 불일치, 경로/링크/크기 제한, 기존 ELF 호환성, 설치 후 보조 파일 변조와 잘못된 실행 링크를 검사합니다. 기존 CI Job 안에서 실행하며 제품 Python 의존성을 추가하지 않았습니다.

### 2026-09-10 실제 공개 입력 검증

Mac의 전용 비공개 디렉터리에서 다음 실제 파일을 확보했습니다. 이는 합성 Fixture가 아니지만 **Guest 설치·바이너리 실행·KVM/GUI 검증은 아닙니다.**

| 입력 | 고정한 대상과 실제 확인 |
| --- | --- |
| Ubuntu | [Noble `release-20260826`](https://cloud-images.ubuntu.com/releases/noble/release-20260826/) amd64 `.img`를 내려받아 공식 `SHA256SUMS`와 일치 확인. GPG 서명·qcow2 내부 검사는 아직 미실시. |
| Codex | `0.154.0-linux-x64` 플랫폼 Archive와 최상위 npm 배포물의 SHA-512를 공식 Registry Metadata의 Integrity와 비교. 플랫폼 8개 파일을 실제 추출·설치 Helper·Inventory 재검사. 인증된 Codex 작업은 미실시. |
| Browser | [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/) `153.0.8010.36` Linux64 ZIP의 실제 경계 검사·추출. 계산한 SHA-256을 후보 Lock에 기록했으며 독립 배포자 SHA-256 증명으로 표현하지 않음. |
| Runner | `1fd999fbaf32706d8bbe8f07a803c61135d96ba4`의 Git Archive에서 `hatchling==1.32.0`으로 Wheel 생성. Linux amd64/Python 3.12 대상 의존 Wheel 8개의 SHA-256·크기·비 Yank 상태를 PyPI Version Metadata와 비교. Guest `pip check`·실행은 미실시. |
| APT | Ubuntu 24.04의 격리된 메타데이터 전용 컨테이너에서 `20260909T000000Z` Snapshot의 `apt-get update`와 16개 필수 패키지의 amd64 `apt-cache policy` 확인. TLS·GPG·유효기간 검사를 유지했으며 Guest 패키지 설치 증거는 아님. |

실제 12개 amd64 입력으로 별도 `prepare.py` CLI가 Exit 0·`inputs_verified`·`release_eligible=false`를 반환했습니다. Manifest SHA-256은 `39e57c6b068eab711f53b5e929460fdf3acb2fea12e685fb505cde5bff106855`입니다. 후보 Bundle·공개 출처 Metadata·검사 로그는 로컬에 보존하고 바이너리·가상환경은 커밋하지 않습니다. 이 입력 검증 시점에는 전용 KVM Host가 없었습니다. 이후 Mac 로컬 arm64 KVM Lab은 검증됐지만, 그 디스크 없는 Linux 부팅 결과를 Golden Image Build/Boot/GUI Gate 완료로 계산하지 않습니다.

### 같은 날의 ARM64 입력 준비

실제 ARM64 입력 12개는 `20260910-arm64-candidate.1`, Manifest SHA-256 `cb6c2a695d04f6f288f2c0b6b062550fe4d7dbbf69efe4cd0ccd5be5e7678970`으로 별도 준비했습니다. 상태는 `inputs_verified`, `release_eligible=false`입니다.

| 입력 | 실제 확인한 Version·크기·SHA-256 |
| --- | --- |
| Ubuntu arm64 | `24.04-20260826`, 619,036,160 Bytes, `afa139bac6f2629c1e1f2f8f34215f3a9ad9779801bcb945521ba1a45016743f`; 공식 Checksum과 일치 |
| Codex arm64 | `0.154.0-linux-arm64`, 122,610,794 Bytes, `a2315b5f64bfeaff79b71e0d35505ba8c22cc1e96cab9dc950b614c804105b24`; Registry SHA-512 일치, 비실행 추출 후 8개 전체 파일 재검증 |
| Chrome arm64 | `153.0.8010.36`, 195,918,275 Bytes, `dfc4955719c5d494c8507990506d2d5bed174c31bf89266aa2dc5593c6607e8b`; 공식 CfT 배포 파일에서 계산한 Hash |
| PyYAML arm64 | `6.0.3`, Python 3.12 manylinux aarch64 Wheel, 775,116 Bytes, `9149cad251584d5fb4981be1ecde53a1ca46c891a79788c0df828d2f166bda28`; PyPI Metadata의 Hash·크기 일치 |

나머지 순수 Python Wheel 8개는 위 검증된 Bundle에서 Hash 검증 후 복사했습니다. Runner 소스는 `1fd999f` 이후 이번 작업까지 동일함을 Git diff로 확인했으며 원래 소스 SHA를 유지합니다. ARM64의 16개 APT 후보 Version은 같은 `20260909T000000Z` Snapshot에서 확인했고 amd64 후보와 일치했습니다. 이전 메모에 잘못 적힌 Ubuntu 파일 크기는 사전 입력 검사에서 거부됐으며 실제 크기·원래 Hash를 재확인한 새 출력만 사용했습니다.

전용 Linux VM에는 Packer `1.16.0` Linux arm64 Archive(SHA-256 `cf18f03460d92265d49b56befff333e80641d845822799eab04357c39f75b5d7`)를 공식 Checksum과 대조해 전달했습니다. 실행 파일의 양쪽 Hash `48a5367d3d1d84ebe3740a411b206b8b43143e91604179f517c77d9cad0af69d`가 일치하고 실제 Version 출력이 `Packer v1.16.0`임을 확인했습니다. 이 입력·도구 준비는 실제 Golden Image 빌드·부팅·화면 검증과 별도입니다.

`3a727a0`까지의 ARM 설치 통합과 `6c2cf2e`의 파일시스템 시계 독립 회귀 보강 후, 실제 Ubuntu 24.04 arm64/Python 3.12에서는 이미지 테스트 **125개 전부 통과**(2.818초)했습니다. Mac에서 건너뛴 `SO_PEERCRED` 검사도 포함합니다. 시계 보강 전에는 빠른 연속 쓰기의 수정 시간이 같아 변조 테스트가 의도한 메타데이터 검사 대신 후속 Hash 검사에 도달했습니다. 테스트가 실제 파일의 수정 시간을 명시적으로 바꾸도록 하고 복사 바이트의 원래 Hash까지 추가 검증했으며, 거부 조건이나 성공 기록 금지 조건을 낮추지 않았습니다. 설치 Helper 직접 호출의 잘못된 날짜·경로·개행 입력 16개 경로도 수정 전 실패/수정 후 통과를 확인했습니다.

`5fd9219` 시점 Packer Template은 실제 ARM 입력·고정 펌웨어·Packer 1.16.0/QEMU Plugin 1.1.6으로 전체 `validate`를 통과했습니다. 이는 후속 Template 변경의 실행 증거가 아닙니다. 임시 SSH Key는 삭제했고 해당 검증은 VM을 생성하지 않았습니다. 전체 회귀는 API 1,270개 통과/1개 Linux systemd Skip, Runner 45개, Worker/Gateway, Web 125개·타입 검사, Lab 18개를 통과했습니다.

후속 실제 실행에서 CD 연결 누락과 ARM64 KVM 식별 오류를 재현했습니다. `11518f9`는 Seed CD 연결을, `34ae353`은 전용 제품명과 KVM 식별의 공존을 수정했습니다. 수정 전 실패하는 회귀 검사 후 `34ae353`에서 실제 Linux 이미지 테스트 **127개 전부 통과**(2.894초), Mac **126개 통과/1개 Skip**, Packer 구문 검사와 `make lint`를 확인했습니다. 실패한 VM과 임시 Key가 정리됐고 후보 승인 기록은 생성되지 않았습니다.

`34ae353`의 새 표준 Builder 실행은 실제 SSH에서 `aarch64`·`kvm`·전용 DMI 제품명을 확인했고, 해당 QEMU 프로세스의 `/dev/kvm`·KVM VM/vCPU 핸들도 확인했습니다. 읽기 전용 Framebuffer에는 Ubuntu 24.04.4 로그인 화면이 표시됐습니다. **이 시점은 패키지 설치 중이며 봉인·두 Boot·Desktop/Browser 사용 완료 증거는 아닙니다.** Mac 화면 공유의 연결 실패도 별도로 기록했으며 원격 입력 성공으로 계산하지 않습니다.

### 이후 빌드와 브라우저 진단, 9월 10–11일

같은 표준 실행에서 설치·봉인이 완료됐습니다. 미출시 qcow2 후보는 5,073,272,832 Bytes, SHA-256 `39958a2014b08a753b0295f033cdf4d0243dd033a6f33909e3fa8d8851d5fcdf`이며 기록 상태는 `image_built_unverified`입니다. 첫 자동 Boot 검사에서 시간이 초과됐으므로 **두 Boot 성공 기록이나 출시 승인은 없습니다.** 원본 후보는 보존했고 이후 실험은 별도 진단용 Overlay에서 수행했습니다.

`fabc600`은 실제 Guest Agent 출력 수집 문제를 수정합니다. 고정 Ubuntu QGA `1:8.2.2+ds-0ubuntu1.18`은 자식이 종료돼도 stdout 전용 수집에서 `exited=false`를 계속 반환했고 별도 비루트 Agent로 재현했습니다. Boolean 수집과 실행 전에 stderr를 버리는 고정·정리된 환경의 Wrapper로 정상 결과를 받았으며 Mac/Linux 회귀를 통과했습니다. Guest 오류 원문이나 환경변수 값은 전달하지 않습니다. 이후 진단 VM에서 봉인 식별자·Builder 접근 제거·패키지 Inventory·유휴 Runner·Xfce/X Display 검사는 통과했지만 브라우저 실행은 사용자 네임스페이스 정책에 의해 거부됐습니다.

`7b46236`은 브라우저 시간 초과가 `runuser`만 종료하고 Chrome/보조 프로세스 13개를 남긴 별도 재현 오류를 수정합니다. 검사는 새 비특권 systemd 임시 서비스를 사용하며 독립적인 30초 실행 제한·5초 종료 제한·cgroup 전체 종료·정확한 소유권 검사를 적용합니다. 자동 정리 경합은 서비스가 사라지고 cgroup이 없거나 비었음을 재확인한 경우에만 허용합니다. 실제 Linux 합성 검사는 정상 종료·Exit 7·종료를 거부하는 분리된 이중 Fork 자식·실행 시간 초과·실제 `systemctl` not-found 경합을 다뤘고 살아 있는 하위 프로세스가 남지 않았습니다. [systemd의 cgroup 종료 의미](https://github.com/systemd/systemd/blob/v255/man/systemd.kill.xml)를 프로세스 그룹만 종료하는 방식으로 대체하지 않습니다. 해당 커밋의 `make test-images`는 **Mac 136개 통과/플랫폼 Skip 1개**(2.510초), **Linux 137개 전부 통과**(2.829초)이며 `make lint`도 통과했습니다. 변경하지 않은 Packer 설정은 이전 검증을 유지하며 새 빌드 증거로 표현하지 않습니다.

정확한 실행 경로의 AppArmor 정책 초안은 **진단용 Overlay에서만** 시험했고 전역 사용자 네임스페이스 제한과 Chrome Sandbox를 유지했습니다. Ubuntu의 [앱별 네임스페이스 정책](https://documentation.ubuntu.com/security/security-features/privilege-restriction/apparmor/)에 따른 방식이며 `unconfined` Profile 자체를 추가 MAC 격리로 표현하지 않습니다. 표본 Renderer에서 기대 Profile·`NoNewPrivs`·seccomp 필터·분리된 사용자/PID 네임스페이스를 확인했습니다. Computer Use로 Xfce와 JavaScript 계산 결과 `4`를 표시하는 실제 Chrome 창을 확인했습니다. 전용 Viewer는 읽기 전용이므로 데스크톱 입력이나 제품 Console 권한 검증 증거는 아닙니다.

표준 `--dump-dom` 검사는 별도 90초 진단에서도 결과를 반환하지 않았고 시작 후 CPU 활동은 적었습니다. GPU·전용 D-Bus 실험도 해결하지 못했습니다. Chrome 153의 [명령 처리 코드](https://github.com/chromium/chromium/blob/153.0.8010.36/components/headless/command_handler/headless_command_handler.cc)는 해당 옵션을 여전히 지원하므로 제거된 옵션이 원인은 아닙니다. 별도 비공개 초안은 디버거 TCP 포트 없이 [CDP Pipe](https://github.com/chromium/chromium/blob/153.0.8010.36/content/browser/devtools/devtools_pipe_handler.cc)로 정확한 Browser Version 확인·페이지 생성·계산된 DOM 검증·정상 종료를 5.11초에 완료했고 cgroup 정리도 통과했습니다. 이는 최종 커밋된 브라우저 검사가 아닌 초안입니다. 시간 제한이 적용된 진단 VM은 이후 종료됐습니다.

로컬 증거는 `arm-browser-cgroup-images-reviewed.log`, `arm-browser-cgroup-linux-reviewed.log`, `arm-browser-cgroup-real-03.log`, `arm-browser-cgroup-gc-real.log`, `arm-browser-headless-timeline-01.log`, `arm-browser-cdp-vm-01.log`와 9월 10일 18:29 KST Computer Use 화면입니다. 승격 전에는 최종 브라우저 검사·정확한 정책·파일 무결성 검사를 구현·테스트하고, 커밋된 Recipe로 재빌드해 두 번의 깨끗한 Cold Boot와 실제 상호작용을 검증해야 합니다. 9월 11일 PR #55는 `f96635c`의 Draft였고 ARM64 후속 `7b46236`까지는 미Push·새 Head CI 미실행 상태였습니다. 진단 결과로 MVP 완료 단계 수를 올리지 않습니다.
