"""Python 단독 검색용 Tavily 어댑터. 세션 도구를 SDK로 가장하지 않는다.

실행 전 TAVILY_API_KEY 필요. 키가 없으면 호출·예산 차감 없이 중단한다.
이번 실제 실행은 Codex 세션 도구이며 이 어댑터의 네트워크 실행은 별도다.
"""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import json
import os
import ssl
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dc_verify.step.step7.code import search_executor as executor


def load_tavily_key(env_file=None):
    from dotenv import dotenv_values
    env_file = Path(env_file) if env_file else env_path()
    values = dotenv_values(env_file, interpolate=False) if env_file.is_file() else {}
    return (os.getenv("TAVILY_API_KEY") or values.get("TAVILY_API_KEY") or "").strip()


def tavily_search(query, key, official_hosts, cutoff):
    body = {"query": query, "search_depth": "basic", "max_results": 8,
        "include_domains": official_hosts, "include_answer": False, "include_raw_content": False}
    if cutoff:
        body["end_date"] = cutoff
    request = Request("https://api.tavily.com/search", data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    context = ssl.create_default_context()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    with urlopen(request, context=context, timeout=45) as response:
        raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ValueError("검색 응답 크기 제한 초과")
        return json.loads(raw)


def execute_api(run, key=None, transport=tavily_search):
    """등록된 PYTHON 도구로만 실행한다. 오류 응답 본문·인증 헤더는 기록하지 않는다."""
    run = Path(run)
    key = key or load_tavily_key()
    if not key:
        executor.write_json(run / "python_api_availability.json", {"available": False,
            "reason": "TAVILY_API_KEY_NOT_CONFIGURED", "api_called": False})
        raise ValueError("Python 검색용 TAVILY_API_KEY가 없습니다. 기존 OpenAI 키로 대체하지 않습니다.")
    incoming, runtime = executor.read_json(run / "executor_input.json"), executor.read_json(run / "runtime_tools.json")
    if runtime["execution_context"] != "PYTHON":
        raise ValueError("Python API 전용 실행 환경으로 새 실행을 준비하세요.")
    for task in sorted(incoming["search_plan"]["search_plan"], key=lambda t: t["priority"]):
        state = executor.read_json(run / "state.json")
        tool = executor.select_tool(task, runtime, "PYTHON")
        if not tool or tool["adapter"] != "TAVILY_HTTP":
            continue
        if not executor.remaining_attempts(incoming, state, task["issue_id"]):
            continue
        if any(a["issue_id"] == task["issue_id"] and executor.query_key(a["query_executed"]) == executor.query_key(task["query"]) for a in state["attempts"]):
            continue
        receipt = {"task_id": task["task_id"], "tool": tool["tool_id"], "query": task["query"],
            "provenance": "MOCK" if state["run_mode"] == "MOCK" else "LIVE_PYTHON_API",
            "execution_context": "PYTHON", "executed": True,
            "started_at": executor.now()}
        try:
            raw = transport(task["query"], key, runtime.get("official_hosts", []), incoming["search_plan"]["as_of_date"])
            receipt["response"] = {**raw, "results": [{**r, "search_text": r.get("content", "")} for r in raw.get("results", [])]}
        except Exception as error:
            receipt.update(response=None, error={"type": type(error).__name__,
                "http_status": error.code if isinstance(error, HTTPError) else None})
        receipt["finished_at"] = executor.now()
        path = run / "api_receipts" / (task["task_id"] + ".json")
        executor.write_json(path, receipt)
        executor.import_receipts(run, [path])
    return executor.finalize(run)


def main():
    parser = argparse.ArgumentParser(description="별도 키로 Python 검색 API 실행")
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = execute_api(args.run)
        print(json.dumps({"execution_status": result["execution_status"], "search_request_count": result["search_request_count"]}))
    except ValueError as error:
        print(str(error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
