# Worker VM 명령 시간 제한

한국어 | [English](../en/worker-command-limits.md) · [Firmware 검증](worker-firmware.md) · [MVP 진행](mvp-progress.md)

## 동작과 한계

VM 생성의 `qemu-img`, `cloud-localds`, `virt-install` 호출마다 45초 제한을 적용합니다. 더 짧은 호출자 Deadline은 연장하지 않으며, 취소/시간 초과 시 명령의 프로세스 그룹을 종료합니다. 대기 종료 지연은 1초로 제한합니다. 명령 출력·인자·경로를 오류에 포함하지 않는 기존 진단 경계와 `context.Canceled`/`context.DeadlineExceeded` 분류를 유지합니다. [Go exec의 취소·대기 계약](https://pkg.go.dev/os/exec#Cmd)을 사용하며 새 의존성이나 운영 환경변수는 없습니다.

libvirt가 소유하는 VM은 명령 프로세스의 자식 정리로 종료되지 않습니다. 기존 [물리 정리](worker-lifecycle.md)가 별도 제한 안에서 VM·디스크 부재를 확인한 다음 API/용량 반환을 수행합니다. 명령 제한은 전체 작업의 `budget_minutes`, OS Boot/Runner Readiness 또는 API 임대 만료 강제를 구현한 것이 아닙니다. 프로세스 그룹을 이탈하는 악의적 Host 프로그램을 격리하는 Sandbox/Cgroup 기능도 아닙니다. Host 도구는 신뢰된 설치를 전제합니다.

## 최종 검증 — 2026-09-11

프로그램 Commit `ce8db6af4e619459923fdac9951d34583638350c`, 실제 검사까지 포함한 Commit `ae81537ae8b6b155614ba123a8f4df02dbdbc253`입니다.

- 회귀 2개가 수정 전 실패했습니다: 자체 Deadline 미적용, 부모 취소 뒤 자식의 후속 파일 쓰기. 수정 뒤 통과했고, 더 짧은 부모 Deadline·0/음수 제한의 시작 거부·진단 비노출도 확인했습니다. 자식 Fixture는 실패 시에도 1초 후 스스로 종료하므로 관련 없는 PID를 추측해 종료하지 않습니다.
- `make test-worker` 통과(daemon 6.857초), `make lint` 통과. 최종 명령 회귀·진단 검사의 `go test -race` 통과(2.658초). Linux ARM64 Build Tag 포함 `go vet`도 통과했습니다.
- Mac M4 Pro/macOS 15.7.3의 전용 Lima Ubuntu 24.04 ARM64/libvirt 10.0.0/QEMU 8.2.2/KVM에서 명령 회귀와 실제 VM **3사례가 통과**했습니다. VM 검사는 70.64초: Terminal 27.27초, Transition 거부 21.68초, 실행 VM 생성 후 명령 취소 21.63초입니다.
- 마지막 사례는 실제 VM이 running이지만 명령이 아직 반환하지 않아 journal은 prepared인 시점을 관찰한 후 취소합니다. 명령 종료→물리 정리→모의 API ACK→영속 released→용량 반환을 확인했습니다. `running` 기록을 사후에 조작하지 않습니다.
- Go 1.24.1 Linux ARM64 테스트 바이너리 SHA-256 `72d0b6923604078f039db3092566bf8d21c60c08e17e2446571061b75b3defd6`; Commit 후 재컴파일한 바이너리는 실제 사용한 바이너리와 바이트 동일합니다. [실제 로그](../assets/worker-lifecycle/actual-command-limits.log), SHA-256 `af3d4ed7321000a67a2abacbf4e1e804dde93efe717dfb656d66c2e0312daa60`.
- 최종 Domain 목록은 비었고 빈 원본/Overlay/seed/NVRAM·임시 도구/HTTP 프로세스를 정리했습니다. 실행별 `0600` 소유권 기록, 기존 Image와 비활성 `default` Network는 보존했습니다. 제거한 Fixture는 재생성할 수 있습니다.

폐기 가능한 비특권 Linux ARM64 KVM Host에서만 실제 검사를 실행합니다.

```bash
cd apps/worker
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only go test -tags libvirt_integration ./internal/daemon -run 'TestVMCommand|TestCommandFailure|TestDedicatedLibvirtExecutorFirmware' -count=1 -v
```

Firmware Fixture는 NIC/Graphics를 `none`으로 교체하고 빈 디스크·모의 HTTP·가짜 임대를 사용합니다. 명령 취소 사례에서는 실제 `virt-install` 성공 뒤 테스트 Wrapper를 대기시켜 취소합니다. 실제 libvirt 내부 RPC가 처리 중인 시점의 강제 중단·Golden Image/Runner·정상 OS 종료·전체 VM 시간 예산·네트워크/GUI·동시 두 작업은 검증하지 않았습니다. UI 변경이 없어 화면/키보드 검사는 해당하지 않습니다.

선행 PR #58/#60은 `main`에 병합됐으며 PR #61은 `main`을 Base로 명령 제한 변경만 비교합니다. 이 기능의 정상·실패·취소 경로는 위 실제 증거로 검증됐습니다. 전체 Golden Image/Runner·네트워크·GUI·VM 시간 예산은 별도 기능과 MVP 출시 조건이지 이 PR의 Draft 유지 사유가 아닙니다. 최종 Head CI·리뷰·병합 상태는 [PR #61](https://github.com/haesookimDev/dev-agent/pull/61)에 기록합니다. 적용/롤백 모두 검증된 중지 후 수행하고, 미확인 정리의 기록·예약은 보존합니다. 느린 Host가 45초를 초과하면 작업 실패/정리 경로로 들어가며 제한을 자동 연장하거나 증거를 성공으로 바꾸지 않습니다. MVP 고정 완료율은 1/7(14.3%)입니다.

2026-09-13 선행 변경을 통합한 `ec30f6a`에서 `make test-worker`(daemon 7.873초), `make lint`와 Diff 검사가 통과했습니다. 실제 검증 Commit 이후 실행 코드·테스트·설정은 동일하며, 충돌은 한·영 진행 기록에서만 해결했습니다. 원본 실제 로그 Hash와 검증 범위를 대조했으며 같은 VM 검사를 새로 실행했다고 표현하지 않습니다. 문서 전용 후속이므로 로컬 전체 `make test`와 실제 VM 검사는 반복하지 않습니다.
