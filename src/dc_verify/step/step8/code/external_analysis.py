"""STEP 8-1: 기존 STEP 3 Agent를 호출하고 Admission guard를 적용한다."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import copy
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
from dc_verify.step.step3.code import analysis as agent
from dc_verify.step.step8.code.source_admission import guard_claims, read_json, write_json, digest, now
from dc_verify.step.step8.code.prompt import EXTERNAL_ANALYSIS_INSTRUCTIONS, EXTERNAL_ANALYSIS_CODEX_PROMPT
from dc_verify.codex import safe_error, DEFAULT_MODEL


def latest_run(base, pattern, required):
    matches = []
    for file in Path(base).glob(pattern + "/run.json"):
        try:
            run = read_json(file)
            if run.get("status") == "SUCCESS" and all((file.parent / n).is_file() for n in required):
                matches.append((run.get("finished_at", ""), str(file.parent), file.parent))
        except (ValueError, OSError):
            continue
    if not matches:
        raise ValueError("완료된 입력 실행 결과를 찾지 못했습니다.")
    return max(matches)[2]


def load_admitted_jobs(gate_run):
    gate_run = Path(gate_run)
    input_file = gate_run / "analysis_execution_input.json"
    if not input_file.exists():
        input_file = gate_run / "analysis_input.json"
    incoming = read_json(input_file)
    admissions = read_json(gate_run / "source_admission.json")["documents"]
    decisions = {d["document_id"]: d for d in admissions}
    if len(decisions) != len(admissions) or not incoming["claim_output_guard_required"] or incoming["final_evidence_allowed"] is not False:
        raise ValueError("Admission 또는 Claim guard 계약이 유효하지 않습니다.")
    jobs = incoming["analysis_jobs"]
    if len({j["document_id"] for j in jobs}) != len(jobs):
        raise ValueError("분석 작업 문서 ID 중복")
    expected = {d["document_id"] for d in admissions if d["analysis_allowed"]}
    if {j["document_id"] for j in jobs} != expected:
        raise ValueError("Admission과 실행 작업 목록이 다릅니다.")
    for job in jobs:
        decision = decisions[job["document_id"]]
        if decision["analysis_admission"] not in {"ALLOW", "CONDITIONAL"} or job["analysis_admission"] != decision["analysis_admission"]:
            raise ValueError("DENY 또는 미허용 분석 작업")
        if job["bundle"] != read_json(job["bundle_file"]) or job["selection"] != read_json(job["selection_file"]):
            raise ValueError("Gate의 인라인 입력과 파일 입력 불일치")
        doc = job["bundle"]["documents"][0]
        if digest(Path(doc["source_file"]).read_bytes()) != decision["source_sha256"] or doc["published_at"] != decision["published_at"]:
            raise ValueError("원본 무결성 또는 발행일 변경")
        policy = job["bundle"]["analysis_handoff"]["source_admission_policy"]
        if policy["analysis_admission"] != decision["analysis_admission"] or policy["final_evidence_allowed"] is not False:
            raise ValueError("Admission policy 변경")
        agent.prepare_input(job["bundle"], job["selection"])
    return incoming, admissions, jobs


def execute(source_run=None, output_dir=stage_data(8) / 'output', resume=None, dry_run=False, client=None, max_attempts=2, retry_documents=None):
    source = Path(source_run).resolve() if source_run else latest_run(stage_data(8) / 'output', "source_admission_*",
        ["source_admission.json", "analysis_input.json"]).resolve()
    incoming, admissions, jobs = load_admitted_jobs(source)
    output = Path(resume).resolve() if resume else Path(output_dir) / ("external_analysis_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=bool(resume))
    watched = list(source.rglob("*.json")) + [Path(d["source_file"]) for d in admissions]
    snapshots = {str(p.resolve()): digest(p.read_bytes()) for p in watched if p.is_file()}
    snapshot_file = output / "input_snapshot.json"
    if snapshot_file.exists() and read_json(snapshot_file) != snapshots:
        raise ValueError("재개 입력 또는 원본이 변경되었습니다.")
    write_json(snapshot_file, snapshots)
    write_json(output / "analysis_execution_input.json", incoming)
    write_json(output / "source_admission_snapshot.json", admissions)
    (output / "used_codex_prompt.txt").write_text(EXTERNAL_ANALYSIS_CODEX_PROMPT, encoding="utf-8")
    log_path = output / "execution_log.jsonl"
    def event(name, **values):
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": now(), "event": name, **values}, ensure_ascii=False) + "\n")
    event("EXTERNAL_ANALYSIS_STARTED", source_run=str(source), job_count=len(jobs), dry_run=dry_run)
    results, all_claims = [], []
    decisions = {d["document_id"]: d for d in admissions}
    for job in jobs:
        folder = output / "documents" / job["document_id"]
        folder.mkdir(parents=True, exist_ok=True)
        status_file = folder / "job_result.json"
        force_retry = job["document_id"] in (retry_documents or [])
        if not force_retry and status_file.exists() and read_json(status_file)["execution_status"] == "SUCCESS":
            status = read_json(status_file)
            result = read_json(folder / "analysis_result.json")
            payload = agent.prepare_input(job["bundle"], job["selection"])
            _, validation = agent.check_result(result, payload)
            if validation["status"] == "PASSED":
                guarded = guard_claims(result["claims"], admissions)
                all_claims += guarded
                results.append(status)
                event("COMPLETED_DOCUMENT_REUSED", document_id=job["document_id"])
                continue
            event("COMPLETED_RESULT_RECHECK_FAILED", document_id=job["document_id"], errors=validation["errors"])
        if dry_run:
            payload = agent.prepare_input(job["bundle"], job["selection"])
            write_json(folder / "prepared_input.json", payload)
            results.append({"document_id": job["document_id"], "execution_status": "NOT_EXECUTED", "analysis_admission": job["analysis_admission"]})
            continue
        attempts_dir = folder / "attempts"
        previous = sorted(attempts_dir.glob("analysis_*/run.json"))
        recovered = False
        for previous_run in ([] if force_retry else reversed(previous)):
            response_file = previous_run.parent / "analysis_response.json"
            if not response_file.exists():
                continue
            payload = agent.prepare_input(job["bundle"], job["selection"])
            candidate, bindings = agent.bind_source_whitespace(read_json(response_file), payload)
            result, validation, handling = agent.conservative_extraction(candidate, payload)
            if validation["status"] != "PASSED":
                continue
            guarded = guard_claims(result["claims"], admissions)
            write_json(folder / "analysis_result.json", result)
            write_json(folder / "guarded_claims.json", guarded)
            write_json(folder / "validation.json", validation)
            write_json(folder / "extraction_handling.json", handling)
            write_json(folder / "quote_bindings.json", bindings)
            write_json(folder / "guard_validation.json", {"status": "PASSED", "claim_count": len(guarded), "all_final_evidence_use_blocked": True})
            status = {"document_id": job["document_id"], "analysis_admission": job["analysis_admission"],
                "execution_status": "SUCCESS", "claim_count": len(guarded), "final_evidence_allowed": False,
                "analysis_status": result["analysis_status"], "latest_attempt": str(previous_run.parent.resolve()),
                "reprocessed_completed_response": True, "rejected_claim_count": len(handling["rejected_claims"]),
                "required_metric_coverage": result["required_metric_coverage"], "missing_required_information": result["missing_required_information"]}
            write_json(status_file, status)
            all_claims += guarded
            results.append(status)
            event("COMPLETED_RESPONSE_REPROCESSED", document_id=job["document_id"], accepted=len(guarded), rejected=len(handling["rejected_claims"]))
            recovered = True
            break
        if recovered:
            continue
        feedback = None
        if previous and (previous[-1].parent / "validation.json").is_file():
            feedback = read_json(previous[-1].parent / "validation.json")["errors"]
        if force_retry and (folder / "extraction_handling.json").exists():
            handling = read_json(folder / "extraction_handling.json")
            feedback = {"quarantined_claims": handling["rejected_claims"],
                "failed_checks": handling["initial_validation"]["errors"],
                "instruction": "관련 지표가 격리된 원문 위치 오류를 다시 확인하라. 값을 보정하지 말고 직접 인용 가능한 Claim만 추출하라."}
        status = {"document_id": job["document_id"], "analysis_admission": job["analysis_admission"],
            "execution_status": "FAILED", "claim_count": 0, "final_evidence_allowed": False}
        for attempt in range(len(previous), max_attempts):
            print("외부 문서 Analysis:", job["document_id"], job["analysis_admission"], "시도", attempt + 1, flush=True)
            event("ANALYSIS_API_STARTED", document_id=job["document_id"], attempt=attempt + 1, model=DEFAULT_MODEL)
            suffix = EXTERNAL_ANALYSIS_INSTRUCTIONS.format(document_id=job["document_id"], admission=job["analysis_admission"])
            try:
                log, attempt_output = agent.run_analysis(job["bundle_file"], job["selection_file"], attempts_dir,
                    client=client, prompt_suffix=suffix, retry_feedback=feedback, bind_whitespace=True, quarantine_invalid=True)
                status["latest_attempt"] = str(attempt_output.resolve())
                if log["status"] != "SUCCESS":
                    if (attempt_output / "validation.json").is_file():
                        feedback = read_json(attempt_output / "validation.json")["errors"]
                        status["errors"] = feedback
                        event("LOCAL_VALIDATION_FAILED", document_id=job["document_id"], errors=feedback)
                        continue
                    status["error"] = log.get("error")
                    break
                result = read_json(attempt_output / "analysis_result.json")
                guarded = guard_claims(result["claims"], admissions)
                write_json(folder / "analysis_result.json", result)
                write_json(folder / "guarded_claims.json", guarded)
                write_json(folder / "validation.json", read_json(attempt_output / "validation.json"))
                if (attempt_output / "extraction_handling.json").exists():
                    write_json(folder / "extraction_handling.json", read_json(attempt_output / "extraction_handling.json"))
                write_json(folder / "guard_validation.json", {"status": "PASSED", "claim_count": len(guarded),
                    "all_final_evidence_use_blocked": all(c["final_evidence_allowed"] is False for c in guarded)})
                status.update(execution_status="SUCCESS", claim_count=len(guarded), analysis_status=result["analysis_status"],
                    required_metric_coverage=result["required_metric_coverage"], missing_required_information=result["missing_required_information"])
                status.pop("errors", None)
                all_claims += guarded
                break
            except Exception as error:
                status["error"] = safe_error(error)
                event("ANALYSIS_OR_GUARD_FAILED", document_id=job["document_id"], error_type=type(error).__name__)
                break
        write_json(status_file, status)
        results.append(status)
        event("DOCUMENT_ANALYSIS_FINISHED", **status)
    if len({c["claim"]["claim_id"] for c in all_claims}) != len(all_claims):
        raise ValueError("외부 문서 전체에서 Claim ID 중복")
    original_reference = {"status": "NOT_PROVIDED", "analysis_reexecuted": False}
    try:
        original_run = latest_run(stage_data(3) / 'output', "analysis_*", ["analysis_result.json"])
        original_file = original_run / "analysis_result.json"
        original_result = read_json(original_file)
        context_matches = all(original_result.get(k) == incoming["review_context"][k] for k in ("review_goal", "target_projects", "as_of_date"))
        if context_matches:
            shutil.copyfile(original_file, output / "original_analysis_result.json")
        original_reference.update(status="PRESERVED" if context_matches else "CONTEXT_MISMATCH_NOT_ATTACHED",
            source_file=str(original_file.resolve()), sha256=digest(original_file.read_bytes()), claim_count=len(original_result["claims"]))
    except ValueError:
        pass
    write_json(output / "original_analysis_reference.json", original_reference)
    unchanged = all(Path(p).is_file() and digest(Path(p).read_bytes()) == value for p, value in snapshots.items())
    if not unchanged:
        raise ValueError("실행 중 기존 Gate 또는 원문 변경")
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    api_runs = []
    for file in output.glob("documents/*/attempts/analysis_*/run.json"):
        record = read_json(file)
        if record.get("api_attempted"):
            api_runs.append({"file": str(file.resolve()), "completed_response_received": bool(record.get("response_id")),
                "usage_recorded": record.get("usage") is not None,
                **{k: record.get(k) for k in ("status", "model", "response_id", "request_id", "usage")}})
            for key in usage:
                usage[key] += (record.get("usage") or {}).get(key, 0)
    success = sum(r["execution_status"] == "SUCCESS" for r in results)
    status = "PREPARED" if dry_run else ("SUCCESS" if success == len(jobs) else "PARTIAL_FAILURE")
    write_json(output / "analysis_results.json", {"execution_status": status, "documents": results})
    write_json(output / "external_claims.json", {"claim_count": len(all_claims), "claims": all_claims,
        "source_verification_performed": False, "cross_check_performed": False, "final_evidence_allowed": False})
    quarantined = []
    for job in jobs:
        handling_file = output / "documents" / job["document_id"] / "extraction_handling.json"
        if handling_file.exists():
            handling = read_json(handling_file)
            for claim in handling["rejected_claims"]:
                reasons = next((c["issues"] for c in handling["initial_validation"]["evidence_checks"] if c["claim_id"] == claim["claim_id"]), [])
                quarantined.append({"document_id": job["document_id"], "claim": claim,
                    "status": "REJECTED_EXTRACTION_PENDING_REVIEW", "reasons": reasons, "included_in_accepted_claims": False})
    write_json(output / "quarantined_claims.json", {"count": len(quarantined), "claims": quarantined})
    verification_jobs = []
    for job in jobs:
        folder = output / "documents" / job["document_id"]
        if (folder / "guard_validation.json").exists():
            verification_jobs.append({"document_id": job["document_id"], "analysis_result": read_json(folder / "analysis_result.json"),
                "guarded_claims": read_json(folder / "guarded_claims.json"), "source_bundle": job["bundle"],
                "mechanical_checks": read_json(folder / "validation.json"), "source_admission": decisions[job["document_id"]]})
    write_json(output / "verification_input.json", {"next_stage": "STEP8_2_SOURCE_AND_EVIDENCE_VERIFICATION", "verification_jobs": verification_jobs,
        "original_analysis_reference": original_reference, "cross_check_performed": False})
    gate_observation = read_json(source / "controller_observation.json")
    write_json(output / "controller_observation.json", {"analysis_execution_status": status, "completed_document_count": success,
        "failed_document_ids": [r["document_id"] for r in results if r["execution_status"] == "FAILED"],
        "extracted_claim_count": len(all_claims), "provisional_claim_count": sum(c["is_provisional"] for c in all_claims),
        "quarantined_claim_count": len(quarantined),
        "unresolved_issues": gate_observation["unresolved_issues"], "source_verification_performed": False,
        "evidence_assessment_performed": False, "cross_check_performed": False, "comparison_performed": False,
        "controller_replanned": False, "new_web_searches": 0, "input_snapshots_unchanged": unchanged,
        "next_stage": "STEP8_2_SOURCE_AND_EVIDENCE_VERIFICATION"})
    report = {"status": status, "source_run": str(source), "document_count": len(jobs), "completed_document_count": success,
        "claim_count": len(all_claims), "provisional_claim_count": sum(c["is_provisional"] for c in all_claims),
        "model_requested": DEFAULT_MODEL, "model_api_attempts": api_runs, "token_usage": usage,
        "completed_model_responses": sum(r["completed_response_received"] for r in api_runs),
        "token_usage_complete": all(r["usage_recorded"] for r in api_runs),
        "quarantined_claim_count": len(quarantined),
        "inputs_unchanged": unchanged, "finished_at": now(), "new_web_searches": 0,
        "source_verification_performed": False, "cross_check_performed": False,
        "code_sha256": digest(Path(__file__).read_bytes()), "shared_agent_sha256": digest(Path(agent.__file__).read_bytes())}
    write_json(output / "run.json", report)
    event("EXTERNAL_ANALYSIS_FINISHED", status=status, claim_count=len(all_claims))
    return report, output


def main():
    parser = argparse.ArgumentParser(description="STEP8-1 기존 Analysis Agent 실행 및 guard 적용")
    parser.add_argument("--source-run", type=Path)
    parser.add_argument("--output-dir", type=Path, default=stage_data(8) / 'output')
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--retry-document", action="append", default=[], help="검사 실패로 필요한 Claim이 격리된 문서만 기존 시도 한도 내 재추출")
    args = parser.parse_args()
    report, output = execute(args.source_run, args.output_dir, args.resume, args.dry_run, retry_documents=args.retry_document)
    print(json.dumps({k: report[k] for k in ("status", "document_count", "completed_document_count", "claim_count", "provisional_claim_count", "token_usage")}, ensure_ascii=False, indent=2))
    print("결과 폴더:", output)
    return 0 if report["status"] in {"SUCCESS", "PREPARED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
