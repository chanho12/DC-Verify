"""STEP 4-1: OCR 인용 검사 + 해시가 확인된 원본 PDF 이미지 대조."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

HERE = Path(__file__).resolve().parent
STEP = HERE.parent
from dc_verify.codex import DEFAULT_MODEL, request_image_verification, safe_error
from dc_verify.step.step4.code.source_verifier_prompt import SOURCE_VERIFIER_SYSTEM_PROMPT, build_user_prompt

PdfStatus = Literal["VERIFIED", "MISMATCH", "NEEDS_MANUAL_REVIEW", "PDF_NOT_VERIFIED", "VERIFICATION_FAILED"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


FieldStatus = Literal["MATCH", "MISMATCH", "UNCLEAR", "NOT_APPLICABLE"]


class FieldChecks(StrictModel):
    value: FieldStatus
    unit: FieldStatus
    metric_name: FieldStatus
    project_scope: FieldStatus
    temporal_conditions: FieldStatus
    quote_context: FieldStatus
    notes: FieldStatus


class ClaimResult(StrictModel):
    claim_id: str
    source_page: int
    ocr_match: bool
    pdf_status: PdfStatus
    error_types: list[str]
    reason: str
    review_required: bool
    image_evidence_text: Optional[str]
    context_type: Literal["project_specific", "general_definition", "unknown"]
    temporal_context: Optional[str]
    field_checks: FieldChecks


class Observation(StrictModel):
    verified_claim_ids: list[str]
    recheck_claim_ids: list[str]
    unresolved_issues: list[str]
    technical_failures: list[str]


class VerificationResult(StrictModel):
    verification_status: Literal["COMPLETED", "PARTIAL", "FAILED"]
    claim_results: list[ClaimResult]
    controller_observation: Observation


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def load_inputs(analysis_path, ocr_path):
    analysis, payload = read_json(analysis_path), read_json(ocr_path)
    claims = analysis.get("claims", [])
    ids = [c["claim_id"] for c in claims]
    if not claims or len(ids) != len(set(ids)):
        raise ValueError("Claim이 비어 있거나 ID가 중복됐습니다.")
    if len(payload["documents"]) != 1:
        raise ValueError("이번 실행은 단일 PDF 입력만 지원합니다.")
    doc_id = payload["documents"][0]["document_id"]
    if any(c["source_document_id"] != doc_id or type(c["source_page"]) is not int or c["source_page"] < 1 for c in claims):
        raise ValueError("Claim 출처 문서 또는 페이지가 유효하지 않습니다.")
    pages = payload["ocr_pages"]
    keys = [(p["document_id"], p["source_page"]) for p in pages]
    if len(keys) != len(set(keys)):
        raise ValueError("OCR 페이지가 중복됐습니다.")
    for page in pages:
        if sha256(page["text"].encode()) != page["ocr_text_sha256"]:
            raise ValueError("OCR 원문 해시가 일치하지 않습니다.")
    return analysis, payload


def check_ocr(claims, pages):
    texts = {(p["document_id"], p["source_page"]): p["text"] for p in pages}
    checks = {}
    for claim in claims:
        text = texts.get((claim["source_document_id"], claim["source_page"]), "")
        quote = claim["evidence_quote"]
        start = text.find(quote) if quote.strip() else -1
        issues = []
        if start < 0:
            issues.append("OCR_QUOTE_MISMATCH")
        for key in ("raw_value", "metric_name"):
            if not claim[key].strip() or claim[key] not in quote:
                issues.append(key.upper() + "_MISMATCH")
        if claim.get("value") is not None:
            numbers = []
            for match in re.finditer(r"\d+(?:,\d{3})*(?:\.\d+)?\s*(?:만|억)?", claim["raw_value"]):
                raw = match[0].strip()
                scale = 10000 if raw.endswith("만") else 100000000 if raw.endswith("억") else 1
                numbers.append(float(raw.rstrip("만억").strip().replace(",", "")) * scale)
            if claim["value"] not in numbers:
                issues.append("OCR_VALUE_MISMATCH")
        if claim.get("unit") and claim["unit"].casefold() not in claim["raw_value"].casefold():
            issues.append("OCR_UNIT_MISMATCH")
        item = {"claim_id": claim["claim_id"], "ocr_match": not issues, "issues": issues,
                "source_page": claim["source_page"], "quote_exact_match": start >= 0}
        if start >= 0:
            item.update(ocr_character_start=start, ocr_character_end=start + len(quote),
                        ocr_line_start=text[:start].count("\n") + 1)
        checks[claim["claim_id"]] = item
    return checks


def render_original_pages(pdf_path, metadata, claims, output, dpi=200):
    """원본 해시가 맞는 PDF만 렌더링. 실패 원인을 페이지별로 반환한다."""
    import pymupdf

    refs = list(dict.fromkeys(c["source_page"] for c in claims))
    doc_id = metadata["document_id"]
    images, failures = [], {}
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    try:
        path = Path(pdf_path)
        if not path.is_file():
            return [], {n: {"status": "PDF_NOT_VERIFIED", "reason": "원본 PDF 파일 없음"} for n in refs}
        raw = path.read_bytes()
        expected = metadata.get("source_pdf_sha256")
        if not expected or sha256(raw) != expected:
            return [], {n: {"status": "VERIFICATION_FAILED", "reason": "원본 PDF 해시 미확정 또는 불일치"} for n in refs}
        with pymupdf.open(stream=raw, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise ValueError("암호화 PDF")
            for n in refs:
                try:
                    if not 1 <= n <= len(pdf):
                        raise ValueError("PDF 페이지 범위 초과")
                    pixmap = pdf[n - 1].get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
                    target = output / f"page_{n:04d}.png"
                    pixmap.save(target)
                    images.append({"document_id": doc_id, "source_page": n, "path": str(target.resolve()),
                        "sha256": sha256(target.read_bytes()), "source_pdf_sha256": expected,
                        "width": pixmap.width, "height": pixmap.height, "dpi": dpi,
                        "render_engine": "PyMuPDF " + pymupdf.VersionBind})
                except Exception as error:
                    failures[n] = {"status": "VERIFICATION_FAILED", "reason": type(error).__name__ + ": " + str(error)}
    except Exception as error:
        failures = {n: {"status": "VERIFICATION_FAILED", "reason": type(error).__name__ + ": " + str(error)} for n in refs}
    return images, failures


def validate_model_result(result, claims, checks, images):
    parsed = VerificationResult.model_validate(result).model_dump(mode="json")
    expected = {c["claim_id"]: c for c in claims}
    actual = [c["claim_id"] for c in parsed["claim_results"]]
    if Counter(actual) != Counter(expected.keys()):
        raise ValueError("API 응답에서 Claim이 누락·중복되거나 추가됐습니다.")
    provided = {(i["document_id"], i["source_page"]) for i in images}
    for item in parsed["claim_results"]:
        claim = expected[item["claim_id"]]
        if item["source_page"] != claim["source_page"] or item["ocr_match"] != checks[item["claim_id"]]["ocr_match"]:
            raise ValueError("API 응답의 페이지 또는 OCR 검사값이 변경됐습니다.")
        verified = item["pdf_status"] == "VERIFIED"
        if verified and ((claim["source_document_id"], claim["source_page"]) not in provided
                         or not item["ocr_match"] or not item["image_evidence_text"]
                         or item["error_types"] or item["review_required"]
                         or any(s in {"MISMATCH", "UNCLEAR"} for s in item["field_checks"].values())):
            raise ValueError("이미지·OCR·문맥 조건 없이 VERIFIED를 반환했습니다.")
        if not verified and not item["review_required"]:
            raise ValueError("미확인·불일치 항목의 review_required가 false입니다.")
    observation = parsed["controller_observation"]
    verified = [c["claim_id"] for c in parsed["claim_results"] if c["pdf_status"] == "VERIFIED"]
    recheck = [c["claim_id"] for c in parsed["claim_results"] if c["pdf_status"] != "VERIFIED"]
    if Counter(observation["verified_claim_ids"]) != Counter(verified) or Counter(observation["recheck_claim_ids"]) != Counter(recheck):
        raise ValueError("Controller 관찰 목록과 Claim별 판정이 다릅니다.")
    return parsed


def failed_result(claim, check, status, reason):
    return {"claim_id": claim["claim_id"], "source_page": claim["source_page"],
        "ocr_match": check["ocr_match"], "pdf_status": status, "error_types": [status],
        "reason": reason, "review_required": True, "image_evidence_text": None,
        "context_type": "unknown", "temporal_context": None,
        "field_checks": {k: "UNCLEAR" for k in FieldChecks.model_fields}}


def apply_review_guards(result, claims):
    """문맥 분류 모순·제목만 있는 근거는 재검토로 낮춘다. 원응답·Claim은 수정하지 않는다.

    일반 용어라는 이유만으로 원문 일치를 부정하지 않지만, 시설 지표로
    해석된 필드가 함께 있거나 제목만으로 notes를 지지하면 통과시키지 않는다.
    """
    import copy
    guarded = copy.deepcopy(result)
    originals = {c["claim_id"]: c for c in claims}
    for item in guarded["claim_results"]:
        claim = originals[item["claim_id"]]
        issues = []
        if item["context_type"] == "general_definition" and any(
                claim.get(k) is not None for k in ("value", "facility_scope", "development_status")):
            issues.append(("SCOPE_CLASSIFICATION_CONFLICT", "모델은 일반 정의로 분류했으나 Claim에 시설별 값·범위·상태가 있어 분류 재검토 필요"))
        if (claim["evidence_quote"].strip() == claim["metric_name"].strip()
                == claim["raw_value"].strip() and claim.get("notes")):
            issues.append(("HEADING_ONLY_EVIDENCE", "인용이 항목 제목뿐이어서 notes의 내용까지 뒷받침하는지 사람 검토 필요"))
        if issues:
            if item["pdf_status"] == "VERIFIED":
                item["pdf_status"] = "NEEDS_MANUAL_REVIEW"
            item["review_required"] = True
            item["error_types"] = list(dict.fromkeys(item["error_types"] + [code for code, _ in issues]))
            item["reason"] += " | 로컬 검토 규칙: " + "; ".join(reason for _, reason in issues)
            if "SCOPE_CLASSIFICATION_CONFLICT" in item["error_types"]:
                item["field_checks"]["project_scope"] = "UNCLEAR"
            if "HEADING_ONLY_EVIDENCE" in item["error_types"]:
                item["field_checks"]["quote_context"] = "UNCLEAR"
                item["field_checks"]["notes"] = "UNCLEAR"
    observation = guarded["controller_observation"]
    observation["verified_claim_ids"] = [c["claim_id"] for c in guarded["claim_results"] if c["pdf_status"] == "VERIFIED"]
    observation["recheck_claim_ids"] = [c["claim_id"] for c in guarded["claim_results"] if c["pdf_status"] != "VERIFIED"]
    return guarded


def assemble_results(claims, model_result, local_results):
    """입력 Claim 순서를 유지하며 기술 실패도 누락 없이 합친다. 원 Claim은 수정하지 않는다."""
    results = {c["claim_id"]: c for c in local_results}
    if model_result:
        results.update({c["claim_id"]: c for c in model_result["claim_results"]})
    ordered = [results[c["claim_id"]] for c in claims]
    unavailable = sum(r["pdf_status"] in {"PDF_NOT_VERIFIED", "VERIFICATION_FAILED"} for r in ordered)
    technical = [r["claim_id"] + ": " + r["reason"] for r in ordered if r["pdf_status"] == "VERIFICATION_FAILED"]
    unresolved = [r["claim_id"] + ": " + r["reason"] for r in ordered if r["pdf_status"] != "VERIFIED"]
    if model_result:
        unresolved += model_result["controller_observation"]["unresolved_issues"]
        technical += model_result["controller_observation"]["technical_failures"]
    return VerificationResult.model_validate({
        "verification_status": "FAILED" if all(r["pdf_status"] == "VERIFICATION_FAILED" for r in ordered)
                               else "PARTIAL" if unavailable else "COMPLETED",
        "claim_results": ordered, "controller_observation": {
            "verified_claim_ids": [r["claim_id"] for r in ordered if r["pdf_status"] == "VERIFIED"],
            "recheck_claim_ids": [r["claim_id"] for r in ordered if r["pdf_status"] != "VERIFIED"],
            "unresolved_issues": list(dict.fromkeys(unresolved)),
            "technical_failures": list(dict.fromkeys(technical))}}).model_dump(mode="json")


def run_verifier(analysis_path, ocr_path, pdf_path, output_dir, dry_run=False, client=None):
    analysis, payload = load_inputs(analysis_path, ocr_path)
    output = Path(output_dir) / ("source_verification_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    claims, metadata = analysis["claims"], payload["documents"][0]
    checks = check_ocr(claims, payload["ocr_pages"])
    images, render_failures = render_original_pages(pdf_path, metadata, claims, output / "original_pages")
    write_json(output / "ocr_checks.json", checks)
    write_json(output / "image_manifest.json", {"images": images, "render_failures": render_failures})
    selected = [c for c in claims if c["source_page"] not in render_failures]
    subset = {**analysis, "claims": selected}
    rendered = build_user_prompt(subset, payload["ocr_pages"], payload["documents"], checks, images)
    write_json(output / "verification_input.json", {"analysis_result": analysis, "ocr_pages": payload["ocr_pages"],
        "document_metadata": payload["documents"], "ocr_checks": checks, "image_manifest": images})
    log = {"status": "PREPARED", "model_requested": DEFAULT_MODEL, "api_attempted": False,
        "started_at": datetime.now(timezone.utc).isoformat(), "claim_count": len(claims),
        "rendered_pages": [i["source_page"] for i in images], "pdf_hash_verified": bool(images),
        "input_hashes": {"analysis": sha256(Path(analysis_path).read_bytes()), "ocr_source": sha256(Path(ocr_path).read_bytes())},
        "prompts": {"system": SOURCE_VERIFIER_SYSTEM_PROMPT, "user": rendered},
        "prompt_file_sha256": sha256((HERE / "source_verifier_prompt.py").read_bytes()),
        "external_search_performed": False, "evidence_execution_verified": False, "controller_called": False}
    write_json(output / "run.json", log)
    if not dry_run:
        local = [failed_result(c, checks[c["claim_id"]], render_failures[c["source_page"]]["status"],
                               render_failures[c["source_page"]]["reason"]) for c in claims if c["source_page"] in render_failures]
        parsed = None
        if selected:
            try:
                log.update(status="RUNNING", api_attempted=True)
                write_json(output / "run.json", log)
                print(f"Source Verifier API 호출 중: {len(selected)} Claims, 원본 {log['rendered_pages']}쪽", flush=True)
                response = request_image_verification(SOURCE_VERIFIER_SYSTEM_PROMPT, rendered, images, VerificationResult, client=client)
                log.update({k: v for k, v in response.items() if k not in {"text", "parsed"}})
                (output / "verifier_response.txt").write_text(response["text"], encoding="utf-8")
                if response["parsed"] is not None:
                    write_json(output / "verifier_response.json", response["parsed"])
                if response["api_status"] != "completed" or response["parsed"] is None:
                    raise ValueError("검증 API의 유효한 완료 응답이 없습니다.")
                parsed = apply_review_guards(validate_model_result(response["parsed"], selected, checks, images), selected)
            except Exception as error:
                log["error"] = safe_error(error)
                reason = "API 호출 또는 응답 검증 실패: " + type(error).__name__
                if type(error) is ValueError:
                    log["local_validation_error"] = str(error)
                local.extend(failed_result(c, checks[c["claim_id"]], "VERIFICATION_FAILED", reason) for c in selected)
        final = assemble_results(claims, parsed, local)
        write_json(output / "source_verification.json", final)
        write_json(output / "controller_observation.json", {**final["controller_observation"],
            "source_verification_status": final["verification_status"],
            "analysis_missing_required_information": analysis["missing_required_information"],
            "source_verification_file": str((output / "source_verification.json").resolve()),
            "controller_called": False, "evidence_verification_performed": False,
            "general_definition_claim_ids": [c["claim_id"] for c in final["claim_results"] if c["context_type"] == "general_definition"],
            "note": "VERIFIED는 원문 대조 결과일 뿐 운영 이행·독립 증빙 확인이 아님"})
        log.update(status="TECHNICAL_FAILURE" if final["controller_observation"]["technical_failures"] else "SUCCESS",
                   verification_status=final["verification_status"], counts=dict(Counter(c["pdf_status"] for c in final["claim_results"])))
    log["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_json(output / "run.json", log)
    print(json.dumps({k: log.get(k) for k in ("status", "verification_status", "model", "counts", "usage", "error", "local_validation_error")}, ensure_ascii=False, indent=2), flush=True)
    print("결과 폴더:", output, flush=True)
    return log, output


def main():
    parser = argparse.ArgumentParser(description="STEP 4-1 Source Verification, gpt-5.4-mini")
    parser.add_argument("--analysis", type=Path, default=stage_data(4) / 'analysis_result.json')
    parser.add_argument("--ocr-source", type=Path, default=stage_data(4) / 'analysis_input.json')
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=stage_data(4) / 'output')
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        log, _ = run_verifier(args.analysis, args.ocr_source, args.pdf, args.output_dir, args.dry_run)
        return 0 if log["status"] in {"PREPARED", "SUCCESS"} else 1
    except Exception as error:
        print(json.dumps(safe_error(error), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
