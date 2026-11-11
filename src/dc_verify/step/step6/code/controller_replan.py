"""STEP 6 — Observe & Replan. 다음 행동을 선택하고 저장한다."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel, ConfigDict, create_model

HERE = Path(__file__).resolve().parent
STEP = HERE.parent
from dc_verify.codex import PLANNING_MODEL as DEFAULT_MODEL, request_structured_output, safe_error
from dc_verify.step.step6.code.controller_decision_prompt import CONTROLLER_DECISION_SYSTEM_PROMPT, build_user_prompt

ActionType = Literal["SEARCH_OFFICIAL", "SEARCH_LOCAL", "REVERIFY_SOURCE", "ASK_USER", "DEFER", "FINALIZE"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PrioritizedIssue(StrictModel):
    issue_id: str
    priority: int
    issue: str
    reason: str


class SearchConstraints(StrictModel):
    official_sources_only: bool
    published_on_or_before: Optional[str]
    same_project_required: bool
    metric_definition_required: bool
    metric_as_of_required: bool
    scope_and_status_required: bool
    reverify_before_use: bool


class LocalScope(StrictModel):
    document_id: str
    pages: list[int]


class NextAction(StrictModel):
    type: ActionType
    target: str
    reason: str
    issue_ids: list[str]
    required_evidence: list[str]
    query_suggestions: list[str]
    local_scope: list[LocalScope]
    search_constraints: SearchConstraints
    retry_justification: Optional[str]
    new_information_event_ids: list[str]


class PlannedTask(StrictModel):
    task_id: str
    priority: int
    action: Literal["SEARCH_OFFICIAL", "SEARCH_LOCAL", "REVERIFY_SOURCE", "ASK_USER", "DEFER", "FINALIZE",
                    "ANALYZE_DOCUMENT", "VERIFY_SOURCE", "ASSESS_EVIDENCE", "CHECK_COMPARABILITY", "REPLAN"]
    issue_ids: list[str]
    description: str
    execution_status: Literal["PLANNED"]


class ControllerDecision(StrictModel):
    goal_status: Literal["INCOMPLETE", "COMPLETE", "NEEDS_INPUT"]
    review_goal: str
    target_projects: list[str]
    as_of_date: Optional[str]
    observations: list[str]
    source_verified_claim_ids: list[str]
    source_recheck_claim_ids: list[str]
    goal_supported_claim_ids: list[str]
    prioritized_issues: list[PrioritizedIssue]
    next_action: NextAction
    updated_plan: list[PlannedTask]
    remaining_issues: list[str]
    comparison_status: Literal["NOT_ASSESSED"]
    controller_status: Literal["ACTION_SELECTED"]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def signature(action):
    """검색어 표현을 바꾸는 것만으로 같은 쟁점의 중복 탐색을 피하지 못하게 한다."""
    value = [action["type"], action["target"], sorted(action["issue_ids"])]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def prepare_input(data_dir):
    names = ["initial_plan", "analysis_result", "source_verification", "evidence_assessment", "execution_history"]
    payload = {name: read_json(Path(data_dir) / (name + ".json")) for name in names}
    plan, analysis, source, evidence, history = (payload[name] for name in names)
    for key in ("review_goal", "target_projects", "as_of_date"):
        if not (plan[key] == analysis[key] == evidence[key]):
            raise ValueError("기존 단계의 검토 맥락 불일치: " + key)
    if plan["as_of_date"]:
        date.fromisoformat(plan["as_of_date"])
    ids = [c["claim_id"] for c in analysis["claims"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Analysis Claim ID 중복")
    source_ids = [c["claim_id"] for c in source["claim_results"]]
    evidence_ids = [c["claim_id"] for c in evidence["claim_assessments"]]
    if Counter(ids) != Counter(source_ids) or Counter(ids) != Counter(evidence_ids):
        raise ValueError("기존 결과의 Claim ID 누락·중복")
    verified = [c["claim_id"] for c in source["claim_results"] if c["pdf_status"] == "VERIFIED"]
    recheck = [c["claim_id"] for c in source["claim_results"] if c["pdf_status"] != "VERIFIED"]
    supported = [c["claim_id"] for c in evidence["claim_assessments"] if c["supports_review_goal"]]
    if Counter(supported) != Counter(evidence["controller_observation"]["supported_claim_ids"]):
        raise ValueError("증빙 평가의 목적 지지 목록이 모순됨")
    for item in evidence["claim_assessments"]:
        if item["claim_id"] in recheck and (item["evidence_level"] != "NOT_ASSESSED" or item["supports_review_goal"]):
            raise ValueError("원문 미검증 Claim의 증빙 수준이 승격됨")
    if not 0 <= history["search_policy"]["max_attempts_per_issue"] <= 2:
        raise ValueError("쟁점별 탐색 한도는 0~2여야 함")
    issues = []
    for detail in evidence["unresolved_requirement_details"]:
        issue_id = "requirement_" + hashlib.sha256(detail["requested_metric"].encode()).hexdigest()[:12]
        issues.append({"issue_id": issue_id, "kind": "GOAL_EVIDENCE", "claim_ids": [],
                       "description": detail["requested_metric"], "reason": detail["reason"],
                       "required_evidence": detail["needed_evidence"]})
    for item in source["claim_results"]:
        if item["claim_id"] in recheck:
            issues.append({"issue_id": "source_review_" + item["claim_id"], "kind": "SOURCE_REVIEW",
                           "claim_ids": [item["claim_id"]], "description": "원문 재검토: " + item["claim_id"],
                           "reason": item["reason"], "required_evidence": "원문 문맥·대상·조건 재확인"})
    if len({i["issue_id"] for i in issues}) != len(issues):
        raise ValueError("미해결 쟁점 ID 중복")
    payload["observed_state"] = {
        "source_verified_claim_ids": verified, "source_recheck_claim_ids": recheck,
        "goal_supported_claim_ids": supported, "evidence_review_goal_status": evidence["review_goal_status"],
        "source_verification_status": source["verification_status"],
        "available_document_dates": [{"document_id": d["document_id"], "published_at": d.get("published_at")}
            for d in history["available_documents"]],
        "review_as_of_date": plan["as_of_date"],
        "unprocessed_controller_pages": analysis["unprocessed_controller_pages"],
        "comparison_target_supplied": bool(plan.get("comparison_project")),
        "note": "supported 목록이 비어 있어도 원문 확인 실패가 아니다. 비교 대상은 초기 계획에 명시된 경우에만 제공된 것으로 본다."}
    payload["issue_inventory"] = issues
    return payload


def validate_decision(value, payload):
    result = ControllerDecision.model_validate(value).model_dump(mode="json")
    errors = []
    plan, observed = payload["initial_plan"], payload["observed_state"]
    history = payload["execution_history"]
    inventory = {i["issue_id"]: i for i in payload["issue_inventory"]}
    for key in ("review_goal", "target_projects", "as_of_date"):
        if result[key] != plan[key]:
            errors.append("검토 맥락 변경: " + key)
    for key in ("source_verified_claim_ids", "source_recheck_claim_ids", "goal_supported_claim_ids"):
        if Counter(result[key]) != Counter(observed[key]):
            errors.append("기존 판정 목록 변경: " + key)
    if observed["evidence_review_goal_status"] == "INCOMPLETE" and result["goal_status"] == "COMPLETE":
        errors.append("증빙 부족을 목적 완료로 승격")
    priorities = result["prioritized_issues"]
    if Counter(i["issue_id"] for i in priorities) != Counter(inventory.keys()):
        errors.append("우선순위 목록에 미해결 쟁점 누락·중복·추가")
    if any(i["priority"] < 1 for i in priorities) or len({i["priority"] for i in priorities}) != len(priorities):
        errors.append("쟁점 우선순위는 서로 다른 양의 정수여야 함")
    action = result["next_action"]
    action_issues = action["issue_ids"]
    if not set(action_issues).issubset(inventory) or len(action_issues) != len(set(action_issues)):
        errors.append("다음 행동의 쟁점 ID가 유효하지 않음")
    if not action["reason"].strip() or not result["observations"]:
        errors.append("관찰 또는 다음 행동 이유 누락")
    if action["type"] in {"SEARCH_OFFICIAL", "SEARCH_LOCAL", "REVERIFY_SOURCE"}:
        if not action_issues or action["target"] not in plan["target_projects"] or not action["required_evidence"]:
            errors.append("행동의 대상·쟁점·필요 증빙 누락 또는 범위 변경")
    searching = action["type"] in {"SEARCH_OFFICIAL", "SEARCH_LOCAL"}
    if searching and any(inventory[i]["kind"] == "SOURCE_REVIEW" for i in action_issues if i in inventory):
        errors.append("검색 행동에 기존 원문 재검증 쟁점을 혼합함; REVERIFY_SOURCE로 분리 필요")
    constraints = action["search_constraints"]
    if searching:
        if constraints["published_on_or_before"] != plan["as_of_date"]:
            errors.append("검색 자료 공개일 상한이 사용자 기준일과 다름")
        if not all(constraints[k] for k in ("same_project_required", "metric_definition_required",
            "metric_as_of_required", "scope_and_status_required", "reverify_before_use")):
            errors.append("확보 자료의 프로젝트·지표·기준일·범위·재검증 조건 누락")
        if action["type"] == "SEARCH_OFFICIAL" and not constraints["official_sources_only"]:
            errors.append("공식 자료 검색에서 공식 출처 제한 누락")
        executed = [h for h in history["action_history"] if h.get("executed") is True and h["type"] in {"SEARCH_OFFICIAL", "SEARCH_LOCAL"}]
        limit = history["search_policy"]["max_attempts_per_issue"]
        for issue_id in action_issues:
            if sum(issue_id in h["issue_ids"] for h in executed) >= limit:
                errors.append("쟁점별 실제 탐색 한도 도달: " + issue_id)
        repeated = [h for h in executed if signature(h) == signature(action)]
        if repeated:
            events = {e["event_id"]: e for e in history["events"]}
            last_time = max(datetime.fromisoformat(h["finished_at"]) for h in repeated)
            new_ids = action["new_information_event_ids"]
            if (not action["retry_justification"] or not new_ids or
                any(event_id not in events or
                    events[event_id].get("operation") not in {"SOURCE_ADDED", "SOURCE_REVIEW_COMPLETED", "USER_CONTEXT_UPDATED"} or
                    events[event_id].get("status") != "SUCCESS" or
                    not events[event_id].get("finished_at") or
                    datetime.fromisoformat(events[event_id]["finished_at"]) <= last_time for event_id in new_ids)):
                errors.append("같은 행동을 새 정보 없이 반복")
    available = {d["document_id"]: set(d["unprocessed_pages"]) for d in history["available_documents"]}
    if action["type"] == "SEARCH_LOCAL":
        if not action["local_scope"]:
            errors.append("보유 문서 검색 범위 없음")
        for scope in action["local_scope"]:
            if scope["document_id"] not in available or not scope["pages"] or not set(scope["pages"]).issubset(available[scope["document_id"]]):
                errors.append("보유 문서의 미분석 범위를 벗어난 페이지 선택")
    elif action["local_scope"]:
        errors.append("보유 문서 검색이 아닌 행동에 local_scope 지정")
    if action["type"] == "FINALIZE" and (inventory or not observed["comparison_target_supplied"]):
        errors.append("미해결 쟁점 또는 비교 대상 부족 상태에서 최종 비교 완료 금지")
    tasks = result["updated_plan"]
    if not tasks or tasks[0]["action"] != action["type"]:
        errors.append("첫 후속 작업과 다음 행동이 불일치")
    if tasks and Counter(tasks[0]["issue_ids"]) != Counter(action_issues):
        errors.append("첫 후속 작업과 다음 행동의 쟁점 범위 불일치")
    if len({t["task_id"] for t in tasks}) != len(tasks) or any(t["priority"] < 1 for t in tasks):
        errors.append("후속 작업 ID 또는 우선순위 오류")
    if any(not set(t["issue_ids"]).issubset(inventory) for t in tasks):
        errors.append("후속 작업이 알 수 없는 쟁점 참조")
    if Counter(result["remaining_issues"]) != Counter(inventory.keys()):
        errors.append("행동을 실행하지 않았는데 미해결 쟁점 제거 또는 추가")
    return result, {"status": "PASSED" if not errors else "FAILED", "errors": errors,
        "scope": "맥락·상태·참조·공개일·한도·반복·실행 경계 검사. 선택의 실질적 최적성은 별도 검토 필요."}


def build_action_input(result, payload, output):
    """검색어 예시가 최초 지표 정의·대체 금지 지시를 덮어쓰지 않게 함께 전달한다."""
    return {
        "action": result["next_action"], "action_signature": signature(result["next_action"]),
        "execution_status": "PLANNED", "executed": False,
        "review_goal": result["review_goal"], "as_of_date": result["as_of_date"],
        "target_projects": result["target_projects"],
        "initial_analysis_handoff": payload["initial_plan"]["analysis_handoff"],
        "issue_inventory": payload["issue_inventory"], "execution_history": payload["execution_history"],
        "decision_file": str((Path(output) / "controller_decision.json").resolve()),
        "instruction": "STEP 7의 실제 도구 실행 후에만 action_history와 탐색 횟수를 갱신. 검색어·예시는 initial_analysis_handoff의 필수 지표·정의·대체 금지 지시를 변경하지 않음."}


def run_replan(data_dir=stage_data(6), output_dir=stage_data(6) / 'output', dry_run=False, client=None):
    payload = prepare_input(data_dir)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(output_dir) / ("controller_replan_" + stamp)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "controller_input.json", payload)
    rendered = build_user_prompt(payload)
    log = {"status": "PREPARED", "model_requested": DEFAULT_MODEL, "api_attempted": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "input_hashes": {str((Path(data_dir) / (name + ".json")).resolve()):
            hashlib.sha256((Path(data_dir) / (name + ".json")).read_bytes()).hexdigest()
            for name in ("initial_plan", "analysis_result", "source_verification", "evidence_assessment", "execution_history")},
        "prompts": {"system": CONTROLLER_DECISION_SYSTEM_PROMPT, "user": rendered},
        "prompt_file_sha256": hashlib.sha256((HERE / "controller_decision_prompt.py").read_bytes()).hexdigest(),
        "observed_state": payload["observed_state"], "search_executed": False, "additional_agent_called": False}
    write_json(output / "run.json", log)
    if not dry_run:
        try:
            log.update(status="RUNNING", api_attempted=True)
            write_json(output / "run.json", log)
            print(f"Controller Observe & Replan API 호출 중: {DEFAULT_MODEL}; 검색 실행 없음", flush=True)
            response = request_structured_output(CONTROLLER_DECISION_SYSTEM_PROMPT, rendered, ControllerDecision, client=client)
            log.update({k: v for k, v in response.items() if k not in {"parsed", "text"}})
            (output / "controller_response.txt").write_text(response["text"], encoding="utf-8")
            if response["parsed"] is not None:
                write_json(output / "controller_response.json", response["parsed"])
            if response["api_status"] != "completed" or response["parsed"] is None:
                raise ValueError("Controller 재계획의 유효한 완료 응답이 없습니다.")
            result, validation = validate_decision(response["parsed"], payload)
            write_json(output / "validation.json", validation)
            if validation["status"] != "PASSED":
                raise ValueError("판단 검사 실패. validation.json을 확인하세요.")
            write_json(output / "controller_decision.json", result)
            # 선택한 행동은 아직 실행 이력에 넣거나 탐색 횟수로 세지 않는다.
            write_json(output / "next_action_input.json", build_action_input(result, payload, output))
            log.update(status="SUCCESS", goal_status=result["goal_status"],
                next_action=result["next_action"], controller_status=result["controller_status"])
        except Exception as error:
            log.update(status="FAILED", error=safe_error(error))
            if type(error) is ValueError:
                log["local_validation_error"] = str(error)
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "run.json", log)
    print(json.dumps({k: log.get(k) for k in ("status", "model", "goal_status", "next_action", "usage", "error", "local_validation_error")}, ensure_ascii=False, indent=2), flush=True)
    print("결과 폴더:", output, flush=True)
    return log, output


class IssueObservation(StrictModel):
    issue_id: str
    priority: int
    status: Literal['RESOLVED', 'PARTIALLY_SUPPORTED', 'UNRESOLVED', 'NEEDS_HUMAN_REVIEW']
    reason: str
    supporting_claim_keys: list[str]
    remaining_evidence: list[str]


class PostEvidenceDecision(StrictModel):
    decision: Literal['COMPLETE', 'REPLAN', 'DEFER', 'REQUEST_HUMAN_REVIEW']
    goal_status: Literal['COMPLETE', 'INCOMPLETE', 'NEEDS_INPUT']
    review_goal: str
    target_projects: list[str]
    as_of_date: Optional[str]
    observations: list[str]
    observed_execution_claim_keys: list[str]
    current_operation_independently_verified: bool
    issue_decisions: list[IssueObservation]
    next_action: NextAction
    selected_tool_id: Optional[str]
    expected_information_gain: str
    updated_plan: list[PlannedTask]
    remaining_issue_ids: list[str]
    human_review_questions: list[str]
    comparison_status: Literal['NOT_ASSESSED', 'NO_COMPARISON_TARGET', 'CONDITIONAL']
    should_stop: bool
    stop_condition: str
    decision_reason: str


def validate_post_evidence_decision(value, state):
    """Extend the existing Controller's invariants for a stateful post-evidence cycle."""
    r = PostEvidenceDecision.model_validate(value).model_dump(mode='json')
    errors = []
    context = state['review_context']
    issues = {i['issue_id']: i for i in state['issues']}
    claims = set(state['known_claim_keys'])
    for k in ['review_goal', 'target_projects', 'as_of_date']:
        if r[k] != context[k]: errors.append('Review context changed: ' + k)
    if Counter(x['issue_id'] for x in r['issue_decisions']) != Counter(issues.keys()):
        errors.append('Issue inventory coverage differs')
    if Counter(r['observed_execution_claim_keys']) != Counter(state['evidence_observation']['execution_statement_claim_keys']):
        errors.append('Existing completion/execution statements omitted or invented')
    if r['current_operation_independently_verified'] != state['evidence_observation']['current_operation_independently_verified']:
        errors.append('Issuer execution statement promoted to current independent verification')
    if any(not set(x['supporting_claim_keys']) <= claims for x in r['issue_decisions']):
        errors.append('Unknown supporting Claim key')
    for row in r['issue_decisions']:
        issue = issues.get(row['issue_id'])
        if issue and row['status'] == 'RESOLVED' and not issue['goal_completion_allowed']:
            errors.append('Unresolved/provisional evidence marked resolved: ' + row['issue_id'])
        if not row['reason'].strip() or (row['status'] != 'RESOLVED' and not row['remaining_evidence']):
            errors.append('Issue reasoning/remaining evidence missing')
    unresolved = [x['issue_id'] for x in r['issue_decisions'] if x['status'] != 'RESOLVED']
    if Counter(r['remaining_issue_ids']) != Counter(unresolved): errors.append('Remaining issues do not match issue decisions')
    priorities = [x['priority'] for x in r['issue_decisions']]
    if any(p < 1 for p in priorities) or len(priorities) != len(set(priorities)): errors.append('Invalid issue priorities')
    action = r['next_action']
    if not set(action['issue_ids']) <= set(issues) or len(action['issue_ids']) != len(set(action['issue_ids'])):
        errors.append('Unknown/duplicate action issue')
    terminal = r['decision'] in {'COMPLETE', 'DEFER', 'REQUEST_HUMAN_REVIEW'}
    if r['should_stop'] != terminal or not r['stop_condition'].strip(): errors.append('Invalid termination state')
    expected_actions = {'COMPLETE': {'FINALIZE'}, 'DEFER': {'DEFER'}, 'REQUEST_HUMAN_REVIEW': {'ASK_USER'},
        'REPLAN': {'SEARCH_OFFICIAL', 'SEARCH_LOCAL', 'REVERIFY_SOURCE'}}
    if action['type'] not in expected_actions[r['decision']]: errors.append('Decision/action mismatch')
    if r['decision'] == 'COMPLETE' and (not state['goal_evidence_sufficient'] or r['goal_status'] != 'COMPLETE'):
        errors.append('Goal completed without sufficient admitted evidence')
    if r['decision'] == 'COMPLETE' and any(x['status'] != 'RESOLVED' for x in r['issue_decisions'] if issues.get(x['issue_id'], {}).get('kind') == 'GOAL_EVIDENCE'):
        errors.append('Required goal issue still unresolved at completion')
    if r['decision'] != 'COMPLETE' and r['goal_status'] == 'COMPLETE': errors.append('Inconsistent goal completion')
    if r['decision'] in {'DEFER', 'REQUEST_HUMAN_REVIEW'} and not r['human_review_questions']:
        errors.append('Incomplete terminal decision needs due-diligence questions')
    if not state['comparison_target_supplied'] and r['comparison_status'] != 'NO_COMPARISON_TARGET':
        errors.append('Actual comparison target missing')
    if action['target'] not in context['target_projects'] or not action['reason'].strip(): errors.append('Action target/reason missing')
    tools = {t['tool_id']: t for t in state['runtime_tools'] if t['available']}
    if terminal:
        if r['selected_tool_id'] is not None: errors.append('Terminal decision cannot invoke a Tool')
    else:
        t = tools.get(r['selected_tool_id'])
        if not t or action['type'] not in t['supported_actions']: errors.append('Selected Tool is unavailable or incompatible')
        if not action['issue_ids'] or not action['required_evidence'] or not r['expected_information_gain'].strip():
            errors.append('Next work is not concrete/reviewable')
    constraints = action['search_constraints']
    if action['type'] in {'SEARCH_OFFICIAL', 'SEARCH_LOCAL'}:
        if constraints['published_on_or_before'] != context['as_of_date'] or not all(constraints[k] for k in
            ['same_project_required', 'metric_definition_required', 'metric_as_of_required', 'scope_and_status_required', 'reverify_before_use']):
            errors.append('Required source/date/definition constraints missing')
        if action['type'] == 'SEARCH_OFFICIAL' and not constraints['official_sources_only']: errors.append('Official source constraint missing')
        if any(issues.get(i, {}).get('kind') != 'GOAL_EVIDENCE' for i in action['issue_ids']):
            errors.append('Source-review issues cannot receive fresh search budgets')
        history = state['execution_history']['action_history']
        repeated = [h for h in history if h.get('executed') is True and h['type'] == action['type']
            and h['target'] == action['target'] and set(h['issue_ids']) & set(action['issue_ids'])]
        if repeated:
            events = {e['event_id']: e for e in state['new_information_events']}
            last = max(datetime.fromisoformat(h['finished_at'].replace('Z', '+00:00')) for h in repeated)
            refs = action['new_information_event_ids']
            if not action['retry_justification'] or not refs or any(k not in events or events[k]['status'] != 'SUCCESS'
                or events[k]['operation'] not in {'SOURCE_ADDED', 'SOURCE_REVIEW_COMPLETED', 'CROSS_CHECK_COMPLETED', 'USER_CONTEXT_UPDATED'}
                or datetime.fromisoformat(events[k]['finished_at'].replace('Z', '+00:00')) <= last for k in refs):
                errors.append('Repeated work lacks a later real evidence event')
    docs = {d['document_id']: d for d in state['available_documents']}
    if action['type'] in {'SEARCH_LOCAL', 'REVERIFY_SOURCE'}:
        if not action['local_scope']: errors.append('Local work needs document/page scope')
        for scope in action['local_scope']:
            d = docs.get(scope['document_id'])
            permitted = d['unprocessed_pages'] if d and action['type'] == 'SEARCH_LOCAL' else d['all_pages'] if d else []
            if not scope['pages'] or not set(scope['pages']) <= set(permitted): errors.append('Invalid local document/page scope')
    elif action['local_scope']: errors.append('Non-local action contains local scope')
    tasks = r['updated_plan']
    if not tasks or tasks[0]['action'] != action['type'] or Counter(tasks[0]['issue_ids']) != Counter(action['issue_ids']):
        errors.append('First planned task differs from selected action')
    if len({t['task_id'] for t in tasks}) != len(tasks): errors.append('Duplicate planned task IDs')
    planned_counts = Counter()
    for t in tasks:
        if not set(t['issue_ids']) <= set(issues): errors.append('Task references unknown issue')
        if t['action'] in {'SEARCH_OFFICIAL', 'SEARCH_LOCAL'}:
            planned_counts.update(t['issue_ids'])
    for i, count in planned_counts.items():
        if i not in state['search_budget']['remaining_attempts'] or count > state['search_budget']['remaining_attempts'].get(i, 0):
            errors.append('Planned searches exceed restored issue budget: ' + i)
    if terminal and any(t['action'] in {'SEARCH_OFFICIAL', 'SEARCH_LOCAL', 'REVERIFY_SOURCE'} for t in tasks):
        errors.append('Terminal decision has automatically scheduled new investigation')
    if not r['observations'] or not r['decision_reason'].strip(): errors.append('Decision reasoning missing')
    return r, {'status': 'PASSED' if not errors else 'FAILED', 'errors': errors,
        'action_signature': signature(action), 'scope': 'State, evidence gates, repetition, budgets, tools, and termination; no claim of real-world truth.'}


def normalize_post_evidence_metadata(value, state):
    """Mechanical metadata only. Never changes the decision, action, evidence, or issue statuses."""
    result = PostEvidenceDecision.model_validate(value).model_dump(mode='json')
    changes = []
    remaining = [r['issue_id'] for r in result['issue_decisions'] if r['status'] != 'RESOLVED']
    if Counter(result['remaining_issue_ids']) != Counter(remaining):
        changes.append({'field': 'remaining_issue_ids', 'before': result['remaining_issue_ids'], 'after': remaining,
            'reason': 'Derived from all unresolved issue decisions, including Source Review'})
        result['remaining_issue_ids'] = remaining
    priorities = [r['priority'] for r in result['issue_decisions']]
    if all(p > 0 for p in priorities) and len(set(priorities)) != len(priorities):
        order = sorted(range(len(priorities)), key=lambda n: (priorities[n], n))
        for rank, n in enumerate(order, 1): result['issue_decisions'][n]['priority'] = rank
        changes.append({'field': 'issue_priorities', 'before': priorities,
            'after': [r['priority'] for r in result['issue_decisions']], 'reason': 'Stable ordinal presentation of tied ranks'})
    if not state['comparison_target_supplied'] and result['comparison_status'] != 'NO_COMPARISON_TARGET':
        changes.append({'field': 'comparison_status', 'before': result['comparison_status'], 'after': 'NO_COMPARISON_TARGET', 'reason': 'Validated input has no comparison target'})
        result['comparison_status'] = 'NO_COMPARISON_TARGET'
    if result['decision'] in {'COMPLETE', 'DEFER', 'REQUEST_HUMAN_REVIEW'} and result['next_action']['local_scope']:
        changes.append({'field': 'next_action.local_scope', 'before': result['next_action']['local_scope'], 'after': [],
            'reason': 'Terminal decision has no executable local work; original reference scopes retained here'})
        result['next_action']['local_scope'] = []
    return result, changes


def decide_after_cross_check(state, system_prompt, user_prompt, client=None):
    """Same Controller/API pathway as STEP6, extended schema for the post-evidence cycle."""
    targets=state['review_context']['target_projects']
    if not targets:raise ValueError('Final Controller requires an explicit target project')
    action=create_model('ScopedNextAction',__base__=NextAction,target=(Literal[tuple(targets)],...))
    schema=create_model('PostEvidenceDecision',__base__=PostEvidenceDecision,next_action=(action,...))
    response = request_structured_output(system_prompt, user_prompt, schema, client=client, max_output_tokens=12000)
    if response.get('api_status') != 'completed' or response.get('parsed') is None:
        raise ValueError('Controller API did not return a completed structured decision')
    normalized, changes = normalize_post_evidence_metadata(response['parsed'], state)
    result, validation = validate_post_evidence_decision(normalized, state)
    validation['metadata_normalizations'] = changes
    return response, result, validation


def main():
    parser = argparse.ArgumentParser(description="STEP 6 Controller Observe & Replan; 판단만 실행")
    parser.add_argument("--data-dir", type=Path, default=stage_data(6))
    parser.add_argument("--output-dir", type=Path, default=stage_data(6) / 'output')
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        log, _ = run_replan(args.data_dir, args.output_dir, args.dry_run)
        return 0 if log["status"] in {"SUCCESS", "PREPARED"} else 1
    except Exception as error:
        print(json.dumps(safe_error(error), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
