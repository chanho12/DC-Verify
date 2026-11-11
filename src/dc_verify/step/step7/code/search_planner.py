"""STEP 7-1: Controller가 호출할 수 있는 검색 계획 함수와 파일 실행 CLI.

검색 실행·원문 확보·증빙 판정은 수행하지 않는다.
"""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

HERE = Path(__file__).resolve().parent
STEP = HERE.parent
from dc_verify.codex import PLANNING_MODEL as DEFAULT_MODEL, request_structured_output, safe_error
from dc_verify.step.step7.code.prompt import SEARCH_SYSTEM_PROMPT, build_user_prompt


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchTask(StrictModel):
    task_id: str
    issue_id: str
    target: str
    required_evidence: str
    source_type: str
    tool: str
    query: str
    priority: int
    success_condition: str
    tool_selection_reason: str


class DeferredIssue(StrictModel):
    issue_id: str
    reason_code: Literal["NO_AVAILABLE_TOOL", "BUDGET_EXHAUSTED", "CONTROLLER_REPLAN_REQUIRED"]
    reason: str


class SearchConstraints(StrictModel):
    official_sources_only: bool
    published_on_or_before: Optional[str]
    same_project_required: bool
    metric_definition_required: bool
    metric_as_of_required: bool
    scope_and_status_required: bool
    reverify_before_use: bool
    exclude_already_reviewed_documents: Literal[True]
    missing_publication_date_policy: Literal["HOLD_FOR_REVIEW"]
    require_original_document: Literal[True]
    snippet_is_evidence: Literal[False]
    max_attempts_per_issue: int
    planning_consumes_attempts: Literal[False]


class SearchPlan(StrictModel):
    review_goal: str
    target_projects: list[str]
    as_of_date: Optional[str]
    search_plan: list[SearchTask]
    deferred_issues: list[DeferredIssue]
    search_constraints: SearchConstraints
    next_action: Literal["EXECUTE_SEARCH", "RETURN_TO_CONTROLLER"]
    execution_mode: Literal["PLAN_ONLY"]
    searches_executed: Literal[False]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def normalized_query(query):
    return " ".join(query.casefold().split())


def task_signature(task):
    key = [task["tool"], task["target"], normalized_query(task["query"])]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()


def response_schema(payload):
    """선택 범위를 회사명 하드코딩 없이 요청별 JSON enum으로 제한한다."""
    decision = payload["controller_decision"]
    issue_type = Literal[tuple(decision["next_action"]["issue_ids"])]
    tools = [t for t in payload["available_tools"]["tools"] if t["available"]]
    tool_ids = tuple(t["tool_id"] for t in tools)
    sources = tuple(sorted({s for t in tools for s in t["source_types"]}))
    scoped_task = create_model("ScopedSearchTask", __base__=SearchTask,
        issue_id=(issue_type, ...), target=(Literal[decision["next_action"]["target"]], ...),
        tool=(Literal[tool_ids] if tool_ids else str, ...),
        source_type=(Literal[sources] if sources else str, ...))
    scoped_deferred = create_model("ScopedDeferredIssue", __base__=DeferredIssue, issue_id=(issue_type, ...))
    return create_model("ScopedSearchPlan", __base__=SearchPlan,
        search_plan=(list[scoped_task], Field(...) if tool_ids and sources else Field(..., max_length=0)),
        deferred_issues=(list[scoped_deferred], Field(..., max_length=0)))


def prepare_api_input(payload):
    """실제 한도·연결 상태는 코드가 결정하고, 모델에는 검색할 수 있는 쟁점만 준다."""
    scoped = copy.deepcopy(payload)
    usable = any(t["available"] and t["source_types"] for t in payload["available_tools"]["tools"])
    deferred = []
    eligible = []
    for issue in payload["selected_issues"]:
        if payload["search_budget"]["remaining_attempts"][issue["issue_id"]] == 0:
            deferred.append({"issue_id": issue["issue_id"], "reason_code": "BUDGET_EXHAUSTED", "reason": "실제 실행 이력으로 계산한 쟁점별 잔여 검색 횟수가 0회임"})
        elif not usable:
            deferred.append({"issue_id": issue["issue_id"], "reason_code": "NO_AVAILABLE_TOOL", "reason": "지원 자료 유형과 실행 환경이 있는 가용 검색 도구가 없음"})
        else:
            eligible.append(issue["issue_id"])
    scoped["controller_decision"]["next_action"]["issue_ids"] = eligible
    scoped["controller_decision"]["prioritized_issues"] = [i for i in scoped["controller_decision"]["prioritized_issues"] if i["issue_id"] in eligible]
    scoped["controller_decision"]["remaining_issues"] = eligible
    scoped["selected_issues"] = [i for i in scoped["selected_issues"] if i["issue_id"] in eligible]
    scoped["search_budget"]["remaining_attempts"] = {i: payload["search_budget"]["remaining_attempts"][i] for i in eligible}
    return scoped, deferred


def prepare_input(data_dir):
    """파일 입력을 검증하고 실제 검색 이력으로만 잔여 예산을 계산한다."""
    data_dir = Path(data_dir)
    decision = read_json(data_dir / "controller_decision.json")
    handoff = read_json(data_dir / "controller_handoff.json")
    tools = read_json(data_dir / "available_tools.json")
    history = read_json(data_dir / "search_history.json")
    for key in ("review_goal", "target_projects", "as_of_date"):
        if decision[key] != handoff[key]:
            raise ValueError("Controller 입력 맥락 불일치: " + key)
    if decision["as_of_date"]:
        date.fromisoformat(decision["as_of_date"])
    if decision["controller_status"] != "ACTION_SELECTED" or decision["next_action"]["type"] != "SEARCH_OFFICIAL":
        raise ValueError("외부 검색을 선택한 Controller 계획이 필요합니다.")
    if handoff["action"] != decision["next_action"] or handoff["executed"] is not False:
        raise ValueError("Controller 행동 또는 미실행 상태 불일치")
    if handoff["execution_history"] != history:
        raise ValueError("Controller 전달 이력과 검색 이력이 다릅니다.")
    identifiers = [t["tool_id"] for t in tools["tools"]]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("도구 ID 중복")
    for tool in tools["tools"]:
        if type(tool["available"]) is not bool or type(tool["standalone_available"]) is not bool:
            raise ValueError("도구 가용 상태는 boolean이어야 합니다.")
        if tool["available"] and tool["execution_context"] not in {"CODEX_SESSION", "PYTHON"}:
            raise ValueError("가용 도구의 실행 환경이 없습니다.")
    selected = decision["next_action"]["issue_ids"]
    inventory = {i["issue_id"]: i for i in handoff["issue_inventory"]}
    if not selected or len(selected) != len(set(selected)) or not set(selected).issubset(inventory):
        raise ValueError("Controller의 선택 쟁점이 유효하지 않습니다.")
    limit = history["search_policy"]["max_attempts_per_issue"]
    if type(limit) is not int or not 0 <= limit <= 2:
        raise ValueError("실제 검색 한도는 쟁점별 0~2회입니다.")
    executed = [a for a in history["action_history"] if a.get("executed") is True
                and a["type"] in {"SEARCH_OFFICIAL", "SEARCH_LOCAL"}]
    remaining = {i: max(0, limit - sum(i in a["issue_ids"] for a in executed)) for i in selected}
    return {"controller_decision": decision, "available_tools": tools, "search_history": history,
            "selected_issues": [inventory[i] for i in selected],
            "search_budget": {"max_attempts_per_issue": limit, "remaining_attempts": remaining,
                              "planning_consumes_attempts": False},
            "initial_analysis_handoff": handoff["initial_analysis_handoff"]}


def validate_plan(value, payload):
    result = SearchPlan.model_validate(value).model_dump(mode="json")
    decision = payload["controller_decision"]
    issues = set(decision["next_action"]["issue_ids"])
    tools = {t["tool_id"]: t for t in payload["available_tools"]["tools"]}
    remaining = payload["search_budget"]["remaining_attempts"]
    errors = []
    for key in ("review_goal", "target_projects", "as_of_date"):
        if result[key] != decision[key]:
            errors.append("검토 맥락 변경: " + key)
    constraints = result["search_constraints"]
    for key, value in decision["next_action"]["search_constraints"].items():
        if constraints[key] != value:
            errors.append("Controller 검색 조건 변경: " + key)
    if constraints["max_attempts_per_issue"] != payload["search_budget"]["max_attempts_per_issue"]:
        errors.append("탐색 한도 변경")
    tasks, deferred = result["search_plan"], result["deferred_issues"]
    counts = Counter(t["issue_id"] for t in tasks)
    deferred_ids = [i["issue_id"] for i in deferred]
    if set(counts) | set(deferred_ids) != issues or set(counts) & set(deferred_ids):
        errors.append("선택 쟁점 누락·추가 또는 계획/보류 중복")
    if len(deferred_ids) != len(set(deferred_ids)):
        errors.append("보류 쟁점 중복")
    if len({t["task_id"] for t in tasks}) != len(tasks):
        errors.append("작업 ID 중복")
    if len({t["priority"] for t in tasks}) != len(tasks) or any(t["priority"] < 1 for t in tasks):
        errors.append("검색 우선순위 오류")
    signatures = []
    previous = set()
    for action in payload["search_history"]["action_history"]:
        if action.get("executed") is True:
            queries = ([action["query"]] if action.get("query") else []) + action.get("queries", [])
            previous.update(normalized_query(q) for q in queries)
    for task in tasks:
        tool = tools.get(task["tool"])
        if not tool or tool["available"] is not True:
            errors.append("미연결 도구 선택: " + task["tool"])
        elif task["source_type"] not in tool["source_types"]:
            errors.append("도구가 지원하지 않는 자료 유형")
        if task["target"] != decision["next_action"]["target"]:
            errors.append("검색 대상 변경")
        for key in ("task_id", "required_evidence", "query", "success_condition", "tool_selection_reason"):
            if not task[key].strip():
                errors.append("검색 항목 내용 누락: " + key)
        if re.search(r"https?://", task["query"], re.I):
            errors.append("검증되지 않은 URL을 검색어에 삽입")
        domains = re.findall(r"site:([^\s]+)", task["query"], re.I)
        if any(d.lower() not in payload["available_tools"].get("verified_domains", []) for d in domains):
            errors.append("확인되지 않은 공식 도메인을 검색어에 삽입")
        if normalized_query(task["query"]) in previous:
            errors.append("이미 실행한 검색어 반복; Controller 재판단 필요")
        signatures.append(task_signature(task))
    if len(set(signatures)) != len(signatures):
        errors.append("동일 대상·도구·검색어 중복")
    for issue, count in counts.items():
        if count > remaining.get(issue, 0):
            errors.append("쟁점별 실제 검색 잔여 한도 초과: " + issue)
    for item in deferred:
        if not item["reason"].strip():
            errors.append("보류 사유 누락")
        if item["reason_code"] == "BUDGET_EXHAUSTED" and remaining.get(item["issue_id"], 0) != 0:
            errors.append("실제 탐색 한도가 남아 있는데 소진으로 표시")
        if item["reason_code"] == "NO_AVAILABLE_TOOL" and any(t["available"] and t["source_types"] for t in tools.values()):
            # 도구는 있어도 특정 자료형을 처리할 수 없는 경우는 재계획 사유로 기록한다.
            errors.append("가용 도구가 있는데 전체 도구 미연결로 표시")
    expected = "EXECUTE_SEARCH" if tasks else "RETURN_TO_CONTROLLER"
    if result["next_action"] != expected:
        errors.append("계획 유무와 후속 행동 불일치")
    return result, {"status": "PASSED" if not errors else "FAILED", "errors": errors,
                    "scope": "스키마·도구·조건·범위·예산·중복 검사. 검색 성과·증빙 적합성은 후속 단계에서 확인."}


def plan_search(payload, client=None):
    """Controller에서 호출하는 계획 함수. 파일·검색 이력을 수정하지 않는다."""
    scoped, deferred = prepare_api_input(payload)
    if not scoped["selected_issues"]:
        decision = payload["controller_decision"]
        blocked = {k: decision[k] for k in ("review_goal", "target_projects", "as_of_date")}
        blocked.update(search_plan=[], deferred_issues=deferred, next_action="RETURN_TO_CONTROLLER",
            execution_mode="PLAN_ONLY", searches_executed=False, search_constraints={
                **decision["next_action"]["search_constraints"], "exclude_already_reviewed_documents": True,
                "missing_publication_date_policy": "HOLD_FOR_REVIEW", "require_original_document": True,
                "snippet_is_evidence": False, "max_attempts_per_issue": payload["search_budget"]["max_attempts_per_issue"],
                "planning_consumes_attempts": False})
        result, validation = validate_plan(blocked, payload)
        return None, result, validation
    response = request_structured_output(SEARCH_SYSTEM_PROMPT, build_user_prompt(scoped),
                                         response_schema(scoped), client=client)
    if response["api_status"] != "completed" or response["parsed"] is None:
        return response, None, {"status": "FAILED", "errors": ["완료된 계획 응답이 없습니다."]}
    try:
        combined = copy.deepcopy(response["parsed"])
        # 모델의 보류 판단을 임의로 삭제하지 않고 잘못된 응답은 그대로 실패시킨다.
        combined["deferred_issues"] += deferred
        result, validation = validate_plan(combined, payload)
    except ValidationError:
        return response, None, {"status": "FAILED", "errors": ["검색 계획 JSON 스키마 불일치"]}
    return response, result, validation


def build_executor_input(result, payload):
    selected_tools = {t["tool"] for t in result["search_plan"]}
    registry = [t for t in payload["available_tools"]["tools"] if t["tool_id"] in selected_tools]
    return {"execution_status": "PLANNED", "executed": False, "search_plan": result,
            "selected_tools": registry, "standalone_search_ready": bool(registry) and all(t["standalone_available"] for t in registry),
            "search_history": payload["search_history"], "search_budget": payload["search_budget"],
            "initial_analysis_handoff": payload["initial_analysis_handoff"],
            "acceptance_policy": "검색어와 required_evidence의 예시는 필수 지표의 정의를 변경하지 않음. initial_analysis_handoff의 추출 지시가 우선하며 대체 지표로 필수 지표를 계산·환산하지 않음. 검색 결과는 원문·증빙 검증 전까지 후보임.",
            "task_signatures": {t["task_id"]: task_signature(t) for t in result["search_plan"]},
            "instruction": "STEP 7-2에서 도구 가용성을 다시 확인하고 실제 실행 후에만 이력 갱신. 검색 결과는 STEP 7-3 원문 확인 전까지 후보이며 증빙 확정 아님."}


def run_planner(data_dir=stage_data(7), output_dir=stage_data(7) / 'output', dry_run=False, client=None):
    payload = prepare_input(data_dir)
    output = Path(output_dir) / ("search_plan_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "planner_input.json", payload)
    api_input, local_deferred = prepare_api_input(payload)
    write_json(output / "api_input.json", api_input)
    write_json(output / "local_deferred_issues.json", local_deferred)
    log = {"status": "PREPARED", "model_requested": DEFAULT_MODEL, "api_attempted": False,
           "started_at": datetime.now(timezone.utc).isoformat(), "searches_executed": False,
           "input_hashes": {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in Path(data_dir).glob("*.json")},
           "prompts": {"system": SEARCH_SYSTEM_PROMPT, "user": build_user_prompt(api_input)}}
    write_json(output / "run.json", log)
    if not dry_run:
        try:
            log.update(status="RUNNING", api_attempted=bool(api_input["selected_issues"]))
            write_json(output / "run.json", log)
            print("Search Planner: " + DEFAULT_MODEL + "; 실제 검색 없음; API 대상 쟁점 " + str(len(api_input["selected_issues"])), flush=True)
            response, result, validation = plan_search(payload, client)
            if response is not None:
                log.update({k: v for k, v in response.items() if k not in {"parsed", "text"}})
                (output / "planner_response.txt").write_text(response["text"], encoding="utf-8")
                if response["parsed"] is not None:
                    write_json(output / "planner_response.json", response["parsed"])
            write_json(output / "validation.json", validation)
            if validation["status"] != "PASSED":
                raise ValueError("검색 계획 검사 실패. validation.json을 확인하세요.")
            write_json(output / "search_plan.json", result)
            write_json(output / "search_executor_input.json", build_executor_input(result, payload))
            write_json(output / "controller_observation.json", {
                "search_planning_status": "COMPLETED", "searches_executed": False,
                "planned_issue_ids": sorted({t["issue_id"] for t in result["search_plan"]}),
                "deferred_issues": result["deferred_issues"],
                "source_verified_claim_ids": payload["controller_decision"]["source_verified_claim_ids"],
                "source_recheck_claim_ids": payload["controller_decision"]["source_recheck_claim_ids"],
                "goal_supported_claim_ids": payload["controller_decision"]["goal_supported_claim_ids"],
                "note": "자료를 확보하거나 미해결 쟁점을 해결한 것이 아님"})
            log.update(status="SUCCESS", next_action=result["next_action"], search_task_count=len(result["search_plan"]))
        except Exception as error:
            log.update(status="FAILED", error=safe_error(error))
            if type(error) is ValueError:
                log["local_validation_error"] = str(error)
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "run.json", log)
    print(json.dumps({k: log.get(k) for k in ("status", "model", "next_action", "search_task_count", "usage", "local_validation_error")}, ensure_ascii=False, indent=2))
    print("결과 폴더:", output)
    return log, output


def main():
    parser = argparse.ArgumentParser(description="STEP 7-1 검색 계획 생성; 실제 검색 없음")
    parser.add_argument("--data-dir", type=Path, default=stage_data(7))
    parser.add_argument("--output-dir", type=Path, default=stage_data(7) / 'output')
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        log, _ = run_planner(args.data_dir, args.output_dir, args.dry_run)
        return 0 if log["status"] in {"SUCCESS", "PREPARED"} else 1
    except Exception as error:
        print(json.dumps(safe_error(error), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
