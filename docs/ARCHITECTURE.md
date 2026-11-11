# 구현 구조와 통합 수준

한 개의 dc_verify 패키지에서 기존 STEP 모듈을 보존했다. 이름과 입출력 스키마를 유지하고 `dc_verify.step.stepN.code`로 패키지 Import를 명시했다. 별도 Agent 계층·가짜 Storage 구현·전체 Pipeline wrapper는 만들지 않았다.

| STEP | 역할 | 실제 파일 | 주요 실제 함수/클래스 |
|---|---|---|---|
| 1 | PDF Vision OCR | `OCR.py`<br>`prompt.py` | `split_pages`, `run_ocr`, `main` |
| 2 | 문서 페이지 인덱스·초기 Controller Plan | `controller.py`<br>`prompt.py` | `StrictModel`, `DocumentScope`, `Task`, `NextAction`, `AnalysisHandoff`, `ControllerPlan`, `tokenize`, `search_terms`, `read_json`, `write_json`, `load_input`, `build_document_index`, `prepare_input`, `validate_plan`, `build_analysis_input`, `run_controller`, `main` |
| 3 | 선택 페이지 Claim Extraction | `analysis.py`<br>`analysis_prompt.py` | `StrictModel`, `PageRef`, `Claim`, `Coverage`, `MissingInformation`, `AnalysisResult`, `read_json`, `write_json`, `prepare_input`, `explicit_numbers`, `scoped_response_schema`, `bind_source_whitespace`, `external_semantic_issues`, `check_result`, `conservative_extraction`, `run_analysis`, `main`, `build_analysis_user_prompt` |
| 4 | 원본 PDF 페이지·인용 검증 | `source_verifier.py`<br>`source_verifier_prompt.py` | `StrictModel`, `FieldChecks`, `ClaimResult`, `Observation`, `VerificationResult`, `read_json`, `write_json`, `sha256`, `load_inputs`, `check_ocr`, `render_original_pages`, `validate_model_result`, `failed_result`, `apply_review_guards`, `assemble_results`, `run_verifier`, `main`, `build_user_prompt` |
| 5 | 증빙 수준 평가 | `assess_evidence.py`<br>`evidence_prompt.py` | `StrictModel`, `ClaimAssessment`, `UnresolvedRequirement`, `ControllerObservation`, `EvidenceAssessment`, `read_json`, `write_json`, `load_inputs`, `validate_assessment`, `prepare_api_inputs`, `merge_not_assessed`, `assess_evidence`, `main`, `build_user_prompt` |
| 6 | Controller Observe/Replan 정책 | `controller_decision_prompt.py`<br>`controller_replan.py` | `build_user_prompt`, `StrictModel`, `PrioritizedIssue`, `SearchConstraints`, `LocalScope`, `NextAction`, `PlannedTask`, `ControllerDecision`, `read_json`, `write_json`, `signature`, `prepare_input`, `validate_decision`, `build_action_input`, `run_replan`, `IssueObservation`, `PostEvidenceDecision`, `validate_post_evidence_decision`, `normalize_post_evidence_metadata`, `decide_after_cross_check`, `main` |
| 7 | 검색 계획·영수증 실행·원문 수집·전처리 | `document_preparer.py`<br>`prompt.py`<br>`search_api_adapter.py`<br>`search_executor.py`<br>`search_fetcher.py`<br>`search_planner.py` | `StrictModel`, `PageChoice`, `Screening`, `OCRPage`, `OCRResult`, `sha`, `text_quality`, `document_period`, `period_cutoff`, `parse_html`, `matches_target`, `relevant_segments`, `Preparer`, `main`, `build_user_prompt`, `load_tavily_key`, `tavily_search`, `execute_api`, `main`, `now`, `read_json`, `write_json`, `locked`, `canonical_url`, `query_key`, `safe_date`, `parse_session_results`, `select_tool`, `init_run`, `remaining_attempts`, `make_session_request`, `classify_document`, `merge_candidate`, `import_receipts`, `add_link_candidates`, `finalize`, `main`, `NoRedirect`, `check_public_url`, `fetch_bytes`, `PublicationParser`, `publication_date`, `LinkParser`, `discover_download_links`, `collect`, `main`, `StrictModel`, `SearchTask`, `DeferredIssue`, `SearchConstraints`, `SearchPlan`, `read_json`, `write_json`, `normalized_query`, `task_signature`, `response_schema`, `prepare_api_input`, `prepare_input`, `validate_plan`, `plan_search`, `build_executor_input`, `run_planner`, `main` |
| 8 | Admission·외부 Analysis·의미/증빙·교차 검증 | `cross_source.py`<br>`cross_source_input.py`<br>`external_analysis.py`<br>`external_verification.py`<br>`pair_matcher.py`<br>`prompt.py`<br>`source_admission.py` | `StrictModel`, `Dimensions`, `response_schema`, `pair_payload`, `references`, `quantities`, `guard_pair`, `repair_pair`, `local_diagnostic`, `execute`, `verify_execution`, `main`, `InputBlocked`, `require`, `indexed`, `claim_key`, `successful_runs`, `newest_source`, `check_snapshot`, `original_chain`, `find_original_pdf`, `eligibility`, `wrap_claim`, `load_input`, `latest_run`, `load_admitted_jobs`, `execute`, `main`, `StrictModel`, `Correction`, `RequirementSupport`, `ClaimReview`, `DocumentReview`, `SemanticAuditRow`, `SemanticAudit`, `normalize`, `html_format_proof`, `system_prompt`, `load_jobs`, `prepare_original`, `enforce_review`, `reusable_evidence`, `assess_document`, `audit_semantics`, `review_date`, `failed_document`, `execute`, `verify_execution`, `main`, `tokens`, `overlap`, `family`, `is_capacity`, `explicit_time`, `time_relation`, `candidate_kind`, `soft_diagnostic`, `make_pair`, `select_pairs`, `read_json`, `write_json`, `digest`, `now`, `latest_preparation`, `date_window`, `relation_to_cutoff`, `assess_document`, `guard_claims`, `run_gate`, `main` |
| 9 | 계보 복원·Controller 판단/종료 | `controller.py`<br>`prompt.py`<br>`state_restoration.py` | `execute`, `verify_execution`, `main`, `build_user_prompt`, `load_cross_source`, `restore_search_history`, `evidence_observation`, `restore` |
| 10 | 오프라인 최종 리포트·Human Review | `prompt.py`<br>`report_builder.py` | `safe_url`, `safe_csv`, `normalize_pair_time`, `workflow_view`, `load_inputs`, `source_snapshots`, `evidence_rows`, `build_report`, `render_html`, `validate_report`, `build`, `main` |

공통 `codex.py`는 환경 키 로딩·고정 모델 설정·PDF/이미지/구조화 응답 API 호출과 안전한 오류를 제공한다. Prompt는 각 STEP의 별도 Python 파일로 유지한다. src/dc_verify/cli.py는 한 단계의 실제 main()만 전달 호출한다. src/dc_verify/demo.py는 기존 순수 Guard를 합성 자료로 검사할 뿐 Agent를 실행하지 않는다.

## 실제 데이터 전달과 의존성

| 모듈 | 입력 | 출력 | 의존성과 Tool/API |
|---|---|---|---|
| OCR | 사용자 PDF·OCR Prompt | ocr.json·응답·usage | codex.request_pdf_ocr, OpenAI Responses |
| Initial Plan | OCR·review_request | controller_plan.json·analysis_input | 페이지 TF-IDF/글자 n-gram·OpenAI |
| Analysis | analysis_input·analysis_request | analysis_result·validation | Pydantic·인용/숫자 Guard·OpenAI |
| Source Verification | Analysis·OCR·원본 PDF | source_verification·페이지 manifest | PyMuPDF·OpenAI 이미지 |
| Evidence | Analysis·Source·review_context | evidence_assessment | Source 상태 보존·OpenAI |
| Controller Replan | Plan·검증·증빙·이력 | decision·검색 계획 입력 | STEP 5 상태·예산 Guard·OpenAI |
| Search Planner | decision·Tool 가용성·이력 | search_plan·executor_input | 계획만 OpenAI; 실제 검색 없음 |
| Search Executor/Fetcher | 계획·runtime_tools·실제 영수증 | search_results·수집 원문·예산 | 세션 영수증 Import, Python Tavily HTTP 어댑터, URL/robots/중복 Guard |
| Preparation | 수집 원문·해시·이력 | document_validation·analysis_bundle/request | bs4·PyMuPDF; 허용 시 OpenAI OCR/선별; cache-only 가능 |
| Admission | Preparation 산출물 | source_admission·analysis_execution_input | 원문/날짜/출처 Guard, 외부 호출 없음 |
| External Analysis | Admission 작업 | external_claims·quarantined_claims | STEP 3 재사용·guard_claims·OpenAI |
| External Verification | 외부 Claim·원본 | source_semantic_verification·evidence·cross_check_input | STEP 4/5 재사용·OpenAI·PyMuPDF |
| Cross-source | 사용자 Claim·외부 검증·원문 계보 | 비교 Pair·controller_input | 입력 복원·Pair 선별·OpenAI·로컬 Guard |
| Final Controller | Cross-source·검색 이력·runtime_tools | decision·state·human_review_input | STEP 6 재사용·OpenAI·예산 복원 |
| Report | 동일 계보 Controller/원문 | HTML·JSON·CSV·검토 메모 UI | STEP 7/8/9 순수 로직·bs4·PyMuPDF; 새 API 없음 |

STEP 간 JSON 전달은 명시 옵션·설정된 runtime/stepN 기본 위치·일부 이전 성공 실행 탐색을 함께 사용한다. 모든 입력을 하나의 통합 DAG 설정으로 연결한 것은 아니다. 모델은 기존 사용자 계약대로 gpt-5.4-mini로 고정되어 있으며 Key와 데이터/환경 경로는 환경변수다.

## 구현/실행 수준

로컬 원본 프로젝트에는 단계별 실행·실패/재시도·성공 기록이 존재한다. 현재 패키지의 합성 실행은 Guard/스키마/CLI만 검증한다. 단계별 과거 성공을 전체 자동 End-to-End 완료로 표현하지 않는다. 세션 전용 CODEX_WEB_SEARCH는 Python SDK가 아니며 외부 검색 실행 연결은 환경별로 별도 제공해야 한다. Tavily 어댑터는 존재하나 이번 정리에서는 Live 호출하지 않았다. SEC/OpenDART는 선택 후보/레지스트리 항목이며 전용 실행 어댑터는 없다.

Runtime Registry는 Controller의 도구 선택·허용 행동·범위·예산을 검사한다. 그 결과 행동을 실제 도구 실행·후속 Agent·재계획 루프로 자동 이어주는 통합 dispatch는 없다. 과거 실행 JSON에 기록된 절대 경로·해시를 다른 환경으로 옮기는 자동 마이그레이션 기능도 없다. 검증 Guard를 우회해 경로를 바꾸지 않는다.
