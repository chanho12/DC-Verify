"""STEP 4-2 회귀 테스트. API 호출 없이 실제 입력·제약·저장 처리를 검사한다."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dc_verify.step.step5.code import assess_evidence as a


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        if not (stage_data(5) / 'analysis_result.json').is_file(): self.skipTest('Private experiment fixture not distributed')
        self.paths = [stage_data(5) / name for name in
                      ("analysis_result.json", "source_verification.json", "review_context.json")]
        self.analysis, self.source, self.context = a.load_inputs(*self.paths)
        source = {c["claim_id"]: c for c in self.source["claim_results"]}
        rows = []
        for claim in self.analysis["claims"]:
            verified = source[claim["claim_id"]]["pdf_status"] == "VERIFIED"
            rows.append({"claim_id": claim["claim_id"], "statement_type": "UNKNOWN",
                "evidence_level": "SOURCE_STATEMENT_ONLY" if verified else "NOT_ASSESSED",
                "supports_review_goal": False, "reason": "TEST DOUBLE ONLY",
                "missing_evidence": ["TEST ONLY"], "review_required": not verified})
        self.result = {"assessment_status": "COMPLETED", "review_goal_status": "INCOMPLETE",
            **{k: self.context[k] for k in ("review_goal", "target_projects", "as_of_date")},
            "claim_assessments": rows,
            "unresolved_requirement_details": [{"requested_metric": m["requested_metric"],
                "reason": "TEST ONLY", "needed_evidence": "TEST ONLY"} for m in self.analysis["missing_required_information"]],
            "controller_observation": {"supported_claim_ids": [],
                "recheck_claim_ids": [r["claim_id"] for r in rows if r["review_required"]],
                "unresolved_requirements": ["TEST ONLY"], "additional_evidence_needed": ["TEST ONLY"]}}

    def check(self):
        return a.validate_assessment(self.result, self.analysis, self.source, self.context)[1]

    def test_valid_single_source_assessment(self):
        self.assertEqual(self.check()["status"], "PASSED")

    def test_c7_c10_remain_not_assessed(self):
        rows = {r["claim_id"]: r for r in self.result["claim_assessments"]}
        for cid in ("c7", "c10"):
            self.assertEqual(rows[cid]["evidence_level"], "NOT_ASSESSED")
            self.assertTrue(rows[cid]["review_required"])

    def test_unverified_promotion_rejected(self):
        self.result["claim_assessments"][6]["evidence_level"] = "SOURCE_STATEMENT_ONLY"
        self.assertEqual(self.check()["status"], "FAILED")

    def test_independent_corroboration_without_evidence_rejected(self):
        self.result["claim_assessments"][0]["evidence_level"] = "INDEPENDENTLY_CORROBORATED"
        self.assertEqual(self.check()["status"], "FAILED")

    def test_derivative_files_are_not_additional_documents(self):
        self.result["claim_assessments"][0]["evidence_level"] = "ADDITIONAL_DOCUMENT_SUPPORT"
        self.assertEqual(self.check()["status"], "FAILED")

    def test_missing_claim_rejected(self):
        self.result["claim_assessments"].pop()
        self.assertEqual(self.check()["status"], "FAILED")

    def test_duplicate_claim_rejected(self):
        self.result["claim_assessments"][-1] = copy.deepcopy(self.result["claim_assessments"][0])
        self.assertEqual(self.check()["status"], "FAILED")

    def test_scope_and_date_changes_rejected(self):
        for key, value in [("as_of_date", "2030-01-01"), ("target_projects", ["다른 시설"]), ("review_goal", "다른 목적")]:
            with self.subTest(key=key):
                old = self.result[key]
                self.result[key] = value
                self.assertEqual(self.check()["status"], "FAILED")
                self.result[key] = old

    def test_planned_statement_cannot_be_execution(self):
        self.result["claim_assessments"][0]["statement_type"] = "EXECUTION_CLAIM"
        self.assertEqual(self.check()["status"], "FAILED")

    def test_unresolved_input_does_not_become_sufficient(self):
        self.result["review_goal_status"] = "SUFFICIENT"
        self.assertEqual(self.check()["status"], "FAILED")

    def test_required_information_not_dropped(self):
        self.result["unresolved_requirement_details"] = []
        self.assertEqual(self.check()["status"], "FAILED")

    def test_controller_recheck_list_is_consistent(self):
        self.result["controller_observation"]["recheck_claim_ids"] = []
        self.assertEqual(self.check()["status"], "FAILED")

    def test_source_page_tampering_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = copy.deepcopy(self.source)
            source["claim_results"][0]["source_page"] = 99
            path = Path(tmp) / "source.json"
            a.write_json(path, source)
            with self.assertRaises(ValueError):
                a.load_inputs(self.paths[0], path, self.paths[2])

    def test_full_pipeline_saves_actual_function_result(self):
        before = [p.read_bytes() for p in self.paths]
        api_result = copy.deepcopy(self.result)
        api_result["claim_assessments"] = [r for r in api_result["claim_assessments"] if r["evidence_level"] != "NOT_ASSESSED"]
        api_result["controller_observation"]["recheck_claim_ids"] = []
        response = {"api_status": "completed", "model": "TEST DOUBLE NOT LIVE", "text": "TEST DOUBLE", "parsed": api_result}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(a, "request_structured_output", return_value=response):
                log, out = a.assess_evidence(*self.paths, output_dir=Path(tmp))
            self.assertEqual(log["status"], "SUCCESS")
            self.assertFalse(log["external_search_performed"])
            actual = a.read_json(out / "evidence_assessment.json")
            self.assertEqual(len(actual["claim_assessments"]), 10)
            self.assertEqual(actual["controller_observation"]["recheck_claim_ids"], ["c7", "c10"])
            self.assertFalse(a.read_json(out / "controller_observation.json")["controller_called"])
        self.assertEqual(before, [p.read_bytes() for p in self.paths])

    def test_unverified_claims_not_sent_for_api_assessment(self):
        analysis, source = a.prepare_api_inputs(self.analysis, self.source)
        self.assertEqual(len(analysis["claims"]), 8)
        self.assertTrue(all(c["pdf_status"] == "VERIFIED" for c in source["claim_results"]))
        self.assertTrue({"c7", "c10"}.isdisjoint(c["claim_id"] for c in analysis["claims"]))

    def test_api_failure_is_not_assessment_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(a, "request_structured_output", side_effect=TimeoutError("TEST ONLY")):
                log, out = a.assess_evidence(*self.paths, output_dir=Path(tmp))
            self.assertEqual(log["status"], "FAILED")
            self.assertFalse((out / "evidence_assessment.json").exists())


if __name__ == "__main__":
    unittest.main()
