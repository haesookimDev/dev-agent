# Golden Image 후보 빌드 — Draft

한국어 | [English](../en/golden-image-inputs.md) · [MVP 현황](mvp-progress.md) · [IMG-001](roadmap-detailed.md#img-001--재현-가능한-golden-image-pipeline--l)

## 이번 범위

[`prepare.py`](../../infra/images/prepare.py)는 입력 검증·별도 사본 준비만 수행합니다. [`build.py`](../../infra/images/build.py)와 [Packer 설정](../../infra/images/ubuntu.pkr.hcl)은 승인한 전용 KVM Host에서 [`guest.py`](../../infra/images/guest.py)의 설치·봉인을 실행하는 후보 Builder입니다. **실제 Image Build/Boot/Desktop 검증과 릴리즈 Gate는 미완료이며 IMG-001도 미완료입니다.** 현재 Worker의 Base Image 설정이나 실행 경로를 바꾸거나 자동 Rollout하지 않습니다.

승인된 Ubuntu 24.04 amd64 이미지, Codex 실행 파일, Linux 브라우저 ZIP, Runner와 전체 Python 의존성 Wheel을 먼저 확보해야 합니다. 기대 Hash는 검토한 공식 배포 자료/빌드 증거에서 확인하세요. 출처를 확인하지 않은 파일을 직접 Hash한 것만으로 신뢰할 수 있는 배포물이 되지는 않습니다. 저장소에는 실제 검증하지 않은 Version·Checksum을 릴리즈 Lock으로 넣지 않습니다.

## Version 1 입력 계약

Manifest는 UTF-8 JSON이며 최대 128 KiB입니다. 아래 Field만 허용하고 중복 JSON Key·알 수 없는 Field·가변 Alias(`latest`, `current`, `main` 등)를 거부합니다. 입력 준비 단계에서 Version은 선언 값이며 실제 설치 가능성 검사는 아닙니다.

| Field | 필수 내용 |
| --- | --- |
| `schema_version`, `architecture` | 정수 `1`, 문자열 `amd64` |
| `image_version` | 검토한 고정 이미지 Version 식별자 |
| `ubuntu_snapshot` | 유효한 UTC 날짜 `YYYYMMDDTHHMMSSZ`; 실제 가용성은 Guest의 APT 실행에서 확인 |
| `runner_source_commit` | Runner 소스의 소문자 40자리 Git SHA; Wheel과 소스 간 Provenance 증명은 후속 Gate |
| `apt_packages` | Package 이름 → 정확한 Version 문자열의 Object, 최대 256개 |
| `base_image`, `codex`, `browser` | 각각 `file`, `version`, `sha256`, `size_bytes`를 가진 Object |
| `runner_wheels` | 동일 4개 Field와 `name`을 가진 Wheel Object 목록, 1~64개. `kelpie-vm-runner` 필수, 정규화된 Package 이름 중복 금지 |

입력 준비의 필수 APT 이름은 `build-essential`, `ca-certificates`, `dbus-x11`, `git`, `nodejs`, `npm`, `python3.12-venv`, `qemu-guest-agent`, `xfce4`, `xorg`입니다. Builder는 추가로 `lightdm`, `libnss3`, `libgbm1`, `libasound2t64`, `fonts-liberation`의 정확한 Version을 요구합니다. 이 목록이 완전한 Desktop/Browser 의존성 집합임을 보장하지 않으며 실제 설치·사용 검증이 필요합니다.

`file`은 하위 경로 없는 ASCII 파일명이며 대소문자 Alias까지 중복을 금지합니다. Base Image 확장자는 `.qcow2`/`.img`(최대 64 GiB), Browser는 `.zip`(2 GiB), Wheel은 `.whl`(개당 256 MiB), Codex는 압축 해제된 단일 실행 파일(1 GiB)입니다. `size_bytes`는 양의 정수, `sha256`은 소문자 64자리입니다. 확장자 검사는 내부 형식·Architecture·실행 안전성 검사가 아닙니다.

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

이 절차는 승인된 **Linux amd64 비 Root 사용자·접근 가능한 `/dev/kvm`**에서만 실행합니다. 로컬 Mac이나 GitHub 일반 Runner에서 VM 구동으로 대체하지 않습니다. Host에 검토한 `qemu-system-x86_64`, `qemu-img`, `ssh-keygen`, `xorriso`와 **Packer 1.16.0**이 필요합니다. [공식 Archive의 SHA-256](https://releases.hashicorp.com/packer/1.16.0/packer_1.16.0_SHA256SUMS)을 확인해 별도로 설치하고 실행 경로를 지정합니다. Packer가 전용 Plugin 디렉터리에 설치하는 [QEMU Plugin](https://developer.hashicorp.com/packer/integrations/hashicorp/qemu/latest/components/builder/qemu)은 **1.1.6**으로 고정합니다. 승인되지 않은 Host 접속·환경 탐색·자동 의존성 설치는 하지 않습니다.

```sh
image_build_workspace="$(mktemp -d /tmp/kelpie-build.XXXXXX)"
python3 infra/images/build.py \
  --bundle /approved/prepared-bundle \
  --output "$image_build_workspace/run" \
  --packer /approved/tools/packer \
  --execute-on-dedicated-host
```

- 실행 Flag가 없거나 Host 조건이 다르면 파일 준비·VM 생성 전에 거부합니다. Flag는 Host 승인 사실에 대한 운영자 확인이며 권한을 발급하거나 Host를 격리하는 장치가 아닙니다. 운영 서비스가 없는 전용 테스트 Host와 검토한 입력만 사용하세요.
- 출력 부모는 소유한 `0700` 디렉터리, 정규화한 출력 전체 경로는 최대 80자의 ASCII 문자·숫자·`/_.-`여야 합니다. 새로운 경로만 허용하며 충분한 디스크 공간이 필요합니다. 입력 사본·Packer Cache·40 GiB Guest Disk가 함께 존재할 수 있습니다.
- 완전한 입력 성공 기록과 Manifest Hash를 확인한 뒤 모든 입력을 다시 검증·복사합니다. 실행 Recipe와 Runner Unit도 사본과 SHA-256을 기록합니다. Base/출력은 외부 Backing/Data File이나 암호화가 없는 독립 qcow2, 가상 크기 최대 40 GiB여야 합니다. `.img`도 실제 형식은 qcow2여야 합니다.
- 빌드마다 임시 SSH Key를 생성하고 SSH Agent·주변 API/SCM/Cloud 환경을 전달하지 않습니다. SSH Forward와 VNC는 `127.0.0.1`, Guest Agent Socket은 비공개 실행 디렉터리에 제한합니다. 빌드 NAT의 외부 접근은 Package 설치용이며 **작업별 운영 네트워크 격리를 구현한 것은 아닙니다.**
- Guest는 Ubuntu 24.04 amd64·KVM·전용 DMI 표식을 확인한 뒤 설치합니다. [Ubuntu Snapshot](https://snapshot.ubuntu.com/)에 고정한 APT Version·설치 Inventory, Hash 고정 Offline Wheel·Metadata·`pip check`, Codex ELF/Version, Browser ZIP 경계/Version을 검사합니다. Xfce/LightDM과 배정 전 비활성 Runner를 구성하며 Browser의 `--no-sandbox` 우회는 사용하지 않습니다.
- 일반 자동 APT Update Timer를 Mask합니다. 이미지는 새 Snapshot·Lock으로 재빌드하고 검증 후 교체해야 하며 이 후보를 보안 업데이트 없이 운영하지 않습니다. 설치 Inventory는 Guest의 `/opt/kelpie/apt-inventory.json`, `python-inventory.json`에 보존합니다. 아직 완전한 SBOM은 아닙니다.
- 봉인은 Builder 계정 잠금·전용 sudo 권한/SSH Key 제거, SSH Host Key·cloud-init Seed/Machine ID 초기화 후 전원을 끄도록 구성했습니다. 이 코드의 존재를 실제 재부팅·Secret 비노출 증거로 계산하지 않습니다.
- Host 실행기는 명령별 Timeout과 최대 1시간의 Packer Build Timeout, 소유한 Process Group 종료를 사용합니다. 종료 시 임시 Key 쌍을 삭제하며 기존 Bundle/부분 실행 디렉터리는 삭제하지 않습니다. Host 강제 종료·전원 장애 후 실제 잔여 VM/Key/파일 정합성 복구는 별도 Gate입니다.
- 성공 기록은 `candidate.json`의 `image_built_unverified`, **`release_eligible=false`**, Image Hash·Recipe Hash입니다. 원문 도구 진단은 CLI에 노출하지 않습니다. 실패한 디렉터리를 재사용하거나 후보를 Worker Base Image로 자동 지정하지 마세요. 원인 조사·소유 자원 정리 후 새 경로로 재실행합니다.

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
