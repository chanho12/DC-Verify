"""STEP 3-1 Analysis Agent. 선택 OCR만 추출; 검색·재계획·원본 검증은 수행하지 않는다."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import copy
import hashlib
import json
import math
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model

HERE = Path(__file__).resolve().parent
STEP = HERE.parent
from dc_verify.codex import DEFAULT_MODEL, request_structured_output, safe_error
from dc_verify.step.step3.code.analysis_prompt import ANALYSIS_SYSTEM_PROMPT, build_analysis_user_prompt

INPUT_PATH = stage_data(3) / 'analysis_input.json'
REQUEST_PATH = stage_data(3) / 'analysis_request.json'
OUTPUT_DIR = stage_data(3) / 'output'

MetricType = Literal["power_intake_capacity", "backup_power_capacity", "total_power_including_backup",
    "design_it_capacity", "contracted_it_capacity", "operational_it_capacity", "expected_power_usage",
    "planned_service_start", "planned_completion", "provided_it_capacity", "actual_it_power_usage",
    "contract_ratio", "contract_status", "occupancy_rate", "service_start", "completion", "other"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class PageRef(StrictModel):
    document_id: str
    source_page: int


class Claim(StrictModel):
    claim_id: str
    project_name: str
    metric_type: MetricType
    metric_name: str = Field(description="Exact contiguous substring of evidence_quote, NOT a generated descriptive label. Use a short original label.")
    value: Optional[float] = Field(description="Number explicitly stated in raw_value; must not substitute a different rounded or detailed number from elsewhere.")
    unit: Optional[str]
    raw_value: str = Field(description="Exact contiguous substring of this claim's evidence_quote. Never combine or borrow wording from another page.")
    value_qualifier: Literal["exact", "approximate", "at_least", "at_most", "range", "text_only", "uncertain"]
    facility_scope: Optional[str]
    development_status: Optional[str]
    source_published_at: Optional[str]
    metric_as_of: Optional[str]
    source_document_id: str
    source_page: int
    evidence_quote: str = Field(description="Exact contiguous substring of the OCR text on source_page, preserving whitespace and punctuation.")
    temporal_basis: Literal["planned", "expected", "operating_claim", "historical", "unspecified"]
    notes: list[str]
    measurement_basis: Optional[Literal["CUSTOMER_PROVIDED_CAPACITY", "OPERATIONAL_CAPACITY", "CONTRACTED_CAPACITY",
        "ACTUAL_MEASURED_USAGE", "CONTRACT_RATIO", "OCCUPANCY_RATIO", "DESIGN_CAPACITY", "PLANNED_CAPACITY",
        "GENERAL_CONTEXT", "OTHER", "UNSPECIFIED"]] = None
    statement_status: Optional[Literal["PLAN", "TARGET", "ESTIMATE", "EXECUTION_CLAIM", "GENERAL_CONTEXT", "UNSPECIFIED"]] = None


class Coverage(StrictModel):
    requested_metric: str
    status: Literal["FOUND", "NOT_FOUND", "AMBIGUOUS"]
    claim_ids: list[str]
    reason: str


class MissingInformation(StrictModel):
    requested_metric: str
    metric_type: Optional[MetricType]
    reason: str
    needed_evidence: str


class AnalysisResult(StrictModel):
    analysis_status: Literal["COMPLETE", "INCOMPLETE"]
    review_goal: str
    target_projects: list[str]
    as_of_date: Optional[str]
    analyzed_pages: list[PageRef]
    unprocessed_controller_pages: list[int]
    claims: list[Claim]
    required_metric_coverage: list[Coverage]
    missing_required_information: list[MissingInformation]
    ambiguities: list[str]
    ocr_review_flags: list[str]
    recommendations_for_controller: list[str]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def prepare_input(bundle, selection):
    handoff = bundle["analysis_handoff"]
    if handoff.get("as_of_date"):
        date.fromisoformat(handoff["as_of_date"])
    # 현재 priority_pages는 문서별 구조가 아니므로 모호한 복수 문서 입력은 거절한다.
    documents = bundle["documents"]
    if len(documents) != 1 or handoff["document_ids"] != [documents[0]["document_id"]]:
        raise ValueError("현재 입력은 단일 문서만 지원합니다. 복수 문서는 문서별 페이지 지정이 필요합니다.")
    doc = documents[0]
    pages = {}
    for page in doc["pages"]:
        number = page["source_page"]
        if type(number) is not int or number < 1 or number in pages or not page["text"].strip():
            raise ValueError("중복·빈 OCR 페이지 또는 잘못된 페이지 번호입니다.")
        if page.get("ocr_text_sha256") != hashlib.sha256(page["text"].encode()).hexdigest():
            raise ValueError("Controller가 전달한 OCR 페이지의 해시가 일치하지 않습니다.")
        pages[number] = page
    chosen = selection["selected_pages"]
    if not chosen or len(chosen) != len(set(chosen)):
        raise ValueError("분석 페이지는 비어 있거나 중복될 수 없습니다.")
    if any(type(n) is not int or n not in pages or n not in handoff["priority_pages"] for n in chosen):
        raise ValueError("선택 페이지가 Controller의 작업 범위를 벗어났습니다.")
    remaining = [n for n in handoff["priority_pages"] if n not in chosen]
    metadata = {k: v for k, v in doc.items() if k != "pages"}
    return {
        "analysis_handoff": handoff, "as_of_date": handoff["as_of_date"],
        "execution_scope": {"selected_pages": chosen, "unprocessed_controller_pages": remaining,
                            "selection_authority": selection["selection_authority"], "note": selection.get("note")},
        "documents": [metadata],
        "ocr_pages": [{"document_id": doc["document_id"], "source_page": n,
                       "text": pages[n]["text"], "ocr_text_sha256": pages[n]["ocr_text_sha256"]} for n in chosen],
    }


def explicit_numbers(text):
    """원문 숫자 표기의 의미만 읽는다. kW→MW 환산·PUE 계산은 하지 않는다."""
    numbers = set()
    for match in re.finditer(r"(?<![\d.])\d+(?:,\d{3})*(?:\.\d+)?\s*(?:만|억)?", text):
        raw = match[0].strip()
        scale = 10000 if raw.endswith("만") else 100000000 if raw.endswith("억") else 1
        numeric = raw.rstrip("만억").strip().replace(",", "")
        numbers.add(float(numeric) * scale)
    return numbers


def scoped_response_schema(payload):
    """입력 문서·대상·페이지·필수 지표만 JSON enum으로 허용한다."""
    handoff = payload["analysis_handoff"]
    fields = {"source_document_id": (Literal[tuple(handoff["document_ids"])], ...),
        "source_page": (Literal[tuple(payload["execution_scope"]["selected_pages"])], ...),
        "project_name": (Literal[tuple(handoff["target_projects"])], ...)}
    if handoff.get("source_admission_policy"):
        prefix = re.escape(handoff["document_ids"][0]) + ":"
        fields["claim_id"] = (str, Field(..., pattern="^" + prefix + "[A-Za-z0-9_-]+$"))
    scoped_claim = create_model("ScopedAnalysisClaim", __base__=Claim, **fields)
    metric = Literal[tuple(handoff["required_metrics"])]
    coverage = create_model("ScopedCoverage", __base__=Coverage, requested_metric=(metric, ...))
    missing = create_model("ScopedMissingInformation", __base__=MissingInformation, requested_metric=(metric, ...))
    return create_model("ScopedAnalysisResult", __base__=AnalysisResult,
        claims=(list[scoped_claim], ...), required_metric_coverage=(list[coverage], ...),
        missing_required_information=(list[missing], ...))


def bind_source_whitespace(value, payload):
    """공백만 다른 유일한 인용을 실제 원문 문자열에 연결한다. 숫자·단어·의미 보정 없음."""
    result = copy.deepcopy(value)
    pages = {(p["document_id"], p["source_page"]): p["text"] for p in payload["ocr_pages"]}
    bindings = []
    def bind(proposal, source):
        if not proposal.strip() or proposal in source:
            return proposal
        pattern = r"\s+".join(re.escape(token) for token in proposal.strip().split())
        matches = list(re.finditer(pattern, source))
        return matches[0][0] if len(matches) == 1 else proposal
    for claim in result.get("claims", []):
        source = pages.get((claim["source_document_id"], claim["source_page"]), "")
        original = {key: claim[key] for key in ("evidence_quote", "raw_value", "metric_name")}
        claim["evidence_quote"] = bind(claim["evidence_quote"], source)
        for key in ("raw_value", "metric_name"):
            claim[key] = bind(claim[key], claim["evidence_quote"])
        if any(claim[key] != val for key, val in original.items()):
            bindings.append({"claim_id": claim["claim_id"], "rule": "UNIQUE_WHITESPACE_EQUIVALENT_SOURCE_SPAN",
                "original": original, "bound": {k: claim[k] for k in original}, "fact_verified": False})
    return result, bindings


def external_semantic_issues(claim):
    """Reusable typed-metric invariants, independent of OCR quote formatting."""
    issues = []
    if claim.get("measurement_basis") is None or claim.get("statement_status") is None:
        issues.append("외부 문서 Claim의 측정 기준·주장 상태 누락")
    basis = {"provided_it_capacity": "CUSTOMER_PROVIDED_CAPACITY", "actual_it_power_usage": "ACTUAL_MEASURED_USAGE",
        "operational_it_capacity": "OPERATIONAL_CAPACITY", "contract_ratio": "CONTRACT_RATIO", "occupancy_rate": "OCCUPANCY_RATIO"}
    if claim["metric_type"] in basis and claim.get("measurement_basis") != basis[claim["metric_type"]]:
        issues.append("지표 유형과 측정 기준이 모순됨")
    capacity_types = {"provided_it_capacity", "operational_it_capacity", "actual_it_power_usage", "design_it_capacity",
        "contracted_it_capacity", "power_intake_capacity", "backup_power_capacity", "total_power_including_backup"}
    if claim["metric_type"] in capacity_types and (not claim.get("unit") or not re.fullmatch(r"[kmg]?w|[km]?va", claim["unit"], re.I)):
        issues.append("전력 용량·사용량 지표에 직접 기재된 전력 단위가 없음")
    label = claim["metric_name"] + " " + claim["raw_value"]
    if claim["metric_type"] == "operational_it_capacity" and not re.search(r"(?:운영|가동).{0,8}IT|operational.{0,12}(?:IT|capacity)|operating.{0,12}(?:IT|capacity)", label, re.I):
        issues.append("제공·계약 IT Load만으로 운영 IT 용량을 확정할 수 없음")
    if claim["metric_type"] == "total_power_including_backup" and not re.search(r"총|합계|total|including\s+backup", label, re.I):
        issues.append("원문 지표 라벨에 예비 전력을 포함한 총량 표현이 없음")
    if claim["metric_type"] in {"planned_service_start", "planned_completion"}:
        if claim.get("statement_status") == "EXECUTION_CLAIM" or (re.search(r"완료|launched|completed|opened", claim["raw_value"], re.I)
            and not re.search(r"예정|계획|목표|planned|expected|target", claim["raw_value"], re.I)):
            issues.append("완료 주장과 계획 지표 유형이 모순됨")
    if claim.get("statement_status") == "EXECUTION_CLAIM" and claim["temporal_basis"] == "planned":
        issues.append("실행 주장과 계획 시점 분류가 모순됨")
    return issues


def check_result(result, payload):
    """JSON·범위·인용의 기계적 확인. 의미·원본 PDF 검증은 STEP 4에서 수행한다."""
    parsed = AnalysisResult.model_validate(result).model_dump(mode="json")
    errors, evidence_checks = [], []
    handoff = payload["analysis_handoff"]
    expected_pages = [{"document_id": p["document_id"], "source_page": p["source_page"]} for p in payload["ocr_pages"]]
    if parsed["analyzed_pages"] != expected_pages:
        errors.append("전달한 페이지 순서·범위와 analyzed_pages가 다름")
    for key in ("review_goal", "target_projects", "as_of_date"):
        if parsed[key] != handoff[key]:
            errors.append(f"Controller의 {key} 변경")
    if parsed["unprocessed_controller_pages"] != payload["execution_scope"]["unprocessed_controller_pages"]:
        errors.append("미처리 Controller 페이지 목록 불일치")
    pages = {(p["document_id"], p["source_page"]): p["text"] for p in payload["ocr_pages"]}
    metadata = {d["document_id"]: d for d in payload["documents"]}
    ids = [c["claim_id"] for c in parsed["claims"]]
    if len(ids) != len(set(ids)):
        errors.append("중복 claim_id")
    for claim in parsed["claims"]:
        issues = []
        text = pages.get((claim["source_document_id"], claim["source_page"]), "")
        quote = claim["evidence_quote"]
        start = text.find(quote) if quote.strip() else -1
        if start < 0:
            issues.append("evidence_quote가 지정 OCR 페이지의 정확한 연속 문자열이 아님")
        for key in ("raw_value", "metric_name"):
            if not claim[key].strip() or claim[key] not in quote:
                issues.append(f"{key}가 evidence_quote에 없음")
        if claim["project_name"] not in handoff["target_projects"]:
            issues.append("지정 프로젝트 외의 claim")
        if claim["source_published_at"] != metadata.get(claim["source_document_id"], {}).get("published_at"):
            issues.append("문서 발표일 메타데이터 변경")
        value = claim["value"]
        if value is not None and not any(math.isclose(value, n, rel_tol=1e-9) for n in explicit_numbers(claim["raw_value"])):
            issues.append("수치가 raw_value의 명시적 숫자 표기와 불일치")
        if claim["unit"] and claim["unit"].casefold() not in claim["raw_value"].casefold():
            issues.append("단위가 raw_value에 없음")
        if handoff.get("source_admission_policy"):
            issues.extend(external_semantic_issues(claim))
        check = {"claim_id": claim["claim_id"], "status": "MATCH" if not issues else "MISMATCH", "issues": issues,
                 "document_id": claim["source_document_id"], "source_page": claim["source_page"]}
        if start >= 0:
            check.update(ocr_character_start=start, ocr_character_end=start + len(quote),
                         ocr_line_start=text[:start].count("\n") + 1,
                         ocr_line_end=text[:start + len(quote)].count("\n") + 1)
        evidence_checks.append(check)
        errors.extend(f"{claim['claim_id']}: {issue}" for issue in issues)
    requested = handoff["required_metrics"]
    coverage = parsed["required_metric_coverage"]
    if len(coverage) != len(requested) or {c["requested_metric"] for c in coverage} != set(requested):
        errors.append("필수 지표의 coverage 누락 또는 중복")
    missing = parsed["missing_required_information"]
    unresolved = {c["requested_metric"] for c in coverage if c["status"] != "FOUND"}
    if len(missing) != len(unresolved) or {m["requested_metric"] for m in missing} != unresolved:
        errors.append("필수 지표 coverage와 missing_required_information 불일치")
    for item in coverage:
        if not set(item["claim_ids"]).issubset(set(ids)) or (item["status"] == "FOUND" and not item["claim_ids"]):
            errors.append("coverage가 없는 claim을 참조하거나 FOUND에 근거가 없음")
    if parsed["analysis_status"] != ("INCOMPLETE" if unresolved else "COMPLETE"):
        errors.append("필수 정보 누락과 analysis_status 불일치")
    return parsed, {"status": "PASSED" if not errors else "FAILED", "errors": errors,
        "evidence_checks": evidence_checks, "scope": "OCR text only; no PDF/independent operational verification",
        "limitations": ["지표 정의·프로젝트 연결·상태·인용의 충분성은 의미 검증이 별도로 필요",
                        "누락 정보 판정은 이번 선택 페이지에만 적용"]}


def conservative_extraction(candidate, payload):
    """실패 Claim은 격리하고 모순된 FOUND만 낮춘다. 원문 사실·Claim 값을 보정하지 않는다."""
    result, initial = check_result(candidate, payload)
    rejected_ids = {c["claim_id"] for c in initial["evidence_checks"] if c["status"] != "MATCH"}
    rejected = [copy.deepcopy(c) for c in result["claims"] if c["claim_id"] in rejected_ids]
    result["claims"] = [c for c in result["claims"] if c["claim_id"] not in rejected_ids]
    valid_ids = {c["claim_id"] for c in result["claims"]}
    missing = {}
    for item in result["missing_required_information"]:
        if item["requested_metric"] in missing:
            # 서로 다른 누락 사유는 모델 출력 그대로 합쳐 보존한다.
            previous = missing[item["requested_metric"]]
            previous["reason"] += " / " + item["reason"]
            previous["needed_evidence"] += " / " + item["needed_evidence"]
        else:
            missing[item["requested_metric"]] = copy.deepcopy(item)
    changes = []
    canonical_missing = []
    for coverage in result["required_metric_coverage"]:
        original = copy.deepcopy(coverage)
        coverage["claim_ids"] = [c for c in coverage["claim_ids"] if c in valid_ids]
        metric = coverage["requested_metric"]
        if coverage["status"] == "FOUND" and (metric in missing or not coverage["claim_ids"]):
            coverage["status"] = "AMBIGUOUS" if coverage["claim_ids"] else "NOT_FOUND"
            coverage["reason"] += " [로컬 보수적 처리: 누락 판정과 모순되거나 인용 검사를 통과한 Claim이 없음]"
        if coverage["status"] != "FOUND":
            canonical_missing.append(missing.get(metric, {"requested_metric": metric, "metric_type": None,
                "reason": coverage["reason"], "needed_evidence": "요청 지표를 정의·대상·시점과 함께 직접 기재한 검증 가능한 원문"}))
        if coverage != original:
            changes.append({"requested_metric": metric, "before": original, "after": copy.deepcopy(coverage)})
    result["missing_required_information"] = canonical_missing
    result["analysis_status"] = "INCOMPLETE" if canonical_missing else result["analysis_status"]
    if rejected:
        result["ambiguities"].append("일부 모델 Claim은 원문 인용·필드 검사 실패로 격리됨. accepted Claim만 반환함.")
    accepted, final = check_result(result, payload)
    return accepted, final, {"initial_validation": initial, "rejected_claims": rejected,
        "coverage_changes": changes, "rule": "QUARANTINE_INVALID_CLAIMS_AND_NEVER_UPGRADE_COVERAGE",
        "claim_values_modified": False, "fact_verification_performed": False}


def run_analysis(input_path=INPUT_PATH, request_path=REQUEST_PATH, output_dir=OUTPUT_DIR, dry_run=False, client=None,
                 prompt_suffix="", retry_feedback=None, bind_whitespace=False, quarantine_invalid=False):
    payload = prepare_input(read_json(input_path), read_json(request_path))
    output = Path(output_dir) / ("analysis_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "analysis_input.json", payload)
    rendered = build_analysis_user_prompt(payload)
    if prompt_suffix:
        rendered += "\n\n" + prompt_suffix
    if retry_feedback:
        rendered += "\n\n이전 출력의 로컬 검사 오류를 원문에서 다시 확인하라. 값을 임의 보정하지 않는다:\n" + json.dumps(retry_feedback, ensure_ascii=False)
    log = {"status": "PREPARED", "model_requested": DEFAULT_MODEL,
        "started_at": datetime.now(timezone.utc).isoformat(), "api_attempted": False,
        "external_search_performed": False, "controller_replan_called": False,
        "source_input": str(Path(input_path).resolve()),
        "source_input_sha256": hashlib.sha256(Path(input_path).read_bytes()).hexdigest(),
        "prompt_file_sha256": hashlib.sha256((HERE / "analysis_prompt.py").read_bytes()).hexdigest(),
        "prompts": {"system": ANALYSIS_SYSTEM_PROMPT, "user": rendered}}
    write_json(output / "run.json", log)
    if not dry_run:
        try:
            log.update(status="RUNNING", api_attempted=True)
            write_json(output / "run.json", log)
            print(f"Analysis API 호출 중: {payload['execution_scope']['selected_pages']}쪽, {DEFAULT_MODEL}", flush=True)
            response = request_structured_output(ANALYSIS_SYSTEM_PROMPT, rendered, scoped_response_schema(payload), client=client, max_output_tokens=16384)
            log.update({k: v for k, v in response.items() if k not in {"parsed", "text"}})
            (output / "analysis_response.txt").write_text(response["text"], encoding="utf-8")
            if response["api_status"] != "completed" or response["parsed"] is None:
                raise ValueError("Analysis API가 유효한 완료 응답을 반환하지 않았습니다.")
            write_json(output / "analysis_response.json", response["parsed"])
            candidate = response["parsed"]
            if bind_whitespace:
                candidate, bindings = bind_source_whitespace(candidate, payload)
                write_json(output / "quote_bindings.json", bindings)
            if quarantine_invalid:
                result, validation, handling = conservative_extraction(candidate, payload)
                write_json(output / "extraction_handling.json", handling)
            else:
                result, validation = check_result(candidate, payload)
            write_json(output / "validation.json", validation)
            if validation["status"] != "PASSED":
                raise ValueError("인용·범위 검사가 실패했습니다. validation.json을 확인하세요.")
            write_json(output / "analysis_result.json", result)
            write_json(output / "controller_observation.json", {
                "analysis_result": result, "mechanical_validation": validation,
                "next_stage": "SOURCE_VERIFIER_THEN_CONTROLLER_REPLAN", "next_stage_executed": False})
            log.update(status="SUCCESS", analysis_status=result["analysis_status"],
                       claims_count=len(result["claims"]), missing_count=len(result["missing_required_information"]))
        except Exception as error:
            log.update(status="FAILED", error=safe_error(error))
            if type(error) is ValueError:
                log["local_validation_error"] = str(error)
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "run.json", log)
    print(json.dumps({k: log.get(k) for k in ("status", "model", "analysis_status", "claims_count", "missing_count", "usage", "error", "local_validation_error")}, ensure_ascii=False, indent=2), flush=True)
    print("결과 폴더:", output, flush=True)
    return log, output


def main():
    parser = argparse.ArgumentParser(description="STEP 3-1 Analysis Agent — gpt-5.4-mini 고정")
    parser.add_argument("--input", type=Path, default=INPUT_PATH)
    parser.add_argument("--request-config", type=Path, default=REQUEST_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        log, _ = run_analysis(args.input, args.request_config, args.output_dir, args.dry_run)
        return 0 if log["status"] in {"PREPARED", "SUCCESS"} else 1
    except Exception as error:
        print(json.dumps(safe_error(error), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
