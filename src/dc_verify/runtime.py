"""Durable execution ledger, content-addressed artifacts, and runtime limits."""
from __future__ import annotations
import hashlib
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def tree_hash(path):
    path = Path(path)
    return fingerprint({str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(path.rglob('*')) if p.is_file() and '__pycache__' not in p.parts})


class RuntimeDenied(ValueError):
    pass


class Ledger:
    def __init__(self, path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS records (
          seq INTEGER PRIMARY KEY, run_id TEXT, kind TEXT, stage TEXT, created REAL, payload TEXT);
        CREATE TABLE IF NOT EXISTS artifact_cache (
          cache_key TEXT PRIMARY KEY, path TEXT, content_hash TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS verification_cache (
          cache_key TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS chunks (
          chunk_id TEXT PRIMARY KEY, document_id TEXT, version TEXT, content_hash TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS document_versions (
          document_id TEXT, version TEXT, run_id TEXT, created REAL, metadata TEXT,
          PRIMARY KEY(document_id, version));
        ''')
        self.db.commit()

    def record(self, run_id, kind, stage, payload):
        with self.db:
            self.db.execute('INSERT INTO records(run_id,kind,stage,created,payload) VALUES (?,?,?,?,?)',
                            (run_id, kind, str(stage), time.time(), json.dumps(payload, ensure_ascii=False, default=str)))

    def cached(self, key):
        row = self.db.execute('SELECT path,content_hash FROM artifact_cache WHERE cache_key=?', (key,)).fetchone()
        if not row or not Path(row[0]).is_dir(): return None
        return Path(row[0]) if tree_hash(row[0]) == row[1] else None

    def cache(self, key, path):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO artifact_cache VALUES (?,?,?,?)',
                            (key, str(Path(path).resolve()), tree_hash(path), time.time()))

    def close(self): self.db.close()


class RuntimeGuard:
    ALLOWED = {'retrieve_local_evidence', 'choose_verification_route', 'search_official', 'embed', 'responses'}

    def __init__(self, ledger, run_id, as_of, domains, max_calls=64, max_tokens=2_000_000, max_searches=8):
        self.ledger, self.run_id, self.as_of = ledger, run_id, as_of
        self.domains = {urlsplit(d if '://' in d else 'https://' + d).hostname for d in domains}
        self.max_calls, self.max_tokens, self.max_searches = max_calls, max_tokens, max_searches
        if min(max_calls, max_tokens, max_searches) < 0: raise RuntimeDenied('Budget must be nonnegative')
        self.calls = self.tokens = self.searches = 0

    def check(self, tool, url=None, published_at=None, token_reserve=0):
        reason = None
        if tool not in self.ALLOWED: reason = 'TOOL_NOT_ALLOWED'
        if url:
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.hostname not in self.domains or parsed.username or parsed.password:
                reason = 'ACCESS_SCOPE_DENIED'
        if published_at:
            from dc_verify.step.step8.code.source_admission import date_window
            if date_window(published_at)[1] > self.as_of: reason = 'AFTER_REVIEW_DATE'
        if tool in {'responses', 'embed'} and (self.calls >= self.max_calls or self.tokens + token_reserve > self.max_tokens):
            reason = 'MODEL_BUDGET_EXHAUSTED'
        if tool == 'search_official' and self.searches >= self.max_searches: reason = 'SEARCH_BUDGET_EXHAUSTED'
        self.ledger.record(self.run_id, 'runtime_guard', tool, {'allowed': reason is None, 'rule_id': reason or 'RUNTIME_ALLOWED'})
        if reason: raise RuntimeDenied(reason)

    def charge(self, tool):
        self.check(tool)
        if tool in {'responses', 'embed'}: self.calls += 1
        elif tool == 'search_official': self.searches += 1


def transient(error):
    return (getattr(error, 'status_code', None) in {408, 409, 429} or
            (getattr(error, 'status_code', None) or 0) >= 500 or
            type(error).__name__ in {'APIConnectionError', 'APITimeoutError', 'TimeoutError', 'ConnectError'})


class ResponsesProxy:
    def __init__(self, raw, owner): self.raw, self.owner = raw, owner

    def __getattr__(self, method):
        operation = getattr(self.raw, method)
        def call(**kwargs):
            from dc_verify.codex import DEFAULT_MODEL, PLANNING_MODEL, safe_error
            owner = self.owner
            stage = str(owner.status.get('active_stage', '0'))
            planning = stage in {'2', '6', '7-plan', '9', 'counterevidence', 'final-verifier', 'dispatch'}
            kwargs['model'] = PLANNING_MODEL if planning else DEFAULT_MODEL
            kwargs['reasoning'] = {'effort': 'low' if planning else 'none'}
            feedback = getattr(owner, 'validation_feedback', None)
            if feedback: kwargs['instructions'] = kwargs.get('instructions', '') + '\nLOCAL VALIDATION ERRORS TO CORRECT:\n' + feedback
            # Reserve output plus a conservative text/image allowance before dispatch.
            def estimate(value):
                if isinstance(value,dict):
                    if value.get('type')=='input_file':
                        import pymupdf
                        with pymupdf.open(owner.pdf) as pdf:return len(pdf)*3000
                    if value.get('type')=='input_image':return 3000
                    return sum(estimate(v) for v in value.values())
                if isinstance(value,list):return sum(estimate(v) for v in value)
                return len(str(value))
            reserve = kwargs.get('max_output_tokens', 8192) + estimate(kwargs.get('input', ''))
            for attempt in range(3):
                owner.guard.check('responses', token_reserve=reserve)
                owner.guard.charge('responses')
                owner.status['model_requests'] += 1; owner.save()
                receipt = {'operation': method, 'model': kwargs['model'], 'attempt': attempt + 1, 'status': 'STARTED'}
                owner.ledger.record(owner.root.name, 'call', stage, receipt)
                try:
                    response = operation(**kwargs)
                    usage = response.usage.model_dump() if response.usage else None
                    owner.guard.tokens += (usage or {}).get('total_tokens', 0)
                    record = {'response_id': response.id, 'model': response.model, 'status': response.status, 'usage': usage}
                    owner.status['model_responses'].append(record)
                    owner.ledger.record(owner.root.name, 'call_result', stage, record); owner.save()
                    return response
                except Exception as error:
                    receipt.update(status='FAILED', error=safe_error(error), transient=transient(error))
                    schema_failure=method=='parse' and type(error).__name__ in {'ValidationError','JSONDecodeError'}
                    owner.ledger.record(owner.root.name, 'schema_failure' if schema_failure else 'technical_failure', stage, receipt)
                    if schema_failure:raise ValueError('Structured output schema validation failed: '+str(error)[:3000]) from error
                    if not transient(error) or attempt == 2: raise
                    time.sleep(min(2 ** attempt, 4))
        return call


class GuardedClient:
    def __init__(self, raw, owner):
        self.raw = raw; self.responses = ResponsesProxy(raw.responses, owner)
        self.embeddings = raw.embeddings if hasattr(raw, 'embeddings') else None
