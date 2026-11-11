# 실제 데이터 전수조사

범위는 `01_experiments/`뿐이다. 가상환경·패키지·`__pycache__`·OS 캐시는 제외했다. 환경 파일은 존재와 크기만 기록하고 값은 읽지 않았다. JSON 580개를 파싱했으며 파싱 실패는 0개다. 상세 파일별 해시·크기·원본/파생 역할은 manifests/data_inventory.json과 CSV에 있다. 원문 자체는 이 배포 폴더에 없다.

| 항목 | 실제 집계 |
|---|---:|
| 파일 수 | 962 |
| PDF 파일 수(복사 포함) | 12 |
| HTML 파일 수(템플릿·리포트 포함) | 5 |
| JSON / CSV / JSONL | 580 / 1 / 12 |
| PNG 렌더링 파일 | 145 |
| 고유 원본 문서(해시 기준) | 8 |
| 사용자 입력 / Agent 수집 | 1 / 7 |
| 등록된 Resident 문서 / Claim | 0 / 0 |
| DB 파일(확장자 및 SQLite 헤더 검사) | 0 |
| 최신 계보의 Claim(사용자 / 외부) | 58 (10 / 48) |
| 전체 원문·의미 통과 / 외부 격리 | 33 / 15 |
| API 비교 Pair / 로컬 진단 Pair | 24 / 12 |
| 검증 산출물 파일 인스턴스 | 14 |
| 동일 바이트 중복 그룹 / 중복 저장 수 | 136 / 209 |

고유 원본은 PDF 5종(사용자 1종·외부 4종), 수집 HTML 3종이다. 보고서용 HTML 템플릿과 렌더링 결과는 원본 문서 수에 더하지 않는다. 수집 HTML 중 IR 목록 페이지는 EXCLUDED이므로 최신 분석에는 사용자 PDF와 외부 6종만 쓰였다.

| 문서 ID | 형식 | 최초 출처 | 공개일 | 현재 분석 정책 | 원본 위치(기존 프로젝트 상대 경로) |
|---|---|---|---|---|---|
| doc_9d88b0c55cd78a2a | PDF | AGENT_ACQUIRED_EVIDENCE | 미확인 | CONDITIONAL | 01_experiments/code/step/step7/data/output/search_execution_20261111T082440141178Z/original_documents/doc_9d88b0c55cd78a2a.pdf |
| doc_74aa1979ec78e89c | PDF | AGENT_ACQUIRED_EVIDENCE | 미확인 | CONDITIONAL | 01_experiments/code/step/step7/data/output/search_execution_20261111T082440141178Z/original_documents/doc_74aa1979ec78e89c.pdf |
| doc_15f26f6212f7eb30 | HTML | AGENT_ACQUIRED_EVIDENCE | 2016-08-23 | EXCLUDED | 01_experiments/code/step/step7/data/output/search_execution_20261111T082440141178Z/original_documents/doc_15f26f6212f7eb30.html |
| doc_04f930205c9d279f | PDF | AGENT_ACQUIRED_EVIDENCE | 미확인 | CONDITIONAL | 01_experiments/code/step/step7/data/output/search_execution_20261111T082440141178Z/original_documents/doc_04f930205c9d279f.pdf |
| doc_df4a434b5066a1d7 | HTML | AGENT_ACQUIRED_EVIDENCE | 2024-08-22 | ALLOW | 01_experiments/code/step/step7/data/output/search_execution_20261111T082440141178Z/original_documents/doc_df4a434b5066a1d7.html |
| kinx_ir_240925 | PDF | USER_PROVIDED | 2024-09 | USER_PROVIDED | 01_experiments/code/step/step1/data/kinx_ir_240925.pdf |
| doc_bf0d9fd0c5091be1 | PDF | AGENT_ACQUIRED_EVIDENCE | 미확인 | CONDITIONAL | 01_experiments/code/step/step7/data/output/search_execution_20261111T082440141178Z/original_documents/doc_bf0d9fd0c5091be1.pdf |
| doc_55ce28545b708a18 | HTML | AGENT_ACQUIRED_EVIDENCE | 2024-08-26 | ALLOW | 01_experiments/code/step/step7/data/output/search_execution_20261111T082440141178Z/original_documents/doc_55ce28545b708a18.html |

Claim 수는 `01_experiments/code/step/step10/data/output/review_report_20261111T124219779051Z/report.json`의 고유 Claim Key를 기준으로 집계했다. 사용자 원문 검증 10개와 외부 원문·의미 검토 48개가 연결된다. 격리 재검토 15개는 외부 48개의 부분집합이며 다시 합산하지 않는다. 증빙 평가도 사용자 10개·외부 48개에 연결된다. 검증 산출물 14개에는 실패/미완료 실행과 후속 복사본이 포함되므로 레코드 수 합계를 고유 Claim 수로 사용하지 않는다. 파일별 배열 크기와 해시는 inventory의 verification_artifacts에 있다.

공식 IR PDF 4개는 서로 다른 기간·언어·해시의 버전이다. 파일명 날짜는 공개일 증명이 아니다. 같은 문서의 반복 복사는 duplicates에 따로 기록했다. 버전 간 대체·우선순위나 실제 운영 사실을 새로 판단하지 않았다. HTML 목록 메타데이터의 2016년 날짜는 개별 IR 발행일로 사용하지 않는다.
