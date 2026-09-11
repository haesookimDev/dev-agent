# 작업별 네트워크 기반 검증

한국어 | [English](../en/worker-network-foundation.md)

2026-09-11 소스 `50ada438156ea596cb07d317911bf12f715e194b`의 **미연결 기반 코드**입니다. 실행기의 `network=default`는 아직 교체하지 않았으며, 실제 패킷 격리나 MVP 완료를 입증하지 않습니다.

이 문서는 당시의 스키마 전용 검증 기록을 보존합니다. 이후 영속 소유권과 실제 Network 정리는 [네트워크 수명주기 검증](worker-network-lifecycle.md)에 기록합니다.

- `392d3922b169f42a25e039e3dc930adc3c49ed4f`: 검증된 임대 UUID에서 전용 이름·Bridge·MAC을 만들고 RFC1918 Pool의 /30을 제안합니다. 기존 Run·제외 Prefix와 충돌하거나 재고가 모순·고갈되면 실패합니다. 호출자가 현재 Host 재고 수집과 영속 생성의 직렬화를 책임져야 합니다.
- `6cd8cdc5a380bce696f545e31cdf43b7a11b6520`: 고정 DHCP 대상·소유권 Metadata·IPv6 비활성 Network XML과 예외 없는 양방향 Ethernet 차단 Filter XML을 생성합니다. NAT·Port Isolation만으로 Host 접근이 차단되는 것은 아닙니다. [libvirt Network](https://libvirt.org/formatnetwork.html), [Filter](https://libvirt.org/formatnwfilter.html)
- `50ada438156ea596cb07d317911bf12f715e194b`: 설치된 libvirt 스키마를 사용하는 Opt-in 검사입니다. Network/Filter 정의·활성화, VM 실행이나 방화벽 변경은 하지 않습니다.

## 검증

`make test-worker`(최종 전체 daemon 6.587초), `make lint`, 집중 네트워크 테스트·Race(1.272초), Linux ARM64 Build Tag 포함 `go vet`가 통과했습니다. Worker만 수정하여 로컬 `make test` 전체는 재실행하지 않았고 UI/브라우저 검사는 해당하지 않습니다.

Mac M4 Pro/macOS 15.7.3의 전용 Lima Ubuntu 24.04 ARM64에서 비특권 Worker 계정으로 최종 네트워크 테스트와 libvirt 10.0.0 스키마 2개가 통과했습니다(스키마 합계 0.01초). 기존 도구에 필요한 `libxml2-utils` 2.9.14+dfsg-1.3ubuntu3.8만 전용 Lab에 추가했습니다(288kB). 제품 의존성·CI Job·운영 설정은 추가하지 않았습니다.

```sh
cd apps/worker
GOOS=linux GOARCH=arm64 go test -c -tags libvirt_integration -o /tmp/worker-network.test ./internal/daemon
# Copy to the dedicated Linux host and run as its unprivileged Worker user:
KELPIE_LIBVIRT_TEST_ACK=disposable-host-only /tmp/worker-network.test -test.run 'TestDedicatedLibvirtNetworkSchemas|TestRunNetwork' -test.v
```

최종 테스트 바이너리 SHA256은 `7c921cd17cdb97df864b366c0951cecd7f2ebb117f513912ebb1f6fbd1924478`이며 Mac/실행 Host의 값이 일치했습니다. [실행 로그](../assets/worker-lifecycle/network-schema.log) SHA256은 `e1b34385bc84c9f509ab59ea693d9cf6d1f03bac58c97c7b91f40d95b9b7c491`입니다. 임시 XML은 제거했고 Domain 목록은 비어 있으며 기존 default Network는 inactive 상태 그대로입니다.

## 남은 조건

영속 Network 소유권·Host 충돌 검사·Executor 연결·허용 송신 정책·Host/Metadata/다른 VM 차단·기존 연결 격리·정확한 Network/Filter 정리·재시작 복구와 실제 두 VM 패킷 검증이 남았습니다. XML을 생성한 사실을 libvirt가 실제 필터를 적용한 증거로 사용하지 않습니다. 현재 기반 코드에는 운영 적용이나 데이터 마이그레이션이 없으며, 연결 전까지 새 함수는 실행기 동작을 바꾸지 않습니다. 미병합·통합 중인 작업으로 유지하고 릴리즈 완료율은 1/7(14.3%)입니다.
