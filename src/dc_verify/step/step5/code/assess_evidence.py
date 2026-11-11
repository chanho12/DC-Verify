"""STEP 4-2 — AssessEvidence. 원문 기재와 독립 입증을 구분한다."""
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
from pydantic import BaseModel, ConfigDict

HERE = Path(__file__).resolve().parent
STEP = HERE.parent
from dc_verify.codex import DEFAULT_MODEL, request_structured_output, safe_error
from dc_verify.step.step5.code.evidence_prompt import EVIDENCE_SYSTEM_PROMPT, build_user_prompt


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ClaimAssessment(StrictModel):
    claim_id: str
    statement_type: Literal["PLAN", "TARGET", "ESTIMATE", "ANNOUNCEMENT", "EXECUTION_CLAIM", "GENERAL_CONTEXT", "UNKNOWN"]
    evidence_level: Literal["SOURCE_STATEMENT_ONLY", "ADDITIONAL_DOCUMENT_SUPPORT", "INDEPENDENTLY_CORROBORATED", "INSUFFICIENT", "NOT_ASSESSED"]
    supports_review_goal: bool
    reason: str
    missing_evidence: list[str]
    review_required: bool


class UnresolvedRequirement(StrictModel):
    requested_metric: str
    reason: str
    needed_evidence: str


class ControllerObservation(StrictModel):
    supported_claim_ids: list[str]
    recheck_claim_ids: list[str]
    unresolved_requirements: list[str]
    additional_evidence_needed: list[str]


class EvidenceAssessment(StrictModel):
    assessment_status: Literal["COMPLETED", "PARTIAL"]
    review_goal_status: Literal["SUFFICIENT", "INCOMPLETE"]
    review_goal: str
    target_projects: list[str]
    as_of_date: Optional[str]
    claim_assessments: list[ClaimAssessment]
    unresolved_requirement_details: list[UnresolvedRequirement]
    controller_observation: ControllerObservation


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def load_inputs(analysis_path, source_path, context_path):
    analysis, source, context = (read_json(p) for p in (analysis_path, source_path, context_path))
    claims, checked = analysis["claims"], source["claim_results"]
    ids, source_ids = [c["claim_id"] for c in claims], [c["claim_id"] for c in checked]
    if not ids or len(ids) != len(set(ids)) or Counter(ids) != Counter(source_ids):
        raise ValueError("Analysis와 Source Verification의 Claim ID가 누락·중복·불일치합니다.")
    originals = {c["claim_id"]: c for c in claims}
    for item in checked:
        if item["source_page"] != originals[item["claim_id"]]["source_page"]:
            raise ValueError("Source Verification의 페이지가 Analysis와 다릅니다.")
        if item["pdf_status"] not in {"VERIFIED", "MISMATCH", "NEEDS_MANUAL_REVIEW", "PDF_NOT_VERIFIED", "VERIFICATION_FAILED"}:
            raise ValueError("알 수 없는 원문 검증 상태입니다.")
        if item["pdf_status"] == "VERIFIED" and (item["review_required"] or not item["ocr_match"]):
            raise ValueError("원문 통과와 재검토·OCR 상태가 모순됩니다.")
    for key in ("review_goal", "target_projects", "as_of_date"):
        if context.get(key) != analysis.get(key):
            raise ValueError("Controller와 Analysis의 검토 맥락이 다릅니다: " + key)
    if context.get("as_of_date"):
        date.fromisoformat(context["as_of_date"])
    inventory = context.get("available_evidence", {})
    if inventory.get("additional_documents") or inventory.get("independent_evidence"):
        raise ValueError("현재 단계는 원본 문서 하나의 평가입니다. 추가 문서 검증은 후속 단계에서 연결하세요.")
    context = {**context, "available_evidence": {
        "additional_documents": [], "independent_evidence": [],
        "scope": "Original document and derivative pipeline results only; no additional or independent evidence supplied."}}
    return analysis, source, context


def validate_assessment(value, analysis, source, context):
    result = EvidenceAssessment.model_validate(value).model_dump(mode="json")
    errors = []
    expected = {c["claim_id"]: c for c in analysis["claims"]}
    statuses = {c["claim_id"]: c for c in source["claim_results"]}
    rows = result["claim_assessments"]
    if Counter(c["claim_id"] for c in rows) != Counter(expected.keys()):
        errors.append("모든 Claim ID가 정확히 한 번씩 포함되지 않음")
    for key in ("review_goal", "target_projects", "as_of_date"):
        if result[key] != context.get(key):
            errors.append("검토 맥락 변경: " + key)
    for row in rows:
        cid = row["claim_id"]
        if cid not in expected:
            continue
        if not row["reason"].strip():
            errors.append(cid + ": 평가 사유 없음")
        if statuses[cid]["pdf_status"] != "VERIFIED":
            if row["evidence_level"] != "NOT_ASSESSED" or row["supports_review_goal"] or not row["review_required"]:
                errors.append(cid + ": 원문 미검증 Claim의 증빙 평가·목적 지지 승격 금지")
        if row["evidence_level"] in {"ADDITIONAL_DOCUMENT_SUPPORT", "INDEPENDENTLY_CORROBORATED"}:
            errors.append(cid + ": 실제 추가 문서·독립 증빙이 없는데 증빙 수준 승격")
        if row["evidence_level"] in {"NOT_ASSESSED", "INSUFFICIENT"} and row["supports_review_goal"]:
            errors.append(cid + ": 미평가·불충분 상태에서 목적 증빙 충족으로 표시")
        if (expected[cid].get("temporal_basis") in {"planned", "expected"}
                and row["statement_type"] == "EXECUTION_CLAIM"):
            errors.append(cid + ": 계획·예상을 실행 주장으로 변환")
    obs = result["controller_observation"]
    supported = [r["claim_id"] for r in rows if r["supports_review_goal"]]
    recheck = [r["claim_id"] for r in rows if r["review_required"]]
    if Counter(obs["supported_claim_ids"]) != Counter(supported) or Counter(obs["recheck_claim_ids"]) != Counter(recheck):
        errors.append("Controller 관찰과 Claim별 목적 지지·재검토 목록 불일치")
    missing = [m["requested_metric"] for m in analysis.get("missing_required_information", [])]
    details = result["unresolved_requirement_details"]
    if Counter(d["requested_metric"] for d in details) != Counter(missing):
        errors.append("Analysis의 미해결 필수 지표가 누락·변경됨")
    if any(not d["reason"].strip() or not d["needed_evidence"].strip() for d in details):
        errors.append("미해결 요구사항의 사유·추가 증빙 설명 누락")
    if missing and (result["review_goal_status"] != "INCOMPLETE" or not obs["unresolved_requirements"] or not obs["additional_evidence_needed"]):
        errors.append("필수 지표 미확인인데 검토 목적 완료 또는 추가 증빙 필요사항 누락")
    return result, {"status": "PASSED" if not errors else "FAILED", "errors": errors,
        "checks": ["Claim ID coverage", "source-verification gate", "no unsupported corroboration",
                   "no plan-to-execution promotion", "context unchanged", "missing requirements preserved",
                   "controller observation consistency"],
        "scope": "Structural and evidence inventory constraints, not proof of real-world truth."}


def prepare_api_inputs(analysis, source):
    """미검증 항목은 모델의 판단 대상에서 제외해 증빙 승격 자체를 막는다."""
    verified = {c["claim_id"] for c in source["claim_results"] if c["pdf_status"] == "VERIFIED"}
    return ({**analysis, "claims": [c for c in analysis["claims"] if c["claim_id"] in verified]},
            {**source, "claim_results": [c for c in source["claim_results"] if c["claim_id"] in verified],
             "controller_observation": {**source["controller_observation"],
                 "verified_claim_ids": [c["claim_id"] for c in source["claim_results"] if c["claim_id"] in verified],
                 "recheck_claim_ids": []}})


def merge_not_assessed(result, analysis, source):
    """API 응답을 보존하고, 미검증 Claim은 규칙에 따른 미평가 행으로 합친다."""
    import copy
    merged = copy.deepcopy(result)
    rows = {r["claim_id"]: r for r in merged["claim_assessments"]}
    sources = {r["claim_id"]: r for r in source["claim_results"]}
    local = []
    for claim in analysis["claims"]:
        checked = sources[claim["claim_id"]]
        if checked["pdf_status"] != "VERIFIED":
            row = {"claim_id": claim["claim_id"], "statement_type": "UNKNOWN",
                "evidence_level": "NOT_ASSESSED", "supports_review_goal": False,
                "reason": "원문 검증 미완료로 평가하지 않음: " + checked["reason"],
                "missing_evidence": ["원문 재검토 필요: " + checked["reason"]], "review_required": True}
            rows[claim["claim_id"]] = row
            local.append(row)
    merged["claim_assessments"] = [rows[c["claim_id"]] for c in analysis["claims"]]
    observation = merged["controller_observation"]
    observation["supported_claim_ids"] = [r["claim_id"] for r in merged["claim_assessments"] if r["supports_review_goal"]]
    observation["recheck_claim_ids"] = [r["claim_id"] for r in merged["claim_assessments"] if r["review_required"]]
    observation["unresolved_requirements"] += [r["claim_id"] + ": " + r["reason"] for r in local]
    observation["additional_evidence_needed"] += [r["claim_id"] + ": " + r["missing_evidence"][0] for r in local]
    return merged, local


def assess_evidence(analysis_path=stage_data(5) / 'analysis_result.json',
                    source_path=stage_data(5) / 'source_verification.json',
                    context_path=stage_data(5) / 'review_context.json',
                    output_dir=stage_data(5) / 'output', dry_run=False, client=None):
    analysis, source, context = load_inputs(analysis_path, source_path, context_path)
    output = Path(output_dir) / ("evidence_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "evidence_input.json", {"analysis_result": analysis, "source_verification": source, "review_context": context})
    api_analysis, api_source = prepare_api_inputs(analysis, source)
    write_json(output / "api_input.json", {"analysis_result": api_analysis, "source_verification": api_source, "review_context": context})
    user_prompt = build_user_prompt(api_analysis, api_source, context)
    log = {"status": "PREPARED", "model_requested": DEFAULT_MODEL, "api_attempted": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "input_hashes": {str(Path(p).resolve()): hashlib.sha256(Path(p).read_bytes()).hexdigest()
                         for p in (analysis_path, source_path, context_path)},
        "prompt_file_sha256": hashlib.sha256((HERE / "evidence_prompt.py").read_bytes()).hexdigest(),
        "prompts": {"system": EVIDENCE_SYSTEM_PROMPT, "user": user_prompt},
        "external_search_performed": False, "controller_called": False, "comparability_assessed": False,
        "input_claim_count": len(analysis["claims"]), "api_claim_count": len(api_analysis["claims"]),
        "local_not_assessed_claim_ids": [c["claim_id"] for c in source["claim_results"] if c["pdf_status"] != "VERIFIED"]}
    write_json(output / "run.json", log)
    if not dry_run:
        try:
            if not api_analysis["claims"]:
                raise ValueError("원문 검증을 통과한 Claim이 없습니다. 이번 API 평가는 실행하지 않습니다.")
            log.update(status="RUNNING", api_attempted=True)
            write_json(output / "run.json", log)
            print(f"AssessEvidence API 호출 중: 원문 통과 {len(api_analysis['claims'])} Claims; 미검증 항목은 코드에서 NOT_ASSESSED 처리, {DEFAULT_MODEL}", flush=True)
            response = request_structured_output(EVIDENCE_SYSTEM_PROMPT, user_prompt, EvidenceAssessment, client=client)
            log.update({k: v for k, v in response.items() if k not in {"text", "parsed"}})
            (output / "evidence_response.txt").write_text(response["text"], encoding="utf-8")
            if response["parsed"] is not None:
                write_json(output / "evidence_response.json", response["parsed"])
            if response["api_status"] != "completed" or response["parsed"] is None:
                raise ValueError("증빙 평가 API의 유효한 완료 응답이 없습니다.")
            api_result, api_validation = validate_assessment(response["parsed"], api_analysis, api_source, context)
            write_json(output / "api_validation.json", api_validation)
            if api_validation["status"] != "PASSED":
                raise ValueError("API 증빙 평가 검사 실패. api_validation.json을 확인하세요.")
            merged, local = merge_not_assessed(api_result, analysis, source)
            write_json(output / "local_not_assessed.json", local)
            result, validation = validate_assessment(merged, analysis, source, context)
            write_json(output / "validation.json", validation)
            if validation["status"] != "PASSED":
                raise ValueError("증빙 수준 검사 실패. validation.json을 확인하세요.")
            write_json(output / "evidence_assessment.json", result)
            write_json(output / "controller_observation.json", {**result["controller_observation"],
                "review_goal": result["review_goal"], "review_goal_status": result["review_goal_status"],
                "as_of_date": result["as_of_date"], "unresolved_requirement_details": result["unresolved_requirement_details"],
                "source_unverified_claim_ids": [c["claim_id"] for c in source["claim_results"] if c["pdf_status"] != "VERIFIED"],
                "evidence_assessment_file": str((output / "evidence_assessment.json").resolve()),
                "controller_called": False, "external_search_performed": False,
                "note": "추가 자료 필요사항만 반환하며 다음 행동은 Controller가 결정함"})
            log.update(status="SUCCESS", assessment_status=result["assessment_status"],
                review_goal_status=result["review_goal_status"],
                evidence_counts=dict(Counter(r["evidence_level"] for r in result["claim_assessments"])))
        except Exception as error:
            log.update(status="FAILED", error=safe_error(error))
            if type(error) is ValueError:
                log["local_validation_error"] = str(error)
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "run.json", log)
    print(json.dumps({k: log.get(k) for k in ("status", "model", "assessment_status", "review_goal_status", "evidence_counts", "usage", "error", "local_validation_error")}, ensure_ascii=False, indent=2), flush=True)
    print("결과 폴더:", output, flush=True)
    return log, output


def main():
    parser = argparse.ArgumentParser(description="STEP 4-2 AssessEvidence (step5), gpt-5.4-mini 고정")
    parser.add_argument("--analysis", type=Path, default=stage_data(5) / 'analysis_result.json')
    parser.add_argument("--source", type=Path, default=stage_data(5) / 'source_verification.json')
    parser.add_argument("--review-context", type=Path, default=stage_data(5) / 'review_context.json')
    parser.add_argument("--output-dir", type=Path, default=stage_data(5) / 'output')
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        log, _ = assess_evidence(args.analysis, args.source, args.review_context, args.output_dir, args.dry_run)
        return 0 if log["status"] in {"SUCCESS", "PREPARED"} else 1
    except Exception as error:
        print(json.dumps(safe_error(error), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
