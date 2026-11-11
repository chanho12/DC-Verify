"""API 호출 없는 STEP 3 회귀 테스트. 실제 입력·검사 함수를 사용한다."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dc_verify.step.step3.code import analysis as a


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        if not a.INPUT_PATH.is_file(): self.skipTest('Private experiment fixture not distributed')
        self.bundle = a.read_json(a.INPUT_PATH)
        self.selection = a.read_json(a.REQUEST_PATH)
        self.payload = a.prepare_input(self.bundle, self.selection)
        h = self.payload["analysis_handoff"]
        # 가짜 API 응답임을 명시한 테스트 자료. 실제 결과 파일로 저장하지 않는다.
        self.result = {
            "analysis_status": "INCOMPLETE", "review_goal": h["review_goal"],
            "target_projects": h["target_projects"], "as_of_date": h["as_of_date"],
            "analyzed_pages": [{"document_id": p["document_id"], "source_page": p["source_page"]} for p in self.payload["ocr_pages"]],
            "unprocessed_controller_pages": self.payload["execution_scope"]["unprocessed_controller_pages"],
            "claims": [{"claim_id": "TEST_ONLY_1", "project_name": h["target_projects"][0],
                "metric_type": "power_intake_capacity", "metric_name": "수전용량", "value": 20.0,
                "unit": "MW", "raw_value": "2만kw(20Mw)", "value_qualifier": "exact",
                "facility_scope": None, "development_status": None,
                "source_published_at": "2024-09", "metric_as_of": None,
                "source_document_id": "kinx_ir_240925", "source_page": 23,
                "evidence_quote": "가. 수전용량 2만kw(20Mw), 별도 예비수전 20Mw, 총 40Mw, 사용전력량 1만kw 이상 수준 예상",
                "temporal_basis": "planned", "notes": ["TEST ONLY"]}],
            "required_metric_coverage": [{"requested_metric": m, "status": "NOT_FOUND", "claim_ids": [], "reason": "TEST ONLY"} for m in h["required_metrics"]],
            "missing_required_information": [{"requested_metric": m, "metric_type": "operational_it_capacity" if i == 0 else None,
                "reason": "TEST ONLY", "needed_evidence": "추가 근거"} for i, m in enumerate(h["required_metrics"])],
            "ambiguities": [], "ocr_review_flags": [], "recommendations_for_controller": []}

    def test_subset_and_handoff_preserved(self):
        self.assertEqual([p["source_page"] for p in self.payload["ocr_pages"]], [23, 6, 5, 7])
        self.assertEqual(self.payload["analysis_handoff"], self.bundle["analysis_handoff"])
        self.assertEqual(len(self.payload["ocr_pages"]), 4)

    def test_unselected_page_rejected(self):
        self.selection["selected_pages"] = [99]
        with self.assertRaises(ValueError):
            a.prepare_input(self.bundle, self.selection)

    def test_duplicate_page_rejected(self):
        self.selection["selected_pages"] = [23, 23]
        with self.assertRaises(ValueError):
            a.prepare_input(self.bundle, self.selection)

    def test_corrupt_page_hash_rejected(self):
        self.bundle["documents"][0]["pages"][0]["text"] += "변조"
        with self.assertRaises(ValueError):
            a.prepare_input(self.bundle, self.selection)

    def test_actual_quote_matches_with_offsets(self):
        _, checked = a.check_result(self.result, self.payload)
        self.assertEqual(checked["status"], "PASSED")
        position = checked["evidence_checks"][0]
        page = self.payload["ocr_pages"][0]["text"]
        self.assertEqual(page[position["ocr_character_start"]:position["ocr_character_end"]], self.result["claims"][0]["evidence_quote"])

    def test_invented_quote_rejected(self):
        self.result["claims"][0]["evidence_quote"] += " 원문에 없는 문장"
        self.assertEqual(a.check_result(self.result, self.payload)[1]["status"], "FAILED")

    def test_wrong_number_rejected_even_if_quote_matches(self):
        self.result["claims"][0]["value"] = 80.0
        self.assertEqual(a.check_result(self.result, self.payload)[1]["status"], "FAILED")

    def test_wrong_page_rejected(self):
        self.result["claims"][0]["source_page"] = 6
        self.assertEqual(a.check_result(self.result, self.payload)[1]["status"], "FAILED")

    def test_wrong_date_rejected(self):
        self.result["as_of_date"] = "2030-01-01"
        self.assertEqual(a.check_result(self.result, self.payload)[1]["status"], "FAILED")

    def test_missing_required_must_remain_missing(self):
        self.result["missing_required_information"] = []
        self.assertEqual(a.check_result(self.result, self.payload)[1]["status"], "FAILED")

    def test_cannot_claim_complete_with_missing(self):
        self.result["analysis_status"] = "COMPLETE"
        self.assertEqual(a.check_result(self.result, self.payload)[1]["status"], "FAILED")

    def test_number_notation_not_unit_conversion(self):
        self.assertIn(10000, a.explicit_numbers("1만kw 이상"))
        self.assertNotIn(10, a.explicit_numbers("1만kw 이상"))

    def test_missing_schema_field_rejected(self):
        del self.result["claims"][0]["evidence_quote"]
        with self.assertRaises(ValueError):
            a.check_result(self.result, self.payload)

    def test_full_pipeline_with_test_double(self):
        response = {"api_status": "completed", "model": "TEST_DOUBLE_NOT_LIVE", "usage": None,
                    "text": "TEST DOUBLE", "parsed": self.result}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(a, "request_structured_output", return_value=response) as call:
                log, output = a.run_analysis(output_dir=Path(tmp))
            self.assertEqual(log["status"], "SUCCESS")
            self.assertEqual(log["analysis_status"], "INCOMPLETE")
            self.assertFalse(log["external_search_performed"])
            self.assertTrue((output / "validation.json").exists())
            self.assertEqual(call.call_args.args[0], a.ANALYSIS_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
