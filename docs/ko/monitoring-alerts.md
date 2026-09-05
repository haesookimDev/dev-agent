# 제어 API 장애 알림

한국어 | [English](../en/monitoring-alerts.md)

## 범위와 계약

[Prometheus 규칙](../../infra/monitoring/alerts.yml)은 아래 장애와 한국어·영어 대응 안내를 제공합니다. 자동 재시작·재승인·전달·데이터 삭제는 수행하지 않습니다.

| 알림 | 조건 | 심각도 |
| --- | --- | --- |
| `KelpieApiScrapeUnavailable` | `kelpie-api` 대상의 수집 실패가 2분 지속 | critical |
| `KelpieApiTargetMissing` | `kelpie-api`의 `up` 시계열이 전혀 없는 상태가 2분 지속 | critical |
| `KelpieRecoveryMetricsMissing` | 수집은 성공하지만 복구 `completed` 상태 시계열이 없는 상태가 2분 지속 | warning |
| `KelpieStartupRecoveryStalled` | 수집 가능한 API의 시작 시 복구가 완료되지 않은 상태가 5분 지속 | warning |
| `KelpieDeliveryFailures` | 최근 10분 동안 전달 실패 Counter 증가를 관찰하고 현재 수집도 성공 | warning |

집계에서 단계 Label을 제거하므로 `waiting_for_database → running → retrying` 전환은 대기 시간을 초기화하지 않습니다. `not_started`와 수집 가능한 `cancelled`도 미완료입니다. [복구 완료는 전체 SCM 성공이 아니므로](delivery-recovery-metrics.md) 전달 실패는 독립적으로 판단합니다. 수집 실패 중에는 복구·전달 알림 대신 수집 실패를 진단합니다.

`for`는 Prometheus가 조건을 연속 관찰한 시간이며 API 기동 시각이나 모든 장애의 정확한 시작 시각이 아닙니다. Scrape·평가 간격, 오래된 시계열 소멸과 Prometheus 재기동이 감지 시점에 영향을 줍니다. 조건 소멸 후 다음 평가에서 해제됩니다. 정상적인 긴 전달도 5분 Warning을 만들 수 있으므로 기준과 테스트를 배포의 정상 처리 시간에 맞춰 함께 조정하세요. [공식 규칙 의미](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)

Counter 첫 수집 이전 실패, Scrape 사이에 시작·종료한 프로세스, 최초 값만 있고 증가를 관찰하지 못한 실패는 놓칠 수 있습니다. Counter Reset 자체는 새 실패로 세지 않습니다. 알림은 모든 Event 전달을 보장하거나 Job·Event·감사를 대체하지 않습니다. 같은 규칙 파일에 추가된 [Worker Heartbeat·Lease·Queued 작업 지속 관측과 관측 실패 알림](runtime-monitoring.md)은 별도 계약과 대응 절차를 따릅니다. 외부 Object Store/SCM Readiness, 실행·DeliveryJob 전체의 정체 감시와 통합 운영 Dashboard는 남아 있습니다.

## 설치·검증

실제 PromQL 평가 의미를 검사하기 위해 공식 `promtool`을 개발·운영 검증 도구로 추가합니다. 애플리케이션 패키지 의존성은 추가하지 않습니다. [공식 3.14.0 Release](https://github.com/prometheus/prometheus/releases/tag/v3.14.0)의 대상 Architecture용 Archive를 받고 압축 해제 전에 SHA-256을 확인합니다.

| Archive | SHA-256 |
| --- | --- |
| `prometheus-3.14.0.linux-amd64.tar.gz` | `f665c6da19eb7ba399c915d30c7d9793c9b417bf8a749b504bc470678631478d` |
| `prometheus-3.14.0.darwin-arm64.tar.gz` | `a9623f7f4fe65b1b171b423c1a72bbf23dfdf41a171dcb33e7dd302af80dc01c` |

Root에서 `make test-monitoring PROMTOOL=/path/to/promtool`을 실행합니다. `PROMTOOL`은 실행 파일 경로를 지정하는 Make 변수이지 API 환경변수가 아닙니다. 설정·규칙 문법과 중복 규칙 Lint, 가상 시계열 기반 PromQL 테스트를 실행하므로 5분·10분을 실제로 기다리지 않습니다. 기존 `make test`는 애플리케이션 검증을 유지합니다. CI의 필수 `Go` 검사에 Monitoring 검증을 포함하며, Version·Checksum으로 Archive를 캐시하고 Cache Hit도 압축 해제 전에 SHA-256을 검증합니다. [공식 규칙 테스트](https://prometheus.io/docs/prometheus/latest/configuration/unit_testing_rules/)

## 실행과 운영 적용

[예제 설정](../../infra/monitoring/prometheus.example.yml)은 같은 Host의 `127.0.0.1:8000/metrics`를 15초마다, 3초 Timeout으로 수집하고 30초마다 규칙을 평가합니다. 다른 환경에서는 대상 주소를 승인된 내부 API로 바꾸되 `job_name: kelpie-api`를 유지합니다. 설정과 Rules 파일을 함께 배치합니다. 기본 Compose·서비스 자동 시작·운영 배포는 변경하지 않습니다.

쓰기 가능한 **전용** 데이터 디렉터리를 준비하고 공식 실행 파일을 사용합니다. 다음 디렉터리 경로는 실제 경로로 바꿔야 하는 예시입니다.

```sh
prometheus --config.file=infra/monitoring/prometheus.example.yml \
  --web.listen-address=127.0.0.1:9090 \
  --storage.tsdb.path=/path/to/dedicated-monitoring-data \
  --storage.tsdb.retention.time=7d --storage.tsdb.retention.size=1GB
```

`http://127.0.0.1:9090/alerts`에서 규칙을 펼쳐 Pending/Firing·Label·한국어/영어 요약·Runbook을 확인합니다. `/targets`는 대상 건강 상태, `/query`는 `up{job="kelpie-api"}`와 `ALERTS{job="kelpie-api"}` 조회에 사용합니다. Prometheus 자체 UI이며 Kelpie 대시보드의 새 화면은 아닙니다. 저장 제한은 오래된 **Monitoring 시계열**을 제거하며 Work·Artifact 보존 정책을 대신하지 않습니다.

예제는 외부 알림을 전송하지 않습니다. 운영 알림에는 승인된 Alertmanager의 내부 HTTPS 주소·인증·수신자·묶음/중복 제거·Maintenance Silence를 별도 구성하고 실제 수신을 확인해야 합니다. 기존 Secret Provider·접근 통제를 사용하고 Token을 설정·문서·화면에 넣지 마세요. Prometheus UI/API와 `/metrics`는 내부 Network로 제한하며 TLS 검증을 끄거나 공개 Listen 주소로 바꾸지 않습니다. [공식 설정](https://prometheus.io/docs/prometheus/latest/configuration/configuration/)

Rollback은 이전 Monitoring 설정·규칙을 복원·검증한 뒤 정상적인 설정 Reload 또는 재시작으로 적용합니다. API를 복구 지표 이전 Version으로 되돌리면 의존 규칙도 함께 되돌립니다. API 데이터 Migration이나 TSDB 삭제는 필요하지 않습니다.

## 대응 안내

<a id="api-scrape"></a>

### API 수집 실패

Prometheus `/targets` 오류와 API 프로세스·내부 DNS/Network·Proxy 제한을 확인합니다. 승인된 내부 경로의 `/healthz`, `/readyz`, `/metrics`를 구분하여 검사합니다. 수집 실패가 DB 장애를 직접 증명하지는 않습니다. Token·원본 연결 문자열을 복사하거나 Network 제한을 해제하지 마세요.

<a id="missing-target"></a>

### 수집 대상 누락

`job_name: kelpie-api`, 실제 배포 설정·Service Discovery와 의도한 단일 API 대상을 확인합니다. 다른 Job의 `up=1`은 Kelpie 정상의 증거가 아닙니다. 계획된 중지는 승인된 Maintenance Silence로 관리하며 장애를 숨기기 위해 규칙을 삭제하지 않습니다.

<a id="missing-metrics"></a>

### 복구 지표 누락

대상이 실제 Kelpie API인지, 배포 Version이 [복구 지표](delivery-recovery-metrics.md)를 포함하는지, Metric Relabeling이 제거하는지 확인합니다. `completed`의 **시계열 존재**가 필요하며 값은 0이어도 정상 계약입니다. 이전 API Rollback이면 규칙도 호환 Version으로 되돌립니다.

<a id="startup-recovery"></a>

### 시작 시 복구 지연

단계·경과 시간·`checks_total`과 `/readyz`를 함께 확인합니다. DB 미준비는 연결·Migration을, 반복 오류는 고정 경고·DB 잠금·풀 상태를, 오래 실행 중인 전달은 Job·Event·SCM 접근과 정상 소요 시간을 확인합니다. DB 복구에 추가 API 재시작·재승인은 필요하지 않습니다. 중복 전달 위험이 있으므로 임의 재승인이나 두 번째 API를 시작하지 말고 [단일 프로세스 경계](delivery-recovery.md)를 지킵니다.

<a id="delivery-failures"></a>

### 전달 실패

Work/DeliveryJob의 최종 상태, [안전한 단계·오류 코드](delivery-failure-safety.md), 검증 Bundle과 승인 이력을 확인합니다. GitHub 설치·저장소 권한 또는 일시적인 SCM 장애를 점검하되 Token·Patch·원본 예외를 알림에 넣지 마세요. 복구 완료와 개별 실패를 혼동하지 않습니다. 이 규칙은 자동 재시도하거나 승인·격리 Gate를 우회할 권한을 부여하지 않습니다.

## 검증 기록

아래는 기본 알림 도입 당시 기록입니다. 추가된 지속 관측의 검증은 [실행 관측 문서](runtime-monitoring.md)를 확인하세요.

규칙 `43af5cf`, 검증 명령 `04ed249`, CI `126337f` 기준입니다. 이후 이 PR의 변경은 지침·문서뿐입니다.

- Prometheus 3.14.0 `make test-monitoring`: 5개 규칙과 예제 설정, 9개 시나리오·27개 시점 판정 통과. 단계 전환 중 대기 유지, 완료·장애 복구, 대상/지표 누락, 다른 Job 배제, Counter Reset·첫 관찰 한계·실패 Window 만료·Stale 지표를 검증합니다.
- `KELPIE_TEST_POSTGRES_URL=<격리된 PostgreSQL 17> make test`: API 367개(37.02초), Runner 6개, Web 40개·타입 검사, Worker/Gateway 통과. `make lint` 통과. Workflow YAML 파싱·읽기 전용 권한, 문서 Link와 양쪽 언어 Runbook Anchor 10개를 확인했습니다.
- 실제 구동은 일회성 SQLite를 Migration한 Uvicorn과 Checksum을 확인한 macOS ARM64 Prometheus를 Loopback에서 실행했습니다. 예제의 대상 Port·Rules 파일 경로만 바꾸고 15초 Scrape, 30초 평가, 실제 5분/2분 조건은 그대로 유지했습니다.

| 직접 실행한 여정 | 관찰 결과 |
| --- | --- |
| 별도 DB의 Schema Revision을 미준비로 설정하고 API 기동 | 실제 지표 수집 성공; 복구 지연 Pending → Firing |
| 자체 Schema Marker만 원복 | API 재시작·재승인 없이 복구 완료 1, 알림 해제 |
| 자체 API 프로세스만 종료 | `/targets`에 연결 거부·Down; 수집 실패 Pending → Firing, 복구 알림 없음 |
| API 재기동 | `/readyz` 200, Target Up·오류 없음, 활성 알림 0개·규칙 5개 Inactive |

Orca 브라우저에서 규칙을 펼치고 한국어/영어 요약·Runbook·Firing 및 최종 Inactive 화면을 확인했습니다. Computer-use로 해당 데스크톱 화면도 확인했습니다. 네이티브 포커스가 없어 입력은 검사하지 않았으며 네이티브/Web UI 코드는 변경하지 않습니다. 캡처된 브라우저 요청 40개는 모두 200, 콘솔 메시지는 없었습니다. Prometheus의 의도한 API 수집 실패는 `/targets`에서 별도로 확인했습니다.

전달 실패·지표 누락의 조건은 가상 시계열 테스트이며 실제 외부 SCM·알림 수신자·Worker/VM 증거는 아닙니다. 외부 알림 전송과 운영 배포를 수행하지 않았습니다. 화면에는 로컬 경로가 포함되어 공개 업로드하지 않았습니다. 자체 API/Prometheus·탭·터미널과 일회성 SQLite·TSDB·로그를 정리했으며, 검증 데이터는 재실행으로 재생성할 수 있습니다. 사용자 프로세스·DB는 변경하지 않았습니다.
