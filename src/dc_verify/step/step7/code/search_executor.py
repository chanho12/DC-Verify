"""STEP 7-2: 검색 실행 어댑터, 실제 세션 응답 수집, 후보·예산 기록.

Codex 도구는 Python SDK가 아니다. 세션은 실제 web__run 응답 영수증을
import-receipts로 전달한다. Python 단독 실행은 연결된 API 어댑터를 사용한다.
"""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import copy
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
STEP = HERE.parent
DEFAULT_INPUT = stage_data(7) / 'search_executor_input.json'


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def locked(run):
    lock = Path(run) / ".executor_lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError("실행 기록을 다른 프로세스가 쓰고 있거나 잠금 복구가 필요합니다.")
    try:
        yield
    finally:
        lock.rmdir()


def canonical_url(value):
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("공개 HTTP(S) URL이 필요합니다.")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(sorted(query)), ""))


def query_key(query):
    return " ".join(query.casefold().split())


def safe_date(value):
    if not value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def parse_session_results(text):
    """실제 web__run 텍스트의 제목·URL·인용 ID만 추출한다. 본문의 날짜·수치 추론 없음."""
    if not isinstance(text, str):
        raise ValueError("세션 응답 텍스트 형식 불일치")
    headers = list(re.finditer(r"^([^\n]+?) \((https?://[^\s]+)\)\s*$", text, re.M))
    results = []
    for n, header in enumerate(headers):
        block = text[header.end():headers[n + 1].start() if n + 1 < len(headers) else len(text)]
        ref = re.search(r"cite([^]+)", block)
        explicit = re.search(r"Published:\s*(\d{4}-\d{2}-\d{2})(?!\d)", block)
        results.append({"title": header[1].strip(), "url": header[2],
            "published_at": None, "search_reported_date": safe_date(explicit[1]) if explicit else None,
            "citation_ref": ref[1] if ref else None, "search_text": block})
    if not results and text.strip() and not re.search(r"no (?:search )?results|검색 결과가 없", text, re.I):
        raise ValueError("응답을 검색 결과 형식으로 해석하지 못했습니다.")
    return results


def select_tool(task, registry, execution_context):
    available = [t for t in registry["tools"] if t.get("available") is True
                 and t.get("execution_context") == execution_context
                 and task["source_type"] in t["source_types"]]
    rank = {"DIRECT_OFFICIAL_API": 0, "WEB_SEARCH": 1, "OFFICIAL_SITE": 2}
    available.sort(key=lambda t: (rank.get(t.get("kind"), 3), t["tool_id"] != task["tool"]))
    return available[0] if available else None


def init_run(input_file, runtime_file, output_dir=stage_data(7) / 'output', allow_mock=False):
    incoming, runtime = read_json(input_file), read_json(runtime_file)
    tasks = incoming["search_plan"]["search_plan"]
    if incoming["executed"] is not False or not tasks:
        raise ValueError("미실행 검색 계획이 필요합니다.")
    if len({t["task_id"] for t in tasks}) != len(tasks):
        raise ValueError("작업 ID 중복")
    if any(not t["query"].strip() or t["target"] not in incoming["search_plan"]["target_projects"] for t in tasks):
        raise ValueError("검색어 누락 또는 검토 대상 변경")
    limit = incoming["search_budget"]["max_attempts_per_issue"]
    if type(limit) is not int or not 0 <= limit <= 2:
        raise ValueError("쟁점별 검색 한도는 0~2회입니다.")
    if incoming["search_plan"].get("execution_mode", "PLAN_ONLY") != "PLAN_ONLY" or incoming["search_plan"].get("searches_executed", False) is not False:
        raise ValueError("실행하지 않은 계획이 필요합니다.")
    if incoming["search_plan"]["as_of_date"]:
        date.fromisoformat(incoming["search_plan"]["as_of_date"])
    input_digest = hashlib.sha256(Path(input_file).read_bytes()).hexdigest()
    if not allow_mock:
        for prior in Path(output_dir).glob("search_execution_*/state.json"):
            saved = read_json(prior)
            if saved.get("run_mode") == "LIVE" and saved.get("input_sha256") == input_digest and saved.get("attempts"):
                raise ValueError("동일 입력의 실제 실행 기록이 있습니다. 새 실행으로 예산을 초기화하지 말고 기존 run을 재개하세요.")
    run = Path(output_dir) / ("search_execution_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    run.mkdir(parents=True, exist_ok=False)
    write_json(run / "executor_input.json", incoming)
    write_json(run / "runtime_tools.json", runtime)
    decisions = []
    for task in sorted(tasks, key=lambda t: t["priority"]):
        tool = select_tool(task, runtime, runtime["execution_context"])
        decisions.append({"task_id": task["task_id"], "selected_tool": tool,
            "reason": "TOOL_AVAILABLE_IN_CONTEXT" if tool else "NO_SUPPORTED_TOOL_IN_EXECUTION_CONTEXT",
            "selection_policy": "원출처 API, 웹 검색, 공식 사이트 순서로 실행 환경·가용성·자료 유형 확인"})
    write_json(run / "tool_selection.json", decisions)
    write_json(run / "state.json", {"started_at": now(), "run_mode": "MOCK" if allow_mock else "LIVE",
        "input_sha256": input_digest,
        "input_file": str(Path(input_file).resolve()), "attempts": [], "documents": [],
        "history": copy.deepcopy(incoming["search_history"]), "retrieval_events": [],
        "execution_policy": {"no_metric_substitution": True, "comparison_performed": False,
            "comparison_scope": "COMMON_METRIC_DEFINITIONS_ONLY", "claim_verification_performed": False}})
    return run


def remaining_attempts(incoming, state, issue_id):
    # 입력의 예약 횟수가 아니라 과거+이번 실제 실행 이력으로 계산한다.
    limit = incoming["search_budget"]["max_attempts_per_issue"]
    used = sum(h.get("executed") is True and h.get("type") in {"SEARCH_OFFICIAL", "SEARCH_LOCAL"}
               and issue_id in h.get("issue_ids", []) for h in state["history"]["action_history"])
    return max(0, limit - used)


def make_session_request(run, task_id, fallback=False):
    """Codex 호스트가 실제 도구 호출에 사용할 요청. 생성만으로 예산 차감 없음."""
    run = Path(run)
    incoming, state, runtime = read_json(run / "executor_input.json"), read_json(run / "state.json"), read_json(run / "runtime_tools.json")
    task = next(t for t in incoming["search_plan"]["search_plan"] if t["task_id"] == task_id)
    tool = select_tool(task, runtime, "CODEX_SESSION")
    if not tool or not remaining_attempts(incoming, state, task["issue_id"]):
        raise ValueError("세션 도구 또는 실제 검색 잔여 한도가 없습니다.")
    query, source_type, reason = task["query"], task["source_type"], None
    if fallback:
        alternatives = runtime.get("fallback_source_types", {}).get(source_type, [])
        source_type = next((s for s in alternatives if s in tool["source_types"]), source_type)
        terms = runtime.get("fallback_terms", {}).get(source_type)
        if not terms:
            raise ValueError("입력에 대체 출처용 검색어가 없습니다.")
        query = task["target"] + " " + terms
        hosts = runtime.get("preferred_search_hosts", [])
        if hosts:
            if hosts[0] not in runtime.get("official_hosts", []):
                raise ValueError("확인되지 않은 대체 검색 호스트")
            query += " site:" + hosts[0]
        reason = "요청 자료 유형이 확인되지 않거나 결과가 부족해 허용된 다른 공식 자료 유형으로 경로 변경"
    if any(a["issue_id"] == task["issue_id"] and query_key(a["query_executed"]) == query_key(query) for a in state["attempts"]):
        raise ValueError("이미 실행한 검색 요청")
    return {"task_id": task_id, "query": query, "tool": tool["adapter"], "effective_source_type": source_type,
            "adjustment_reason": reason, "executed": False, "tool_arguments": {"search_query": [{"q": query}], "response_length": "long"}}


def classify_document(candidate, task, runtime, cutoff):
    url = canonical_url(candidate["url"])
    host = urlsplit(url).hostname
    official = host in runtime.get("official_hosts", [])
    target_norm = re.sub(r"\W", "", task["target"]).casefold()
    content = re.sub(r"\W", "", candidate["title"] + candidate.get("search_text", "")).casefold()
    tokens = [re.sub(r"\W", "", t).casefold() for t in task["target"].split()]
    possible = bool(target_norm and (target_norm in content or
                    (len(tokens) > 1 and all(t and t in content for t in tokens))))
    published = safe_date(candidate.get("published_at"))
    # 검색 결과의 날짜는 원문 공개일로 간주하지 않는다.
    if candidate.get("publication_date_provenance") not in {"ORIGINAL_HTML_METADATA", "OFFICIAL_DOCUMENT_LISTING"}:
        published = None
    status = "CANDIDATE" if official and possible else "NEEDS_SOURCE_OR_PROJECT_REVIEW"
    if not published:
        date_status = "UNKNOWN_REVIEW_REQUIRED"
    elif cutoff and published > cutoff:
        date_status, status = "AFTER_CUTOFF", "EXCLUDED_AFTER_CUTOFF"
    else:
        date_status = "WITHIN_CUTOFF_PROVISIONAL"
    return {"document_id": "doc_" + hashlib.sha256(url.encode()).hexdigest()[:16],
        "title": candidate["title"], "url": url, "source_type": "PDF_DOCUMENT" if urlsplit(url).path.lower().endswith(".pdf") else "WEB_PAGE",
        "requested_source_types": [task["source_type"]], "published_at": published,
        "publication_date_provenance": candidate.get("publication_date_provenance") if published else None,
        "search_reported_date": candidate.get("search_reported_date"), "date_status": date_status,
        "source_status": "OFFICIAL_HOST_CANDIDATE" if official else "UNVERIFIED_SOURCE",
        "source_verified": False, "project_match": "POSSIBLE_MATCH" if possible else "UNVERIFIED",
        "document_status": status, "related_issue_ids": [task["issue_id"]],
        "access_status": "NOT_CHECKED", "evidence_verified": False, "discoveries": []}


def merge_candidate(state, candidate, task, runtime, cutoff, provenance):
    value = classify_document(candidate, task, runtime, cutoff)
    current = next((d for d in state["documents"] if d["url"] == value["url"]), None)
    if current is None:
        current = value
        state["documents"].append(current)
    for field in ("related_issue_ids", "requested_source_types"):
        current[field] = sorted(set(current[field] + value[field]))
    if value["project_match"] == "POSSIBLE_MATCH":
        current["project_match"] = "POSSIBLE_MATCH"
    if current["document_status"] == "NEEDS_SOURCE_OR_PROJECT_REVIEW" and value["document_status"] == "CANDIDATE":
        current["document_status"] = "CANDIDATE"
    current["discoveries"].append(provenance)
    return current["document_id"]


def import_receipts(run, receipt_files):
    run = Path(run)
    with locked(run):
        incoming, runtime, state = read_json(run / "executor_input.json"), read_json(run / "runtime_tools.json"), read_json(run / "state.json")
        tasks = {t["task_id"]: t for t in incoming["search_plan"]["search_plan"]}
        for path in receipt_files:
            receipt = read_json(path)
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            if any(a["receipt_sha256"] == digest for a in state["attempts"]):
                continue
            task = tasks[receipt["task_id"]]
            expected = "MOCK" if state["run_mode"] == "MOCK" else "LIVE_CODEX_TOOL"
            accepted = {"MOCK"} if state["run_mode"] == "MOCK" else {"LIVE_CODEX_TOOL", "LIVE_PYTHON_API"}
            if receipt.get("provenance") not in accepted or receipt.get("executed") is not True:
                raise ValueError("실제 호출 영수증과 모의 결과를 혼합할 수 없습니다.")
            if state["run_mode"] == "LIVE" and receipt.get("provenance") == "MOCK":
                raise ValueError("실제 실행에 모의 응답을 넣을 수 없습니다.")
            tool = select_tool(task, runtime, receipt.get("execution_context", runtime["execution_context"]))
            if not tool or receipt["tool"] not in {tool["tool_id"], tool.get("adapter")}:
                raise ValueError("실행 환경에서 확인되지 않은 도구 응답")
            if remaining_attempts(incoming, state, task["issue_id"]) == 0:
                raise ValueError("실제 검색 한도 초과")
            if receipt["query"] != task["query"] and not receipt.get("adjustment_reason"):
                raise ValueError("검색어 변경 이유가 필요합니다.")
            if any(a["issue_id"] == task["issue_id"] and query_key(a["query_executed"]) == query_key(receipt["query"]) for a in state["attempts"]):
                raise ValueError("동일 검색어 반복. 기존 영수증을 재사용하세요.")
            attempt_id = "attempt_" + digest[:16]
            raw_path = run / "raw_search" / (attempt_id + ".json")
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_bytes(Path(path).read_bytes())
            attempt = {"attempt_id": attempt_id, "task_id": task["task_id"], "issue_id": task["issue_id"],
                "tool_used": tool["tool_id"], "execution_context": tool["execution_context"],
                "query_executed": receipt["query"], "adjustment_reason": receipt.get("adjustment_reason"),
                "planned_source_type": task["source_type"], "effective_source_type": receipt.get("effective_source_type", task["source_type"]),
                "requested_document_type_existence": "UNVERIFIED", "required_information_status": "NOT_VERIFIED",
                "started_at": receipt["started_at"], "finished_at": receipt["finished_at"],
                "receipt_sha256": digest, "raw_response_file": str(raw_path.resolve()), "document_ids": [],
                "execution_status": "TOOL_FAILED" if receipt.get("error") else "PENDING_PARSE"}
            # 실제 호출은 결과 파싱에 실패해도 예산을 소비한다.
            state["history"]["action_history"].append({"type": "SEARCH_OFFICIAL", "executed": True,
                "target": task["target"], "issue_ids": [task["issue_id"]], "query": receipt["query"],
                "tool": tool["tool_id"], "attempt_id": attempt_id, "started_at": receipt["started_at"],
                "finished_at": receipt["finished_at"], "status": attempt["execution_status"]})
            try:
                if not receipt.get("error"):
                    candidates = parse_session_results(receipt["response"]) if tool["execution_context"] == "CODEX_SESSION" else receipt["response"].get("results", [])
                    for candidate in candidates:
                        try:
                            doc_id = merge_candidate(state, candidate, task, runtime, incoming["search_plan"]["as_of_date"],
                                {"attempt_id": attempt_id, "task_id": task["task_id"], "citation_ref": candidate.get("citation_ref"),
                                 "search_text_location": str(raw_path.resolve()), "discovery_method": "SEARCH_RESULT"})
                            attempt["document_ids"].append(doc_id)
                        except (ValueError, KeyError):
                            attempt.setdefault("rejected_candidates", []).append({"url": candidate.get("url"), "reason": "후보 형식 또는 URL 오류"})
                    suitable = [d for d in state["documents"] if d["document_id"] in attempt["document_ids"] and d["document_status"] == "CANDIDATE"]
                    attempt["execution_status"] = "SUCCESS" if suitable else ("NO_SUITABLE_CANDIDATES" if candidates else "NO_RESULTS")
            except (ValueError, TypeError, KeyError):
                attempt["execution_status"] = "PARSE_FAILED"
            if receipt.get("error"):
                attempt["error_type"] = receipt["error"].get("type", "ToolError")
            state["history"]["action_history"][-1]["status"] = attempt["execution_status"]
            state["history"]["external_search_executed"] = True
            state["attempts"].append(attempt)
            write_json(run / "state.json", state)
    return finalize(run)


def add_link_candidates(run, candidates, retrieval_receipt):
    """검색 횟수를 늘리지 않는 실제 공식 사이트 링크 탐색. 관련 쟁점은 명시적으로 연결한다."""
    run = Path(run)
    with locked(run):
        state, incoming, runtime = read_json(run / "state.json"), read_json(run / "executor_input.json"), read_json(run / "runtime_tools.json")
        if retrieval_receipt.get("operation") != "OPEN_DOCUMENT" or not retrieval_receipt.get("response"):
            raise ValueError("원문 탐색 호출 기록이 필요합니다.")
        receipt_path = run / "raw_retrieval" / (hashlib.sha256(json.dumps(retrieval_receipt, ensure_ascii=False).encode()).hexdigest()[:16] + ".json")
        write_json(receipt_path, retrieval_receipt)
        tasks = {t["task_id"]: t for t in incoming["search_plan"]["search_plan"]}
        for candidate in candidates:
            if candidate["url"] not in retrieval_receipt["response"]:
                raise ValueError("실제 탐색 응답에서 확인되지 않은 링크")
            for task_id in candidate["task_ids"]:
                task = tasks[task_id]
                merge_candidate(state, {**candidate, "search_text": candidate.get("context", "")}, task, runtime,
                    incoming["search_plan"]["as_of_date"], {"discovery_method": "OFFICIAL_SITE_LINK",
                    "retrieval_receipt": str(receipt_path.resolve()), "task_id": task_id})
        state["retrieval_events"].append({"operation": "OPEN_DOCUMENT", "receipt": str(receipt_path.resolve()), "search_budget_consumed": False})
        write_json(run / "state.json", state)
    return finalize(run)


def finalize(run):
    with locked(run):
        return _finalize(run)


def _finalize(run):
    run = Path(run)
    state, incoming = read_json(run / "state.json"), read_json(run / "executor_input.json")
    docs = {d["document_id"]: d for d in state["documents"]}
    rows = [{**a, "documents": [docs[i] for i in dict.fromkeys(a["document_ids"])]} for a in state["attempts"]]
    task_ids = {t["task_id"] for t in incoming["search_plan"]["search_plan"]}
    latest = {a["task_id"]: a for a in state["attempts"]}
    technical = [a for a in state["attempts"] if a["execution_status"] in {"TOOL_FAILED", "PARSE_FAILED"}]
    completed = task_ids.issubset(latest) and all(a["execution_status"] not in {"TOOL_FAILED", "PARSE_FAILED"} for a in latest.values())
    discovered = [d for d in docs.values() if d["source_status"] == "OFFICIAL_HOST_CANDIDATE"
                  and d["document_status"] not in {"EXCLUDED_AFTER_CUTOFF", "DUPLICATE_PREVIOUSLY_REVIEWED", "DUPLICATE_CONTENT"}]
    unresolved = [{"issue_id": t["issue_id"], "status": "UNRESOLVED_PENDING_SOURCE_VERIFICATION",
        "reason": "후보 발견은 근거 검증 완료가 아님" if any(t["issue_id"] in d["related_issue_ids"] for d in discovered) else "관련 공식 후보 미발견"}
        for t in incoming["search_plan"]["search_plan"]]
    observation = {"discovered_documents": [d["document_id"] for d in discovered], "unresolved_issues": unresolved,
        "unexecuted_tasks": [{"task_id": t["task_id"], "issue_id": t["issue_id"],
            "reason": "SEARCH_BUDGET_EXHAUSTED" if not remaining_attempts(incoming, state, t["issue_id"]) else "TOOL_NOT_AVAILABLE_OR_NOT_DISPATCHED"}
            for t in incoming["search_plan"]["search_plan"] if t["task_id"] not in latest],
        "search_failures": [{k: a.get(k) for k in ("attempt_id", "task_id", "execution_status", "error_type")} for a in technical],
        "retrieval_failures": [e for e in state["retrieval_events"] if e.get("status") in {"FAILED", "BLOCKED"}],
        "recommended_next_action": "VERIFY_RETRIEVED_SOURCES" if discovered else "RETURN_SEARCH_OBSERVATION_TO_CONTROLLER"}
    result = {"execution_status": "COMPLETED" if completed else ("PARTIALLY_COMPLETED" if rows else "NOT_EXECUTED"),
        "run_mode": state["run_mode"], "searches_executed": bool(rows), "search_request_count": len(rows),
        "search_results": rows, "documents": state["documents"], "controller_observation": observation,
        "execution_policy": state["execution_policy"]}
    write_json(run / "search_results.json", result)
    write_json(run / "controller_observation.json", observation)
    write_json(run / "search_history_updated.json", state["history"])
    budget = {t["issue_id"]: remaining_attempts(incoming, state, t["issue_id"]) for t in incoming["search_plan"]["search_plan"]}
    write_json(run / "search_budget_updated.json", {"remaining_attempts": budget, "counts_only_executed_search_actions": True})
    collected = [d for d in discovered if d.get("access_status") == "DOWNLOADED"]
    uncollected = [d for d in discovered if d.get("access_status") != "DOWNLOADED"]
    write_json(run / "candidate_manifest.json", state["documents"])
    write_json(run / "source_verification_input.json", {"review_goal": incoming["search_plan"]["review_goal"],
        "target_projects": incoming["search_plan"]["target_projects"], "as_of_date": incoming["search_plan"]["as_of_date"],
        "documents": collected, "uncollected_candidates": uncollected,
        "search_constraints": incoming["search_plan"]["search_constraints"],
        "initial_analysis_handoff": incoming["initial_analysis_handoff"], "acceptance_policy": incoming["acceptance_policy"],
        "execution_policy": state["execution_policy"], "claim_verification_performed": False})
    write_json(run / "run.json", {"execution_status": result["execution_status"], "started_at": state["started_at"],
        "updated_at": now(), "run_mode": state["run_mode"], "input_file": state["input_file"], "input_sha256": state["input_sha256"],
        "search_request_count": len(rows), "candidate_document_count": len(discovered),
        "downloaded_document_count": len(collected), "comparison_performed": False,
        "search_failures": len(technical), "standalone_session_tool_supported": False})
    return result


def main():
    parser = argparse.ArgumentParser(description="STEP 7-2 범용 Search Executor")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    init.add_argument("--runtime-tools", type=Path, required=True)
    init.add_argument("--output-dir", type=Path, default=stage_data(7) / 'output')
    ingest = sub.add_parser("import-receipts")
    ingest.add_argument("--run", type=Path, required=True)
    ingest.add_argument("files", type=Path, nargs="+")
    finish = sub.add_parser("finalize")
    finish.add_argument("--run", type=Path, required=True)
    request = sub.add_parser("session-request")
    request.add_argument("--run", type=Path, required=True)
    request.add_argument("--task-id", required=True)
    request.add_argument("--fallback", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        print(init_run(args.input, args.runtime_tools, args.output_dir))
    elif args.command == "import-receipts":
        result = import_receipts(args.run, args.files)
        print(json.dumps({k: result[k] for k in ("execution_status", "search_request_count")}, ensure_ascii=False))
    elif args.command == "session-request":
        print(json.dumps(make_session_request(args.run, args.task_id, args.fallback), ensure_ascii=False))
    else:
        result = finalize(args.run)
        print(json.dumps({k: result[k] for k in ("execution_status", "search_request_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
