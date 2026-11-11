"""실제 PDF 렌더링·OCR 검사와 보수적 상태 처리를 검증. 유료 API 호출 없음."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from dc_verify.step.step4.code import source_verifier as v
from dc_verify.codex import APIConfig, request_image_verification


class SourceVerifierTests(unittest.TestCase):
    def setUp(self):
        if not (stage_data(4) / 'analysis_result.json').is_file(): self.skipTest('Private experiment fixture not distributed')
        self.analysis_path = stage_data(4) / 'analysis_result.json'
        self.ocr_path = stage_data(4) / 'analysis_input.json'
        self.pdf = stage_data(4) / 'kinx_ir_240925.pdf'
        self.analysis, self.ocr = v.load_inputs(self.analysis_path, self.ocr_path)
        self.claims = self.analysis["claims"]
        self.checks = v.check_ocr(self.claims, self.ocr["ocr_pages"])

    def example_response(self):
        rows = [{"claim_id": c["claim_id"], "source_page": c["source_page"], "ocr_match": True,
                 "pdf_status": "VERIFIED", "error_types": [], "reason": "TEST DOUBLE ONLY",
                 "review_required": False, "image_evidence_text": "TEST DOUBLE ONLY",
                 "context_type": "unknown", "temporal_context": None,
                 "field_checks": {k: "MATCH" for k in v.FieldChecks.model_fields}} for c in self.claims]
        return {"verification_status": "COMPLETED", "claim_results": rows,
                "controller_observation": {"verified_claim_ids": [c["claim_id"] for c in self.claims],
                    "recheck_claim_ids": [], "unresolved_issues": [], "technical_failures": []}}

    def image_refs(self):
        return [{"document_id": c["source_document_id"], "source_page": c["source_page"]} for c in self.claims]

    def test_all_ocr_quotes_match(self):
        self.assertEqual(len(self.checks), 10)
        self.assertTrue(all(c["ocr_match"] for c in self.checks.values()))

    def test_fake_quote_and_value_detected(self):
        claims = copy.deepcopy(self.claims)
        claims[0]["evidence_quote"] = "존재하지 않는 인용문"
        claims[2]["value"] = 999.0
        checked = v.check_ocr(claims, self.ocr["ocr_pages"])
        self.assertFalse(checked[claims[0]["claim_id"]]["ocr_match"])
        self.assertIn("OCR_VALUE_MISMATCH", checked[claims[2]["claim_id"]]["issues"])

    def test_pdf_hash_and_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            images, errors = v.render_original_pages(self.pdf, self.ocr["documents"][0], self.claims, Path(tmp))
            self.assertFalse(errors)
            self.assertEqual([i["source_page"] for i in images], [23, 6, 7])
            self.assertTrue(all(v.sha256(Path(i["path"]).read_bytes()) == i["sha256"] for i in images))

    def test_wrong_hash_never_renders(self):
        metadata = {**self.ocr["documents"][0], "source_pdf_sha256": "wrong"}
        with tempfile.TemporaryDirectory() as tmp:
            images, errors = v.render_original_pages(self.pdf, metadata, self.claims, Path(tmp))
        self.assertFalse(images)
        self.assertTrue(all(e["status"] == "VERIFICATION_FAILED" for e in errors.values()))

    def test_missing_pdf_is_not_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            images, errors = v.render_original_pages(Path(tmp) / "absent.pdf", self.ocr["documents"][0], self.claims, Path(tmp))
        self.assertFalse(images)
        self.assertTrue(all(e["status"] == "PDF_NOT_VERIFIED" for e in errors.values()))

    def test_verified_requires_images(self):
        with self.assertRaises(ValueError):
            v.validate_model_result(self.example_response(), self.claims, self.checks, [])

    def test_missing_claim_rejected(self):
        result = self.example_response()
        result["claim_results"].pop()
        with self.assertRaises(ValueError):
            v.validate_model_result(result, self.claims, self.checks, self.image_refs())

    def test_duplicate_claim_rejected(self):
        result = self.example_response()
        result["claim_results"][-1] = copy.deepcopy(result["claim_results"][0])
        with self.assertRaises(ValueError):
            v.validate_model_result(result, self.claims, self.checks, self.image_refs())

    def test_wrong_source_page_rejected(self):
        result = self.example_response()
        result["claim_results"][0]["source_page"] = 99
        with self.assertRaises(ValueError):
            v.validate_model_result(result, self.claims, self.checks, self.image_refs())

    def test_verified_cannot_have_errors(self):
        result = self.example_response()
        result["claim_results"][0]["error_types"] = ["TEMPORAL_CONTEXT_ERROR"]
        with self.assertRaises(ValueError):
            v.validate_model_result(result, self.claims, self.checks, self.image_refs())

    def test_ocr_mismatch_cannot_be_verified(self):
        checks = copy.deepcopy(self.checks)
        checks[self.claims[0]["claim_id"]]["ocr_match"] = False
        result = self.example_response()
        result["claim_results"][0]["ocr_match"] = False
        with self.assertRaises(ValueError):
            v.validate_model_result(result, self.claims, checks, self.image_refs())

    def test_observation_matches_claim_statuses(self):
        result = self.example_response()
        result["controller_observation"]["verified_claim_ids"] = []
        with self.assertRaises(ValueError):
            v.validate_model_result(result, self.claims, self.checks, self.image_refs())

    def test_conflicting_scope_classification_needs_review(self):
        result = self.example_response()
        result["claim_results"][2]["context_type"] = "general_definition"
        guarded = v.apply_review_guards(result, self.claims)
        self.assertEqual(guarded["claim_results"][2]["pdf_status"], "NEEDS_MANUAL_REVIEW")
        self.assertEqual(result["claim_results"][2]["pdf_status"], "VERIFIED")

    def test_heading_only_evidence_does_not_verify_notes(self):
        claims = copy.deepcopy(self.claims)
        claims[0].update(evidence_quote="테스트 제목", metric_name="테스트 제목", raw_value="테스트 제목", notes=["추가 서술"])
        guarded = v.apply_review_guards(self.example_response(), claims)
        self.assertEqual(guarded["claim_results"][0]["pdf_status"], "NEEDS_MANUAL_REVIEW")
        self.assertIn("HEADING_ONLY_EVIDENCE", guarded["claim_results"][0]["error_types"])

    def test_missing_pdf_pipeline_makes_no_api_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(v, "request_image_verification", side_effect=AssertionError("must not call")):
                log, out = v.run_verifier(self.analysis_path, self.ocr_path, Path(tmp) / "absent.pdf", Path(tmp))
            result = v.read_json(out / "source_verification.json")
        self.assertFalse(log["api_attempted"])
        self.assertEqual(len(result["claim_results"]), 10)
        self.assertTrue(all(c["pdf_status"] == "PDF_NOT_VERIFIED" for c in result["claim_results"]))

    def test_api_failure_is_technical_and_preserves_all_claims(self):
        original = self.analysis_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(v, "request_image_verification", side_effect=TimeoutError("TEST ONLY")):
                log, out = v.run_verifier(self.analysis_path, self.ocr_path, self.pdf, Path(tmp))
            result = v.read_json(out / "source_verification.json")
        self.assertEqual(log["status"], "TECHNICAL_FAILURE")
        self.assertEqual(len(result["claim_results"]), 10)
        self.assertTrue(all(c["pdf_status"] == "VERIFICATION_FAILED" for c in result["claim_results"]))
        self.assertEqual(original, self.analysis_path.read_bytes())

    def test_actual_png_is_in_api_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            images, _ = v.render_original_pages(self.pdf, self.ocr["documents"][0], self.claims, Path(tmp))
            parsed = v.VerificationResult.model_validate(self.example_response())
            response = SimpleNamespace(status="completed", model="TEST DOUBLE", id="test", usage=None,
                output_text="TEST DOUBLE", output_parsed=parsed)
            parse = Mock(return_value=response)
            request_image_verification("system", "user", images, v.VerificationResult,
                config=APIConfig(api_key="test-only"), client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))
            sent = parse.call_args.kwargs
            self.assertEqual(sent["model"], "gpt-5.4-mini")
            self.assertFalse(sent["store"])
            content = sent["input"][0]["content"]
            self.assertEqual(len([c for c in content if c["type"] == "input_image"]), 3)
            self.assertTrue(all(c["image_url"].startswith("data:image/png;base64,") for c in content if c["type"] == "input_image"))
            Path(images[0]["path"]).write_bytes(b"\x89PNG\r\n\x1a\nchanged")
            with self.assertRaises(ValueError):
                request_image_verification("system", "user", images, v.VerificationResult,
                    config=APIConfig(api_key="test-only"), client=SimpleNamespace(responses=SimpleNamespace(parse=parse)))


if __name__ == "__main__":
    unittest.main()
