"""API 없이 상태 보존·검색 선택·반복 제한·실행 경계를 검사한다."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dc_verify.step.step6.code import controller_replan as module


def valid_decision(payload):
    plan = payload["initial_plan"]
    issues = payload["issue_inventory"]
    ids = [i["issue_id"] for i in issues]
    goal_ids = [i["issue_id"] for i in issues if i["kind"] == "GOAL_EVIDENCE"]
    return {
        "goal_status": "INCOMPLETE", "review_goal": plan["review_goal"],
        "target_projects": plan["target_projects"], "as_of_date": plan["as_of_date"],
        "observations": ["원문 일치와 목적 증빙 충분성을 구분한다."],
        **{k: copy.deepcopy(payload["observed_state"][k]) for k in
           ("source_verified_claim_ids", "source_recheck_claim_ids", "goal_supported_claim_ids")},
        "prioritized_issues": [{"issue_id": i["issue_id"], "priority": n,
            "issue": i["description"], "reason": i["reason"]} for n, i in enumerate(issues, 1)],
        "next_action": {"type": "SEARCH_OFFICIAL", "target": plan["target_projects"][0],
            "reason": "기존 계획 문서로 현재 상태가 확인되지 않는다.", "issue_ids": goal_ids,
            "required_evidence": ["기준일과 시설 범위가 명시된 공식 운영 자료"],
            "query_suggestions": [plan["target_projects"][0]], "local_scope": [],
            "search_constraints": {"official_sources_only": True,
                "published_on_or_before": plan["as_of_date"], "same_project_required": True,
                "metric_definition_required": True, "metric_as_of_required": True,
                "scope_and_status_required": True, "reverify_before_use": True},
            "retry_justification": None, "new_information_event_ids": []},
        "updated_plan": [{"task_id": "t1", "priority": 1, "action": "SEARCH_OFFICIAL",
            "issue_ids": goal_ids, "description": "선택한 자료 검색", "execution_status": "PLANNED"}],
        "remaining_issues": ids, "comparison_status": "NOT_ASSESSED",
        "controller_status": "ACTION_SELECTED"}


class ReplanTests(unittest.TestCase):
    def setUp(self):
        if not (stage_data(6) / 'initial_plan.json').is_file(): self.skipTest('Private experiment fixture not distributed')
        self.payload = module.prepare_input(stage_data(6))
        self.decision = valid_decision(self.payload)

    def check_status(self, expected):
        _, validation = module.validate_decision(self.decision, self.payload)
        self.assertEqual(validation["status"], expected, validation)

    def add_search(self, executed=True):
        action = copy.deepcopy(self.decision["next_action"])
        action.update(executed=executed, status="FAILED", finished_at="2026-11-10T00:00:00+00:00")
        self.payload["execution_history"]["action_history"].append(action)

    def test_actual_source_and_goal_states_are_distinct(self):
        state = self.payload["observed_state"]
        self.assertEqual(len(state["source_verified_claim_ids"]), 8)
        self.assertEqual(state["source_recheck_claim_ids"], ["c7", "c10"])
        self.assertEqual(state["goal_supported_claim_ids"], [])
        self.assertEqual(len(self.payload["issue_inventory"]), 6)
        self.check_status("PASSED")

    def test_source_verified_not_erased_by_empty_support(self):
        self.decision["source_verified_claim_ids"] = []
        self.check_status("FAILED")

    def test_unverified_claim_cannot_be_promoted(self):
        self.decision["source_verified_claim_ids"].append("c7")
        self.decision["source_recheck_claim_ids"].remove("c7")
        self.check_status("FAILED")

    def test_incomplete_not_complete(self):
        self.decision["goal_status"] = "COMPLETE"
        self.check_status("FAILED")

    def test_cutoff_preserved(self):
        self.decision["next_action"]["search_constraints"]["published_on_or_before"] = "2027-01-01"
        self.check_status("FAILED")

    def test_context_not_rewritten(self):
        self.decision["as_of_date"] = "2024-09-01"
        self.check_status("FAILED")

    def test_no_hardcoded_project_selection(self):
        self.payload["initial_plan"]["target_projects"] = ["새로운 검토 프로젝트"]
        self.decision = valid_decision(self.payload)
        self.check_status("PASSED")

    def test_local_existing_unprocessed_page(self):
        doc = self.payload["execution_history"]["available_documents"][0]
        self.decision["next_action"].update(type="SEARCH_LOCAL",
            local_scope=[{"document_id": doc["document_id"], "pages": doc["unprocessed_pages"][:1]}])
        self.decision["updated_plan"][0]["action"] = "SEARCH_LOCAL"
        self.check_status("PASSED")

    def test_local_unknown_page_rejected(self):
        doc = self.payload["execution_history"]["available_documents"][0]
        self.decision["next_action"].update(type="SEARCH_LOCAL",
            local_scope=[{"document_id": doc["document_id"], "pages": [999]}])
        self.decision["updated_plan"][0]["action"] = "SEARCH_LOCAL"
        self.check_status("FAILED")

    def test_search_budget_counts_executed_only(self):
        self.add_search(executed=False)
        self.add_search(executed=False)
        self.check_status("PASSED")

    def test_actual_search_budget_exhausted(self):
        self.add_search()
        self.add_search()
        self.check_status("FAILED")

    def test_repeated_search_needs_new_information(self):
        self.add_search()
        self.check_status("FAILED")

    def test_model_response_is_not_new_evidence(self):
        self.add_search()
        history = self.payload["execution_history"]
        history["events"].append({"event_id": "model", "operation": "MODEL_API",
            "status": "SUCCESS", "finished_at": "2026-11-11T00:00:00+00:00"})
        self.decision["next_action"].update(retry_justification="새 모델 응답", new_information_event_ids=["model"])
        self.check_status("FAILED")

    def test_repeated_search_with_new_source_allowed(self):
        self.add_search()
        self.payload["execution_history"]["events"].append({"event_id": "source", "operation": "SOURCE_ADDED",
            "status": "SUCCESS", "finished_at": "2026-11-11T00:00:00+00:00"})
        self.decision["next_action"].update(retry_justification="새 공식 문서 입수", new_information_event_ids=["source"])
        self.check_status("PASSED")

    def test_unresolved_not_removed_by_selection(self):
        self.decision["remaining_issues"].pop()
        self.check_status("FAILED")

    def test_no_final_comparison(self):
        self.decision["next_action"]["type"] = "FINALIZE"
        self.decision["updated_plan"][0]["action"] = "FINALIZE"
        self.check_status("FAILED")

    def test_reverification_not_mixed_into_unprocessed_search(self):
        self.decision["next_action"]["issue_ids"].append("source_review_c7")
        self.check_status("FAILED")

    def test_first_task_same_scope_as_next_action(self):
        self.decision["updated_plan"][0]["issue_ids"] = []
        self.check_status("FAILED")

    def test_failed_api_does_not_create_accepted_decision(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(module, "request_structured_output", side_effect=RuntimeError("test failure")):
                log, output = module.run_replan(output_dir=Path(temp))
            self.assertEqual(log["status"], "FAILED")
            self.assertFalse((output / "controller_decision.json").exists())

    def test_selection_does_not_search_or_modify_inputs(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (stage_data(6)).glob("*.json")}
        response = {"api_status": "completed", "text": json.dumps(self.decision),
                    "parsed": self.decision, "model": "mock", "usage": {}}
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(module, "request_structured_output", return_value=response) as request:
                log, output = module.run_replan(output_dir=Path(temp))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(log["status"], "SUCCESS")
            self.assertFalse(log["search_executed"])
            action_input = module.read_json(output / "next_action_input.json")
            self.assertFalse(action_input["executed"])
            self.assertEqual(action_input["execution_status"], "PLANNED")
            self.assertEqual(action_input["execution_history"]["action_history"], [])
            self.assertEqual(action_input["initial_analysis_handoff"], self.payload["initial_plan"]["analysis_handoff"])
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items()))


if __name__ == "__main__":
    unittest.main()
