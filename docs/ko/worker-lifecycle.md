# Worker VM 소유권과 자원 반환

한국어 | [English](../en/worker-lifecycle.md)

## 보장하는 순서

Worker는 API가 Claim에 발급한 임대 UUID를 Run UUID로 사용하고, VM 명령 전에 비공개 Schema 2 소유권 기록을 디스크에 동기화합니다. 이미 존재하는 임대 디렉터리는 `released` 상태여도 재사용하지 않으며 새로운 작업 재시도 프로토콜을 추가하는 것은 아닙니다. 실행 디렉터리의 `.worker.lock`은 프로세스 간 중복 소유를 차단하며 실행 정리와 실패 보고가 끝날 때까지 유지합니다.

`prepared → running(선택) → cleanup-pending → cleaned → released`는 덮어쓰지 않는 단계별 JSON 기록입니다. 기록에는 작업·실행 UUID, 자원 수량, 생성 시각만 들어가고 임대 토큰·할당 내용·환경변수·저장소 주소는 저장하지 않습니다.

1. 실제 도메인 UUID·이름·소유권 Description·디스크 경로를 확인합니다.
2. ACPI 종료를 요청하고 최대 20초를 관찰합니다. 필요하면 소유권을 재확인한 뒤 해당 UUID만 강제 종료합니다.
3. 정지를 확인한 도메인만 등록 해제합니다. 다른 도메인의 현재 XML과 영속 도메인의 다음 부팅 설정도 검사합니다.
4. 해당 도메인의 부재와 다른 도메인의 디스크 참조 부재를 확인한 뒤 정확한 실행 산출물만 삭제합니다. 재귀 삭제나 libvirt의 전체 Storage 삭제는 사용하지 않습니다.
5. `cleaned`를 기록한 뒤 API 임대 해제를 요청합니다. API 응답과 `released` 기록이 확인되어야 로컬 용량을 반환합니다.

정리 실패·조회 실패·알 수 없는 파일·소유권 불일치는 성공으로 추정하지 않습니다. 취소된 실행의 정리는 별도 60초 제한으로 수행하고, daemon 종료도 실행 정리가 끝날 때까지 기다립니다. 정리 중 Heartbeat는 기존 예약을 유지합니다. API 해제 응답 후 로컬 기록 저장만 실패했다면 같은 프로세스의 재시도는 API 해제를 반복하지 않습니다.

## Linux 파일 접근 경계

지원 호스트는 Ubuntu의 `libvirt-qemu` 계정과 `qemu:///system`입니다. VM을 Worker나 root 계정으로 실행하거나 libvirt DAC/AppArmor를 끄지 않습니다. 실행 디렉터리에는 해당 QEMU UID의 **진입만 허용하는 ACL**을 부여합니다. 목록 조회·쓰기·기본 상속 ACL·다른 사용자 권한은 허용하지 않으며 메타데이터와 기록은 Worker 소유 `0600`을 유지합니다.

ACL의 Mask 때문에 디렉터리의 표시 Mode는 `0710`일 수 있습니다. 이 숫자만 신뢰하지 않고 정확한 이름 있는 UID의 ACL을 검증합니다. ACL 읽기와 설정은 열린 디렉터리 FD에만 수행합니다. 알 수 없는 기존 ACL을 덮어쓰지 않습니다. `getfacl`·`setfacl`을 제공하는 Ubuntu `acl` 패키지가 필요합니다.

libvirt는 종료 뒤 읽기 전용 seed의 QEMU 소유권을 유지하거나, 생성 실패 뒤에도 QEMU 소유 NVRAM을 남길 수 있습니다. 도메인 부재·외부 참조 검사 뒤 실행의 정확한 `root.qcow2`·`seed.iso`·`nvram.fd`만 해당 UID의 일반 파일 `0600`, Hardlink 수 1을 허용합니다. 다른 소유자·링크·공개 파일·QEMU 소유 메타데이터는 거부합니다. 호스트 접근 제약은 [libvirt 문서](https://www.libvirt.org/drvqemu.html), ACL의 권한 계산은 [Linux ACL 명세](https://www.man7.org/linux/man-pages/man5/acl.5.html)를 참고하세요.

WorkRoot 상위 경로와 Base Image는 관리자가 hypervisor에서 접근 가능한 전용 위치에 준비해야 합니다. Worker는 사용자의 Home이나 기존 이미지 디렉터리 권한을 자동으로 변경하지 않습니다. macOS의 기본 테스트는 파일·상태·동시성 계약을 검증하지만 Linux ACL이나 실제 KVM의 증거가 아닙니다.

## 재시작과 마이그레이션 제한

등록 전에 소유권 기록을 읽고 남은 실행을 물리적으로 정리합니다. 종료된 작업의 Schema 2 기록은 개별 Worker 인증으로 정확한 임대·작업·자원을 대조하고, 물리적 부재를 다시 확인한 뒤 API 복구 확인과 `released` 기록을 마칩니다. 다시 등록해 동일 Worker·실행 수 0·설정된 전체 가용 자원을 확인해야 새 작업을 받습니다. 누락·모호한 응답·Redirect를 성공으로 인정하지 않습니다. [재시작 검증](worker-restart-recovery.md)을 참고하세요.

Schema 1의 독립적인 임의 Run UUID는 물리 정리를 위해 읽을 수 있지만 API 임대로 자동 채택하지 않습니다. 실행 중 작업·Claim 응답 유실·알 수 없는 기록·유효하지 않은 자격증명·서버의 미해결 예약은 여전히 새 작업 수락을 차단하며 Heartbeat로 0을 덮어쓰지 않습니다. 현재 복구에는 `online` Worker가 필요합니다. 종료 임대 복구이지 모든 중단 작업의 완전 자동 복구는 아닙니다.

이전 `<WorkID>` 디렉터리는 새 UUID 기록으로 자동 채택하거나 삭제하지 않습니다. 운영 적용 전 해당 Worker를 Drain하고 기존 VM·임대·파일의 소유권을 확인해야 합니다. 기록을 지우거나 `released` 파일을 수동 생성해서 시작 제한을 우회하지 마세요. API Schema/Migration은 변경하지 않습니다. 롤백은 실행 중 작업과 기록을 보존한 채 검증된 중지 절차 뒤에 수행하며, 구버전은 새 기록을 이해하지 못하므로 같은 WorkRoot에서 바로 재실행하지 않습니다.

## 검증 범위

```sh
make test-worker
(cd apps/worker && go test -race ./...)
make lint
```

일반 회귀는 기존 Go CI에 포함되며 추가 Job·Matrix·Timeout은 없습니다. 실제 libvirt 검사는 별도의 명시적 동의와 `libvirt_integration` Build Tag가 필요합니다. 빈 도메인 목록을 가진 폐기 가능한 Linux 호스트에서만 실행합니다.

```sh
cd apps/worker
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only go test -tags libvirt_integration ./internal/daemon -run '^TestDedicatedLibvirtCleanupBeforeRelease$' -count=1 -v
```

이 검사는 기존 이미지 대신 작은 빈 디스크, 빈 cloud-init, 네트워크 없는 실제 KVM을 만듭니다. API 응답은 합성입니다. 빈 VM의 ACPI 미응답에 따른 강제 종료와 호출자 취소를 검사하며 OS 정상 종료·Runner·브라우저·Console·운영 API Acceptance를 대신하지 않습니다. 성공·실패의 소유권 기록을 `/var/tmp/kelpie-lifecycle-*`에 보존하고 정리가 확인되지 않은 파일을 재귀 삭제하지 않습니다.

전체 Golden Image·작업별 네트워크·실제 시간 예산 강제·Readiness·실행 중 작업/Claim 유실 복구·동시 두 작업 Acceptance는 여전히 별도 완료 조건입니다. 현재 릴리즈 진행률은 [MVP 기록](mvp-progress.md)의 고정 기준을 따릅니다.

### 실제 Mac 내부 Linux 검증 — 2026-09-11

검증 소스는 `74a8edf35e6d3e3d4ae1b6aefee729473112ea47`입니다. Go 1.24.1에서 Linux/ARM64·CGO 없이 컴파일한 테스트 바이너리를 Mac의 전용 Lima Ubuntu 24.04에서 실행했습니다. 커밋 후 재컴파일한 바이너리와 실제 사용한 바이너리의 바이트가 동일하며 SHA-256은 `8225eb15db7ce27a6f66d541c81e78918f06c4b8f849332b27e43ee0cb64442c`입니다. libvirt 10.0.0/QEMU 8.2.2, 실제 KVM, 비특권 Worker UID를 사용했습니다.

- [실제 VM 2사례](../assets/worker-lifecycle/actual-libvirt.log): Terminal 경로 21.98초, 호출자 취소 21.60초, 합계 43.62초 통과. 각 VM은 순차 생성됐으며 둘 다 빈 디스크의 ACPI 미응답 뒤 강제 종료를 검증했습니다. 동시 두 작업이나 정상 OS 종료의 증거가 아닙니다.
- [생성 실패 후 NVRAM 복구](../assets/worker-lifecycle/recovery-creation-failure.log)와 [종료 후 QEMU 소유 seed 복구](../assets/worker-lifecycle/recovery-seed-owner.log): 각각 0.03초 통과. 초기 실제 실패가 남긴 파일을 새 바이너리로 정리하고 `cleaned` 기록을 보존했으며 API 해제 확인을 만들지 않았습니다.
- 실제 `libvirt-qemu` UID로 실행 디렉터리 진입 가능, 목록 조회·쓰기 불가, `run.json`·`user-data` 읽기 불가를 확인했습니다. 최종 실제 도메인 목록은 비어 있습니다. 삭제한 것은 이 검사의 빈 디스크·seed·NVRAM뿐이며 Fixture로 재생성할 수 있습니다. 기존 이미지·사용자 데이터는 보존했습니다.
- Mac `make test-worker`·전체 Worker Race 검사·`make lint`, Host Shell 문법 통과. Linux의 소유권·복구 관련 24개 회귀에 이어 최종 ACL·파일 소유자·정리 회귀 12개가 통과했습니다. PATH 도구 선택은 수정 전 Linux에서 실패하고 절대 경로 수정 후 통과했습니다. GitHub 최종 Head CI는 PR에 별도로 기록합니다.

실제 검증은 소유권 저장소·ACL·정리기·daemon 자원 반환을 연결한 테스트입니다. `LibvirtExecutor` 전체 Golden Image 부팅·Runner 제어·기본 네트워크·ARM Firmware 구성은 아직 별도 검증이 필요하므로 이를 전체 실행기 Acceptance로 보고하지 않습니다.
