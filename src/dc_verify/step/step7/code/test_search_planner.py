"""검색 호출 없이 도구 선택·예산·Controller 범위·실행 경계를 검사한다."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError
from dc_verify.step.step7.code import search_planner as module


def valid_plan(payload):
    decision = payload["controller_decision"]
    return {"review_goal": decision["review_goal"], "target_projects": decision["target_projects"],
        "as_of_date": decision["as_of_date"],
        "search_plan": [{"task_id": "t" + str(n), "issue_id": issue["issue_id"],
            "target": decision["next_action"]["target"], "required_evidence": issue["description"],
            "source_type": "OFFICIAL_IR", "tool": "CODEX_WEB_SEARCH",
            "query": decision["next_action"]["target"] + " " + issue["description"] + " 공식 IR",
            "priority": n, "success_condition": "공식 원문에서 대상·지표·시점이 일치하는지 확인",
            "tool_selection_reason": "공식 IR 후보를 검색할 수 있는 가용 도구"}
            for n, issue in enumerate(payload["selected_issues"], 1)],
        "deferred_issues": [], "search_constraints": {
            **copy.deepcopy(decision["next_action"]["search_constraints"]),
            "exclude_already_reviewed_documents": True, "missing_publication_date_policy": "HOLD_FOR_REVIEW",
            "require_original_document": True, "snippet_is_evidence": False,
            "max_attempts_per_issue": payload["search_budget"]["max_attempts_per_issue"],
            "planning_consumes_attempts": False},
        "next_action": "EXECUTE_SEARCH", "execution_mode": "PLAN_ONLY", "searches_executed": False}


class SearchPlannerTests(unittest.TestCase):
    def setUp(self):
        if not (stage_data(7) / 'controller_decision.json').is_file(): self.skipTest('Private experiment fixture not distributed')
        self.payload = module.prepare_input(stage_data(7))
        self.result = valid_plan(self.payload)

    def assert_status(self, expected):
        _, validation = module.validate_plan(self.result, self.payload)
        self.assertEqual(validation["status"], expected, validation)

    def test_current_inputs_preserve_four_selected_issues(self):
        self.assertEqual(len(self.payload["selected_issues"]), 4)
        self.assertEqual(set(self.payload["search_budget"]["remaining_attempts"].values()), {2})
        self.assert_status("PASSED")

    def test_unconnected_api_cannot_be_selected(self):
        self.result["search_plan"][0]["tool"] = "TAVILY"
        self.assert_status("FAILED")

    def test_unknown_tool_cannot_be_selected(self):
        self.result["search_plan"][0]["tool"] = "INVENTED_TOOL"
        self.assert_status("FAILED")

    def test_tool_source_capability_checked(self):
        self.result["search_plan"][0]["source_type"] = "INVENTED_SOURCE"
        self.assert_status("FAILED")

    def test_newly_configured_financial_tool_can_be_selected(self):
        # 연결된 다른 도구를 전달하는 상황을 모사한다. 실제 연결 선언이 아니다.
        tool = next(t for t in self.payload["available_tools"]["tools"] if t["tool_id"] == "OPEN_DART")
        tool.update(available=True, execution_context="PYTHON", standalone_available=True)
        self.result["search_plan"][0].update(tool="OPEN_DART", source_type="FINANCIAL_STATEMENT",
            required_evidence="감사 재무제표", query="예시 기업 감사 재무제표 공식 공시")
        self.assert_status("PASSED")

    def test_date_cannot_be_extended(self):
        self.result["search_constraints"]["published_on_or_before"] = "2027-01-01"
        self.assert_status("FAILED")

    def test_official_source_constraint_preserved(self):
        self.result["search_constraints"]["official_sources_only"] = False
        self.assert_status("FAILED")

    def test_target_cannot_change(self):
        self.result["search_plan"][0]["target"] = "다른 프로젝트"
        self.assert_status("FAILED")

    def test_no_selected_issue_omitted(self):
        self.result["search_plan"].pop()
        self.assert_status("FAILED")

    def test_source_rechecks_cannot_be_added_to_search(self):
        self.result["search_plan"][0]["issue_id"] = "source_review_c7"
        self.assert_status("FAILED")

    def test_query_duplicates_normalized(self):
        self.result["search_plan"][1]["query"] = "  " + self.result["search_plan"][0]["query"].upper() + "  "
        self.assert_status("FAILED")

    def test_previously_executed_query_not_repeated(self):
        self.payload["search_history"]["action_history"].append({"executed": True,
            "query": self.result["search_plan"][0]["query"]})
        self.assert_status("FAILED")

    def test_budget_cannot_be_exceeded(self):
        first = self.result["search_plan"][0]
        self.payload["search_budget"]["remaining_attempts"][first["issue_id"]] = 0
        self.assert_status("FAILED")

    def test_no_tools_returns_to_controller(self):
        for tool in self.payload["available_tools"]["tools"]:
            tool["available"] = False
        self.result["search_plan"] = []
        self.result["deferred_issues"] = [{"issue_id": i["issue_id"], "reason_code": "NO_AVAILABLE_TOOL",
            "reason": "연결된 검색 도구가 없다."} for i in self.payload["selected_issues"]]
        self.result["next_action"] = "RETURN_TO_CONTROLLER"
        self.assert_status("PASSED")

    def test_snippet_cannot_be_evidence(self):
        self.result["search_constraints"]["snippet_is_evidence"] = True
        with self.assertRaises(ValidationError):
            module.validate_plan(self.result, self.payload)

    def test_fabricated_results_rejected_by_schema(self):
        self.result["retrieved_documents"] = [{"claim": "운영 확인 완료"}]
        with self.assertRaises(ValidationError):
            module.validate_plan(self.result, self.payload)

    def test_actual_history_only_consumes_budget(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            for path in (stage_data(7)).glob("*.json"):
                (directory / path.name).write_bytes(path.read_bytes())
            history = module.read_json(directory / "search_history.json")
            issue = self.result["search_plan"][0]["issue_id"]
            history["action_history"] = [
                {"type": "SEARCH_OFFICIAL", "executed": False, "issue_ids": [issue]},
                {"type": "SEARCH_OFFICIAL", "executed": True, "issue_ids": [issue]}]
            module.write_json(directory / "search_history.json", history)
            handoff = module.read_json(directory / "controller_handoff.json")
            handoff["execution_history"] = history
            module.write_json(directory / "controller_handoff.json", handoff)
            prepared = module.prepare_input(directory)
            self.assertEqual(prepared["search_budget"]["remaining_attempts"][issue], 1)

    def test_plan_generation_never_executes_search(self):
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (stage_data(7)).glob("*.json")}
        response = {"api_status": "completed", "parsed": self.result,
            "text": json.dumps(self.result), "model": "mock", "usage": {}}
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(module, "request_structured_output", return_value=response) as request:
                log, output = module.run_planner(output_dir=Path(temp))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(log["status"], "SUCCESS")
            handoff = module.read_json(output / "search_executor_input.json")
            self.assertFalse(handoff["executed"])
            self.assertFalse(handoff["standalone_search_ready"])
            self.assertEqual(handoff["search_history"]["action_history"], [])
            self.assertEqual(handoff["initial_analysis_handoff"], self.payload["initial_analysis_handoff"])
            observation = module.read_json(output / "controller_observation.json")
            self.assertEqual(len(observation["source_verified_claim_ids"]), 8)
            self.assertEqual(observation["source_recheck_claim_ids"], ["c7", "c10"])
        self.assertTrue(all(hashlib.sha256(p.read_bytes()).hexdigest() == digest for p, digest in before.items()))

    def test_invalid_api_plan_preserved_but_not_accepted(self):
        self.result["search_plan"][0]["tool"] = "TAVILY"
        response = {"api_status": "completed", "parsed": self.result, "text": json.dumps(self.result)}
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(module, "request_structured_output", return_value=response):
                log, output = module.run_planner(output_dir=Path(temp))
            self.assertEqual(log["status"], "FAILED")
            self.assertTrue((output / "planner_response.json").exists())
            self.assertFalse((output / "search_plan.json").exists())

    def test_dry_run_never_calls_api(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(module, "request_structured_output") as request:
                log, _ = module.run_planner(output_dir=Path(temp), dry_run=True)
            request.assert_not_called()
            self.assertEqual(log["status"], "PREPARED")

    def test_api_schema_locks_selected_scope(self):
        schema = module.response_schema(self.payload)
        schema.model_validate(self.result)
        self.result["search_plan"][0]["issue_id"] = "source_review_c7"
        with self.assertRaises(ValidationError):
            schema.model_validate(self.result)

    def test_unverified_site_filter_blocked(self):
        self.result["search_plan"][0]["query"] += " site:invented-domain.example"
        self.assert_status("FAILED")

    def test_no_available_tool_skips_generation_api(self):
        for tool in self.payload["available_tools"]["tools"]:
            tool["available"] = False
        with patch.object(module, "request_structured_output") as request:
            response, result, validation = module.plan_search(self.payload)
        request.assert_not_called()
        self.assertIsNone(response)
        self.assertEqual(validation["status"], "PASSED")
        self.assertEqual(result["next_action"], "RETURN_TO_CONTROLLER")
        self.assertEqual(len(result["deferred_issues"]), 4)

    def test_budget_exhausted_issue_excluded_before_api(self):
        issue = self.result["search_plan"][0]["issue_id"]
        self.payload["search_budget"]["remaining_attempts"][issue] = 0
        scoped, deferred = module.prepare_api_input(self.payload)
        self.assertNotIn(issue, scoped["controller_decision"]["next_action"]["issue_ids"])
        self.assertEqual(deferred[0]["reason_code"], "BUDGET_EXHAUSTED")
        response_result = valid_plan(scoped)
        response = {"api_status": "completed", "parsed": response_result, "text": json.dumps(response_result)}
        with patch.object(module, "request_structured_output", return_value=response):
            _, result, validation = module.plan_search(self.payload)
        self.assertEqual(validation["status"], "PASSED")
        self.assertEqual(result["deferred_issues"], deferred)
        self.assertEqual(len(result["search_plan"]), 3)


if __name__ == "__main__":
    unittest.main()
