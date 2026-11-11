"""Versioned source chunks: BM25 sparse + OpenAI dense, fused by Qdrant RRF.

BM25 is computed locally with k1=1.2, b=.75. No downloaded language model or
Qdrant Cloud subscription is required. Korean words and overlapping Hangul
bigrams are indexed; originals, offsets, table/footnote context remain intact.
"""
from __future__ import annotations
import hashlib
import json
import math
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from qdrant_client import QdrantClient, models
from dc_verify.runtime import fingerprint, transient

EMBEDDING_MODEL = 'text-embedding-3-large'
DIMENSIONS = 3072


def tokens(text):
    words = re.findall(r'[a-z0-9]+|[가-힣]+', text.casefold())
    return words + [w[i:i+2] for w in words if re.fullmatch('[가-힣]+', w) for i in range(len(w)-1)]


class EvidenceStore:
    COLLECTION = 'dc_verify_sources_v1'

    def __init__(self, path, ledger, client, guard):
        self.path, self.ledger, self.client, self.guard = Path(path), ledger, client, guard
        self.path.mkdir(parents=True, exist_ok=True)
        self.qdrant = QdrantClient(path=str(self.path))
        if not self.qdrant.collection_exists(self.COLLECTION):
            self.qdrant.create_collection(self.COLLECTION,
                vectors_config={'dense': models.VectorParams(size=DIMENSIONS, distance=models.Distance.COSINE)},
                sparse_vectors_config={'bm25': models.SparseVectorParams()})

    def embed(self, texts):
        results = []
        for text in texts:
            key = 'embedding:' + fingerprint([EMBEDDING_MODEL, DIMENSIONS, text])
            row = self.ledger.db.execute('SELECT payload FROM verification_cache WHERE cache_key=?', (key,)).fetchone()
            if row: results.append(json.loads(row[0])); continue
            for attempt in range(3):
                self.guard.check('embed', token_reserve=max(1, len(text)))
                self.guard.charge('embed')
                self.ledger.record(self.guard.run_id, 'call', 'embed', {'model': EMBEDDING_MODEL, 'attempt': attempt+1})
                try:
                    response = self.client.embeddings.create(model=EMBEDDING_MODEL, input=[text], dimensions=DIMENSIONS)
                    vector = response.data[0].embedding
                    if len(vector) != DIMENSIONS or not all(math.isfinite(n) for n in vector):
                        raise ValueError('Invalid embedding dimensions or values')
                    usage = response.usage.model_dump()
                    self.guard.tokens += usage.get('total_tokens', 0)
                    self.ledger.record(self.guard.run_id, 'call_result', 'embed', {'model': EMBEDDING_MODEL, 'usage': usage})
                    with self.ledger.db:
                        self.ledger.db.execute('INSERT OR REPLACE INTO verification_cache VALUES (?,?)', (key, json.dumps(vector)))
                    results.append(vector); break
                except Exception as error:
                    self.ledger.record(self.guard.run_id, 'technical_failure', 'embed', {'error_type': type(error).__name__})
                    if not transient(error) or attempt == 2: raise
                    time.sleep(2 ** attempt)
        return results

    def index(self, document_id, version, pages, metadata):
        previous = {r[0] for r in self.ledger.db.execute(
            'SELECT content_hash FROM chunks WHERE document_id=?', (document_id,))}
        rows = []; changed = 0
        for page in pages:
            text = page['text']; page_no = page.get('source_page', page.get('input_page',page.get('page')))
            if type(page_no) is not int or page_no<1:raise ValueError('Source chunk requires an actual positive page number')
            for start in range(0, max(1, len(text)), 5700):
                chunk = text[start:start+6000]
                if not chunk.strip(): continue
                content_hash = fingerprint(chunk)
                changed += content_hash not in previous
                point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint([document_id, version, page_no, start])))
                payload = {**metadata, 'document_id': document_id, 'version': version, 'source_page': page_no,
                    'character_start': start, 'character_end': start+len(chunk), 'text': chunk,
                    'content_hash': content_hash, 'page_context': page,
                    'source_verified': False, 'retrieval_is_evidence': False}
                rows.append((point_id, document_id, version, content_hash, json.dumps(payload, ensure_ascii=False)))
        with self.ledger.db:
            self.ledger.db.executemany('INSERT OR REPLACE INTO chunks VALUES (?,?,?,?,?)', rows)
            self.ledger.db.execute('INSERT OR REPLACE INTO document_versions VALUES (?,?,?,?,?)',
                (document_id, version, self.guard.run_id, time.time(), json.dumps(metadata, ensure_ascii=False)))
        # BM25 corpus length normalization is refreshed when the corpus changes;
        # embeddings are content-addressed, so unchanged text never incurs another embedding request.
        all_rows = list(self.ledger.db.execute('SELECT chunk_id,payload FROM chunks ORDER BY chunk_id'))
        terms = [Counter(tokens(json.loads(row[1])['text'])) for row in all_rows]
        avgdl = sum(sum(t.values()) for t in terms) / max(1, len(terms))
        for (point_id, raw), counts in zip(all_rows, terms):
            payload = json.loads(raw); dense = self.embed([payload['text']])[0]
            length = sum(counts.values())
            # Exact term registry avoids lossy hash collisions in sparse indices.
            self.ledger.db.execute('CREATE TABLE IF NOT EXISTS bm25_terms (id INTEGER PRIMARY KEY, term TEXT UNIQUE)')
            indices = []; values = []
            for term, count in counts.items():
                self.ledger.db.execute('INSERT OR IGNORE INTO bm25_terms(term) VALUES (?)', (term,))
                index = self.ledger.db.execute('SELECT id FROM bm25_terms WHERE term=?', (term,)).fetchone()[0]
                indices.append(index); values.append(count * 2.2 / (count + 1.2 * (.25 + .75 * length/max(avgdl, 1))))
            self.ledger.db.commit()
            self.qdrant.upsert(self.COLLECTION, points=[models.PointStruct(id=point_id, payload=payload,
                vector={'dense': dense, 'bm25': models.SparseVector(indices=indices, values=values)})])
        result = {'document_id': document_id, 'version': version, 'chunks': len(rows), 'changed_chunks': changed,
                  'embedding_model': EMBEDDING_MODEL, 'fusion': 'RRF', 'final_evidence_allowed': False}
        self.ledger.record(self.guard.run_id, 'source_version', 'store', result)
        return result

    def search(self, query, as_of, document_ids=None, limit=8):
        self.guard.check('retrieve_local_evidence')
        versions = self.ledger.db.execute('SELECT document_id,version,created FROM document_versions ORDER BY created DESC').fetchall()
        current = {}
        for doc, version, _ in versions:
            if doc not in current and (not document_ids or doc in document_ids): current[doc] = version
        rows = [json.loads(r[0]) for r in self.ledger.db.execute('SELECT payload FROM chunks')]
        from dc_verify.step.step8.code.source_admission import relation_to_cutoff
        allowed = [r for r in rows if current.get(r['document_id']) == r['version'] and
                   type(r.get('source_page')) is int and r['source_page']>0 and
                   relation_to_cutoff(r.get('published_at'), as_of) not in {'AFTER_CUTOFF', 'OVERLAPS_CUTOFF'}]
        if not allowed: return []
        counts = [Counter(tokens(r['text'])) for r in rows]; n = len(counts)
        indices = []; weights = []
        for term in set(tokens(query)):
            row = self.ledger.db.execute('SELECT id FROM bm25_terms WHERE term=?', (term,)).fetchone()
            if row:
                df = sum(term in c for c in counts)
                indices.append(row[0]); weights.append(math.log(1+(n-df+.5)/(df+.5)))
        # Filter by content-addressed point IDs. Unknown dates are returned as candidates only.
        ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, fingerprint([r['document_id'], r['version'], r['source_page'], r['character_start']]))) for r in allowed]
        scope = models.Filter(must=[models.HasIdCondition(has_id=ids)])
        prefetch = [models.Prefetch(query=self.embed([query])[0], using='dense', filter=scope, limit=limit*3)]
        if indices: prefetch.append(models.Prefetch(query=models.SparseVector(indices=indices, values=weights), using='bm25', filter=scope, limit=limit*3))
        points = self.qdrant.query_points(self.COLLECTION, prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF), limit=limit, with_payload=True).points
        result = [{'score': p.score, **p.payload, 'date_status': relation_to_cutoff(p.payload.get('published_at'), as_of),
                   'final_evidence_allowed': False} for p in points]
        self.ledger.record(self.guard.run_id, 'retrieval', 'store', {'query': query, 'point_ids': [str(p.id) for p in points], 'fusion': 'RRF'})
        return result

    def close(self): self.qdrant.close()
