"""STEP7-3 로컬 모의 파일 테스트. 검색·Analysis·실제 API 호출 없음."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz
from dc_verify.step.step7.code import document_preparer as module


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.body = b'<html><head><meta property="article:published_time" content="2026-09-01"></head><body><nav>menu</nav><main><article><h1>Example Campus</h1><p>Operating capacity and project status description.</p></article></main><footer>footer</footer></body></html>'

    def prepare(self, body=None, published=True, index=False, host="issuer.example", expected_hash=None):
        body = self.body if body is None else body
        if not published:
            body = body.replace(b'<meta property="article:published_time" content="2026-09-01">', b'')
        path = self.source / ("original.pdf" if body.startswith(b"%PDF") else "original.html")
        path.write_bytes(body)
        doc = {"document_id": "doc_example", "url": "https://" + host + "/doc", "title": "Example Campus",
            "downloaded_file": str(path), "sha256": expected_hash or module.sha(body), "bytes": len(body)}
        handoff = {"review_goal": "Review campus operation", "target_projects": ["Example Campus"], "as_of_date": "2026-11-11",
            "document_ids": ["old"], "priority_pages": [1], "required_metrics": ["operating capacity"], "extraction_instructions": ["Do not convert metrics"]}
        module.write_json(self.source / "source_verification_input.json", {"documents": [doc], "review_goal": handoff["review_goal"],
            "target_projects": handoff["target_projects"], "as_of_date": handoff["as_of_date"], "initial_analysis_handoff": handoff})
        module.write_json(self.source / "executor_input.json", {"search_plan": {"search_plan": [{"issue_id": "issue_capacity", "required_evidence": "operating capacity", "query": "Example Campus official", "priority": 1}]}})
        module.write_json(self.source / "runtime_tools.json", {"official_hosts": ["issuer.example"], "official_host_provenance": {
            "method": "archived corporate links", "observed_urls": ["https://issuer.example/"]}})
        module.write_json(self.source / "raw_retrieval/identity.json", {"operation": "OPEN_DOCUMENT", "response": "Example official site https://issuer.example/"})
        module.write_json(self.source / "state.json", {"retrieval_events": [{"operation": "DISCOVER_DOCUMENT_LINKS", "parent_url": doc["url"]}] if index else []})
        module.write_json(self.source / "search_history_updated.json", {"action_history": [{"executed": True, "query": "prior search"}]})
        module.write_json(self.source / "search_budget_updated.json", {"remaining_attempts": {"issue_capacity": 1}})
        preparer = module.Preparer(self.source, self.root / "output")
        return preparer, doc

    def test_hash_mismatch_excluded(self):
        prep, doc = self.prepare(expected_hash="0" * 64)
        row = prep.process(doc)
        self.assertEqual(row["hash_check"], "MISMATCH")
        self.assertEqual(row["decision"], "EXCLUDED")
        self.assertIsNone(row["analysis_bundle"])

    def test_missing_file_excluded(self):
        prep, doc = self.prepare()
        Path(doc["downloaded_file"]).unlink()
        row = prep.process(doc)
        self.assertEqual(row["decision"], "EXCLUDED")
        self.assertIn("SOURCE_FILE_MISSING", row["reason_codes"])

    def test_original_primary_html_ready(self):
        prep, doc = self.prepare()
        row = prep.process(doc)
        self.assertEqual(row["decision"], "READY")
        self.assertEqual(row["published_at"], "2026-09-01")
        self.assertFalse(row["claims_verified"])

    def test_unknown_publication_is_held(self):
        prep, doc = self.prepare(published=False)
        row = prep.process(doc)
        self.assertEqual(row["decision"], "HOLD")
        self.assertIn("PUBLICATION_DATE_UNCONFIRMED", row["reason_codes"])
        self.assertTrue(Path(row["analysis_bundle"]).is_file())

    def test_index_date_not_used_for_linked_documents(self):
        prep, doc = self.prepare(index=True)
        row = prep.process(doc)
        self.assertEqual(row["decision"], "EXCLUDED")
        self.assertIn("INDEX_NOT_EVIDENCE_DOCUMENT", row["reason_codes"])
        self.assertIsNone(row["analysis_bundle"])

    def test_future_publication_excluded(self):
        prep, doc = self.prepare(self.body.replace(b"2026-09-01", b"2027-01-01"))
        row = prep.process(doc)
        self.assertEqual(row["decision"], "EXCLUDED")
        self.assertIn("PUBLISHED_AFTER_CUTOFF", row["reason_codes"])

    def test_unrelated_document_excluded(self):
        body = self.body.replace(b"Example Campus", b"Marine Exhibition").replace(b"Operating capacity and project status description.", b"Ocean wildlife story.")
        prep, doc = self.prepare(body)
        self.assertEqual(prep.process(doc)["decision"], "EXCLUDED")

    def test_similar_but_different_host_not_official(self):
        prep, doc = self.prepare(host="issuer.example.evil.test")
        row = prep.process(doc)
        self.assertEqual(row["decision"], "HOLD")
        self.assertIn("OFFICIAL_ORIGIN_UNCONFIRMED", row["reason_codes"])

    def test_menu_and_footer_removed(self):
        text, selector = module.parse_html(self.body)
        self.assertIn("Operating capacity", text)
        self.assertNotIn("menu", text)
        self.assertNotIn("footer", text)
        self.assertIn(selector, ["article", "main"])

    def test_font_mapping_failure_detected(self):
        self.assertEqual(module.text_quality("팀" * 100 + " 2026.9 20MW"), "SUSPECT_FONT_MAPPING")
        self.assertEqual(module.text_quality(""), "EMPTY_OR_SPARSE")
        self.assertEqual(module.text_quality("A readable English document with ordinary words and numbers."), "READABLE")

    def test_month_is_not_invented_as_exact_day(self):
        value = module.document_period("IR Book\n2026년 9월")
        self.assertEqual(value["value"], "2026-09")
        self.assertEqual(value["precision"], "MONTH")
        self.assertEqual(module.period_cutoff(value, "2026-11-11"), "WITHIN_CUTOFF")

    def test_cutoff_overlaps_month(self):
        self.assertEqual(module.period_cutoff(module.document_period("November 2026"), "2026-11-11"), "OVERLAPS_CUTOFF")
        self.assertEqual(module.period_cutoff(module.document_period("December 2026"), "2026-11-11"), "AFTER_CUTOFF")

    def test_filename_not_used_as_document_date(self):
        self.assertIsNone(module.document_period("IR Book\nFor investors")["value"])

    def test_corrupt_pdf_excluded(self):
        prep, doc = self.prepare(b"%PDF-1.7\ninvalid document")
        row = prep.process(doc)
        self.assertEqual(row["decision"], "EXCLUDED")
        self.assertIn("CORRUPT_OR_UNREADABLE_DOCUMENT", row["reason_codes"])

    def test_ocr_empty_response_held(self):
        pdf = fitz.open()
        pdf.new_page()
        body = pdf.tobytes()
        pdf.close()
        prep, doc = self.prepare(body)
        prep.allow_vision = True
        screen = {"document_role": "PRIMARY_DOCUMENT", "target_relation": "DIRECT", "selected_pages": [
            {"source_page": 1, "issue_ids": ["issue_capacity"], "reason": "topic"}], "uncertainty_flags": []}
        with patch.object(prep, "api", side_effect=[screen, {"pages": [{"source_page": 1, "text": "", "uncertainty_flags": []}]}]):
            row = prep.process(doc)
        self.assertEqual(row["decision"], "HOLD")
        self.assertIn("DOCUMENT_OR_OCR_PROCESSING_FAILED", row["reason_codes"])

    def test_page_text_and_segments_are_hash_bound(self):
        prep, doc = self.prepare()
        row = prep.process(doc)
        bundle = module.read_json(row["analysis_bundle"])
        page = bundle["documents"][0]["pages"][0]
        self.assertEqual(page["ocr_text_sha256"], module.sha(page["text"].encode()))
        for segment in module.read_json(row["relevant_segments_file"]):
            self.assertEqual(page["text"][segment["character_start"]:segment["character_end"]], segment["text"])

    def test_search_history_budget_and_inputs_unchanged(self):
        prep, doc = self.prepare()
        with patch.object(module, "request_structured_output") as text_api, patch.object(module, "request_image_verification") as vision_api:
            report = prep.run()
        text_api.assert_not_called()
        vision_api.assert_not_called()
        self.assertTrue(report["search_history_unchanged"])
        self.assertEqual(report["new_web_searches"], 0)
        self.assertFalse(report["analysis_agent_executed"])

    def test_held_documents_not_default_analysis_inputs(self):
        prep, doc = self.prepare(published=False)
        prep.run()
        output = module.read_json(prep.output / "analysis_input.json")
        self.assertEqual(output["ready_documents"], [])
        self.assertEqual(len(output["held_documents_conditional_only"]), 1)
        self.assertIn("bundle", output["held_documents_conditional_only"][0])

    def test_cache_only_blocks_new_api_requests(self):
        prep, _ = self.prepare()
        prep.cache_only = True
        with patch.object(module, "request_structured_output") as api:
            with self.assertRaises(ValueError):
                prep.api(prep.output / "doc", "ocr", "system", "user", module.OCRResult)
        api.assert_not_called()

    def test_host_allowlist_alone_is_not_source_proof(self):
        prep, doc = self.prepare()
        prep.proof_hashes = {}
        row = prep.process(doc)
        self.assertEqual(row["decision"], "HOLD")
        self.assertIn("OFFICIAL_ORIGIN_UNCONFIRMED", row["reason_codes"])


if __name__ == "__main__":
    unittest.main()
