"""Source Admission Gate 테스트. 실제 모델·검색·Analysis 호출 없음."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dc_verify.step.step8.code import source_admission as gate


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.file = self.source / "original.html"
        self.file.write_bytes(b"Example Project original source")
        self.proof = self.source / "identity.txt"
        self.proof.write_text("https://issuer.example/ official identity", encoding="utf-8")
        sha = gate.digest(self.file.read_bytes())
        self.context = {"review_goal": "Review Example Project", "target_projects": ["Example Project"], "as_of_date": "2026-11-11"}
        self.row = {"document_id": "example", "url": "https://issuer.example/project", "source_file": str(self.file),
            "decision": "READY", "hash_check": "MATCH", "expected_sha256": sha, "actual_sha256": sha,
            "origin_check": "SUPPORTED_BY_ARCHIVED_OFFICIAL_HOST_LINKS", "published_at": "2026-09-01",
            "publication_precision": "DAY", "document_period": None, "reason_codes": [], "flags": ["SOURCE_WARNING"],
            "related_issue_ids": ["issue_operation"], "screening": {"document_role": "PRIMARY_DOCUMENT", "target_relation": "DIRECT"},
            "archived_origin_proof_files": [{"file": str(self.proof), "sha256": gate.digest(self.proof.read_bytes())}]}
        text = "Example Project original page text."
        self.bundle = {"analysis_handoff": {**self.context, "document_ids": ["example"], "priority_pages": [1],
            "required_metrics": ["operation"], "extraction_instructions": ["Keep original metrics"]},
            "documents": [{"document_id": "example", "source_url": self.row["url"], "source_file": str(self.file),
                "source_sha256": sha, "published_at": "2026-09-01", "pages": [{"source_page": 1,
                    "text": text, "ocr_text_sha256": gate.digest(text.encode()), "uncertainty_flags": ["PAGE_WARNING"]}]}]}
        self.selection = {"selected_pages": [1], "selection_authority": "FIXTURE"}

    def assess(self):
        return gate.assess_document(self.row, self.bundle, self.selection, self.context)

    def conditional(self):
        self.row.update(decision="HOLD", published_at=None, publication_precision=None,
            document_period={"value": "2026-09", "precision": "MONTH"}, reason_codes=["PUBLICATION_DATE_UNCONFIRMED"])
        self.bundle["documents"][0]["published_at"] = None
        return self.assess()

    def claim(self, decision):
        return {"claim_id": "new1", "source_document_id": decision["document_id"], "source_page": 1,
                "source_published_at": decision["published_at"], "evidence_quote": "original page text"}

    def persist(self):
        gate.write_json(self.source / "bundle.json", self.bundle)
        gate.write_json(self.source / "selection.json", self.selection)
        self.row.update(analysis_bundle=str(self.source / "bundle.json"), analysis_request=str(self.source / "selection.json"))
        gate.write_json(self.source / "document_validation.json", {"documents": [self.row]})
        entry = {"document_id": "example", "decision": self.row["decision"], "bundle": self.bundle, "selection": self.selection}
        gate.write_json(self.source / "analysis_input.json", {"review_goal": self.context["review_goal"], "as_of_date": self.context["as_of_date"],
            "ready_documents": [entry] if self.row["decision"] == "READY" else [],
            "held_documents_conditional_only": [entry] if self.row["decision"] == "HOLD" else []})
        gate.write_json(self.source / "controller_observation.json", {"unresolved_issues": [{"issue_id": "issue_operation"}]})

    def test_ready_allows_analysis_but_not_final_evidence(self):
        result = self.assess()
        self.assertEqual(result["analysis_admission"], "ALLOW")
        self.assertFalse(result["final_evidence_allowed"])
        self.assertFalse(result["claims_verified"])

    def test_date_only_hold_is_conditional_and_status_preserved(self):
        before = copy.deepcopy(self.row)
        result = self.conditional()
        self.assertEqual(result["analysis_admission"], "CONDITIONAL")
        self.assertEqual(result["original_preparation_status"], "HOLD")
        self.assertTrue(result["provisional_claims_required"])
        self.assertIsNone(result["published_at"])
        self.assertEqual(result["source_sha256"], before["expected_sha256"])

    def test_excluded_is_never_admitted(self):
        self.row["decision"] = "EXCLUDED"
        self.assertEqual(self.assess()["analysis_admission"], "DENY")

    def test_source_hash_mismatch_denied(self):
        self.file.write_bytes(b"changed original")
        self.assertIn("SOURCE_INTEGRITY_MISMATCH", self.assess()["reason_codes"])

    def test_missing_source_denied(self):
        self.file.unlink()
        self.assertEqual(self.assess()["analysis_admission"], "DENY")

    def test_origin_proof_tampering_denied(self):
        self.proof.write_text("altered identity")
        self.assertIn("ORIGIN_PROOF_INTEGRITY_MISMATCH", self.assess()["reason_codes"])

    def test_unknown_official_origin_denied(self):
        self.row["origin_check"] = "UNCONFIRMED"
        self.assertEqual(self.assess()["analysis_admission"], "DENY")

    def test_future_publication_denied(self):
        self.row["published_at"] = self.bundle["documents"][0]["published_at"] = "2026-11-12"
        self.assertIn("PUBLISHED_AFTER_CUTOFF", self.assess()["reason_codes"])

    def test_cutoff_day_is_inclusive(self):
        self.row["published_at"] = self.bundle["documents"][0]["published_at"] = "2026-11-11"
        self.assertEqual(self.assess()["analysis_admission"], "ALLOW")

    def test_month_overlapping_cutoff_is_conditional(self):
        self.row["published_at"] = self.bundle["documents"][0]["published_at"] = "2026-11"
        self.assertEqual(self.assess()["analysis_admission"], "CONDITIONAL")
        self.assertIn("DATE_PRECISION_OVERLAPS_CUTOFF", self.assess()["reason_codes"])

    def test_future_document_period_denied(self):
        self.conditional()
        self.row["document_period"]["value"] = "2027-01"
        self.assertEqual(self.assess()["analysis_admission"], "DENY")

    def test_missing_publication_and_period_stay_conditional(self):
        self.conditional()
        self.row["document_period"] = None
        self.assertEqual(self.assess()["analysis_admission"], "CONDITIONAL")

    def test_corrupt_or_ocr_failed_hold_is_not_promoted(self):
        self.conditional()
        self.row["reason_codes"] = ["DOCUMENT_OR_OCR_PROCESSING_FAILED"]
        self.assertIn("HOLD_REASON_REQUIRES_REPREPARATION", self.assess()["reason_codes"])

    def test_prepared_text_hash_mismatch_denied(self):
        self.bundle["documents"][0]["pages"][0]["text"] = "changed OCR"
        self.assertIn("PREPARED_TEXT_INTEGRITY_MISMATCH", self.assess()["reason_codes"])

    def test_source_url_mismatch_denied(self):
        self.bundle["documents"][0]["source_url"] = "https://other.example/"
        self.assertIn("SOURCE_PROVENANCE_MISMATCH", self.assess()["reason_codes"])

    def test_review_goal_cannot_change(self):
        self.bundle["analysis_handoff"]["review_goal"] = "Different goal"
        self.assertEqual(self.assess()["analysis_admission"], "DENY")

    def test_page_outside_selection_denied(self):
        self.selection["selected_pages"] = [2]
        self.assertIn("PAGE_SELECTION_MISMATCH", self.assess()["reason_codes"])

    def test_provisional_claim_and_warnings_propagated(self):
        result = self.conditional()
        claim = self.claim(result)
        before = copy.deepcopy(claim)
        wrapped = gate.guard_claims([claim], [result])[0]
        self.assertTrue(wrapped["is_provisional"])
        self.assertFalse(wrapped["final_evidence_allowed"])
        self.assertEqual(wrapped["claim_verification_status"], "PENDING")
        self.assertIn("PAGE_WARNING", wrapped["source_warnings"])
        self.assertEqual(claim, before)
        self.assertEqual(wrapped["claim"], before)

    def test_verified_promotion_rejected_for_conditional(self):
        result = self.conditional()
        claim = {**self.claim(result), "verification_status": "VERIFIED"}
        with self.assertRaises(ValueError):
            gate.guard_claims([claim], [result])

    def test_allow_claim_cannot_skip_source_verification(self):
        result = self.assess()
        claim = {**self.claim(result), "final_evidence_allowed": True}
        with self.assertRaises(ValueError):
            gate.guard_claims([claim], [result])

    def test_string_boolean_cannot_bypass_guard(self):
        result = self.conditional()
        with self.assertRaises(ValueError):
            gate.guard_claims([{**self.claim(result), "verified": "true"}], [result])

    def test_provisional_marker_cannot_be_removed(self):
        result = self.conditional()
        with self.assertRaises(ValueError):
            gate.guard_claims([{**self.claim(result), "is_provisional": False}], [result])

    def test_invented_publication_date_rejected(self):
        result = self.conditional()
        with self.assertRaises(ValueError):
            gate.guard_claims([{**self.claim(result), "source_published_at": "2026-09-16"}], [result])

    def test_unknown_document_claim_rejected(self):
        result = self.assess()
        with self.assertRaises(ValueError):
            gate.guard_claims([{**self.claim(result), "source_document_id": "other"}], [result])

    def test_duplicate_claim_ids_rejected(self):
        result = self.assess()
        with self.assertRaises(ValueError):
            gate.guard_claims([self.claim(result), self.claim(result)], [result])

    def test_denied_document_claim_rejected(self):
        self.row["decision"] = "EXCLUDED"
        result = self.assess()
        with self.assertRaises(ValueError):
            gate.guard_claims([self.claim(result)], [result])

    def test_tampered_admission_cannot_enable_evidence(self):
        result = self.conditional()
        result["final_evidence_allowed"] = True
        with self.assertRaises(ValueError):
            gate.guard_claims([self.claim(result)], [result])

    def test_pure_assessment_preserves_inputs(self):
        original = copy.deepcopy((self.row, self.bundle, self.selection))
        self.assess()
        self.assertEqual((self.row, self.bundle, self.selection), original)

    def test_gate_outputs_and_source_status_unchanged(self):
        self.conditional()
        self.persist()
        before = {str(p): p.read_bytes() for p in self.source.rglob('*') if p.is_file()}
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            report, output = gate.run_gate(self.source, self.root / "outputs")
        self.assertEqual(report["counts"], {"CONDITIONAL": 1})
        self.assertFalse(report["analysis_agent_executed"])
        self.assertEqual(report["model_api_calls"], 0)
        self.assertTrue(all(Path(p).read_bytes() == content for p, content in before.items()))
        queue = gate.read_json(output / "analysis_input.json")
        self.assertEqual(len(queue["analysis_jobs"]), 1)
        policy = queue["analysis_jobs"][0]["bundle"]["analysis_handoff"]["source_admission_policy"]
        self.assertTrue(policy["provisional_claims_required"])
        self.assertFalse(policy["final_evidence_allowed"])
        self.assertEqual(gate.read_json(output / "source_admission.json")["documents"][0]["original_preparation_status"], "HOLD")

    def test_inline_and_file_bundle_mismatch_denied(self):
        self.persist()
        altered = copy.deepcopy(self.bundle)
        altered["analysis_handoff"]["extraction_instructions"].append("different instruction")
        gate.write_json(self.source / "bundle.json", altered)
        report, _ = gate.run_gate(self.source, self.root / "outputs")
        self.assertEqual(report["counts"], {"DENY": 1})

    def test_latest_successful_preparation_skips_failed_newer_run(self):
        for folder, status, finished in [("document_preparation_a", "SUCCESS", "2026-11-11T01:00:00+00:00"),
                                          ("document_preparation_b", "FAILED", "2026-11-11T02:00:00+00:00")]:
            p = self.root / folder
            gate.write_json(p / "run.json", {"status": status, "finished_at": finished})
            for name in ["document_validation.json", "analysis_input.json", "controller_observation.json"]:
                gate.write_json(p / name, {})
        self.assertEqual(gate.latest_preparation(self.root).name, "document_preparation_a")


if __name__ == "__main__":
    unittest.main()
