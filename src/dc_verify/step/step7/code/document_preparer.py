"""STEP 7-3: 저장된 원문 검증·관련 페이지 선별·OCR·Analysis 입력 준비.

웹 검색·원문 추가 수집·Analysis 실행·Controller 재계획 없음.
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
from typing import Literal
from urllib.parse import urlsplit

import fitz
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict, Field, create_model

HERE = Path(__file__).resolve().parent
from dc_verify.codex import request_image_verification, request_structured_output, DEFAULT_MODEL, safe_error
from dc_verify.step.step7.code.prompt import (CODEX_PROMPT, DOCUMENT_PREPARATION_POLICY,
    DOCUMENT_SCREEN_SYSTEM_PROMPT, PREPARATION_OCR_SYSTEM_PROMPT)
from dc_verify.step.step7.code.search_executor import read_json, write_json, now
from dc_verify.step.step7.code.search_fetcher import publication_date


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PageChoice(StrictModel):
    source_page: int
    issue_ids: list[str]
    reason: str


class Screening(StrictModel):
    document_role: Literal["PRIMARY_DOCUMENT", "INDEX", "UNRELATED"]
    target_relation: Literal["DIRECT", "GENERAL_CONTEXT", "UNRELATED", "UNCERTAIN"]
    selected_pages: list[PageChoice] = Field(max_length=6)
    uncertainty_flags: list[str]


class OCRPage(StrictModel):
    source_page: int
    text: str
    uncertainty_flags: list[str]


class OCRResult(StrictModel):
    pages: list[OCRPage]


def sha(value):
    return hashlib.sha256(value).hexdigest()


def text_quality(text):
    if len(text.strip()) < 20:
        return "EMPTY_OR_SPARSE"
    hangul = re.findall(r"[가-힣]", text)
    if len(hangul) >= 12 and Counter(hangul).most_common(1)[0][1] / len(hangul) > .55:
        return "SUSPECT_FONT_MAPPING"
    if text.count("\ufffd") / len(text) > .02:
        return "SUSPECT_ENCODING"
    return "READABLE"


def document_period(cover_text):
    """표지의 문서 년월만 읽는다. 파일명·PDF 생성일은 사용하지 않는다."""
    match = re.search(r"(20\d{2})\s*년\s*(1[0-2]|0?[1-9])\s*월", cover_text)
    if match:
        return {"value": f"{int(match[1]):04d}-{int(match[2]):02d}", "precision": "MONTH", "verbatim": match[0]}
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    match = re.search(r"\b(" + "|".join(months) + r")\s+(20\d{2})\b", cover_text, re.I)
    if match:
        return {"value": f"{int(match[2]):04d}-{months.index(match[1].capitalize()) + 1:02d}", "precision": "MONTH", "verbatim": match[0]}
    match = re.search(r"(?<!\d)(20\d{2})[./-](0?[1-9]|1[0-2])(?:[./-]([0-2]?\d|3[01]))?(?!\d)", cover_text)
    if match:
        value = f"{int(match[1]):04d}-{int(match[2]):02d}"
        if match[3]:
            value += f"-{int(match[3]):02d}"
            date.fromisoformat(value)
        return {"value": value, "precision": "DAY" if match[3] else "MONTH", "verbatim": match[0]}
    return {"value": None, "precision": None, "verbatim": None}


def period_cutoff(period, cutoff):
    if not period["value"] or not cutoff:
        return "UNKNOWN"
    value = period["value"]
    if period["precision"] == "DAY":
        return "AFTER_CUTOFF" if value > cutoff else "WITHIN_CUTOFF"
    year, month = map(int, value.split("-"))
    start, end = f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"
    return "AFTER_CUTOFF" if start > cutoff else ("WITHIN_CUTOFF" if end <= cutoff else "OVERLAPS_CUTOFF")


def parse_html(body):
    soup = BeautifulSoup(body.decode("utf-8", errors="replace"), "html.parser")
    for element in soup.select("script,style,nav,header,footer,form,iframe,noscript"):
        element.decompose()
    selector, main = "body", None
    for candidate in ("article .entry-content", "article", "main", '[role="main"]', "body"):
        main = soup.select_one(candidate)
        if main is not None:
            selector = candidate
            break
    text = (main or soup).get_text(separator="\n", strip=True)
    return text, selector


def matches_target(text, projects):
    compact = re.sub(r"\W", "", text).casefold()
    return any(all(re.sub(r"\W", "", token).casefold() in compact for token in p.split()) for p in projects)


def relevant_segments(text, issues, source_page, html_selector=None):
    """저장된 추출 텍스트의 행·문자 위치이며 원본 파일의 바이트 위치는 아니다."""
    lines = text.splitlines(keepends=True)
    offsets, offset = [], 0
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    found = []
    for issue in issues:
        terms = [t.casefold() for t in re.findall(r"[\w]+", issue["required_evidence"]) if len(t) >= 2]
        scored = [(sum(t in line.casefold() for t in terms), n) for n, line in enumerate(lines)]
        for score, n in sorted(scored, reverse=True)[:3]:
            if not score:
                continue
            begin, end = max(0, n - 2), min(len(lines), n + 4)
            start = offsets[begin]
            stop = offsets[end] if end < len(offsets) else len(text)
            found.append({"issue_id": issue["issue_id"], "source_page": source_page,
                "locator_type": "HTML_EXTRACTED_TEXT" if html_selector else "PDF_PAGE_TRANSCRIPTION",
                "html_selector": html_selector, "line_start": begin + 1, "line_end": end,
                "character_start": start, "character_end": stop, "text": text[start:stop],
                "relevance_status": "HEURISTIC_CANDIDATE_NOT_CLAIM_VERIFICATION"})
    return found


class Preparer:
    def __init__(self, source_run, output, allow_vision=False, client=None, cache_only=False):
        self.source = Path(source_run).resolve()
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.allow_vision, self.client = allow_vision, client
        self.cache_only = cache_only
        self.input = read_json(self.source / "source_verification_input.json")
        self.executor = read_json(self.source / "executor_input.json")
        self.runtime = read_json(self.source / "runtime_tools.json")
        self.state = read_json(self.source / "state.json")
        self.issues = self.executor["search_plan"]["search_plan"]
        self.cutoff = self.input["as_of_date"]
        self.api_usage = []
        watched = list(self.source.glob("*.json")) + [Path(d["downloaded_file"]) for d in self.input["documents"]]
        self.source_hashes = {str(p.resolve()): sha(p.read_bytes()) for p in watched if p.is_file()}
        snapshot = self.output / "source_snapshot.json"
        if snapshot.exists() and read_json(snapshot) != self.source_hashes:
            raise ValueError("재개 입력이 최초 스냅샷과 다릅니다.")
        write_json(snapshot, self.source_hashes)
        self.proof_hashes = {str(p.resolve()): sha(p.read_bytes()) for p in (self.source / "raw_retrieval").glob("*.json")}
        proof_snapshot = self.output / "source_proofs_snapshot.json"
        if proof_snapshot.exists() and read_json(proof_snapshot) != self.proof_hashes:
            raise ValueError("보관된 출처 연결 근거가 변경되었습니다.")
        write_json(proof_snapshot, self.proof_hashes)
        (self.output / "used_codex_prompt.txt").write_text(CODEX_PROMPT, encoding="utf-8")

    def log(self, event, **values):
        with (self.output / "execution_log.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": now(), "event": event, **values}, ensure_ascii=False) + "\n")

    def api(self, folder, name, system, user, schema, images=None):
        cache = folder / (name + "_response.json")
        cache_hit = cache.exists()
        write_json(folder / (name + "_request.json"), {"system_prompt": system, "user_prompt": user,
            "model_requested": DEFAULT_MODEL, "image_manifest": images or [], "schema": schema.model_json_schema(),
            "provenance": "RECONSTRUCTED_CONTEXT_FOR_CACHED_RESPONSE" if cache_hit else "CURRENT_API_REQUEST_CONTEXT"})
        if cache.exists():
            response = read_json(cache)
            self.log("API_CACHE_REUSED", operation=name, document_id=folder.name, response_id=response.get("response_id"))
        else:
            if self.cache_only:
                raise ValueError("캐시 전용 재개에서 API 응답이 없는 작업은 실행하지 않습니다.")
            self.log("API_STARTED", operation=name, model=DEFAULT_MODEL, document_id=folder.name)
            if images:
                response = request_image_verification(system, user, images, schema, client=self.client, max_output_tokens=24000)
            else:
                response = request_structured_output(system, user, schema, client=self.client, max_output_tokens=6000)
            write_json(cache, response)
        self.api_usage.append({"document_id": folder.name, "operation": name, "cache_reused": cache_hit,
            **{k: response.get(k) for k in ("model", "response_id", "request_id", "usage", "api_status")}})
        if response["api_status"] != "completed" or response["parsed"] is None:
            raise ValueError("문서 처리 API 완료 응답 없음")
        return schema.model_validate(response["parsed"]).model_dump(mode="json")

    def render(self, pdf, numbers, folder, scale):
        images = []
        for number in numbers:
            path = folder / (f"page_{number:04d}_{scale}.png")
            if not path.exists():
                pdf[number - 1].get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False).save(str(path))
            images.append({"document_id": folder.name, "source_page": number, "path": str(path), "sha256": sha(path.read_bytes())})
        return images

    def pdf_pages(self, doc, body, folder):
        pdf = fitz.open(stream=body, filetype="pdf")
        if pdf.needs_pass:
            raise ValueError("암호화 PDF")
        native = {n + 1: page.get_text(sort=True) for n, page in enumerate(pdf)}
        qualities = {n: text_quality(text) for n, text in native.items()}
        poor = sum(q != "READABLE" for q in qualities.values()) > len(pdf) / 3
        write_json(folder / "native_text_diagnostic.json", {"page_count": len(pdf), "qualities": qualities,
            "pdf_metadata_hints_only": pdf.metadata, "requires_ocr": poor})
        allowed = tuple(t["issue_id"] for t in self.issues)
        choice = create_model("ScopedPageChoice", __base__=PageChoice,
            source_page=(int, Field(..., ge=1, le=len(pdf))), issue_ids=(list[Literal[allowed]], Field(..., min_length=1)))
        schema = create_model("ScopedScreening", __base__=Screening, selected_pages=(list[choice], Field(..., max_length=6)))
        context = {"document_id": doc["document_id"], "target_projects": self.input["target_projects"],
            "issues": self.issues, "page_count": len(pdf), "purpose": "관련 페이지 선별만 수행. Claim 추출·검증 아님."}
        if poor:
            if not self.allow_vision:
                return [], {"document_role": "PRIMARY_DOCUMENT", "target_relation": "UNCERTAIN", "selected_pages": [],
                    "uncertainty_flags": ["OCR_REQUIRED_VISION_NOT_ENABLED"]}, native.get(1, ""), qualities, pdf.metadata
            previews = self.render(pdf, list(native), folder, .55)
            screen = self.api(folder, "screen", DOCUMENT_SCREEN_SYSTEM_PROMPT, json.dumps(context, ensure_ascii=False), schema, previews)
        else:
            context["page_previews"] = [{"source_page": n, "text": text[:2200]} for n, text in native.items()]
            if not self.allow_vision:
                # API 없는 경로는 기계적인 후보 페이지만 제공하고 의미 적합성은 미확인으로 둔다.
                candidates = [n for n, text in native.items() if matches_target(text, self.input["target_projects"])]
                screen = {"document_role": "PRIMARY_DOCUMENT", "target_relation": "UNCERTAIN",
                    "selected_pages": [{"source_page": n, "issue_ids": list(allowed), "reason": "LEXICAL_TARGET_MATCH_ONLY"} for n in candidates[:6]],
                    "uncertainty_flags": ["SEMANTIC_SCREENING_NOT_PERFORMED"]}
            else:
                screen = self.api(folder, "screen", DOCUMENT_SCREEN_SYSTEM_PROMPT, json.dumps(context, ensure_ascii=False), schema)
        choices = screen["selected_pages"]
        numbers = [p["source_page"] for p in choices]
        if len(set(numbers)) != len(numbers):
            raise ValueError("선택 페이지 중복")
        prepared = []
        cover = native.get(1, "")
        if poor and numbers:
            required = sorted(set([1] + numbers))
            images = self.render(pdf, required, folder, 1.7)
            write_json(folder / "ocr_image_manifest.json", images)
            result = self.api(folder, "ocr", PREPARATION_OCR_SYSTEM_PROMPT,
                json.dumps({"document_id": doc["document_id"], "required_pages": required, "instruction": "지정 이미지 전체를 정확하게 전사. 수치·의미 판단 없음."}, ensure_ascii=False), OCRResult, images)
            if Counter(p["source_page"] for p in result["pages"]) != Counter(required):
                raise ValueError("OCR 페이지 누락·중복·추가")
            transcribed = {p["source_page"]: p for p in result["pages"]}
            if any(not p["text"].strip() for p in result["pages"]):
                raise ValueError("빈 OCR 응답")
            cover = transcribed[1]["text"]
            for number in numbers:
                prepared.append({"source_page": number, "text": transcribed[number]["text"],
                    "uncertainty_flags": transcribed[number]["uncertainty_flags"], "extraction_method": "VISION_OCR"})
        else:
            prepared = [{"source_page": n, "text": native[n], "uncertainty_flags": [], "extraction_method": "NATIVE_PDF_TEXT"} for n in numbers]
        for page in prepared:
            page["related_issue_ids"] = next(p["issue_ids"] for p in choices if p["source_page"] == page["source_page"])
            page["source_locator"] = {"type": "PHYSICAL_PDF_PAGE", "page": page["source_page"]}
        pdf.close()
        return prepared, screen, cover, qualities, {"note": "PDF creation/modification metadata are hints only"}

    def process(self, doc):
        folder = self.output / "documents" / doc["document_id"]
        folder.mkdir(parents=True, exist_ok=True)
        cached = folder / "validation.json"
        if cached.exists():
            return read_json(cached)
        row = {"document_id": doc["document_id"], "url": doc["url"], "title": doc["title"],
            "decision": "EXCLUDED", "hash_check": "NOT_CHECKED", "related_issue_ids": [],
            "reason_codes": [], "flags": [], "analysis_bundle": None, "analysis_request": None,
            "claims_verified": False, "source_file": doc["downloaded_file"]}
        path = Path(doc["downloaded_file"])
        try:
            body = path.read_bytes()
            actual_hash = sha(body)
            row.update(actual_sha256=actual_hash, expected_sha256=doc["sha256"])
            if actual_hash != doc["sha256"] or len(body) != doc["bytes"]:
                row.update(hash_check="MISMATCH", reason_codes=["FILE_INTEGRITY_MISMATCH"])
                return row
            row["hash_check"] = "MATCH"
            host = urlsplit(doc["url"]).hostname
            provenance = self.runtime.get("official_host_provenance", {})
            observed = any(urlsplit(url).hostname == host for url in provenance.get("observed_urls", []))
            proofs = []
            for proof_path, digest in self.proof_hashes.items():
                response = read_json(proof_path).get("response", "")
                if isinstance(response, str) and host in response:
                    proofs.append({"file": proof_path, "sha256": digest})
            supported = host in self.runtime.get("official_hosts", []) and observed and bool(proofs)
            row["origin_check"] = "SUPPORTED_BY_ARCHIVED_OFFICIAL_HOST_LINKS" if supported else "UNCONFIRMED"
            row["origin_provenance"] = self.runtime.get("official_host_provenance")
            row["archived_origin_proof_files"] = proofs
            published = None
            if body.startswith(b"%PDF-"):
                pages, screen, cover, quality, _ = self.pdf_pages(doc, body, folder)
                period = document_period(cover)
                period.update(source_page=1, source_text=cover, basis="DOCUMENT_COVER_NOT_PUBLIC_POSTING_TIMESTAMP")
                row.update(format="PDF", document_period=period, text_quality=quality, publication_date_status="UNCONFIRMED",
                    published_at=None, publication_precision=None, publication_date_provenance=None,
                    document_period_cutoff=period_cutoff(period, self.cutoff), screening=screen)
                row["flags"] += ["OFFICIAL_PUBLICATION_DATE_UNCONFIRMED", "PDF_METADATA_AND_FILENAME_NOT_PUBLICATION_PROOF"]
            else:
                text, selector = parse_html(body)
                published = publication_date(body.decode("utf-8", errors="replace"))
                is_index = any(e.get("operation") == "DISCOVER_DOCUMENT_LINKS" and e.get("parent_url") == doc["url"]
                               for e in self.state["retrieval_events"])
                related = matches_target(text, self.input["target_projects"])
                any_target_token = any(token.casefold() in text.casefold() for p in self.input["target_projects"] for token in p.split())
                relation = "DIRECT" if related else ("UNCERTAIN" if any_target_token else "UNRELATED")
                screen = {"document_role": "INDEX" if is_index else "PRIMARY_DOCUMENT", "target_relation": relation,
                    "selected_pages": [{"source_page": 1, "issue_ids": [i["issue_id"] for i in self.issues], "reason": "HTML_PRIMARY_CONTENT_LEXICAL_SCREEN"}],
                    "uncertainty_flags": ["CONTENT_AS_OF_UNCONFIRMED"]}
                pages = [{"source_page": 1, "text": text, "uncertainty_flags": [], "extraction_method": "HTML_PRIMARY_TEXT",
                    "related_issue_ids": [i["issue_id"] for i in self.issues],
                    "source_locator": {"type": "HTML_SINGLE_TEXT_SURROGATE", "selector": selector, "note": "HTML has no physical PDF page"}}]
                row.update(format="HTML", screening=screen, published_at=published, publication_precision="DAY" if published else None,
                    publication_date_provenance="SAVED_HTML_PUBLICATION_METADATA" if published else None,
                    publication_date_status="STATED_IN_SAVED_HTML" if published else "UNCONFIRMED")
                row["flags"].append("PAGE_PUBLICATION_DATE_IS_NOT_LINKED_DOCUMENT_DATE_OR_CONTENT_AS_OF")
            role, relation = screen["document_role"], screen["target_relation"]
            row["related_issue_ids"] = sorted({i for p in pages for i in p["related_issue_ids"]})
            row["flags"] += screen["uncertainty_flags"]
            if role in {"INDEX", "UNRELATED"} or relation == "UNRELATED":
                row["reason_codes"] = ["INDEX_NOT_EVIDENCE_DOCUMENT" if role == "INDEX" else "UNRELATED_DOCUMENT"]
                return row
            if published and self.cutoff and published > self.cutoff:
                row["reason_codes"] = ["PUBLISHED_AFTER_CUTOFF"]
                return row
            row["decision"] = "READY" if supported and published and pages and relation in {"DIRECT", "GENERAL_CONTEXT"} else "HOLD"
            row["reason_codes"] = []
            if not supported:
                row["reason_codes"].append("OFFICIAL_ORIGIN_UNCONFIRMED")
            if not published:
                row["reason_codes"].append("PUBLICATION_DATE_UNCONFIRMED")
            if relation == "UNCERTAIN":
                row["reason_codes"].append("RELEVANCE_UNCONFIRMED")
            if row.get("document_period_cutoff") in {"AFTER_CUTOFF", "OVERLAPS_CUTOFF"}:
                row["decision"] = "HOLD"
                row["reason_codes"].append("DOCUMENT_PERIOD_OUTSIDE_OR_OVERLAPPING_CUTOFF")
            if not pages:
                row["reason_codes"].append("TEXT_OR_OCR_PREPARATION_REQUIRED")
                return row
            for page in pages:
                page["ocr_text_sha256"] = sha(page["text"].encode())
                if "[판독불가]" in page["text"] and "UNREADABLE_TEXT_MARKER" not in page["uncertainty_flags"]:
                    page["uncertainty_flags"].append("UNREADABLE_TEXT_MARKER")
                (folder / f"page_{page['source_page']:04d}.txt").write_text(page["text"], encoding="utf-8")
            page_flags = [f"{doc['document_id']} page {p['source_page']}: {flag}" for p in pages for flag in p["uncertainty_flags"]]
            row["flags"] += page_flags
            handoff = copy.deepcopy(self.input["initial_analysis_handoff"])
            inherited_ocr_flags = handoff.get("ocr_review_flags", [])
            handoff.update(document_ids=[doc["document_id"]], priority_pages=[p["source_page"] for p in pages])
            handoff["ocr_review_flags"] = page_flags + ["원문 전사·관련 구간 선별이며 Claim 검증 완료가 아님"]
            handoff.setdefault("extraction_instructions", []).append("이번 입력은 원문 후보이며 지표·진위·공개일·현재 실적의 검증 완료가 아님. 비교 대상 부재 시 공통 정의만 확인.")
            metadata = {"document_id": doc["document_id"], "title": doc["title"], "source_url": doc["url"],
                "published_at": published, "document_period": row.get("document_period"), "source_sha256": actual_hash,
                "source_file": str(path.resolve()), "format": row["format"], "date_flags": row["flags"],
                "inherited_original_document_ocr_flags_context_only": inherited_ocr_flags,
                "preparation_decision": row["decision"], "pages": pages}
            bundle = {"analysis_handoff": handoff, "documents": [metadata]}
            request = {"selected_pages": handoff["priority_pages"], "selection_authority": "STEP7_3_DOCUMENT_PREPARATION",
                "note": "HOLD 문서는 날짜 등 미확인 사항을 해결하거나 명시적으로 허용하기 전까지 조건부 준비만 수행. Analysis 미실행."}
            write_json(folder / "analysis_bundle.json", bundle)
            write_json(folder / "analysis_request.json", request)
            row.update(analysis_bundle=str((folder / "analysis_bundle.json").resolve()), analysis_request=str((folder / "analysis_request.json").resolve()),
                source_pages=handoff["priority_pages"])
            segments = []
            for page in pages:
                applicable = [i for i in self.issues if i["issue_id"] in page["related_issue_ids"]]
                segments += relevant_segments(page["text"], applicable, page["source_page"], page["source_locator"].get("selector"))
            write_json(folder / "relevant_segments.json", segments)
            row["relevant_segments_file"] = str((folder / "relevant_segments.json").resolve())
        except Exception as error:
            reason = "SOURCE_FILE_MISSING" if isinstance(error, FileNotFoundError) else (
                "CORRUPT_OR_UNREADABLE_DOCUMENT" if isinstance(error, fitz.FileDataError) else "DOCUMENT_OR_OCR_PROCESSING_FAILED")
            row.update(decision="HOLD" if row["hash_check"] == "MATCH" and reason == "DOCUMENT_OR_OCR_PROCESSING_FAILED" else "EXCLUDED",
                reason_codes=[reason], error=safe_error(error))
            self.log("DOCUMENT_FAILED", document_id=doc["document_id"], error_type=type(error).__name__)
        finally:
            write_json(cached, row)
        return row

    def run(self):
        self.log("PREPARATION_STARTED", policy=DOCUMENT_PREPARATION_POLICY)
        rows = []
        for doc in self.input["documents"]:
            print("문서 준비:", doc["document_id"], flush=True)
            row = self.process(doc)
            rows.append(row)
            self.log("DOCUMENT_FINISHED", document_id=doc["document_id"], decision=row["decision"], reason_codes=row["reason_codes"])
            print("  상태:", row["decision"], row["reason_codes"], flush=True)
        unchanged = all(Path(p).is_file() and sha(Path(p).read_bytes()) == digest
                        for p, digest in {**self.source_hashes, **self.proof_hashes}.items())
        if not unchanged:
            raise ValueError("원본 또는 검색 이력이 변경되었습니다.")
        entries = [{"document_id": r["document_id"], "bundle_file": r["analysis_bundle"], "selection_file": r["analysis_request"],
            "decision": r["decision"], "published_at": r.get("published_at"), "source_pages": r.get("source_pages", []),
            "related_issue_ids": r["related_issue_ids"], "bundle": read_json(r["analysis_bundle"]),
            "selection": read_json(r["analysis_request"])} for r in rows if r["analysis_bundle"]]
        write_json(self.output / "document_validation.json", {"validation_status": "COMPLETED", "documents": rows,
            "scope": "원문 무결성·출처·날짜·관련 구간·분석 준비. Claim 사실 판정 아님."})
        write_json(self.output / "analysis_input.json", {"input_format": "PER_DOCUMENT_STEP3_BUNDLES",
            "review_goal": self.input["review_goal"], "as_of_date": self.cutoff,
            "ready_documents": [r for r in entries if r["decision"] == "READY"],
            "held_documents_conditional_only": [r for r in entries if r["decision"] == "HOLD"],
            "analysis_agent_executed": False, "policy": DOCUMENT_PREPARATION_POLICY})
        write_json(self.output / "controller_observation.json", {"prepared_document_ids": [r["document_id"] for r in rows if r["decision"] == "READY"],
            "held_document_ids": [r["document_id"] for r in rows if r["decision"] == "HOLD"],
            "excluded_document_ids": [r["document_id"] for r in rows if r["decision"] == "EXCLUDED"],
            "unresolved_issues": [{"issue_id": t["issue_id"], "status": "UNRESOLVED_PENDING_ANALYSIS_AND_VERIFICATION"} for t in self.issues],
            "new_searches": 0, "search_history_unchanged": unchanged, "analysis_agent_executed": False,
            "controller_replanned": False, "comparison_performed": False,
            "next_stage": "STEP8_ANALYSIS_ONLY_FOR_ALLOWED_DOCUMENTS", "remaining_limitations": ["PDF 표지의 문서 년월은 공식 게시일과 별도", "OCR는 사실 검증이 아님", "목록 페이지 생성일은 개별 문서 날짜가 아님"]})
        totals = Counter()
        known_responses = {item.get("response_id") for item in self.api_usage}
        for file in (self.output / "documents").glob("*/*_response.json"):
            response = read_json(file)
            if response.get("response_id") not in known_responses:
                self.api_usage.append({"document_id": file.parent.name, "operation": file.name.split("_response")[0],
                    "cache_reused": True, **{k: response.get(k) for k in ("model", "response_id", "request_id", "usage", "api_status")}})
                known_responses.add(response.get("response_id"))
        for item in self.api_usage:
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                totals[key] += (item.get("usage") or {}).get(key, 0)
        report = {"status": "SUCCESS", "processed_documents": len(rows), "decisions": dict(Counter(r["decision"] for r in rows)),
            "new_web_searches": 0, "search_history_unchanged": unchanged, "analysis_agent_executed": False,
            "policy": DOCUMENT_PREPARATION_POLICY, "api_calls": self.api_usage, "token_usage": dict(totals),
            "new_api_requests_in_this_invocation": sum(not item["cache_reused"] for item in self.api_usage),
            "token_usage_basis": "사용한 고유 응답의 사용량. 캐시 재사용은 새 과금 호출이 아님.",
            "prompt_file_sha256": sha((HERE / "prompt.py").read_bytes()), "finished_at": now()}
        write_json(self.output / "run.json", report)
        self.log("PREPARATION_COMPLETED", decisions=report["decisions"], source_inputs_unchanged=unchanged)
        return report


def main():
    parser = argparse.ArgumentParser(description="STEP7-3 저장 원문 검증·전처리. 웹 검색 없음")
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=stage_data(7) / 'output')
    parser.add_argument("--allow-vision", action="store_true", help="필요한 OCR·페이지 선별에 기존 API 사용 허용. 검색·Analysis 실행 없음")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--cache-only", action="store_true", help="기존 API 응답만 사용하고 새 요청 차단")
    args = parser.parse_args()
    output = args.resume or args.output_dir / ("document_preparation_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    report = Preparer(args.source_run, output, args.allow_vision, cache_only=args.cache_only).run()
    print(json.dumps({k: report[k] for k in ("status", "processed_documents", "decisions", "token_usage")}, ensure_ascii=False, indent=2))
    print("결과 폴더:", output)


if __name__ == "__main__":
    main()
