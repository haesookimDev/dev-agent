# Worker ARM64 Firmware 소유권 검증

한국어 | [English](../en/worker-firmware.md) · [수명주기](worker-lifecycle.md) · [진행 현황](mvp-progress.md)

## 변경과 호환성

`LibvirtExecutor`는 KVM을 명시하고 ARM64에서 UEFI와 실행별 `<WorkRoot>/<lease_id>/nvram.fd`, SCSI seed CD를 지정합니다. 쓰기 가능한 UEFI 상태는 [영속 소유권](worker-lifecycle.md)과 같은 실행에 속하며 종료·정의 해제·파일 정리 전에 임대/용량을 반환하지 않습니다. amd64의 기존 Firmware·seed Bus 선택은 유지합니다. 지원하지 않는 아키텍처는 VM 명령 전에 실패합니다.

Firmware는 신뢰된 Host의 libvirt 설정에서 선택합니다. ARM64의 AAVMF와 KVM 지원이 필요하며, 자동 선택 실패 시 BIOS/TCG로 우회하지 않습니다. [virt-install 4.1 명세](https://github.com/virt-manager/virt-manager/blob/v4.1.0/man/virt-install.rst)와 [libvirt NVRAM 명세](https://www.libvirt.org/formatdomain.html#guest-firmware)를 따릅니다. 새 운영 환경변수·의존성·DB Migration은 없습니다. Image를 교체하거나 Host Firmware 파일을 수정하지 않습니다.

초기 수정에서 모든 아키텍처에 UEFI를 적용했지만 Golden Image의 amd64 BIOS 계약과 충돌할 수 있음을 검토로 확인했습니다. 후속 회귀가 amd64/지원하지 않는 아키텍처에서 먼저 실패했고 ARM64에 한정한 수정 뒤 통과했습니다. 이 초기 상태는 배포하지 않았으며 Commit 기록은 보존합니다.

## 최종 코드 검증 — 2026-09-11

소스·테스트 Commit: `dafc0dab677756887b2707871e83deb3ab1748da`. Go 1.24.1 Linux ARM64 테스트 바이너리 SHA-256: `da18ebe5803d75b9dad649ec24c8c77b3ffb2452cf06b3c4edfec0f8627b9833`. Commit 후 다시 컴파일한 바이너리와 실제 사용한 바이너리는 바이트가 동일합니다.

- `make test-worker`: 통과(daemon 5.314초). `make lint`, Linux ARM64 Build Tag 포함 `go vet` 통과.
- 회귀: 소유 NVRAM/KVM/SCSI 인자 누락 RED→GREEN, amd64 부팅 계약 보존·미지원 아키텍처 거부 RED→GREEN.
- 실제 Mac M4 Pro/macOS 15.7.3의 전용 Lima Ubuntu 24.04 ARM64, libvirt 10.0.0/QEMU 8.2.2/KVM, 비특권 Worker UID에서 **2사례/48.90초 통과**. 정상 Terminal 반환 27.24초, VM 생성 뒤 API Transition 거부 21.62초입니다.
- 실제 `LibvirtExecutor`의 새 Overlay·cloud-init seed 생성, ACL, `virt-install`, running 기록과 NVRAM 경로를 검사했습니다. Domain은 KVM, Loader는 read-only pflash이며, 종료/정의 해제/산출물 부재 뒤 모의 HTTP API ACK·영속 released·단일 용량 반환을 확인했습니다.
- [최종 실제 로그](../assets/worker-lifecycle/actual-executor-firmware.log), SHA-256 `dc1937eb2cb5b1f067a41fc6c97f1a92346ad03223cc4d9c9459e5e97a556e0d`. 최종 Domain 목록은 비었고 원래 비활성인 `default` Network는 변경하지 않았습니다.

초기 실제 검사는 VM 반환 후 테스트 원본 파일 삭제에서 실패했습니다. libvirt가 QEMU 소유로 바꾼 파일을 `/var/tmp`의 Sticky 규칙 때문에 Worker가 제거할 수 없었습니다. Fixture를 별도 Worker 소유 디렉터리와 정확한 QEMU 진입 ACL로 수정했습니다. 최종 실행에서는 생성한 빈 원본/Overlay/seed/NVRAM과 임시 도구만 제거했고 실행별 `0600` 소유권 기록을 남겼습니다. 초기 실패의 남은 빈 원본 한 개도 미사용·소유권 확인 후 정확한 경로만 정리했습니다. 모두 재생성 가능하며 기존 Image/사용자 파일은 보존했습니다. 실패를 성공으로 계산하지 않습니다.

## 재현과 한계

빈 Domain 목록과 실제 KVM을 가진 폐기 가능한 **Linux ARM64** Host에서만 실행합니다. root 실행을 거부하고 명시적 Opt-in이 없거나 ARM64가 아니면 Skip합니다.

```bash
cd apps/worker
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only go test -tags libvirt_integration ./internal/daemon -run '^TestDedicatedLibvirtExecutorFirmware$' -count=1 -v
```

**이 Fixture는 NIC/Graphics 인자만 `none`으로 교체합니다.** 기존 공유 `default` Network를 켜지 않으며, 빈 원본 디스크·모의 API·가짜 임대만 사용합니다. 따라서 실제 Firmware/생성/정리의 증거이지 Golden Image OS 부팅, 정상 OS Shutdown, Runner 실행, 실제 API/DB, Preview/GUI, 작업별 네트워크·시간 제한 또는 전체 Executor Acceptance의 증거가 아닙니다. UI 변경이 없어 브라우저/키보드 검사는 해당하지 않습니다. amd64 실제 실행은 이번에 수행하지 않았고 기존 Image 검증 Gate로 남습니다.

선행 PR #58의 소유권 코드는 `90af0a4e7e70a5795e95dc85e95add01a45e478d`로 `main`에 병합됐습니다. PR #60의 Base도 `main`으로 옮겨 펌웨어 변경만 비교합니다. 검증된 중지·Drain 후 적용하고 정리 실패 시 기록·용량을 보존합니다. 롤백은 검증된 중지 후 이전 바이너리로 복귀하며, ARM Firmware 경로를 모르는 이전 Executor에서 새로운 ARM 작업을 시작하지 않습니다. 기록 삭제나 수동 released 작성으로 우회하지 않습니다. 이 기능의 실제 생성·펌웨어·정리 검증은 위 증거로 확인됐으며, 최종 Head CI·리뷰·병합 상태는 [PR #60](https://github.com/haesookimDev/dev-agent/pull/60)에 기록합니다. 전체 Golden Image/Runner·네트워크·GUI 조건은 별도 기능과 MVP 출시 기준이며 이 PR의 Draft 유지 사유로 혼동하지 않습니다. MVP 완료율은 고정 기준 1/7(14.3%)입니다. 2026-09-13 재검토에서는 최종 소스 이후 문서/로그만 바뀐 것과 원본 검증 Hash를 확인했으며 동일한 실제 VM 검사를 새로 실행했다고 표현하지 않습니다.
