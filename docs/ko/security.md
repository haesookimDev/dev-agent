# 보안 불변 조건

한국어 | [English](../en/security.md)

다음 조건을 위반하면 Release할 수 없습니다.

1. 승인자의 결정이 기록되기 전에는 작업 VM 안에 저장소 쓰기 자격증명이 존재하지 않아야 합니다.
2. 작업 임대 토큰은 자신의 작업 ID에만 접근할 수 있어야 합니다.
3. 작업 상태 전이는 허용 목록과 예상 버전을 모두 검증해야 합니다.
4. GitHub 및 Slack Webhook Signature는 Payload를 Parse하기 전에 원본 Request Body로 검증해야 합니다.
5. VM 이름과 저장 경로는 검증된 UUID에서만 만들어야 하며 정리 작업은 사용자 경로나 Glob을 받지 않아야 합니다.
6. libvirt Socket, Docker Socket, Host Home Directory, Host SSH Key를 작업 VM에 Mount하지 않아야 합니다.
7. 사용자 Console 제어권 인수는 독점적이어야 합니다. 제어권 인수 시 에이전트 GUI 입력을 중단하고 반환 시 감사 이벤트를 남겨야 합니다.
8. Event 및 Artifact Metadata는 VM 밖으로 나가기 전에 재귀적으로 Redaction해야 하며 원본 자격증명은 Artifact가 될 수 없습니다.
9. Preview 및 Console Route는 만료 시간이 있고 인증된 조직 구성원만 접근할 수 있어야 합니다.
10. 에이전트가 만든 보안, Architecture, Migration, Dependency 이슈는 자신의 실행 라벨을 직접 적용할 수 없어야 합니다.

운영 환경에서 저장소 쓰기를 활성화하기 전에 Prompt Injection이 포함된 이슈가 쓰기 토큰 획득, 자기 승인, 다른 임대 접근, 보호 Branch 변경을 할 수 없음을 증명하는 환경 수준 통합 테스트를 추가해야 합니다.
