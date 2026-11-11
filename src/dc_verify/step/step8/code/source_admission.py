"""STEP 8-0: 로컬 자료의 분석 입장과 최종 증빙 사용을 구분하는 Gate.

모델·검색·분석 Agent 호출 없음. guard_claims는 STEP 8-1 출력의 후속 경계다.
"""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import calendar
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEP = HERE.parent
from dc_verify.step.step8.code.prompt import ADMISSION_POLICY, CODEX_PROMPT


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def latest_preparation(base):
    candidates = []
    for path in Path(base).glob("document_preparation_*/run.json"):
        try:
            run = read_json(path)
            complete = all((path.parent / name).is_file() for name in
                           ("document_validation.json", "analysis_input.json", "controller_observation.json"))
            if run.get("status") == "SUCCESS" and complete:
                finished = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
                candidates.append((finished, str(path.parent), path.parent))
        except (ValueError, KeyError, OSError):
            continue
    if not candidates:
        raise ValueError("완료된 STEP 7-3 입력을 찾지 못했습니다.")
    return max(candidates)[2]


def date_window(value):
    """값을 해석하기 위한 기간이며 알려지지 않은 실제 공개일을 생성하지 않는다."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed = date.fromisoformat(value).isoformat()
        return parsed, parsed
    if re.fullmatch(r"\d{4}-\d{2}", value):
        year, month = map(int, value.split("-"))
        last = calendar.monthrange(year, month)[1]
        return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last:02d}"
    if re.fullmatch(r"\d{4}", value):
        date(int(value), 1, 1)
        return value + "-01-01", value + "-12-31"
    raise ValueError("지원하지 않는 날짜 정밀도")


def relation_to_cutoff(value, cutoff):
    if not value or not cutoff:
        return "UNKNOWN"
    start, end = date_window(value)
    return "AFTER_CUTOFF" if start > cutoff else ("WITHIN_CUTOFF" if end <= cutoff else "OVERLAPS_CUTOFF")


def assess_document(row, bundle, selection, context, bundle_error=None):
    """순수 판정 함수. 원문 상태·bundle·Claim을 수정하지 않는다."""
    result = {"document_id": row["document_id"], "url": row["url"], "source_file": row["source_file"],
        "source_sha256": row.get("expected_sha256"), "original_preparation_status": row["decision"],
        "analysis_admission": "DENY", "analysis_allowed": False, "evidence_admission": "PROHIBITED",
        "final_evidence_allowed": False, "claims_verified": False, "provisional_claims_required": False,
        "published_at": row.get("published_at"), "publication_precision": row.get("publication_precision"),
        "document_period": copy.deepcopy(row.get("document_period")), "as_of_date": context["as_of_date"],
        "related_issue_ids": list(row.get("related_issue_ids", [])), "allowed_source_pages": [],
        "source_warnings": list(row.get("flags", [])), "reason_codes": [], "conditions_to_resolve": []}
    result.update(original_preparation_reason_codes=list(row.get("reason_codes", [])),
        origin_check=row.get("origin_check"), origin_provenance=copy.deepcopy(row.get("origin_provenance")),
        archived_origin_proof_files=copy.deepcopy(row.get("archived_origin_proof_files", [])),
        publication_date_status=row.get("publication_date_status"),
        publication_date_provenance=row.get("publication_date_provenance"))
    reasons = result["reason_codes"]
    if row["decision"] not in {"READY", "HOLD", "EXCLUDED"}:
        reasons.append("UNKNOWN_PREPARATION_STATUS")
    if row["decision"] == "EXCLUDED":
        reasons.append("PREVIOUSLY_EXCLUDED")
    source = Path(row["source_file"])
    if not source.is_file():
        reasons.append("SOURCE_FILE_MISSING")
    else:
        actual = digest(source.read_bytes())
        result["rechecked_sha256"] = actual
        if row.get("hash_check") != "MATCH" or actual != row.get("expected_sha256") or actual != row.get("actual_sha256"):
            reasons.append("SOURCE_INTEGRITY_MISMATCH")
    if row.get("origin_check") != "SUPPORTED_BY_ARCHIVED_OFFICIAL_HOST_LINKS":
        reasons.append("OFFICIAL_ORIGIN_UNCONFIRMED")
    proofs = row.get("archived_origin_proof_files", [])
    if not proofs:
        reasons.append("ORIGIN_PROOF_MISSING")
    for proof in proofs:
        path = Path(proof["file"])
        if not path.is_file() or digest(path.read_bytes()) != proof.get("sha256"):
            reasons.append("ORIGIN_PROOF_INTEGRITY_MISMATCH")
    role = row.get("screening", {}).get("document_role")
    target = row.get("screening", {}).get("target_relation")
    if role != "PRIMARY_DOCUMENT":
        reasons.append("NOT_A_PRIMARY_DOCUMENT")
    if target not in {"DIRECT", "GENERAL_CONTEXT"}:
        reasons.append("TARGET_RELEVANCE_UNCONFIRMED")
    try:
        publication_cutoff = relation_to_cutoff(row.get("published_at"), context["as_of_date"])
        period_cutoff = relation_to_cutoff((row.get("document_period") or {}).get("value"), context["as_of_date"])
    except (ValueError, TypeError):
        reasons.append("INVALID_DATE")
        publication_cutoff = period_cutoff = "UNKNOWN"
    result.update(publication_cutoff=publication_cutoff, document_period_cutoff=period_cutoff)
    if publication_cutoff == "AFTER_CUTOFF":
        reasons.append("PUBLISHED_AFTER_CUTOFF")
    if period_cutoff == "AFTER_CUTOFF":
        reasons.append("FUTURE_DOCUMENT_PERIOD_OR_DATE_CONFLICT")
    supported_holds = {"PUBLICATION_DATE_UNCONFIRMED", "PUBLICATION_DATE_OR_RELEVANCE_UNCONFIRMED",
                       "DOCUMENT_PERIOD_OUTSIDE_OR_OVERLAPPING_CUTOFF", "RELEVANCE_UNCONFIRMED"}
    if row["decision"] == "HOLD" and set(row.get("reason_codes", [])) - supported_holds:
        reasons.append("HOLD_REASON_REQUIRES_REPREPARATION")
    if bundle_error:
        reasons.append(bundle_error)
    if not bundle or not selection:
        reasons.append("ANALYSIS_INPUT_NOT_PREPARED")
    else:
        try:
            handoff, docs = bundle["analysis_handoff"], bundle["documents"]
            if len(docs) != 1 or docs[0]["document_id"] != row["document_id"] or handoff["document_ids"] != [row["document_id"]]:
                reasons.append("DOCUMENT_ID_MISMATCH")
            doc = docs[0]
            if doc["source_url"] != row["url"] or doc["source_sha256"] != result["source_sha256"] or Path(doc["source_file"]).resolve() != source.resolve():
                reasons.append("SOURCE_PROVENANCE_MISMATCH")
            if doc.get("published_at") != row.get("published_at"):
                reasons.append("PUBLICATION_DATE_MISMATCH")
            for key in ("review_goal", "as_of_date", "target_projects"):
                if handoff[key] != context[key]:
                    reasons.append("REVIEW_CONTEXT_MISMATCH:" + key)
            pages = doc["pages"]
            ids = [p["source_page"] for p in pages]
            chosen = selection["selected_pages"]
            if not pages or len(ids) != len(set(ids)) or not chosen or len(chosen) != len(set(chosen)):
                reasons.append("EMPTY_OR_DUPLICATE_PAGE_SELECTION")
            if any(type(n) is not int or n < 1 for n in ids + chosen) or not set(chosen).issubset(ids) or not set(chosen).issubset(handoff["priority_pages"]):
                reasons.append("PAGE_SELECTION_MISMATCH")
            for page in pages:
                if not page["text"].strip() or digest(page["text"].encode()) != page.get("ocr_text_sha256"):
                    reasons.append("PREPARED_TEXT_INTEGRITY_MISMATCH")
                result["source_warnings"] += page.get("uncertainty_flags", [])
            result["allowed_source_pages"] = chosen
        except (ValueError, KeyError, IndexError, TypeError):
            reasons.append("INVALID_ANALYSIS_INPUT")
    result["source_warnings"] = list(dict.fromkeys(result["source_warnings"]))
    result["reason_codes"] = list(dict.fromkeys(reasons))
    if result["reason_codes"]:
        return result
    conditional = row["decision"] == "HOLD" or publication_cutoff != "WITHIN_CUTOFF" or period_cutoff == "OVERLAPS_CUTOFF"
    result.update(analysis_admission="CONDITIONAL" if conditional else "ALLOW", analysis_allowed=True,
        evidence_admission="BLOCKED_UNTIL_CONDITIONS_RESOLVED" if conditional else "PENDING_SOURCE_AND_EVIDENCE_VERIFICATION",
        provisional_claims_required=conditional)
    if not row.get("published_at"):
        result["reason_codes"].append("PUBLICATION_DATE_UNCONFIRMED")
        result["conditions_to_resolve"].append("CONFIRM_OFFICIAL_PUBLICATION_BEFORE_REVIEW_CUTOFF")
    if publication_cutoff == "OVERLAPS_CUTOFF" or period_cutoff == "OVERLAPS_CUTOFF":
        result["reason_codes"].append("DATE_PRECISION_OVERLAPS_CUTOFF")
        result["conditions_to_resolve"].append("RESOLVE_DATE_PRECISION_AT_CUTOFF")
    if not context["as_of_date"]:
        result["conditions_to_resolve"].append("CONFIRM_REVIEW_AS_OF_DATE")
    result["conditions_to_resolve"] += ["VERIFY_ORIGINAL_QUOTE_SCOPE_AND_METRIC", "ASSESS_EVIDENCE_SUFFICIENCY"]
    return result


def guard_claims(claims, admission_results):
    """STEP 8-1 이후 필수 후처리. 검증 승격을 거부하고 원 Claim 바깥에 상태를 연결한다."""
    decisions = {d["document_id"]: d for d in admission_results}
    if len(decisions) != len(admission_results):
        raise ValueError("Admission 문서 ID 중복")
    envelopes, identifiers = [], set()
    forbidden_true = {"verified", "fact_verified", "claims_verified", "evidence_eligible", "final_evidence_allowed", "final_evidence_eligible", "supports_review_goal"}
    unverified_status = {None, "PENDING", "UNVERIFIED", "NOT_ASSESSED"}
    for claim in claims:
        identifier = claim.get("claim_id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("Claim ID 누락 또는 중복")
        identifiers.add(identifier)
        decision = decisions.get(claim.get("source_document_id"))
        if not decision or decision["analysis_admission"] not in {"ALLOW", "CONDITIONAL"} or decision["analysis_allowed"] is not True:
            raise ValueError("미허용 문서의 Claim")
        conditional = decision["analysis_admission"] == "CONDITIONAL"
        expected_evidence = "BLOCKED_UNTIL_CONDITIONS_RESOLVED" if conditional else "PENDING_SOURCE_AND_EVIDENCE_VERIFICATION"
        if decision["final_evidence_allowed"] is not False or decision["claims_verified"] is not False or decision["evidence_admission"] != expected_evidence:
            raise ValueError("위변조되거나 모순된 Admission의 증빙 상태")
        if any(k in claim and claim[k] is not False and claim[k] is not None for k in forbidden_true) or any(
            claim.get(k) not in unverified_status for k in ("verification_status", "claim_verification_status", "evidence_level")):
            raise ValueError("분석 결과를 검증 완료 또는 최종 증빙으로 승격할 수 없습니다.")
        if "source_admission" in claim and claim["source_admission"] != decision["analysis_admission"]:
            raise ValueError("Claim의 Source Admission 상태 변경")
        if conditional and any(k in claim and claim[k] is not True for k in ("provisional", "is_provisional")):
            raise ValueError("조건부 Claim의 잠정 상태 제거")
        if "source_published_at" not in claim or claim["source_published_at"] != decision["published_at"]:
            raise ValueError("원문에 확인되지 않은 발행일 생성 또는 변경")
        if claim.get("source_page") not in decision["allowed_source_pages"]:
            raise ValueError("허용 원문 위치 밖의 Claim")
        envelopes.append({"claim": copy.deepcopy(claim), "source_admission": decision["analysis_admission"],
            "is_provisional": conditional, "claim_verification_status": "PENDING",
            "evidence_admission": decision["evidence_admission"], "final_evidence_allowed": False,
            "source_sha256": decision["source_sha256"], "source_warnings": decision["source_warnings"],
            "conditions_to_resolve": decision["conditions_to_resolve"]})
    return envelopes


def run_gate(source_run=None, output_dir=stage_data(8) / 'output'):
    source = Path(source_run).resolve() if source_run else latest_preparation(stage_data(7) / 'output').resolve()
    validation = read_json(source / "document_validation.json")
    index = read_json(source / "analysis_input.json")
    observation = read_json(source / "controller_observation.json")
    rows = validation["documents"]
    if len({d["document_id"] for d in rows}) != len(rows):
        raise ValueError("검증 문서 ID 중복")
    entries = index["ready_documents"] + index["held_documents_conditional_only"]
    if len({d["document_id"] for d in entries}) != len(entries) or not {d["document_id"] for d in entries}.issubset({r["document_id"] for r in rows}):
        raise ValueError("분석 인덱스 문서 ID 중복 또는 불일치")
    entries = {d["document_id"]: d for d in entries}
    context = {"review_goal": index["review_goal"], "as_of_date": index["as_of_date"], "target_projects": []}
    if context["as_of_date"]:
        date.fromisoformat(context["as_of_date"])
    if entries:
        context["target_projects"] = next(iter(entries.values()))["bundle"]["analysis_handoff"]["target_projects"]
    watched = {p.resolve() for p in source.rglob("*.json")} | {p.resolve() for p in source.rglob("*.jsonl")}
    watched |= {Path(row["source_file"]).resolve() for row in rows}
    watched |= {Path(row[k]).resolve() for row in rows for k in ("analysis_bundle", "analysis_request") if row.get(k)}
    watched |= {Path(proof["file"]).resolve() for row in rows for proof in row.get("archived_origin_proof_files", [])}
    snapshots = {str(p): digest(p.read_bytes()) for p in watched if p.is_file()}
    output = Path(output_dir) / ("source_admission_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "input_snapshot.json", snapshots)
    write_json(output / "policy.json", ADMISSION_POLICY)
    (output / "used_codex_prompt.txt").write_text(CODEX_PROMPT, encoding="utf-8")
    results, jobs = [], []
    log_path = output / "execution_log.jsonl"
    def event(name, **values):
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": now(), "event": name, **values}, ensure_ascii=False) + "\n")
    event("ADMISSION_STARTED", source_run=str(source), mode="LOCAL_POLICY_ONLY")
    for row in rows:
        entry, bundle, selection, error = entries.get(row["document_id"]), None, None, None
        if entry:
            try:
                bundle, selection = read_json(row["analysis_bundle"]), read_json(row["analysis_request"])
                if bundle != entry["bundle"] or selection != entry["selection"] or entry["decision"] != row["decision"]:
                    error = "PREPARATION_INDEX_AND_FILE_MISMATCH"
            except (OSError, ValueError, KeyError):
                error = "PREPARED_FILE_MISSING_OR_INVALID"
        result = assess_document(row, bundle, selection, context, error)
        results.append(result)
        event("DOCUMENT_ADMISSION", document_id=row["document_id"], original_status=row["decision"],
            analysis_admission=result["analysis_admission"], final_evidence_allowed=False, reason_codes=result["reason_codes"])
        if result["analysis_allowed"]:
            derived = copy.deepcopy(bundle)
            admission = {k: copy.deepcopy(result[k]) for k in ("analysis_admission", "provisional_claims_required", "evidence_admission",
                "final_evidence_allowed", "source_warnings", "conditions_to_resolve", "original_preparation_status")}
            derived["analysis_handoff"]["source_admission_policy"] = admission
            derived["analysis_handoff"].setdefault("extraction_instructions", []).append(
                "이 입력의 source_admission_policy를 유지한다. CONDITIONAL 문서는 잠정 Claim만 생성하며 발행일을 꾸미지 않는다. 모든 Claim은 검증 전이고 guard_claims 후처리를 거쳐야 한다.")
            derived["documents"][0]["source_admission_policy"] = admission
            folder = output / "documents" / row["document_id"]
            write_json(folder / "analysis_bundle.json", derived)
            write_json(folder / "analysis_request.json", selection)
            jobs.append({"document_id": row["document_id"], "analysis_admission": result["analysis_admission"],
                "bundle_file": str((folder / "analysis_bundle.json").resolve()),
                "selection_file": str((folder / "analysis_request.json").resolve()), "bundle": derived, "selection": selection,
                "execution_status": "NOT_EXECUTED", "claim_output_guard": "source_admission.guard_claims"})
    unchanged = all(Path(path).is_file() and digest(Path(path).read_bytes()) == value for path, value in snapshots.items())
    if not unchanged:
        raise ValueError("Gate 실행 중 기존 입력·이력이 변경되었습니다.")
    counts = dict(Counter(d["analysis_admission"] for d in results))
    write_json(output / "source_admission.json", {"gate_status": "COMPLETED", "review_context": context,
        "source_preparation_run": str(source), "policy": ADMISSION_POLICY, "documents": results, "counts": counts})
    write_json(output / "analysis_input.json", {"input_format": "ADMITTED_PER_DOCUMENT_STEP3_BUNDLES", "review_context": context,
        "analysis_jobs": jobs, "denied_document_ids": [d["document_id"] for d in results if not d["analysis_allowed"]],
        "analysis_agent_executed": False, "execution_mode": "PREPARE_ONLY", "claim_output_guard_required": True,
        "final_evidence_allowed": False})
    write_json(output / "controller_observation.json", {"source_admission_counts": counts,
        "original_preparation_status_counts": dict(Counter(r["decision"] for r in rows)),
        "unresolved_issues": observation["unresolved_issues"], "resolved_issue_count": 0,
        "new_searches": 0, "api_calls": 0, "search_history_unchanged": True, "previous_inputs_unchanged": unchanged,
        "analysis_agent_executed": False, "controller_replanned": False, "comparison_performed": False,
        "next_stage": "STEP8_1_EXTRACT_WITH_ADMISSION_GUARD", "all_final_evidence_use_blocked_pending_verification": True})
    report = {"status": "SUCCESS", "stage": "STEP8_0_SOURCE_ADMISSION_GATE", "source_run": str(source),
        "processed_documents": len(results), "counts": counts, "analysis_jobs_prepared": len(jobs),
        "source_inputs_unchanged": unchanged, "model_api_calls": 0, "new_web_searches": 0,
        "analysis_agent_executed": False, "finished_at": now(),
        "code_sha256": digest(Path(__file__).read_bytes()), "prompt_sha256": digest((HERE / "prompt.py").read_bytes())}
    event("ADMISSION_COMPLETED", counts=counts, source_inputs_unchanged=unchanged)
    write_json(output / "run.json", report)
    return report, output


def main():
    parser = argparse.ArgumentParser(description="STEP8-0 분석 허용 Gate. 검색·API·Analysis 실행 없음")
    parser.add_argument("--source-run", type=Path)
    parser.add_argument("--output-dir", type=Path, default=stage_data(8) / 'output')
    args = parser.parse_args()
    report, output = run_gate(args.source_run, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("결과 폴더:", output)


if __name__ == "__main__":
    main()
