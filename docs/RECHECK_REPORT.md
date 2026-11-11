# 코드·STEP 실행 재검사

원본 대응 파일 48개(Python 44개·리소스 4개), STEP 1~10 폴더, 함수·클래스 이름/개수, Prompt 상수 29개를 다시 대조했다. 누락은 없고 Prompt 문자열은 유지됐다. STEP 0 폴더는 원래 없으며 공통 codex.py·settings.py·API.env가 준비 역할을 한다.

## 실제 검사 결과

| 검사 | 결과 |
|---|---|
| 보유한 실제 입력·원문·검증 계보를 연결한 로컬 테스트 | 356개 성공, skip·실패·오류 0개 |
| 원문을 포함하지 않은 독립 wheel 설치 환경 | 234개 성공, 실패·오류 0개 |
| 독립 환경에서 원문 부족으로 건너뛰기 | 테스트 91개 + setUpClass 3건 = skip 기록 94건 |
| run.sh 단계별 도움말 | 17개 모두 통과 |
| run.sh 기존 입력 실행·준비 | 12개 모두 통과 |
| 다른 위치에 복사한 run.sh + 독립 로컬 .venv | 도움말·단계 목록·합성 데모 통과 |
| 원본 개발 파일·검증 결과 | 비밀 파일을 읽지 않은 961개 SHA-256 유지 |
| 새 LLM/OCR·검색 요청 | 0회 |

## 보완한 문제

1. Runtime 폴더를 심볼릭 링크로 연결하면 과거 성공 실행의 경로 표기가 달라 STEP 9/10이 차단될 수 있었다. 성공 실행 탐색 결과를 실제 경로로 통일했다. 원본 해시·Claim·정책·계보 동일성 검사는 유지했다.
2. STEP 8 원문 변조 테스트의 소스 경로도 실제 경로로 통일해 패치가 검증 대상 파일에 정확히 적용되게 했다.
3. STEP 9 재검토 테스트 함수 안에 남아 있던 bare import 2개를 패키지 Import로 수정했다. 중첩된 Import까지 검사하는 공개 회귀 테스트를 추가했다.
4. STEP 1의 합성 PDF + 모의 API 응답 저장 흐름, STEP 2 초기 Controller dry-run, 경로 정규화 검사를 공개 테스트에 추가했다.

## 단계별 확인 범위

- 공통 설정: 키 미설정 안전 오류·API.env 경로/Runtime 설정·키 repr 노출 방지.
- STEP 1: 합성 PDF를 실제 파서로 열고 모의 OCR 응답의 페이지/파일 저장 검사. 실제 Vision API 호출은 하지 않음.
- STEP 2: 페이지 인덱스·기존 OCR 입력으로 Controller 입력 준비. 초기 계획 API는 호출하지 않음.
- STEP 3~6: 기존 분석·원본 인용·증빙·Controller 정책 테스트와 실제 입력 dry-run.
- STEP 7: 3개 검색/수집 모듈의 모의 영수증·예산·오류·출처 테스트, Planner 입력 준비, 모든 CLI 도움말. 실제 검색·새 다운로드 없음.
- STEP 8: 보유 문서의 Admission Gate 실행, External Analysis/Verification/Cross-check 입력 준비, 보관 원문/검증 계보 회귀 검사.
- STEP 9: 보관 검색 영수증·예산·검증 상태 복원과 Controller dry-run. 새 판단 API 호출 없음.
- STEP 10: 기존 실제 Controller 결정·원문으로 report.html/JSON/CSV를 임시 검사 폴더에 생성. 공개 폴더에 실제 원문을 추가하지 않음.

위 검사는 새 PDF를 넣어 모든 Agent/API를 끝까지 실행한 검사가 아니다. 전체 자동 End-to-End 실행기는 여전히 미구현이며, 실제 모델/검색 서비스의 응답 가능 여부는 이번 검사에 포함하지 않는다. 상세 기계 판독 결과는 ../manifests/functional_recheck.json에 있다. 원문과 절대 경로가 포함된 raw 로그는 공개 폴더 밖에 보관한다.
