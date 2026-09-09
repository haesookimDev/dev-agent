# 제한된 API 병렬 검증

한국어 | [English](../en/ci-partitions.md) · [개발 지침](development.md)

## 목적과 실행 구조

PR #50 병합 뒤 [main CI](https://github.com/haesookimDev/dev-agent/actions/runs/34316776712)의 Python Job이 API 검사 90% 이후 8분 제한을 초과했습니다. 같은 SHA를 한 번 재실행해 6분 8초에 통과했지만 첫 실패 이력은 보존합니다. Timeout을 늘리거나 회귀를 제거하지 않고 API 전체 수집을 두 묶음으로 나눕니다.

- `API (1/2)`와 `API (2/2)`는 각각 별도 실행기에서 같은 전체 테스트를 수집하고 파일의 상대 경로 SHA-256으로 자기 묶음만 실행합니다. 파일 내부의 Parametrize·Fixture와 수집 순서는 유지하며 새 테스트도 자동 포함됩니다.
- 두 묶음이 끝나면 기존 필수 `Python` Job이 실행됩니다. `always()`로 의존 검사 실패 때도 성공처럼 Skip되지 않게 하고, 첫 Shell Step이 `needs.api.result == success`만 허용합니다. 실패·취소·생략·결과 누락은 통과가 아닙니다.
- `Python`은 이어서 Runner·Ruff와 기존 PostgreSQL 17 Migration·Worker 격리·감사·보존·경쟁·관측·SSE·복원 검사를 모두 실행합니다. 분할 옵션은 API Step에만 있으므로 PostgreSQL 검사를 분할하거나 빠뜨리지 않습니다.
- `Go`·`Web`의 명령, 실제 Chromium 검증과 증거 보존은 유지합니다. 기존 필수 검사 이름 `Python`, `Go`, `Web`을 유지하고 두 API 검사도 직접 확인합니다.

다섯 Job 모두 8분 제한을 유지합니다. 고정된 Python 3.12·Ubuntu 24.04를 쓰며 두 묶음 외의 Version Matrix는 추가하지 않습니다. API Matrix는 `fail-fast: false`이므로 한쪽 실패 후 다른 쪽 결과도 수집하지만 `continue-on-error`로 실패를 숨기지 않습니다. Job 초기화 비용은 늘 수 있고 실행기 성능·대기 시간에 따른 전체 CI 시간 변동은 여전히 있습니다.

## 로컬 검증과 결과 해석

기본 명령은 계속 전체 테스트를 실행합니다.

```sh
make test-api
make lint
```

분할 방식 자체를 바꾸면 다음 두 명령도 각각 실행합니다. 독립된 프로세스에서 병렬 실행할 수 있습니다.

```sh
PYTEST_ADDOPTS='--api-partition=1 --durations=10' make test-api
PYTEST_ADDOPTS='--api-partition=2 --durations=10' make test-api
```

`deselected`는 반대 묶음에 할당한 테스트입니다. 두 결과의 통과·실패·Skip 합과 전체 수집 목록을 비교하고, 한쪽 성공만으로 전체 성공을 선언하지 마세요. PostgreSQL 환경이 없는 API 단계의 Skip은 기존 전용 DB 단계에서 별도로 검증해야 합니다. 임의 `-k`, 경로 목록, 테스트 삭제나 선택 옵션의 전역 설정으로 속도를 맞추지 않습니다.

`test_ci_partition.py`는 기본 전체 수집, 두 묶음의 완전한 합집합·중복 없음, 파일 내 Case 유지, Checkout 위치·수집 순서 독립성과 실제 pytest의 실패 종료 코드·잘못된 옵션 거부를 검증합니다. `test_ci_workflow.py`는 고정 Matrix·필수 의존 Gate·실제 Shell의 실패 차단을 검증합니다. 별도 의존성을 추가하지 않습니다.

## 2026-09-09 로컬 검증 기록

- `f6f7020`의 `make test`·`make lint`: API 1,108개 통과/98 Skip·182.71초, Runner 45개, Worker·Gateway, Web 125개·타입 검사 통과.
- Workflow 회귀는 변경 전 7개 실패, 변경 후 분할 도구와 합쳐 16개 통과입니다.
- `0f9d1fe`의 최종 전체 수집 1,213개 = 첫 묶음 504개 + 둘째 709개, 누락·중복 0개입니다. 실제 병렬 실행은 470개 통과/34 Skip·58.38초와 645개 통과/64 Skip·124.32초였고 `make lint`도 통과했습니다. 로컬 수치는 GitHub 실행 시간 보장이 아닙니다.
- 실제 GitHub 실행의 최신 Head·완료 상태·소요 시간·PostgreSQL 후속 검사와 병합 후 main 결과는 PR에 기록합니다. CI 구성만 변경하므로 새 UI·네이티브 입력 검증 대상은 없습니다. 실제 KVM·Preview Acceptance를 대신하는 검증도 아닙니다.

롤백은 CI 연결과 이에 결합된 Workflow 회귀를 함께 되돌리고 기존 `Python`의 전체 API 명령을 복원합니다. 분할 도구만 남아도 옵션 없는 명령은 전체를 실행합니다. 운영 데이터·권한·배포 설정은 바꾸지 않으며 Action SHA 고정·읽기 전용 Token·기존 캐시를 유지합니다.

GitHub의 [Job 의존성과 `always()`](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-jobs), [Matrix 실패 처리](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations), [`needs` 결과](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#needs-context)를 기준으로 Gate를 구성합니다.
