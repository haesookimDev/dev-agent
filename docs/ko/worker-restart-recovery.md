# Worker 재시작 후 종료 임대 복구

한국어 | [English](../en/worker-restart-recovery.md) · [수명주기](worker-lifecycle.md) · [API 계약](worker-lease-reconciliation.md)

## 구현과 안전 경계

검증 코드·테스트 기준은 `b9fdd484f4d7777345e89694c0c2a00b42f87816`입니다. `main` 병합 여부와 최종 CI는 [진행 현황](mvp-progress.md) 및 PR에서 별도로 확인합니다.

1. Claim의 공개 임대 UUID를 Schema 2 `run_id`로 저장하고 VM 명령 전에 동기화합니다. 같은 임대 디렉터리를 다시 만들지 않으며 작업 Token은 기록하지 않습니다.
2. 시작 시 WorkRoot 잠금을 획득하고 영속 소유권·도메인·외부 디스크 참조를 검증해 남은 VM과 정확한 파일만 정리합니다.
3. 개별 Worker 자격증명으로 등록·임대 조회를 수행하고 Worker/Lease/Work ID와 예약 자원을 대조합니다. 종료된 작업만 허용합니다.
4. 네트워크 조회 후 물리적 부재를 다시 확인하고 복구 API의 정확한 `204`를 받은 뒤에만 `released`를 기록합니다.
5. 같은 Worker의 등록 정보를 새로 읽어 실행 수 0과 설정된 전체 가용 자원을 확인한 뒤 Heartbeat/Claim을 시작합니다.

등록·조회는 정확한 `200`, 제한된 JSON 크기, 필수 필드·타입·정체성을 검사합니다. 누락된 실행 수를 0으로 추정하지 않습니다. 중복 키·Null·대소문자 별칭·후행 JSON·Redirect·잘못된 반환 상태는 거부합니다. 자격증명 파일은 요청마다 다시 읽으며 Worker 인증을 VM이나 Run Token Header로 보내지 않습니다.

응답 유실 시 `cleaned`를 보존하고 다음 시작에서 서버 상태를 재확인합니다. API의 중복 방지와 로컬 `released` 기록으로 자원·감사·이벤트를 중복 가산하지 않습니다. API는 원격 VM을 직접 검사하지 않으며 Worker의 정리 선언을 신뢰한다는 경계는 유지됩니다.

## 실제 검증 — Mac 내부 Linux, 2026-09-11

[수정하지 않은 최종 로그](../assets/worker-lifecycle/actual-restart-postgres.log), SHA-256 `c550e90009cc1f668a2f21e4688bc49e56c1e2661cc40e00d2e23e2332e8575f`.

- Go 1.24.1, Linux/ARM64, CGO 비활성. 실제 Worker 바이너리는 위 Commit과 `vcs.modified=false`를 포함하며 SHA-256은 `99f8b64e27db8cb1ad3a12280ca0665fce8912f222edf3994242398ca3b2f544`입니다. 테스트 바이너리는 `87f8426f5c2928e015319a9af79320dc7dc464f1ac3ced140db173476e28b16c`이며 커밋 전후 재빌드가 동일합니다.
- 전용 Lima Ubuntu 24.04의 비특권 Worker/libvirt 계정과 실제 KVM을 사용했습니다. 작은 빈 디스크·빈 cloud-init·소유 NVRAM만 생성했고 NIC/화면은 연결하지 않았습니다. 기존 Golden Image나 사용자 디스크를 사용하지 않았습니다.
- Mac Uvicorn의 임시 Loopback TCP 포트와 PostgreSQL 17의 새 DB/계정/Schema를 사용했습니다. SSH는 Linux 호스트의 Loopback 포트만 전달하며 SSH Agent·Mac Home을 VM에 전달하지 않았습니다. Lifespan/백그라운드 작업은 끈 Fixture이며 운영 시작 검증은 아닙니다.
- 실제 API Claim→소유 기록→실제 실행 VM→작업 실패 전이 후, Run Token 없이 새 Worker 실행 파일을 시작했습니다. 정리·복구 확인·전체 용량 확인 뒤 준비 로그를 확인하고 정상 종료했습니다. 두 번째 새 프로세스도 같은 기록으로 시작·종료했습니다.
- Go 검사 **22.15초 통과**, PostgreSQL을 포함한 검사 **1개/22.88초 통과**. 최종 DB는 CPU 4·Memory 8192 MiB·Disk 60 GiB, 실행 수 0, `released`, 반환 이벤트 1개·복구 감사 1개, 작업 `failed`/Version 3입니다.
- 최종 실제 Domain 목록과 전달 포트는 비었고 자격증명 파일·임시 DB/계정·SSH/API 프로세스는 정리했습니다. 실행 기록만 `/var/tmp/kelpie-lifecycle-2234943308/e0fec201-cf78-43ce-bc8c-bfe5092bd9e5`에 남겼습니다. 삭제한 빈 디스크·seed·NVRAM은 Fixture로 재생성할 수 있으며 기존 이미지·데이터는 보존했습니다.

이 검사는 실제로 실행 중인 VM의 영속 기록을 Fixture가 준비하고 **새 Worker 프로세스**가 복구한 증거입니다. 기존 전체 Executor 프로세스에 SIGKILL을 주입한 검사, Golden Image의 OS/Runner 정상 실행, GUI·Browser·Console·동시 두 작업 Acceptance는 아닙니다. UI 변경이 없어 화면·키보드 검사는 해당하지 않습니다.

## 반복 실행과 CI

[Go Fixture](../../apps/worker/internal/daemon/libvirt_api_recovery_integration_test.go)와 [API/PostgreSQL 제어 검사](../../apps/api/tests/test_worker_vm_recovery_postgres.py)를 함께 실행합니다. 승인된 빈 Linux KVM Lab에 해당 Commit의 Linux ARM64 Worker·`libvirt_integration` 테스트 바이너리를 복사한 뒤 다음과 같이 비밀정보 없는 로컬 설정을 준비합니다. SSH 설정은 `127.0.0.1`의 Lab과 `kelpie` 사용자를 가리켜야 합니다.

```json
{
  "acknowledgement": "disposable-host-and-isolated-api-only",
  "ssh_config": "/absolute/path/to/lima/kelpie-kvm/ssh.config",
  "ssh_host": "lima-kelpie-kvm",
  "test_binary": "/home/kelpie/worker-recovery-02.test",
  "worker_binary": "/home/kelpie/worker-recovery-02",
  "forward_port": 58867
}
```

`KELPIE_TEST_POSTGRES_URL`은 전용 최신 Schema 테스트 DB에 안전하게 주입합니다. Credential은 검사에서 발급해 표준입력과 `0600` 파일로만 전달하며 명령 인자나 문서에 넣지 않습니다.

```bash
KELPIE_TEST_LIBVIRT_RECOVERY_CONFIG=/absolute/path/recovery-lab.json .venv/bin/python -m pytest -q -s apps/api/tests/test_worker_vm_recovery_postgres.py
make test
make lint
```

일반 API 검사에서는 이 Opt-in 검사를 Skip하며 CI 통과로 대체하지 않습니다. 별도 VM 빌드/Job/Matrix를 추가하지 않고 기존 Go CI에서 일반 복구 회귀를 실행합니다. 최종 `make test`: API 1195 통과/153 Skip, Runner 45, Web 125·타입 검사, Worker/Gateway, Lab 18 통과. `make lint` 및 집중 Worker Race 검사도 통과했습니다. 새 Opt-in 1개는 위 실제 환경에서 별도로 통과했고 기존 PostgreSQL·Linux Gate는 최종 CI에서 확인합니다.

## 적용·롤백·남은 조건

먼저 API의 Claim `lease_id`·조회·복구 경로를 배포해야 합니다. 운영 전 Drain/중지와 소유권·자격증명 점검을 수행하며, 현재 복구 시작에는 `online` Worker가 필요합니다. API Schema/DB Migration·프로덕션 환경변수·의존성 변경은 없습니다. 위 설정과 동의 값은 테스트 전용입니다.

Schema 1과 이전 `<WorkID>` 디렉터리는 자동 임대 채택 대상이 아닙니다. 기록을 고치거나 삭제해 복구 제한을 우회하지 마세요. 롤백은 Worker를 검증된 절차로 중지하고 기록·감사를 보존한 뒤 수행하며, Schema 2를 모르는 구버전을 같은 WorkRoot에서 바로 시작하지 않습니다.

실행 중 작업의 복구 정책·Claim 응답 유실·완전한 Orphan 처리·전체 Executor/Golden Image/Runner·작업별 네트워크·시간 예산·Preview/Console·동시 두 작업은 남아 있습니다. 고정 릴리즈 단계 완료율은 **1/7 = 14.3%**를 유지합니다.
