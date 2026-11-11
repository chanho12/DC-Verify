# 원본 코드와 공개 패키지 대응

기존 개발 폴더는 이동·삭제·수정하지 않았다. 아래 파일을 복사했고 원본·배포본 SHA-256과 실제 함수/클래스·Import는 code_inventory.json에 있다. 데이터 원문과 실행 결과는 복사하지 않았다.

주요 패키징 변경: sys.path 삽입 제거, 패키지 이름을 명시한 Import, DC_VERIFY_DATA_DIR 기반 단계별 Runtime 경로, DC_VERIFY_ENV_FILE/API.env 설정, Source Verifier PDF 경로 필수 입력, Report 템플릿/정책 package-data 포함. 기존 테스트의 원문 의존 Case는 원문이 없으면 명시적 skip하며, 합성 자료로 실제 원문 검증 결과를 가장하지 않는다. 임의 Claim ID·상태·예산 규칙·Prompt 내용·스키마는 바꾸지 않았다.

| 원본(기존 루트 상대 경로) | 배포 경로 | 변경 |
|---|---|---|
| `01_experiments/code/codex.py` | `src/dc_verify/codex.py` | Package-qualified imports, Configured API.env location; key loading/model contract preserved |
| `01_experiments/code/step/step1/code/OCR.py` | `src/dc_verify/step/step1/code/OCR.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step1/code/prompt.py` | `src/dc_verify/step/step1/code/prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step10/code/prompt.py` | `src/dc_verify/step/step10/code/prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step10/code/report.css` | `src/dc_verify/step/step10/code/report.css` | 바이트 동일 |
| `01_experiments/code/step/step10/code/report.html` | `src/dc_verify/step/step10/code/report.html` | 바이트 동일 |
| `01_experiments/code/step/step10/code/report.js` | `src/dc_verify/step/step10/code/report.js` | 바이트 동일 |
| `01_experiments/code/step/step10/code/report_builder.py` | `src/dc_verify/step/step10/code/report_builder.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths, Packaged report policy resource |
| `01_experiments/code/step/step10/code/test_report_builder.py` | `src/dc_verify/step/step10/code/test_report_builder.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths, Packaged report policy resource |
| `01_experiments/code/step/step2/code/controller.py` | `src/dc_verify/step/step2/code/controller.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step2/code/prompt.py` | `src/dc_verify/step/step2/code/prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step3/code/analysis.py` | `src/dc_verify/step/step3/code/analysis.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step3/code/analysis_prompt.py` | `src/dc_verify/step/step3/code/analysis_prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step3/code/test_analysis.py` | `src/dc_verify/step/step3/code/test_analysis.py` | Package-qualified imports, Explicit skip without private fixture |
| `01_experiments/code/step/step4/code/source_verifier.py` | `src/dc_verify/step/step4/code/source_verifier.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths, PDF path is required CLI input |
| `01_experiments/code/step/step4/code/source_verifier_prompt.py` | `src/dc_verify/step/step4/code/source_verifier_prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step4/code/test_source_verifier.py` | `src/dc_verify/step/step4/code/test_source_verifier.py` | Package-qualified imports, DC_VERIFY_DATA_DIR runtime paths, Explicit skip without private fixture, PDF path is required CLI input |
| `01_experiments/code/step/step5/code/assess_evidence.py` | `src/dc_verify/step/step5/code/assess_evidence.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step5/code/evidence_prompt.py` | `src/dc_verify/step/step5/code/evidence_prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step5/code/test_assess_evidence.py` | `src/dc_verify/step/step5/code/test_assess_evidence.py` | Package-qualified imports, DC_VERIFY_DATA_DIR runtime paths, Explicit skip without private fixture |
| `01_experiments/code/step/step6/code/controller_decision_prompt.py` | `src/dc_verify/step/step6/code/controller_decision_prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step6/code/controller_replan.py` | `src/dc_verify/step/step6/code/controller_replan.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step6/code/test_controller_replan.py` | `src/dc_verify/step/step6/code/test_controller_replan.py` | Package-qualified imports, DC_VERIFY_DATA_DIR runtime paths, Explicit skip without private fixture |
| `01_experiments/code/step/step7/code/document_preparer.py` | `src/dc_verify/step/step7/code/document_preparer.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step7/code/prompt.py` | `src/dc_verify/step/step7/code/prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step7/code/search_api_adapter.py` | `src/dc_verify/step/step7/code/search_api_adapter.py` | Package-qualified imports |
| `01_experiments/code/step/step7/code/search_executor.py` | `src/dc_verify/step/step7/code/search_executor.py` | Package-qualified imports, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step7/code/search_fetcher.py` | `src/dc_verify/step/step7/code/search_fetcher.py` | Package-qualified imports |
| `01_experiments/code/step/step7/code/search_planner.py` | `src/dc_verify/step/step7/code/search_planner.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step7/code/test_document_preparer.py` | `src/dc_verify/step/step7/code/test_document_preparer.py` | Package-qualified imports |
| `01_experiments/code/step/step7/code/test_search_executor.py` | `src/dc_verify/step/step7/code/test_search_executor.py` | Package-qualified imports |
| `01_experiments/code/step/step7/code/test_search_planner.py` | `src/dc_verify/step/step7/code/test_search_planner.py` | Package-qualified imports, DC_VERIFY_DATA_DIR runtime paths, Explicit skip without private fixture |
| `01_experiments/code/step/step8/code/cross_source.py` | `src/dc_verify/step/step8/code/cross_source.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step8/code/cross_source_input.py` | `src/dc_verify/step/step8/code/cross_source_input.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths, Canonical successful-run paths for symlink-mounted runtime data |
| `01_experiments/code/step/step8/code/external_analysis.py` | `src/dc_verify/step/step8/code/external_analysis.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step8/code/external_verification.py` | `src/dc_verify/step/step8/code/external_verification.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step8/code/pair_matcher.py` | `src/dc_verify/step/step8/code/pair_matcher.py` | Package-qualified imports |
| `01_experiments/code/step/step8/code/prompt.py` | `src/dc_verify/step/step8/code/prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step8/code/source_admission.py` | `src/dc_verify/step/step8/code/source_admission.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step8/code/test_cross_source.py` | `src/dc_verify/step/step8/code/test_cross_source.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths, Canonical fixture source path |
| `01_experiments/code/step/step8/code/test_external_analysis.py` | `src/dc_verify/step/step8/code/test_external_analysis.py` | Package-qualified imports |
| `01_experiments/code/step/step8/code/test_external_verification.py` | `src/dc_verify/step/step8/code/test_external_verification.py` | Package-qualified imports, Removed sys.path insertion |
| `01_experiments/code/step/step8/code/test_source_admission.py` | `src/dc_verify/step/step8/code/test_source_admission.py` | Package-qualified imports |
| `01_experiments/code/step/step9/code/controller.py` | `src/dc_verify/step/step9/code/controller.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step9/code/prompt.py` | `src/dc_verify/step/step9/code/prompt.py` | Package-qualified imports |
| `01_experiments/code/step/step9/code/state_restoration.py` | `src/dc_verify/step/step9/code/state_restoration.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths |
| `01_experiments/code/step/step9/code/test_controller.py` | `src/dc_verify/step/step9/code/test_controller.py` | Package-qualified imports, Removed sys.path insertion, DC_VERIFY_DATA_DIR runtime paths, Nested package imports corrected |
| `01_experiments/code/step/step10/data/report_policy.json` | `src/dc_verify/report_policy.json` | 바이트 동일 |

새로 추가한 파일은 settings.py(경로 설정), cli.py(단일 단계 전달), demo.py(명시적 합성 Guard 데모), tests/test_package.py(패키징/중립 fixture 검사), tests/test_existing_stages.py(기존 TestSuite 집계), 설치 설정·문서·manifest다. 통합 Store/새 Agent 기능은 추가하지 않았다. 기능상 재설계 없이 기존 STEP 이름 공간을 유지해 대응 관계와 변경 폭을 제한했다.
