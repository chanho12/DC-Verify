"""STEP 2: OCR 인덱스 → Controller 초기 계획 → 원문을 붙인 Analysis 입력.

Controller API만 실행한다. Analysis Agent·검증·외부 탐색은 실행하지 않는다.
"""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict

HERE = Path(__file__).resolve().parent
STEP_DIR = HERE.parent
from dc_verify.codex import PLANNING_MODEL as DEFAULT_MODEL, request_structured_output, safe_error
from dc_verify.step.step2.code.prompt import CONTROLLER_SYSTEM_PROMPT, CONTROLLER_USER_PROMPT

INPUT_PATH = stage_data(2) / 'ocr.json'
REQUEST_PATH = stage_data(2) / 'review_request.json'
OUTPUT_DIR = stage_data(2) / 'output'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class DocumentScope(StrictModel):
    document_id: str
    candidate_pages: list[int]
    reason: str


class Task(StrictModel):
    task_id: str
    priority: int
    action: str
    target: str
    reason: str


class NextAction(StrictModel):
    type: Literal["CALL_ANALYSIS_AGENT", "ASK_USER"]
    reason: str


class AnalysisHandoff(StrictModel):
    review_goal: str
    target_projects: list[str]
    as_of_date: Optional[str]
    document_ids: list[str]
    priority_pages: list[int]
    required_metrics: list[str]
    supporting_metrics: list[str]
    extract_fields: list[str]
    extraction_instructions: list[str]
    ocr_review_flags: list[str]


class ControllerPlan(StrictModel):
    plan_status: Literal["READY", "NEEDS_INPUT"]
    review_goal: str
    target_projects: list[str]
    as_of_date: Optional[str]
    missing_user_inputs: list[str]
    required_metrics: list[str]
    supporting_metrics: list[str]
    verification_requirements: list[str]
    document_scope: list[DocumentScope]
    tasks: list[Task]
    next_action: NextAction
    analysis_handoff: AnalysisHandoff


def tokenize(text):
    """도메인 사전 없이 Unicode 단어를 분리한다. 키워드 표시는 원문 철자 유지."""
    return re.findall(r"[^\W_]+(?:[-./][^\W_]+)*", text, flags=re.UNICODE)


def search_terms(text):
    """단어와 글자 2~3개 조각으로 조사·띄어쓰기 차이의 일부를 처리한다.

    형태소 분석·의미 검색이 아니므로 동의어나 문맥적 관련성까지 보장하지 않는다.
    """
    terms = set()
    for token in tokenize(text.casefold()):
        terms.add("word:" + token)
        if token.isalpha():
            for width in (2, 3):
                terms.update("gram:" + token[i:i + width] for i in range(len(token) - width + 1))
    return terms


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_input(path=INPUT_PATH) -> dict:
    result = read_json(path)
    if not isinstance(result, dict) or not isinstance(result.get("pages"), list) or not result["pages"]:
        raise ValueError("페이지별 OCR 결과가 필요합니다. 전체 텍스트를 임의 분할하지 않습니다.")
    total, seen = result.get("total_pdf_pages"), set()
    if type(total) is not int or total < 1:
        raise ValueError("PDF 페이지 수는 양의 정수여야 합니다.")
    for page in result["pages"]:
        number = page.get("input_page")
        if type(number) is not int or not 1 <= number <= total or number in seen:
            raise ValueError("중복되거나 범위를 벗어난 OCR 페이지 번호입니다.")
        if not isinstance(page.get("text"), str) or not page["text"].strip():
            raise ValueError("비어 있는 OCR 페이지입니다.")
        seen.add(number)
    return result


def build_document_index(ocr, metadata, query=""):
    """문서 기반 TF-IDF 키워드와 요청 기반 관련도·발췌를 만든다.

    모든 페이지의 인덱스를 제공한다. 검색 점수로 페이지를 배제하거나
    지표를 검증하지 않으며 최종 분석 범위는 Controller가 결정한다.
    """
    page_tokens = [tokenize(p["text"]) for p in ocr["pages"]]
    frequencies = [Counter(t.casefold() for t in tokens if len(t) >= 2 and not t.isnumeric())
                   for tokens in page_tokens]
    document_frequency = Counter(term for counts in frequencies for term in counts)
    term_sets = [search_terms(p["text"]) for p in ocr["pages"]]
    search_frequency = Counter(term for terms in term_sets for term in terms)
    query_terms = search_terms(query)
    count = len(ocr["pages"])

    def relevance(terms):
        return sum(math.log(1 + (count + 1) / (search_frequency[t] + 1))
                   for t in query_terms.intersection(terms))

    pages = []
    for page, tokens, counts, terms in zip(ocr["pages"], page_tokens, frequencies, term_sets):
        text = page["text"]
        spellings = {}
        for token in tokens:
            spellings.setdefault(token.casefold(), token)
        weights = {term: (1 + math.log(freq)) * (1 + math.log((count + 1) / (document_frequency[term] + 1)))
                   for term, freq in counts.items()}
        keywords = [spellings[t] for t in sorted(weights, key=lambda t: (-weights[t], t))[:12]]
        lines = [(n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if line.strip()]
        ranked_lines = sorted(lines, key=lambda pair: (-relevance(search_terms(pair[1])), pair[0]))
        excerpts = [{"ocr_line": n, "text": line[:180], "truncated": len(line) > 180}
                    for n, line in sorted(ranked_lines[:2])]
        flags = [flag for flag in ("[판독불가]", "[표 구조 불명확]", "[인식 가능한 텍스트 없음]") if flag in text]
        pages.append({"page": page["input_page"],
            "heading": next((line.strip() for line in text.splitlines() if line.strip()), ""),
            "keywords": keywords, "retrieval_score": round(relevance(terms), 4),
            "excerpts": excerpts, "excerpts_verified": False,
            "ocr_review_flags": flags})
    ranked = sorted(pages, key=lambda p: (-p["retrieval_score"], p["page"]))
    return {**metadata, "total_pages": ocr["total_pdf_pages"], "pages": pages,
        "suggested_priority_pages": [p["page"] for p in ranked[:5] if p["retrieval_score"]],
        "index_method": "document TF-IDF keywords; request word/character n-gram relevance; up to two OCR line excerpts per page",
        "index_limitations": "Lexical retrieval, not semantic search or fact verification; keywords may include stopwords; all page indexes are supplied for Controller selection.",
        "source_ocr_status": ocr.get("status"),
        "unindexed_pages": sorted(set(range(1, ocr["total_pdf_pages"] + 1)) - {p["page"] for p in pages}),
        "ocr_verified_against_source": False}


def prepare_input(ocr, request):
    context = dict(request.get("review_context", {}))
    if context.get("as_of_date"):
        date.fromisoformat(context["as_of_date"])
    context["execution_date"] = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    metadata = request["document_metadata"]
    if not metadata.get("document_id") or not metadata.get("title"):
        raise ValueError("문서 ID·제목을 명시하세요. 발표일은 파일명으로 추정하지 않습니다.")
    query = " ".join([request.get("user_request", ""), *context.get("target_projects", [])])
    return {"user_request": request.get("user_request", ""), "review_context": context,
            "ocr_document_index": [build_document_index(ocr, metadata, query)]}


def validate_plan(plan, payload):
    """스키마와 실제 페이지·대상·기준일·작업 지시의 일관성을 확인한다."""
    parsed = ControllerPlan.model_validate(plan).model_dump(mode="json")
    context = payload["review_context"]
    if set(parsed["target_projects"]) != set(context.get("target_projects", [])):
        raise ValueError("Controller가 검토 대상 프로젝트를 변경했습니다.")
    if parsed["as_of_date"] != context.get("as_of_date"):
        raise ValueError("Controller가 기준일을 변경하거나 추정했습니다.")
    available = {d["document_id"]: {p["page"] for p in d["pages"]} for d in payload["ocr_document_index"]}
    scope = {}
    for item in parsed["document_scope"]:
        doc_id, numbers = item["document_id"], item["candidate_pages"]
        if doc_id not in available or doc_id in scope:
            raise ValueError("존재하지 않거나 중복된 문서를 선택했습니다.")
        if len(numbers) != len(set(numbers)) or not set(numbers).issubset(available[doc_id]):
            raise ValueError("인덱스에 없는 페이지 또는 중복 페이지를 선택했습니다.")
        scope[doc_id] = set(numbers)
    handoff = parsed["analysis_handoff"]
    if parsed["plan_status"] == "READY":
        if parsed["next_action"]["type"] != "CALL_ANALYSIS_AGENT" or parsed["missing_user_inputs"]:
            raise ValueError("READY와 다음 행동·미해결 입력이 모순됩니다.")
        if not context.get("target_projects") or not payload["user_request"].strip():
            raise ValueError("필수 목적·대상이 없는데 READY로 판단했습니다.")
        if not parsed["tasks"] or not parsed["required_metrics"] or not scope:
            raise ValueError("작업·필수 지표·문서 범위가 없습니다.")
        tasks = parsed["tasks"]
        if len({t["task_id"] for t in tasks}) != len(tasks) or any(t["priority"] < 1 for t in tasks):
            raise ValueError("작업 ID 또는 우선순위가 유효하지 않습니다.")
        if set(handoff["document_ids"]) != set(scope) or len(handoff["document_ids"]) != len(scope):
            raise ValueError("Analysis 문서 범위와 Controller 선택 범위가 다릅니다.")
        selected = set().union(*scope.values())
        priority = handoff["priority_pages"]
        if not priority or len(priority) != len(set(priority)) or not set(priority).issubset(selected):
            raise ValueError("Analysis 우선 페이지가 선택 범위를 벗어났습니다.")
        if handoff["as_of_date"] != parsed["as_of_date"] or set(handoff["target_projects"]) != set(parsed["target_projects"]):
            raise ValueError("Analysis 대상 또는 기준일이 계획과 다릅니다.")
        for key in ("required_metrics", "supporting_metrics"):
            if set(handoff[key]) != set(parsed[key]):
                raise ValueError("Analysis와 계획의 지표 목록이 다릅니다.")
        if not handoff["extract_fields"] or not handoff["extraction_instructions"]:
            raise ValueError("Analysis 추출 지시가 없습니다.")
    elif parsed["next_action"]["type"] != "ASK_USER" or handoff["document_ids"] or handoff["priority_pages"]:
        raise ValueError("NEEDS_INPUT인데 실행 가능한 Analysis 작업을 만들었습니다.")
    return parsed


def build_analysis_input(plan, ocr, payload, input_path):
    """반환된 handoff를 변경하지 않고 선택된 OCR 원문을 별도 필드에 붙인다."""
    if plan["plan_status"] != "READY":
        return None
    handoff = plan["analysis_handoff"]
    pages = {p["input_page"]: p["text"] for p in ocr["pages"]}
    doc = payload["ocr_document_index"][0]
    scope = next(s for s in plan["document_scope"] if s["document_id"] == doc["document_id"])
    order = list(dict.fromkeys(handoff["priority_pages"] + scope["candidate_pages"]))
    return {"analysis_handoff": handoff, "analysis_agent_called": False,
        "documents": [{"document_id": doc["document_id"], "title": doc["title"],
            "published_at": doc.get("published_at"),
            "published_at_provenance": doc.get("published_at_provenance"),
            "source_ocr_json": str(Path(input_path).resolve()),
            "source_ocr_json_sha256": hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
            "source_pdf_sha256": ocr.get("source_sha256"),
            "pages": [{"source_page": n, "text": pages[n],
                       "ocr_text_sha256": hashlib.sha256(pages[n].encode()).hexdigest()} for n in order]}]}


def run_controller(input_path=INPUT_PATH, request_path=REQUEST_PATH, output_dir=OUTPUT_DIR,
                   dry_run=False, client=None):
    ocr = load_input(input_path)
    payload = prepare_input(ocr, read_json(request_path))
    rendered = CONTROLLER_USER_PROMPT.format(**{
        k: json.dumps(v, ensure_ascii=False, indent=2) if not isinstance(v, str) else v for k, v in payload.items()})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(output_dir) / f"controller_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "controller_input.json", payload)
    log = {"status": "PREPARED", "model_requested": DEFAULT_MODEL,
        "input_ocr_json": str(Path(input_path).resolve()),
        "input_sha256": hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
        "prompt_file_sha256": hashlib.sha256((HERE / "prompt.py").read_bytes()).hexdigest(),
        "prompts": {"system": CONTROLLER_SYSTEM_PROMPT, "user": rendered},
        "started_at": datetime.now(timezone.utc).isoformat(), "api_attempted": False,
        "full_ocr_sent_to_controller": False, "analysis_agent_called": False}
    write_json(output / "run.json", log)
    if not dry_run:
        try:
            log.update(status="RUNNING", api_attempted=True)
            write_json(output / "run.json", log)
            print("Controller API 호출 중: 메타데이터와 페이지 인덱스만 전달…", flush=True)
            result = request_structured_output(CONTROLLER_SYSTEM_PROMPT, rendered, ControllerPlan, client=client)
            log.update({k: v for k, v in result.items() if k not in {"text", "parsed"}})
            (output / "controller_response.txt").write_text(result["text"], encoding="utf-8")
            if result["api_status"] != "completed" or result["parsed"] is None:
                raise ValueError("유효한 API 완료 계획이 없습니다.")
            write_json(output / "controller_response.json", result["parsed"])
            plan = validate_plan(result["parsed"], payload)
            write_json(output / "controller_plan.json", plan)
            bundle = build_analysis_input(plan, ocr, payload, input_path)
            if bundle is not None:
                write_json(output / "analysis_input.json", bundle)
            log.update(status="SUCCESS", plan_status=plan["plan_status"],
                next_action=plan["next_action"], analysis_input_prepared=bundle is not None)
        except Exception as error:
            log.update(status="FAILED", error=safe_error(error))
            if isinstance(error, ValueError) and not hasattr(error, "status_code"):
                log["validation_error"] = str(error)
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "run.json", log)
    print(json.dumps({k: log.get(k) for k in ("status", "model", "plan_status", "next_action", "usage", "error", "validation_error")}, ensure_ascii=False, indent=2), flush=True)
    print("결과 폴더:", output, flush=True)
    return log, output


def main():
    parser = argparse.ArgumentParser(description="STEP 2 Controller 초기 계획 (gpt-5.4-mini 고정)")
    parser.add_argument("--input", type=Path, default=INPUT_PATH)
    parser.add_argument("--request-config", type=Path, default=REQUEST_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true", help="API 호출 없이 입력만 저장")
    args = parser.parse_args()
    try:
        log, _ = run_controller(args.input, args.request_config, args.output_dir, args.dry_run)
        return 0 if log["status"] in {"SUCCESS", "PREPARED"} else 1
    except Exception as error:
        print(json.dumps(safe_error(error), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
