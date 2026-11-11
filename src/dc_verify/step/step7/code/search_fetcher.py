"""공식 후보 원문 수집. 수치 추출·사실 검증은 수행하지 않는다."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import hashlib
import ipaddress
import json
import socket
import ssl
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib.robotparser import RobotFileParser

from dc_verify.step.step7.code import search_executor as executor

USER_AGENT = "DCVerifyResearch/0.1"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check_public_url(url):
    url = executor.canonical_url(url)
    host = urlsplit(url).hostname
    addresses = socket.getaddrinfo(host, urlsplit(url).port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("공개 인터넷 주소만 수집할 수 있습니다.")
    return url


def fetch_bytes(url, max_bytes=30_000_000, timeout=25, allowed_hosts=None):
    context = ssl.create_default_context()
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    opener = build_opener(NoRedirect(), HTTPSHandler(context=context))
    for _ in range(6):
        url = check_public_url(url)
        if allowed_hosts is not None and urlsplit(url).hostname not in allowed_hosts:
            raise ValueError("미확인 호스트로의 이동을 보류합니다.")
        try:
            response = opener.open(Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)
        except HTTPError as error:
            if error.code in {301, 302, 303, 307, 308} and error.headers.get("Location"):
                url = urljoin(url, error.headers["Location"])
                error.close()
                continue
            return {"status": error.code, "url": url, "content_type": error.headers.get("Content-Type", ""), "body": b""}
        with response:
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ValueError("다운로드 크기 제한 초과")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ValueError("다운로드 크기 제한 초과")
            return {"status": response.status, "url": url,
                    "content_type": response.headers.get("Content-Type", ""), "body": body}
    raise ValueError("리다이렉트 한도 초과")


class PublicationParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.dates = []
        self.json_script = False
        self.script = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag == "meta" and (attrs.get("property", "").lower() in {"article:published_time", "datepublished"}
                              or attrs.get("name", "").lower() in {"pubdate", "publishdate", "datepublished"}):
            self.dates.append(attrs.get("content", "")[:10])
        if tag == "time" and attrs.get("itemprop") == "datePublished":
            self.dates.append(attrs.get("datetime", "")[:10])
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.json_script = True
            self.script = []

    def handle_data(self, data):
        if self.json_script:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.json_script:
            try:
                self.walk(json.loads("".join(self.script)))
            except (ValueError, TypeError):
                pass
            self.json_script = False

    def walk(self, value):
        if isinstance(value, dict):
            if isinstance(value.get("datePublished"), str):
                self.dates.append(value["datePublished"][:10])
            for child in value.values():
                self.walk(child)
        elif isinstance(value, list):
            for child in value:
                self.walk(child)


def publication_date(html):
    parser = PublicationParser()
    parser.feed(html)
    dates = sorted({executor.safe_date(d) for d in parser.dates} - {None})
    return dates[0] if len(dates) == 1 else None


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])


def discover_download_links(run, parent_url, task_ids):
    """실제 다운로드 HTML의 PDF 링크만 등록한다. 링크 발견은 검색 요청으로 세지 않는다."""
    run = Path(run)
    with executor.locked(run):
        state, incoming, runtime = executor.read_json(run / "state.json"), executor.read_json(run / "executor_input.json"), executor.read_json(run / "runtime_tools.json")
        parent = next(d for d in state["documents"] if d["url"] == executor.canonical_url(parent_url))
        body = Path(parent["downloaded_file"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != parent["sha256"]:
            raise ValueError("원문 파일 해시가 바뀌었습니다.")
        parser = LinkParser()
        parser.feed(body.decode("utf-8", errors="replace"))
        tasks = {t["task_id"]: t for t in incoming["search_plan"]["search_plan"]}
        found = []
        for link in parser.links:
            url = urljoin(parent["url"], link)
            if not urlsplit(url).path.lower().endswith(".pdf") or urlsplit(url).hostname not in runtime.get("official_hosts", []):
                continue
            candidate = {"url": url, "title": urlsplit(url).path.rsplit("/", 1)[-1], "search_text": ""}
            for task_id in task_ids:
                doc_id = executor.merge_candidate(state, candidate, tasks[task_id], runtime,
                    incoming["search_plan"]["as_of_date"], {"discovery_method": "DOWNLOADED_HTML_LINK",
                    "parent_document_id": parent["document_id"], "parent_sha256": parent["sha256"], "task_id": task_id})
                found.append(doc_id)
        state["retrieval_events"].append({"operation": "DISCOVER_DOCUMENT_LINKS", "parent_url": parent["url"],
            "document_ids": sorted(set(found)), "search_budget_consumed": False})
        executor.write_json(run / "state.json", state)
    executor.finalize(run)
    return sorted(set(found))


def collect(run, urls, transport=fetch_bytes):
    run = Path(run)
    with executor.locked(run):
        state = executor.read_json(run / "state.json")
        incoming = executor.read_json(run / "executor_input.json")
        runtime = executor.read_json(run / "runtime_tools.json")
        allowed = set(runtime.get("official_hosts", []))
        robots_cache = {}
        for url in dict.fromkeys(executor.canonical_url(u) for u in urls):
            doc = next((d for d in state["documents"] if d["url"] == url), None)
            if doc is None or urlsplit(url).hostname not in allowed:
                raise ValueError("실제 발견한 공식 호스트 후보 URL만 다운로드할 수 있습니다.")
            if doc.get("downloaded_file"):
                continue
            event = {"operation": "DOWNLOAD_ORIGINAL", "url": url, "started_at": executor.now(), "search_budget_consumed": False}
            try:
                origin = urlsplit(url)
                robots_url = origin.scheme + "://" + origin.netloc + "/robots.txt"
                if robots_url not in robots_cache:
                    robots = transport(robots_url, max_bytes=1_000_000, allowed_hosts=allowed)
                    if robots["status"] == 200:
                        parser = RobotFileParser()
                        parser.parse(robots["body"].decode("utf-8", errors="replace").splitlines())
                        robots_cache[robots_url] = parser
                    elif robots["status"] in {404, 410}:
                        robots_cache[robots_url] = None
                    else:
                        raise ValueError("robots.txt 접근 정책을 확인하지 못해 수집 보류")
                    executor.write_json(run / "retrieval_policy" / (hashlib.sha256(robots_url.encode()).hexdigest()[:16] + ".json"),
                        {"url": robots_url, "http_status": robots["status"], "retrieved_at": executor.now(),
                         "body": robots["body"].decode("utf-8", errors="replace")})
                parser = robots_cache[robots_url]
                if parser is not None and not parser.can_fetch(USER_AGENT, url):
                    event.update(status="BLOCKED", reason="ROBOTS_DISALLOWED")
                    doc["access_status"] = "ROBOTS_DISALLOWED"
                    continue
                result = transport(url, allowed_hosts=allowed)
                event.update(http_status=result["status"], final_url=result["url"])
                if result["status"] != 200:
                    event.update(status="FAILED", error_type="HTTP_" + str(result["status"]))
                    doc["access_status"] = "ACCESS_DENIED" if result["status"] in {401, 403, 429} else "HTTP_FAILED"
                    continue
                body = result["body"]
                is_pdf = body.startswith(b"%PDF-")
                if (urlsplit(url).path.lower().endswith(".pdf") and not is_pdf) or not body:
                    raise ValueError("예상 원문 형식과 다름")
                text = "" if is_pdf else body.decode("utf-8", errors="replace")
                if not is_pdf and ("<title>Just a moment" in text or "<title>Access Denied" in text):
                    raise ValueError("접근 제한 페이지로 원문 수집 실패")
                digest = hashlib.sha256(body).hexdigest()
                duplicate = next((d for d in state["documents"] if d is not doc and d.get("sha256") == digest), None)
                if duplicate:
                    duplicate["related_issue_ids"] = sorted(set(duplicate["related_issue_ids"] + doc["related_issue_ids"]))
                    doc.update(duplicate_of=duplicate["document_id"], document_status="DUPLICATE_CONTENT",
                               sha256=digest, downloaded_file=duplicate["downloaded_file"], access_status="DOWNLOADED")
                else:
                    path = run / "original_documents" / (doc["document_id"] + (".pdf" if is_pdf else ".html"))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(body)
                    doc.update(downloaded_file=str(path.resolve()), sha256=digest, bytes=len(body),
                        content_type=result["content_type"], access_status="DOWNLOADED", retrieved_at=executor.now())
                reviewed = runtime.get("reviewed_documents", [])
                if any(d.get("sha256") == digest for d in reviewed):
                    doc["document_status"] = "DUPLICATE_PREVIOUSLY_REVIEWED"
                published = publication_date(text) if text else None
                if published:
                    doc.update(published_at=published, publication_date_provenance="ORIGINAL_HTML_METADATA",
                        date_status="WITHIN_CUTOFF_PROVISIONAL")
                    if incoming["search_plan"]["as_of_date"] and published > incoming["search_plan"]["as_of_date"]:
                        doc.update(date_status="AFTER_CUTOFF", document_status="EXCLUDED_AFTER_CUTOFF")
                event.update(status="SUCCESS", sha256=digest, bytes=len(body))
            except Exception as error:
                event.update(status="FAILED", error_type=type(error).__name__)
                doc["access_status"] = "COLLECTION_FAILED"
            finally:
                event["finished_at"] = executor.now()
                state["retrieval_events"].append(event)
                executor.write_json(run / "state.json", state)
    return executor.finalize(run)


def main():
    parser = argparse.ArgumentParser(description="공식 후보 원문 다운로드; 사실 검증 없음")
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--urls-file", type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.run, executor.read_json(args.urls_file))
    print(json.dumps({"downloaded_documents": sum(d.get("access_status") == "DOWNLOADED" for d in result["documents"]),
        "retrieval_failures": result["controller_observation"]["retrieval_failures"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
