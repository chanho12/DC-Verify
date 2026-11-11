"""STEP 1: prompt.py → codex.py → gpt-5.4-mini PDF OCR → data/output/."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
STEP = HERE.parent
SHARED_CODE = HERE.parents[2]  # 01_experiments/code/

from dc_verify.codex import DEFAULT_MODEL, load_config, read_pdf_file, request_pdf_ocr, safe_error
from dc_verify.step.step1.code.prompt import OCR_SYSTEM_PROMPT, OCR_USER_PROMPT


def split_pages(text: str) -> list[dict]:
    """모델이 출력한 페이지 구분만 읽고 누락된 페이지는 채우지 않는다."""
    markers = list(re.finditer(r"(?m)^--- PAGE (\d+) ---[ \t]*\r?$", text))
    return [{"input_page": int(marker[1]), "text": text[marker.end():markers[i + 1].start()
             if i + 1 < len(markers) else len(text)].strip()}
            for i, marker in enumerate(markers)]


def run_ocr(pdf_path: Path, output_root: Path, max_output_tokens=32768, client=None):
    import pymupdf

    pdf_path = Path(pdf_path).resolve()
    data = read_pdf_file(pdf_path)
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        if pdf.needs_pass:
            raise ValueError("암호를 해제한 PDF를 사용하세요.")
        count = len(pdf)
        if count == 0:
            raise ValueError("PDF에 페이지가 없습니다.")
    config = load_config()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(output_root) / f"{pdf_path.stem}_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    record = {
        "status": "RUNNING", "source_pdf": str(pdf_path),
        "source_sha256": hashlib.sha256(data).hexdigest(), "total_pdf_pages": count,
        "model_requested": DEFAULT_MODEL, "started_at": datetime.now(timezone.utc).isoformat(),
        "prompts": {"system": OCR_SYSTEM_PROMPT, "user": OCR_USER_PROMPT},
        "prompt_file_sha256": hashlib.sha256((HERE / "prompt.py").read_bytes()).hexdigest(),
        "max_output_tokens": max_output_tokens, "api_attempted": False,
    }

    def save():
        (output / "ocr.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    print(f"입력: {pdf_path.name} ({count}쪽)\n모델: {DEFAULT_MODEL}\nPDF와 prompt.py의 프롬프트를 API에 전송 중…", flush=True)
    try:
        record["api_attempted"] = True
        save()
        result = request_pdf_ocr(data, pdf_path.name, OCR_SYSTEM_PROMPT, OCR_USER_PROMPT,
                                 config=config, client=client, max_output_tokens=max_output_tokens)
        record.update(result)
        text = result["text"]
        (output / "ocr.txt").write_text(text, encoding="utf-8")
        pages = split_pages(text)
        record["pages"] = pages
        record["page_markers"] = [p["input_page"] for p in pages]
        missing = sorted(set(range(1, count + 1)) - set(record["page_markers"]))
        record["missing_page_markers"] = missing
        complete_pages = record["page_markers"] == list(range(1, count + 1)) and all(p["text"] for p in pages)
        clean_prefix = not text.split("--- PAGE ", 1)[0].strip() if pages else False
        record["status"] = ("SUCCESS" if result["api_status"] == "completed" and not result["refusals"]
                            and complete_pages and clean_prefix else "PARTIAL" if text.strip() else "FAILED")
        record["quality_note"] = "페이지 구분·비어 있지 않은 응답만 확인. 원문 글자·숫자·표의 정확도는 별도 검수 필요."
        # 중복·잘못된 페이지 구분이 있으면 파일명을 추정해서 만들지 않는다.
        if complete_pages:
            for page in pages:
                (output / f"page_{page['input_page']:04d}.txt").write_text(page["text"], encoding="utf-8")
    except Exception as error:
        record.update(status="FAILED", error=safe_error(error))
    finally:
        record["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
    print(json.dumps({"status": record["status"], "output": str(output),
        "model": record.get("model", DEFAULT_MODEL), "usage": record.get("usage"),
        "error": record.get("error"), "missing_page_markers": record.get("missing_page_markers")}, ensure_ascii=False, indent=2), flush=True)
    return record, output


def main():
    parser = argparse.ArgumentParser(description="STEP 1 — 고정 모델 gpt-5.4-mini Vision OCR")
    parser.add_argument("pdf", type=Path, nargs="?", help="생략하면 step1/data/의 PDF 한 개")
    parser.add_argument("--output-dir", type=Path, default=stage_data(1) / 'output')
    parser.add_argument("--max-output-tokens", type=int, default=32768)
    args = parser.parse_args()
    if args.pdf is None:
        candidates = [p for p in (stage_data(1)).glob("*") if p.is_file() and p.suffix.lower() == ".pdf"]
        if len(candidates) != 1:
            parser.error("step1/data/에 PDF를 한 개 넣거나 PDF 경로를 지정하세요.")
        args.pdf = candidates[0]
    try:
        record, _ = run_ocr(args.pdf, args.output_dir, args.max_output_tokens)
        return 0 if record["status"] == "SUCCESS" else 1
    except Exception as error:
        print(json.dumps(safe_error(error), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
