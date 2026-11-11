# 통합 실행 조건 점검

| 검사 | 원본 상태 | 배포본 상태 |
|---|---|---|
| 단일 Python 환경 설치 | 공통 codex와 단계별 의존성 | setup.cfg/pyproject, wheel 설치 검사 |
| STEP 밖 Import | bare import/sys.path 삽입에 의존 | dc_verify 패키지 Import, 독립 환경 검사 |
| 내부 절대 경로 | 코드 내 개인 절대 경로는 미발견; 실행 JSON에는 존재 | 실행 JSON 미포함, 설정 경로 사용 |
| STEP 파일 전달 | 명시 옵션·기본 파일·성공 실행 탐색 혼합 | Runtime 설정 기반으로 정리; 통합 DAG는 없음 |
| Key/모델 설정 | Key 환경/API.env, 모델 고정 | Key 환경/API.env, 모델 고정 유지 |
| 오프라인 검사 | 기존 334개 로컬 회귀 기록 | 배포 가능한 테스트 + 명시적 private fixture skip |
| Live API 모드 구분 | dry-run/cache-only/실제 요청 모드가 모듈별 존재 | 모듈별 --help와 옵션 보존, 새 호출 없음 |
| Claim/원문 ID 보존 | 기존 Guard·키·해시 | 스키마/Guard 재사용 |
| Tool Registry | 선택·가용성·범위·예산 검사 | 검사 보존, 세션 도구를 Python SDK로 가장하지 않음 |
| 전체 자동 End-to-End | NOT_FOUND / 단계별 실행 기록 | NOT_IMPLEMENTED, 가짜 통합 runner 없음 |
