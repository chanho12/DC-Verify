"""STEP8-1 단위·모의 통합 검사. 실제 검색·모델 호출 없음."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from dc_verify.step.step8.code import external_analysis as external
from dc_verify.step.step8.code.source_admission import run_gate, digest, write_json, read_json


def sample_result(payload):
    handoff = payload["analysis_handoff"]
    doc_id = handoff["document_ids"][0]
    return {"analysis_status": "INCOMPLETE", **{k: handoff[k] for k in ("review_goal", "target_projects", "as_of_date")},
        "analyzed_pages": [{"document_id": p["document_id"], "source_page": p["source_page"]} for p in payload["ocr_pages"]],
        "unprocessed_controller_pages": payload["execution_scope"]["unprocessed_controller_pages"],
        "claims": [{"claim_id": doc_id + ":c1", "project_name": handoff["target_projects"][0], "metric_type": "other",
            "metric_name": "status", "value": None, "unit": None, "raw_value": "pending", "value_qualifier": "text_only",
            "facility_scope": None, "development_status": None, "source_published_at": payload["documents"][0]["published_at"],
            "metric_as_of": None, "source_document_id": doc_id, "source_page": 1, "evidence_quote": "status pending",
            "temporal_basis": "unspecified", "notes": [], "measurement_basis": "OTHER", "statement_status": "UNSPECIFIED"}],
        "required_metric_coverage": [{"requested_metric": m, "status": "NOT_FOUND", "claim_ids": [], "reason": "not explicit"} for m in handoff["required_metrics"]],
        "missing_required_information": [{"requested_metric": m, "metric_type": None, "reason": "not explicit", "needed_evidence": "original source"} for m in handoff["required_metrics"]],
        "ambiguities": [], "ocr_review_flags": [], "recommendations_for_controller": []}


class ExternalAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "prepared"
        source.mkdir()
        proof = source / "identity.txt"
        proof.write_text("issuer.example official identity")
        context = {"review_goal": "Review Example Site", "target_projects": ["Example Site"], "as_of_date": "2026-11-11"}
        rows, ready, held = [], [], []
        for identifier, status in [("doc_a", "READY"), ("doc_b", "HOLD"), ("doc_c", "EXCLUDED")]:
            file = source / (identifier + ".html")
            file.write_text("Example Site status pending")
            published = "2026-09-01" if status == "READY" else None
            sha = digest(file.read_bytes())
            row = {"document_id": identifier, "url": "https://issuer.example/" + identifier, "source_file": str(file),
                "decision": status, "hash_check": "MATCH", "expected_sha256": sha, "actual_sha256": sha,
                "origin_check": "SUPPORTED_BY_ARCHIVED_OFFICIAL_HOST_LINKS", "published_at": published,
                "reason_codes": ["PUBLICATION_DATE_UNCONFIRMED"] if status == "HOLD" else [], "flags": [],
                "related_issue_ids": ["issue1"], "screening": {"document_role": "PRIMARY_DOCUMENT" if status != "EXCLUDED" else "INDEX", "target_relation": "DIRECT"},
                "archived_origin_proof_files": [{"file": str(proof), "sha256": digest(proof.read_bytes())}]}
            if status != "EXCLUDED":
                text = "Example Site status pending."
                bundle = {"analysis_handoff": {**context, "document_ids": [identifier], "priority_pages": [1], "required_metrics": ["operating status"], "extraction_instructions": []},
                    "documents": [{"document_id": identifier, "source_url": row["url"], "source_file": str(file), "source_sha256": sha,
                        "published_at": published, "pages": [{"source_page": 1, "text": text, "ocr_text_sha256": digest(text.encode())}]}]}
                selection = {"selected_pages": [1], "selection_authority": "FIXTURE"}
                bp, sp = source / (identifier + "_bundle.json"), source / (identifier + "_selection.json")
                write_json(bp, bundle);write_json(sp, selection)
                row.update(analysis_bundle=str(bp), analysis_request=str(sp))
                entry = {"document_id": identifier, "decision": status, "bundle": bundle, "selection": selection}
                (ready if status == "READY" else held).append(entry)
            rows.append(row)
        write_json(source / "document_validation.json", {"documents": rows})
        write_json(source / "analysis_input.json", {"review_goal": context["review_goal"], "as_of_date": context["as_of_date"], "ready_documents": ready, "held_documents_conditional_only": held})
        write_json(source / "controller_observation.json", {"unresolved_issues": [{"issue_id": "issue1"}]})
        _, self.gate = run_gate(source, self.root / "gates")
        incoming = read_json(self.gate / "analysis_input.json")
        self.job = incoming["analysis_jobs"][0]
        self.payload = external.agent.prepare_input(self.job["bundle"], self.job["selection"])
        self.result = sample_result(self.payload)

    def mock_agent(self, input_file, request_file, output_dir, **kwargs):
        output = Path(output_dir) / ("analysis_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        output.mkdir(parents=True)
        payload = external.agent.prepare_input(read_json(input_file), read_json(request_file))
        result = sample_result(payload)
        _, validation = external.agent.check_result(result, payload)
        write_json(output / "analysis_result.json", result)
        write_json(output / "validation.json", validation)
        log = {"status": "SUCCESS", "api_attempted": False, "model": "MOCK"}
        write_json(output / "run.json", log)
        return log, output

    def test_mock_pipeline_calls_existing_agent_for_allow_and_conditional(self):
        with patch.object(external.agent, "run_analysis", side_effect=self.mock_agent) as api, patch.object(external, "latest_run", side_effect=ValueError("fixture")):
            report, output = external.execute(self.gate, self.root / "outputs")
        self.assertEqual(api.call_count, 2)
        self.assertEqual(report["completed_document_count"], 2)
        claims = read_json(output / "external_claims.json")["claims"]
        self.assertEqual(sum(c["is_provisional"] for c in claims), 1)
        self.assertTrue(all(not c["final_evidence_allowed"] for c in claims))
        self.assertEqual(len(read_json(output / "verification_input.json")["verification_jobs"]), 2)

    def test_dry_run_never_calls_agent_api(self):
        with patch.object(external.agent, "run_analysis") as api, patch.object(external, "latest_run", side_effect=ValueError("fixture")):
            report, _ = external.execute(self.gate, self.root / "outputs", dry_run=True)
        api.assert_not_called()
        self.assertEqual(report["status"], "PREPARED")

    def test_resume_reuses_completed_documents(self):
        with patch.object(external.agent, "run_analysis", side_effect=self.mock_agent), patch.object(external, "latest_run", side_effect=ValueError("fixture")):
            _, output = external.execute(self.gate, self.root / "outputs")
        with patch.object(external.agent, "run_analysis") as api, patch.object(external, "latest_run", side_effect=ValueError("fixture")):
            report, _ = external.execute(self.gate, resume=output)
        api.assert_not_called()
        self.assertEqual(report["status"], "SUCCESS")

    def test_denied_job_cannot_be_inserted(self):
        p = self.gate / "analysis_input.json"
        incoming = read_json(p)
        incoming["analysis_jobs"].append({**incoming["analysis_jobs"][0], "document_id": "doc_c"})
        write_json(p, incoming)
        with self.assertRaises(ValueError):
            external.load_admitted_jobs(self.gate)

    def test_disabled_guard_contract_rejected(self):
        p = self.gate / "analysis_input.json"
        incoming = read_json(p);incoming["claim_output_guard_required"] = False;write_json(p, incoming)
        with self.assertRaises(ValueError):
            external.load_admitted_jobs(self.gate)

    def test_source_tampering_stops_before_api(self):
        Path(self.job["bundle"]["documents"][0]["source_file"]).write_text("changed")
        with patch.object(external.agent, "run_analysis") as api:
            with self.assertRaises(ValueError):
                external.execute(self.gate, self.root / "outputs")
        api.assert_not_called()

    def test_rejects_computed_number_not_present_in_raw_value(self):
        claim = self.result["claims"][0]
        claim.update(value=5, raw_value="pending")
        _, validation = external.agent.check_result(self.result, self.payload)
        self.assertEqual(validation["status"], "FAILED")

    def test_provided_capacity_cannot_be_measured_consumption(self):
        self.result["claims"][0].update(metric_type="provided_it_capacity", measurement_basis="ACTUAL_MEASURED_USAGE")
        _, validation = external.agent.check_result(self.result, self.payload)
        self.assertEqual(validation["status"], "FAILED")

    def test_publication_date_cannot_be_invented(self):
        incoming = read_json(self.gate / "analysis_input.json")
        job = next(j for j in incoming["analysis_jobs"] if j["analysis_admission"] == "CONDITIONAL")
        payload = external.agent.prepare_input(job["bundle"], job["selection"])
        result = sample_result(payload);result["claims"][0]["source_published_at"] = "2026-09-01"
        _, validation = external.agent.check_result(result, payload)
        self.assertEqual(validation["status"], "FAILED")

    def test_whitespace_binding_is_exact_and_auditable(self):
        payload = copy.deepcopy(self.payload)
        payload["ocr_pages"][0]["text"] = "Example Site status   pending."
        result, bindings = external.agent.bind_source_whitespace(self.result, payload)
        self.assertEqual(result["claims"][0]["evidence_quote"], "status   pending")
        self.assertEqual(len(bindings), 1)
        self.assertFalse(bindings[0]["fact_verified"])
        self.assertEqual(self.result["claims"][0]["evidence_quote"], "status pending")

    def test_ambiguous_whitespace_binding_not_applied(self):
        payload = copy.deepcopy(self.payload)
        payload["ocr_pages"][0]["text"] = "status  pending; status\npending"
        result, bindings = external.agent.bind_source_whitespace(self.result, payload)
        self.assertEqual(result["claims"][0]["evidence_quote"], "status pending")
        self.assertEqual(bindings, [])

    def test_binding_never_corrects_words(self):
        result = copy.deepcopy(self.result);result["claims"][0]["evidence_quote"] = "status completed"
        bound, bindings = external.agent.bind_source_whitespace(result, self.payload)
        self.assertEqual(bound, result)
        self.assertEqual(bindings, [])

    def test_scoped_schema_restricts_document_id_and_prefix(self):
        schema = external.agent.scoped_response_schema(self.payload)
        schema.model_validate(self.result)
        self.result["claims"][0]["claim_id"] = "c1"
        with self.assertRaises(ValueError):
            schema.model_validate(self.result)

    def test_contradictory_found_is_only_downgraded(self):
        self.result["required_metric_coverage"][0].update(status="FOUND", claim_ids=[self.result["claims"][0]["claim_id"]])
        original = copy.deepcopy(self.result)
        accepted, validation, handling = external.agent.conservative_extraction(self.result, self.payload)
        self.assertEqual(validation["status"], "PASSED")
        self.assertEqual(accepted["required_metric_coverage"][0]["status"], "AMBIGUOUS")
        self.assertEqual(accepted["claims"], original["claims"])
        self.assertEqual(self.result, original)
        self.assertEqual(len(handling["coverage_changes"]), 1)

    def test_invalid_claim_quarantined_without_value_repair(self):
        self.result["claims"][0]["value"] = 5
        accepted, validation, handling = external.agent.conservative_extraction(self.result, self.payload)
        self.assertEqual(validation["status"], "PASSED")
        self.assertEqual(accepted["claims"], [])
        self.assertEqual(handling["rejected_claims"][0]["value"], 5)
        self.assertFalse(handling["claim_values_modified"])

    def test_structural_scope_error_not_repaired(self):
        self.result["analyzed_pages"] = []
        _, validation, _ = external.agent.conservative_extraction(self.result, self.payload)
        self.assertEqual(validation["status"], "FAILED")

    def test_valid_found_never_created_from_not_found(self):
        accepted, validation, handling = external.agent.conservative_extraction(self.result, self.payload)
        self.assertEqual(validation["status"], "PASSED")
        self.assertEqual(accepted["required_metric_coverage"][0]["status"], "NOT_FOUND")
        self.assertEqual(handling["coverage_changes"], [])

    def test_design_capacity_requires_power_unit(self):
        self.result["claims"][0].update(metric_type="design_it_capacity", measurement_basis="DESIGN_CAPACITY")
        _, validation = external.agent.check_result(self.result, self.payload)
        self.assertEqual(validation["status"], "FAILED")

    def test_bare_it_load_not_operational_capacity(self):
        text = "IT Load 10MW"
        self.payload["ocr_pages"][0]["text"] = text
        self.result["claims"][0].update(metric_type="operational_it_capacity", measurement_basis="OPERATIONAL_CAPACITY",
            metric_name="IT Load", raw_value="10MW", evidence_quote=text, value=10, unit="MW")
        _, validation = external.agent.check_result(self.result, self.payload)
        self.assertEqual(validation["status"], "FAILED")

    def test_completed_service_cannot_be_typed_as_plan(self):
        text = "service completed"
        self.payload["ocr_pages"][0]["text"] = text
        self.result["claims"][0].update(metric_type="planned_service_start", metric_name="service", raw_value="completed",
            evidence_quote=text, statement_status="EXECUTION_CLAIM")
        _, validation = external.agent.check_result(self.result, self.payload)
        self.assertEqual(validation["status"], "FAILED")

    def test_occupancy_and_contract_ratio_not_interchangeable(self):
        self.result["claims"][0].update(metric_type="occupancy_rate", measurement_basis="CONTRACT_RATIO")
        _, validation = external.agent.check_result(self.result, self.payload)
        self.assertEqual(validation["status"], "FAILED")

    def test_original_analysis_from_another_project_not_attached(self):
        original = self.root / "other_original"
        write_json(original / "analysis_result.json", {"review_goal": "Another review", "target_projects": ["Another Project"],
            "as_of_date": "2026-11-11", "claims": []})
        with patch.object(external.agent, "run_analysis", side_effect=self.mock_agent), patch.object(external, "latest_run", return_value=original):
            _, output = external.execute(self.gate, self.root / "outputs")
        self.assertEqual(read_json(output / "original_analysis_reference.json")["status"], "CONTEXT_MISMATCH_NOT_ATTACHED")
        self.assertFalse((output / "original_analysis_result.json").exists())


if __name__ == "__main__":
    unittest.main()
