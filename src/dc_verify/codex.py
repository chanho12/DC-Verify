"""API.env를 읽어 OpenAI에 연결하는 기본 함수. import만으로는 호출하지 않는다.

기본 실행: 설정 확인만 수행. --check-api: 모델 조회. --prompt: 실제 텍스트 생성.
request_pdf_ocr(): PDF와 전달받은 프롬프트로 Vision OCR 요청.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dc_verify.settings import env_path
ROOT = Path.cwd()
DEFAULT_MODEL = "gpt-5.4-mini"  # 사용자 지정 고정 모델. 환경변수로 바꾸지 않는다.


@dataclass(frozen=True)
class APIConfig:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    env_file: Path = env_path()
    key_source: str = ""

    def __post_init__(self):
        if self.model != DEFAULT_MODEL:
            raise ValueError(f"이 프로젝트의 모델은 {DEFAULT_MODEL}로 고정되어 있습니다.")


def load_config(env_file=None, model=None) -> APIConfig:
    """작업 디렉터리와 무관하게 DC_VERIFY_ENV_FILE 또는 작업 폴더의 API.env 사용. 키는 출력하지 않는다.

    키 우선순위: 환경 OPENAI_API_KEY → 파일 OPENAI_API_KEY → 파일 API.
    기존 API.env를 수정하거나 os.environ에 값을 덮어쓰지 않는다.
    """
    from dotenv import dotenv_values

    path = Path(env_file).expanduser().resolve() if env_file is not None else env_path()
    values = dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path.is_file() else {}
    candidates = [
        (os.getenv("OPENAI_API_KEY"), "environment:OPENAI_API_KEY"),
        (values.get("OPENAI_API_KEY"), "API.env:OPENAI_API_KEY"),
        (values.get("API"), "API.env:API"),
    ]
    key, source = next(((value.strip(), source) for value, source in candidates
                        if value and value.strip()), (None, None))
    if not key:
        raise ValueError("API 키가 없습니다. API.env에 OPENAI_API_KEY=... 또는 API=...를 설정하세요.")
    if model is not None and model != DEFAULT_MODEL:
        raise ValueError(f"모델은 {DEFAULT_MODEL}로 고정되어 있습니다.")
    return APIConfig(api_key=key, model=DEFAULT_MODEL, env_file=path, key_source=source)


def config_summary(config: APIConfig) -> dict:
    """로그나 화면에는 키 자체 대신 설정 상태만 반환."""
    return {"status": "CONFIG_LOADED", "env_file": str(config.env_file),
            "env_file_exists": config.env_file.is_file(), "api_key_configured": bool(config.api_key),
            "key_source": config.key_source, "model": config.model, "api_called": False}


def create_client(config=None, timeout=45.0):
    from openai import OpenAI

    config = config or load_config()
    return OpenAI(api_key=config.api_key, base_url="https://api.openai.com/v1",
                  timeout=timeout, max_retries=0)


def read_pdf_file(path) -> bytes:
    """PDF를 직접 읽는다. 변환·OCR·외부 조회는 여기서 수행하지 않는다."""
    path = Path(path)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("실제 PDF 파일 경로를 지정하세요.")
    if path.stat().st_size >= 50_000_000:
        raise ValueError("PDF는 50MB 미만이어야 합니다.")
    data = path.read_bytes()
    if b"%PDF-" not in data[:1024]:
        raise ValueError("PDF 헤더를 확인하지 못했습니다.")
    return data


def request_pdf_ocr(pdf_bytes: bytes, filename: str, system_prompt: str, user_prompt: str,
                    config=None, client=None, max_output_tokens=32768) -> dict:
    """PDF와 호출자가 제공한 프롬프트를 Responses API에 전송한다.

    별도 Files API 업로드 대신 요청 본문에 PDF를 base64로 전달한다.
    미완료·거절 응답도 원문을 저장할 수 있게 상태와 함께 반환한다.
    """
    if not system_prompt.strip() or not user_prompt.strip():
        raise ValueError("OCR 시스템·사용자 프롬프트를 모두 입력하세요.")
    if not 1 <= max_output_tokens <= 128000:
        raise ValueError("max_output_tokens는 1~128000으로 지정하세요.")
    if not pdf_bytes or len(pdf_bytes) >= 50_000_000 or b"%PDF-" not in pdf_bytes[:1024]:
        raise ValueError("유효한 50MB 미만 PDF 데이터가 필요합니다.")
    config = config or load_config()
    if client is None:
        with create_client(config, timeout=600.0) as owned_client:
            return request_pdf_ocr(pdf_bytes, filename, system_prompt, user_prompt,
                                   config, owned_client, max_output_tokens)
    encoded = base64.b64encode(pdf_bytes).decode("ascii")
    response = client.responses.create(
        model=DEFAULT_MODEL, instructions=system_prompt,
        input=[{"role": "user", "content": [
            {"type": "input_file", "filename": Path(filename).name,
             "file_data": "data:application/pdf;base64," + encoded, "detail": "high"},
            {"type": "input_text", "text": user_prompt},
        ]}],
        reasoning={"effort": "none"}, max_output_tokens=max_output_tokens, store=False,
    )
    refusals = [part.refusal for item in response.output if getattr(item, "type", None) == "message"
                for part in getattr(item, "content", []) if getattr(part, "type", None) == "refusal"]
    return {
        "api_status": response.status, "text": response.output_text or "",
        "model": response.model, "response_id": response.id,
        "request_id": getattr(response, "_request_id", None),
        "usage": response.usage.model_dump() if response.usage else None,
        "incomplete_details": response.incomplete_details.model_dump() if response.incomplete_details else None,
        "refusals": refusals,
    }


def request_structured_output(system_prompt, user_prompt, schema, config=None, client=None,
                              max_output_tokens=8192) -> dict:
    """Pydantic 스키마로 JSON 계획을 받는다. Agent·Tool 실행은 호출자가 결정한다."""
    if not system_prompt.strip() or not user_prompt.strip():
        raise ValueError("시스템·사용자 프롬프트가 필요합니다.")
    config = config or load_config()
    if client is None:
        with create_client(config, timeout=180.0) as owned_client:
            return request_structured_output(system_prompt, user_prompt, schema, config,
                                             owned_client, max_output_tokens)
    response = client.responses.parse(
        model=DEFAULT_MODEL, instructions=system_prompt,
        input=[{"role": "user", "content": user_prompt}], text_format=schema,
        reasoning={"effort": "none"}, max_output_tokens=max_output_tokens, store=False,
    )
    return {
        "api_status": response.status, "model": response.model, "response_id": response.id,
        "request_id": getattr(response, "_request_id", None),
        "usage": response.usage.model_dump() if response.usage else None,
        "text": response.output_text or "",
        "parsed": response.output_parsed.model_dump(mode="json") if response.output_parsed else None,
    }


def check_connection(config=None, client=None) -> dict:
    """GET으로 모델 조회 가능 여부만 확인. 생성이나 PDF 전송은 하지 않는다."""
    config = config or load_config()
    if client is None:
        with create_client(config) as owned_client:
            return check_connection(config, owned_client)
    result = client.models.retrieve(config.model)
    return {"status": "API_CONNECTED", "model": result.id,
            "request_id": getattr(result, "_request_id", None),
            "generation_performed": False, "document_uploaded": False,
            "note": "인증·모델 조회 확인 완료. 생성 API 이용 한도와 PDF 추출 품질은 미확인."}


def request_image_verification(system_prompt, user_prompt, images, schema,
                               config=None, client=None, max_output_tokens=12288):
    """문서·페이지 라벨과 원본에서 렌더링한 PNG를 Structured Outputs에 전달."""
    if not images or not system_prompt.strip() or not user_prompt.strip():
        raise ValueError("검증할 원본 이미지와 프롬프트가 필요합니다.")
    config = config or load_config()
    if client is None:
        with create_client(config, timeout=240.0) as owned_client:
            return request_image_verification(system_prompt, user_prompt, images, schema,
                                              config, owned_client, max_output_tokens)
    content = [{"type": "input_text", "text": user_prompt}]
    for item in images:
        data = Path(item["path"]).read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("렌더링된 원본 페이지는 PNG여야 합니다.")
        if item.get("sha256"):
            import hashlib
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("렌더링 이후 원본 페이지 이미지 해시가 변경됐습니다.")
        content.extend([
            {"type": "input_text", "text": "ORIGINAL_PDF_PAGE " + json.dumps(
                {"document_id": item["document_id"], "source_page": item["source_page"]}, ensure_ascii=False)},
            {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
             "detail": "high"},
        ])
    response = client.responses.parse(model=DEFAULT_MODEL, instructions=system_prompt,
        input=[{"role": "user", "content": content}], text_format=schema,
        reasoning={"effort": "none"}, max_output_tokens=max_output_tokens, store=False)
    return {"api_status": response.status, "model": response.model, "response_id": response.id,
        "request_id": getattr(response, "_request_id", None),
        "usage": response.usage.model_dump() if response.usage else None,
        "text": response.output_text or "",
        "parsed": response.output_parsed.model_dump(mode="json") if response.output_parsed else None}


def ask(prompt: str, config=None, client=None, max_output_tokens=1024) -> dict:
    """Responses API로 텍스트를 생성하는 기본 함수."""
    if not prompt.strip():
        raise ValueError("prompt를 입력하세요.")
    if not 1 <= max_output_tokens <= 32768:
        raise ValueError("max_output_tokens는 1~32768로 지정하세요.")
    config = config or load_config()
    if client is None:
        with create_client(config) as owned_client:
            return ask(prompt, config, owned_client, max_output_tokens)
    response = client.responses.create(model=config.model, input=prompt,
        max_output_tokens=max_output_tokens, store=False)
    status = getattr(response, "status", None)
    text = response.output_text
    if status != "completed" or not text or not text.strip():
        raise RuntimeError("API 응답이 미완료·거절·빈 응답 상태여서 성공으로 처리하지 않습니다.")
    usage = getattr(response, "usage", None)
    return {"status": "COMPLETED", "model": response.model, "response_id": response.id,
            "request_id": getattr(response, "_request_id", None), "text": text,
            "usage": usage.model_dump() if usage else None}


def safe_error(error: Exception) -> dict:
    """SDK 오류 본문에는 키 일부가 포함될 수 있으므로 그대로 출력하지 않는다."""
    code = getattr(error, "status_code", None)
    messages = {401: "API 키 인증에 실패했습니다.", 403: "API 권한이 없습니다.",
                404: "모델 또는 API 경로를 찾지 못했습니다.",
                429: "요청 한도 또는 이용 잔액을 확인하세요."}
    if isinstance(error, ValueError):
        message = "설정값·PDF·프롬프트를 확인하세요. 모델은 gpt-5.4-mini로 고정되어 있습니다."
    elif isinstance(error, ModuleNotFoundError):
        message = "의존성을 설치하세요: python -m pip install openai python-dotenv"
    else:
        message = messages.get(code, "API 연결 또는 응답 처리에 실패했습니다. 네트워크·설정·요청 ID를 확인하세요.")
    return {"status": "FAILED", "error_type": type(error).__name__, "http_status": code,
            "request_id": getattr(error, "request_id", None), "message": message}


def main():
    parser = argparse.ArgumentParser(description="API.env 기반 OpenAI 연결 도우미")
    parser.add_argument("--env-file", type=Path, help="기본: DC_VERIFY_ENV_FILE 또는 작업 폴더의 API.env")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="설정 확인만; 기본 동작")
    mode.add_argument("--check-api", action="store_true", help="모델 조회로 연결 확인; 생성 없음")
    mode.add_argument("--prompt", help="실제 텍스트 요청. API 이용 요금이 발생할 수 있음")
    parser.add_argument("--max-output-tokens", type=int, default=1024)
    args = parser.parse_args()
    try:
        config = load_config(args.env_file)
        if args.check_api:
            result = check_connection(config)
        elif args.prompt is not None:
            result = ask(args.prompt, config, max_output_tokens=args.max_output_tokens)
        else:
            result = config_summary(config)
    except Exception as error:
        print(json.dumps(safe_error(error), ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
