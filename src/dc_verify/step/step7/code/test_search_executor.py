"""모의 응답·전송으로 실행 경계를 검사한다. 실제 검색 성공을 모사해 보고하지 않는다."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dc_verify.step.step7.code import search_executor as ex
from dc_verify.step.step7.code import search_fetcher as fetcher
from dc_verify.step.step7.code import search_api_adapter as api


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        tasks = [{"task_id": "T" + str(n), "issue_id": "issue" + str(n), "target": "Example Project",
            "tool": "SESSION", "source_type": "OFFICIAL_IR", "query": "Example Project metric " + str(n), "priority": n}
            for n in [1, 2]]
        self.incoming = {"executed": False, "search_plan": {"review_goal": "Review metric", "target_projects": ["Example Project"],
            "as_of_date": "2026-11-11", "search_plan": tasks, "search_constraints": {"official_sources_only": True}},
            "search_budget": {"max_attempts_per_issue": 2}, "search_history": {"action_history": [], "events": []},
            "initial_analysis_handoff": {"extraction_instructions": ["No substitution"]}, "acceptance_policy": "Original definitions prevail"}
        self.runtime = {"execution_context": "CODEX_SESSION", "official_hosts": ["issuer.example"],
            "tools": [{"tool_id": "SESSION", "adapter": "session.search", "available": True,
                "execution_context": "CODEX_SESSION", "source_types": ["OFFICIAL_IR"], "kind": "WEB_SEARCH"}],
            "fallback_terms": {"OFFICIAL_IR": "official IR definitions"}, "preferred_search_hosts": ["issuer.example"]}
        ex.write_json(self.directory / "input.json", self.incoming)
        ex.write_json(self.directory / "runtime.json", self.runtime)
        self.run = ex.init_run(self.directory / "input.json", self.directory / "runtime.json", self.directory / "runs", allow_mock=True)

    def receipt(self, task="T1", response=None, **overrides):
        job = next(t for t in self.incoming["search_plan"]["search_plan"] if t["task_id"] == task)
        value = {"task_id": task, "query": job["query"], "tool": "session.search", "executed": True, "provenance": "MOCK",
            "started_at": "2026-11-11T01:00:00Z", "finished_at": "2026-11-11T01:00:01Z",
            "response": response if response is not None else "Example Project IR (https://issuer.example/ir.pdf)\ncitetestref\nExample Project metric definitions"}
        value.update(overrides)
        path = self.directory / ("receipt_" + hashlib.sha256(json.dumps(value).encode()).hexdigest()[:12] + ".json")
        ex.write_json(path, value)
        return path

    def test_plan_request_is_not_execution(self):
        request = ex.make_session_request(self.run, "T1")
        self.assertEqual(request["query"], self.incoming["search_plan"]["search_plan"][0]["query"])
        self.assertFalse(request["executed"])
        self.assertEqual(ex.read_json(self.run / "state.json")["history"]["action_history"], [])

    def test_receipt_records_actual_attempt_only_once(self):
        receipt = self.receipt()
        ex.import_receipts(self.run, [receipt])
        result = ex.import_receipts(self.run, [receipt])
        self.assertEqual(result["search_request_count"], 1)
        budget = ex.read_json(self.run / "search_budget_updated.json")
        self.assertEqual(budget["remaining_attempts"]["issue1"], 1)

    def test_candidate_not_verified_or_hashed(self):
        result = ex.import_receipts(self.run, [self.receipt()])
        doc = result["documents"][0]
        self.assertEqual(doc["project_match"], "POSSIBLE_MATCH")
        self.assertIsNone(doc["published_at"])
        self.assertFalse(doc["source_verified"])
        self.assertFalse(doc["evidence_verified"])
        self.assertNotIn("sha256", doc)
        self.assertEqual(ex.read_json(self.run / "source_verification_input.json")["documents"], [])

    def test_cross_issue_url_deduplication(self):
        result = ex.import_receipts(self.run, [self.receipt(), self.receipt("T2")])
        self.assertEqual(len(result["documents"]), 1)
        self.assertEqual(result["documents"][0]["related_issue_ids"], ["issue1", "issue2"])

    def test_no_results_is_not_tool_failure(self):
        result = ex.import_receipts(self.run, [self.receipt(response="No search results")])
        self.assertEqual(result["search_results"][0]["execution_status"], "NO_RESULTS")
        self.assertEqual(result["controller_observation"]["search_failures"], [])

    def test_tool_failure_consumes_actual_attempt(self):
        result = ex.import_receipts(self.run, [self.receipt(response="", error={"type": "RateLimitError"})])
        self.assertEqual(result["search_request_count"], 1)
        self.assertEqual(result["search_results"][0]["execution_status"], "TOOL_FAILED")
        self.assertEqual(len(result["controller_observation"]["search_failures"]), 1)

    def test_parse_failure_still_consumes_actual_attempt(self):
        result = ex.import_receipts(self.run, [self.receipt(response="unexpected response layout")])
        self.assertEqual(result["search_results"][0]["execution_status"], "PARSE_FAILED")
        self.assertEqual(ex.read_json(self.run / "search_budget_updated.json")["remaining_attempts"]["issue1"], 1)

    def test_mock_receipt_rejected_by_live_run(self):
        live = ex.init_run(self.directory / "input.json", self.directory / "runtime.json", self.directory / "live")
        with self.assertRaises(ValueError):
            ex.import_receipts(live, [self.receipt()])
        self.assertEqual(ex.read_json(live / "state.json")["attempts"], [])

    def test_budget_exhaustion_prevents_more_receipts(self):
        ex.import_receipts(self.run, [self.receipt(), self.receipt(query="alternate query", adjustment_reason="source fallback")])
        with self.assertRaises(ValueError):
            ex.import_receipts(self.run, [self.receipt(query="third query", adjustment_reason="retry")])
        self.assertEqual(ex.read_json(self.run / "search_budget_updated.json")["remaining_attempts"]["issue1"], 0)

    def test_query_change_needs_recorded_reason(self):
        with self.assertRaises(ValueError):
            ex.import_receipts(self.run, [self.receipt(query="changed")])

    def test_partial_success_is_preserved(self):
        result = ex.import_receipts(self.run, [self.receipt(), self.receipt("T2", response="", error={"type": "Timeout"})])
        self.assertEqual(result["execution_status"], "PARTIALLY_COMPLETED")
        self.assertEqual(result["search_results"][0]["execution_status"], "SUCCESS")

    def test_unknown_source_is_held_for_review(self):
        result = ex.import_receipts(self.run, [self.receipt(response="Example Project (https://other.example/doc)\nExample Project")])
        self.assertEqual(result["documents"][0]["source_status"], "UNVERIFIED_SOURCE")
        self.assertEqual(result["search_results"][0]["execution_status"], "NO_SUITABLE_CANDIDATES")

    def test_explicit_future_publication_excluded(self):
        task = self.incoming["search_plan"]["search_plan"][0]
        doc = ex.classify_document({"title": "Example Project", "url": "https://issuer.example/doc", "published_at": "2027-01-01",
            "publication_date_provenance": "ORIGINAL_HTML_METADATA"}, task, self.runtime, "2026-11-11")
        self.assertEqual(doc["document_status"], "EXCLUDED_AFTER_CUTOFF")

    def test_search_date_not_promoted_to_publication(self):
        task = self.incoming["search_plan"]["search_plan"][0]
        doc = ex.classify_document({"title": "Example Project", "url": "https://issuer.example/doc", "published_at": "2020-01-01",
            "publication_date_provenance": "SEARCH_SNIPPET"}, task, self.runtime, "2026-11-11")
        self.assertIsNone(doc["published_at"])

    def test_python_context_cannot_use_session_adapter(self):
        task = self.incoming["search_plan"]["search_plan"][0]
        self.assertIsNone(ex.select_tool(task, self.runtime, "PYTHON"))

    def test_official_api_preferred_over_web_search(self):
        runtime = copy.deepcopy(self.runtime)
        runtime["tools"].append({**runtime["tools"][0], "tool_id": "OFFICIAL", "kind": "DIRECT_OFFICIAL_API"})
        self.assertEqual(ex.select_tool(self.incoming["search_plan"]["search_plan"][0], runtime, "CODEX_SESSION")["tool_id"], "OFFICIAL")

    def test_fallback_uses_runtime_terms_and_host(self):
        request = ex.make_session_request(self.run, "T1", fallback=True)
        self.assertIn("official IR definitions", request["query"])
        self.assertIn("site:issuer.example", request["query"])
        self.assertIsNotNone(request["adjustment_reason"])

    def test_publication_parser_ignores_modification_and_business_dates(self):
        html = '<meta property="article:modified_time" content="2025-01-01"><p>Opened 2024-01-01</p>'
        self.assertIsNone(fetcher.publication_date(html))
        self.assertEqual(fetcher.publication_date('<meta property="article:published_time" content="2024-08-26T00:00:00Z">'), "2024-08-26")

    def test_publication_conflict_is_unknown(self):
        self.assertIsNone(fetcher.publication_date('<meta property="article:published_time" content="2024-01-01"><meta name="pubdate" content="2025-01-01">'))

    def test_hash_is_only_added_for_saved_bytes(self):
        ex.import_receipts(self.run, [self.receipt()])
        body = b"%PDF-1.4\nMOCK"
        def transport(url, **kwargs):
            return {"status": 404 if url.endswith("robots.txt") else 200, "url": url,
                "content_type": "application/pdf", "body": b"" if url.endswith("robots.txt") else body}
        result = fetcher.collect(self.run, ["https://issuer.example/ir.pdf"], transport=transport)
        doc = result["documents"][0]
        self.assertEqual(Path(doc["downloaded_file"]).read_bytes(), body)
        self.assertEqual(doc["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(result["search_request_count"], 1)
        self.assertFalse(doc["evidence_verified"])

    def test_access_denied_has_no_document_hash(self):
        ex.import_receipts(self.run, [self.receipt()])
        def transport(url, **kwargs):
            return {"status": 404 if url.endswith("robots.txt") else 403, "url": url, "content_type": "text/html", "body": b""}
        result = fetcher.collect(self.run, ["https://issuer.example/ir.pdf"], transport=transport)
        self.assertEqual(result["documents"][0]["access_status"], "ACCESS_DENIED")
        self.assertNotIn("sha256", result["documents"][0])

    def test_missing_api_key_does_not_invoke_api(self):
        with patch.object(api, "load_tavily_key", return_value=""), patch.object(api, "tavily_search") as transport:
            with self.assertRaises(ValueError):
                api.execute_api(self.run, transport=transport)
            transport.assert_not_called()
        self.assertFalse(ex.read_json(self.run / "python_api_availability.json")["api_called"])

    def test_original_input_and_goal_are_preserved(self):
        digest = hashlib.sha256((self.directory / "input.json").read_bytes()).hexdigest()
        result = ex.import_receipts(self.run, [self.receipt()])
        self.assertEqual(hashlib.sha256((self.directory / "input.json").read_bytes()).hexdigest(), digest)
        self.assertEqual(result["execution_policy"]["comparison_scope"], "COMMON_METRIC_DEFINITIONS_ONLY")
        self.assertFalse(result["execution_policy"]["claim_verification_performed"])

    def test_python_api_success_with_mock_transport(self):
        runtime = copy.deepcopy(self.runtime)
        runtime.update(execution_context="PYTHON", tools=[{"tool_id": "TAVILY", "adapter": "TAVILY_HTTP",
            "available": True, "execution_context": "PYTHON", "source_types": ["OFFICIAL_IR"], "kind": "WEB_SEARCH"}])
        ex.write_json(self.run / "runtime_tools.json", runtime)
        calls = []
        def transport(query, key, hosts, cutoff):
            calls.append(query)
            return {"results": [{"title": "Example Project", "url": "https://issuer.example/ir.pdf", "content": "Example Project"}]}
        result = api.execute_api(self.run, key="MOCK_KEY", transport=transport)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["execution_status"], "COMPLETED")
        self.assertEqual(result["run_mode"], "MOCK")
        self.assertNotIn("MOCK_KEY", (self.run / "state.json").read_text())

    def test_python_api_errors_do_not_expose_key(self):
        runtime = copy.deepcopy(self.runtime)
        runtime.update(execution_context="PYTHON", tools=[{"tool_id": "TAVILY", "adapter": "TAVILY_HTTP",
            "available": True, "execution_context": "PYTHON", "source_types": ["OFFICIAL_IR"], "kind": "WEB_SEARCH"}])
        ex.write_json(self.run / "runtime_tools.json", runtime)
        def transport(*args):
            raise RuntimeError("secret=MOCK_SECRET")
        result = api.execute_api(self.run, key="MOCK_SECRET", transport=transport)
        self.assertEqual(result["search_request_count"], 2)
        self.assertEqual(len(result["controller_observation"]["search_failures"]), 2)
        for path in self.run.rglob("*.json"):
            self.assertNotIn("MOCK_SECRET", path.read_text())

    def test_robots_disallow_is_not_bypassed(self):
        ex.import_receipts(self.run, [self.receipt()])
        calls = []
        def transport(url, **kwargs):
            calls.append(url)
            return {"status": 200, "url": url, "content_type": "text/plain", "body": b"User-agent: *\nDisallow: /"}
        result = fetcher.collect(self.run, ["https://issuer.example/ir.pdf"], transport=transport)
        self.assertEqual(calls, ["https://issuer.example/robots.txt"])
        self.assertEqual(result["documents"][0]["access_status"], "ROBOTS_DISALLOWED")
        self.assertNotIn("sha256", result["documents"][0])

    def test_duplicate_bytes_are_not_saved_twice(self):
        ex.import_receipts(self.run, [self.receipt(), self.receipt("T2", response="Example Project (https://issuer.example/other.pdf)\nExample Project")])
        def transport(url, **kwargs):
            return {"status": 404 if url.endswith("robots.txt") else 200, "url": url,
                "content_type": "application/pdf", "body": b"" if url.endswith("robots.txt") else b"%PDF-1.4\nSAME"}
        result = fetcher.collect(self.run, ["https://issuer.example/ir.pdf", "https://issuer.example/other.pdf"], transport=transport)
        self.assertEqual(len(list((self.run / "original_documents").glob("*.pdf"))), 1)
        self.assertEqual(sum(d["document_status"] == "DUPLICATE_CONTENT" for d in result["documents"]), 1)

    def test_url_normalization_retains_document_identity_params(self):
        self.assertEqual(ex.canonical_url("https://issuer.example/ir?id=42&utm_source=web#page=2"), "https://issuer.example/ir?id=42")

    def test_restarting_same_live_plan_cannot_reset_budget(self):
        # 전용 임시 디렉터리에 과거 LIVE 상태가 있는 상황만 구성한다. 네트워크 호출 없음.
        state = ex.read_json(self.run / "state.json")
        state.update(run_mode="LIVE", attempts=[{"fixture_only": True}])
        ex.write_json(self.run / "state.json", state)
        with self.assertRaises(ValueError):
            ex.init_run(self.directory / "input.json", self.directory / "runtime.json", self.directory / "runs")

    def test_input_target_mismatch_stops_before_execution(self):
        self.incoming["search_plan"]["search_plan"][0]["target"] = "Other Project"
        ex.write_json(self.directory / "changed.json", self.incoming)
        with self.assertRaises(ValueError):
            ex.init_run(self.directory / "changed.json", self.directory / "runtime.json", self.directory / "invalid", allow_mock=True)

    def test_receipt_bytes_match_audited_hash(self):
        path = self.receipt()
        # 입력 직렬화 형식과 무관하게 받은 바이트를 그대로 보존해야 한다.
        value = ex.read_json(path)
        path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        result = ex.import_receipts(self.run, [path])
        attempt = result["search_results"][0]
        self.assertEqual(Path(attempt["raw_response_file"]).read_bytes(), path.read_bytes())
        self.assertEqual(hashlib.sha256(Path(attempt["raw_response_file"]).read_bytes()).hexdigest(), attempt["receipt_sha256"])


if __name__ == "__main__":
    unittest.main()
