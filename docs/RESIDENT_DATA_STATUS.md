# Resident Store와 Local Retrieval 상태

현재 확인된 등록 Resident 문서는 0개, 등록 Resident Claim은 0개다. 사용자 입력 문서는 USER_PROVIDED, 실행 중 수집 원문은 AGENT_ACQUIRED_EVIDENCE다. 디스크에 남아 있다는 이유로 RESIDENT_EVIDENCE로 바꾸지 않았다.

| 검사 | 상태 | 확인 근거 |
|---|---|---|
| 사전 등록 Corpus·승인 Seed | NOT_FOUND | 파일 목록·등록 코드 조사; 등록 manifest/seed registry 없음 |
| SQLite·기타 명시적 DB | NOT_FOUND | 프로젝트 파일 확장자와 SQLite 헤더 검사 |
| 저장된 원문 | FILES_EXIST_BUT_NOT_INDEXED | 사용자 1종, 수집 7종; 작업 간 Resident 인덱스 없음 |
| 구조화 Claim·원문 ID·검증 상태·검토 기준일 | IMPLEMENTED_AND_TESTED | STEP 3~10 JSON·검증 함수·로컬 기록 및 공개 합성 테스트 |
| 단일 OCR 문서의 페이지 관련도·키워드 인덱스 | IMPLEMENTED_AND_TESTED | STEP 2 build_document_index(), controller_input.json의 ocr_document_index; 합성 페이지 테스트 |
| 문서 내 관련 구간 선별 | IMPLEMENTED_AND_TESTED | STEP 7 relevant_segments()/Preparer와 합성 테스트 |
| 작업 간 Resident 검색 인덱스 | NOT_FOUND | 영속 Corpus·FTS·벡터 저장소 및 등록 파이프라인 없음 |
| 새 작업에서 Resident를 검색하는 Tool 실행기 | NOT_FOUND | 문서 내 선별과 파일 읽기만 존재 |
| Controller의 SEARCH_LOCAL 선택·범위/예산 검사 | IMPLEMENTED_AND_TESTED | STEP 6/9 계획·정책 검증; 실제 Resident 검색 실행과 구분 |
| 외부 검색 전 Resident Tool 자동 실행 | PLANNED_ONLY | 선택 규칙은 있으나 실행 연결 없음 |
| STEP 11 Evidence Store | NOT_FOUND | STEP 디렉터리는 1~10뿐 |

이 결과는 DB를 새로 만든 것이 아니다. data/seed/README.md는 비어 있는 데이터 Pool의 계약 설명이며 등록 문서가 아니다. 합성 데모도 Resident 증빙으로 등록하지 않는다. 다음 개발 후보는 승인 등록·인덱스·재사용 정책·Local Retrieval·Controller dispatch이나 이번 작업에서는 구현하지 않았다.
