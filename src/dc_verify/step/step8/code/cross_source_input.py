"""STEP 8-3 input validation and lineage-bound document scope restoration."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import copy
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
from dc_verify.step.step8.code.source_admission import digest, read_json
from dc_verify.step.step8.code.external_verification import load_jobs, enforce_review, review_date
from dc_verify.step.step3.code.analysis import AnalysisResult
from dc_verify.step.step4.code.source_verifier import VerificationResult
from dc_verify.step.step5.code.assess_evidence import EvidenceAssessment


class InputBlocked(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise InputBlocked(reason)


def indexed(rows, key):
    keys = [key(r) for r in rows]
    require(len(keys) == len(set(keys)), 'Duplicate identity in input inventory')
    return dict(zip(keys, rows))


def claim_key(c):
    return c['source_document_id'] + '::' + c['claim_id']


def successful_runs(base, pattern, filenames):
    runs = []
    for f in Path(base).glob(pattern + '/run.json'):
        try:
            value = read_json(f)
            if value.get('status') == 'SUCCESS':
                finished = datetime.fromisoformat(value['finished_at'].replace('Z', '+00:00'))
                # Missing files in a claimed successful latest run must block, not silently fall back.
                # Canonical paths preserve lineage equality when runtime data is mounted via symlinks.
                runs.append((finished, f.parent.resolve(), value))
        except (OSError, ValueError, KeyError):
            continue
    return sorted(runs, key=lambda x: (x[0], str(x[1])), reverse=True)


def newest_source(base):
    candidates = successful_runs(base, 'external_verification_*', [])
    require(bool(candidates), 'No successful STEP 8-2 run available')
    return candidates[0][1]


def check_snapshot(file, watched):
    snapshot = read_json(file)
    for name, sha in snapshot.items():
        p = Path(name)
        require(p.is_file() and digest(p.read_bytes()) == sha, 'Recorded input hash mismatch: ' + str(p))
        watched.add(p.resolve())


def original_chain(reference, context, watched):
    p = Path(reference['source_file']).resolve()
    require(reference.get('status') == 'PRESERVED' and p.is_file(), 'Original user Analysis missing')
    require(digest(p.read_bytes()) == reference['sha256'], 'Original Analysis reference hash mismatch')
    original = read_json(p)
    AnalysisResult.model_validate(original)
    require(read_json(p.parent / 'run.json')['status'] == 'SUCCESS', 'Original Analysis did not succeed')
    require(all(original[k] == context[k] for k in ('review_goal', 'target_projects', 'as_of_date')), 'Original review context mismatch')
    indexed(original['claims'], claim_key)
    sources = []
    rejected_sources = []
    for _, folder, run in successful_runs(stage_data(4) / 'output', 'source_verification_*', []):
        if run.get('input_hashes', {}).get('analysis') != reference['sha256']:
            continue
        inp, result = read_json(folder / 'verification_input.json'), read_json(folder / 'source_verification.json')
        try:
            VerificationResult.model_validate(result)
        except ValueError:
            rejected_sources.append({'source_run': str(folder), 'reason': 'Unsupported legacy Source schema; not used'})
            continue
        require(inp['analysis_result'] == original, 'STEP4 embedded Analysis differs from original')
        require(Counter(c['claim_id'] for c in result['claim_results']) == Counter(c['claim_id'] for c in original['claims']), 'Original source Claim ID coverage mismatch')
        sources.append((folder, inp, result))
    require(bool(sources), 'No hash-linked original Source Verification')
    for _, folder, run in successful_runs(stage_data(5) / 'output', 'evidence_*', []):
        inp, result = read_json(folder / 'evidence_input.json'), read_json(folder / 'evidence_assessment.json')
        if inp['analysis_result'] != original:
            continue
        EvidenceAssessment.model_validate(result)
        for source_folder, source_input, source_result in sources:
            if inp['source_verification'] != source_result:
                continue
            hashes = run.get('input_hashes', {})
            require(reference['sha256'] in hashes.values() and digest((source_folder / 'source_verification.json').read_bytes()) in hashes.values(), 'Original Evidence input hash lineage mismatch')
            require(Counter(r['claim_id'] for r in result['claim_assessments']) == Counter(c['claim_id'] for c in original['claims']), 'Original Evidence ID coverage mismatch')
            for f in [p, p.parent / 'run.json', source_folder / 'run.json', source_folder / 'verification_input.json',
                      source_folder / 'source_verification.json', folder / 'run.json', folder / 'evidence_input.json', folder / 'evidence_assessment.json']:
                watched.add(f.resolve())
            return original, source_input, source_result, result, {'analysis': str(p), 'source_run': str(source_folder.resolve()),
                'evidence_run': str(folder.resolve()), 'rejected_source_runs': rejected_sources}
    raise InputBlocked('No original Evidence Assessment belonging to the same Analysis/Source chain')


def find_original_pdf(sha, roots):
    for root in roots:
        for p in sorted(Path(root).rglob('*.pdf')):
            if p.is_file() and digest(p.read_bytes()) == sha:
                return p.resolve()
    raise InputBlocked('Original user PDF unavailable for recorded SHA-256')


def eligibility(row):
    if row['source_status'] != 'VERIFIED' or row['semantic_status'] != 'MATCH' or row['original_group'] == 'QUARANTINED':
        return 'DIAGNOSTIC_ONLY'
    return 'PROVISIONAL_STATEMENT' if row['is_provisional'] else 'VERIFIED_SOURCE_STATEMENT'


def wrap_claim(claim, row, doc, group):
    return {'claim_key': claim_key(claim), 'claim': copy.deepcopy(claim), 'origin': group,
        'source_status': row['source_status'], 'semantic_status': row['semantic_status'],
        'semantic_reason': row['semantic_reason'], 'original_group': row['original_group'],
        'is_provisional': row['is_provisional'], 'source_admission': row['source_admission'],
        'evidence_level': row['evidence']['evidence_level'], 'comparison_eligibility': eligibility(row),
        'source_quote': row.get('original_quote') or claim['evidence_quote'],
        'field_checks': row['field_checks'], 'uncertainties': row.get('uncertainties', []),
        'original_quarantine_reasons': row.get('original_quarantine_reasons', []),
        'final_evidence_allowed': False, 'document': doc}


def load_input(source):
    source = Path(source).resolve()
    watched = set()
    names = ['run.json', 'cross_check_input.json', 'verification_results.json', 'source_verification.json',
        'semantic_verification.json', 'evidence_assessment.json', 'quarantine_review.json', 'publication_date_review.json',
        'controller_observation.json', 'accepted_claims_snapshot.json', 'quarantined_claims_snapshot.json', 'input_snapshot.json']
    require(all((source / n).is_file() for n in names), 'STEP8-2 required artifact missing')
    watched.update((source / n).resolve() for n in names)
    run, incoming = read_json(source / 'run.json'), read_json(source / 'cross_check_input.json')
    require(run['status'] == 'SUCCESS' and incoming['execution_ready'] and incoming['next_stage'] == 'STEP_8_3', 'STEP8-2 is not executable/successful')
    require(not incoming['cross_check_performed'], 'Input unexpectedly contains previous cross-check verdict')
    check_snapshot(source / 'input_snapshot.json', watched)
    upstream = Path(run['source_run'])
    _, jobs = load_jobs(upstream)
    require(read_json(upstream / 'run.json')['status'] == 'SUCCESS', 'STEP8-1 lineage did not succeed')
    accepted, quarantined = read_json(source / 'accepted_claims_snapshot.json'), read_json(source / 'quarantined_claims_snapshot.json')
    require(accepted == read_json(upstream / 'external_claims.json') and quarantined == read_json(upstream / 'quarantined_claims.json'), 'STEP8-1 Claim snapshots differ')
    reviews = incoming['external_claim_reviews']
    require(reviews == read_json(source / 'verification_results.json')['claim_results'], 'Cross-check input differs from same-run verification result')
    by_doc = indexed(incoming['original_document_jobs'], lambda j: j['document_id'])
    require(set(by_doc) == {j['document_id'] for j in jobs}, 'External document registry coverage mismatch')
    expected = indexed([x['claim'] for x in accepted['claims'] + quarantined['claims']], claim_key)
    require(Counter(claim_key(r['original_claim']) for r in reviews) == Counter(expected.keys()), 'External Claim ID coverage mismatch')
    for r in reviews:
        require(r['original_claim'] == expected[claim_key(r['original_claim'])], 'External original Claim was changed')
    rebuilt = []
    documents = {}
    date_rows = indexed(incoming['publication_date_review'], lambda d: d['document_id'])
    require(incoming['publication_date_review'] == read_json(source / 'publication_date_review.json')['documents'], 'Publication reviews from different runs')
    for j in jobs:
        doc_id = j['document_id']
        require(by_doc[doc_id]['source_bundle'] == j['source_bundle'] and by_doc[doc_id]['source_admission'] == j['source_admission'], 'Document bundle/Admission lineage mismatch')
        doc = j['source_bundle']['documents'][0]
        folder = source / 'documents' / doc_id
        fnames = ['api_response.json', 'semantic_audit_api_response.json', 'evidence_api_response.json',
            'verification_request.json', 'original_manifest.json', 'verification_result.json']
        require(all((folder / n).is_file() for n in fnames), 'External verification request/response artifact missing')
        watched.update((folder / n).resolve() for n in fnames)
        raw, audit, evidence = (read_json(folder / n) for n in fnames[:3])
        require(all(v.get('api_status') == 'completed' and v.get('response_id') for v in [raw, audit, evidence]), 'Uncompleted upstream API response')
        merged = copy.deepcopy(raw['parsed'])
        audit_map = indexed(audit['parsed']['claim_results'], lambda r: r['claim_id'])
        evidence_map = indexed(evidence['parsed']['claim_results'], lambda r: r['claim_id'])
        require(set(audit_map) == set(evidence_map) == {c['claim']['claim_id'] for c in j['review_claims']}, 'Upstream response coverage differs')
        for row in merged['claim_results']:
            row.update(audit_map[row['claim_id']])
            row['evidence'] = evidence_map[row['claim_id']]['evidence']
            row['requirement_support'] = evidence_map[row['claim_id']]['requirement_support']
        prepared, manifest = read_json(folder / 'verification_request.json'), read_json(folder / 'original_manifest.json')
        require(digest(Path(doc['source_file']).read_bytes()) == manifest['source_sha256'] == doc['source_sha256'], 'External original hash mismatch')
        for image in manifest['images']:
            p = Path(image['path'])
            require(p.is_file() and digest(p.read_bytes()) == image['sha256'], 'Upstream original page image changed')
            watched.add(p.resolve())
        recomputed, _ = enforce_review(merged, j, prepared, manifest['images'])
        require(recomputed['claim_results'] == read_json(folder / 'verification_result.json')['claim_results'], 'Verification output not reproducible from same-run API responses')
        rebuilt += recomputed['claim_results']
        require(review_date(j, prepared, recomputed) == date_rows[doc_id], 'Publication review not reproducible')
        a = j['source_admission']
        documents[doc_id] = {k: doc.get(k) for k in ['document_id', 'title', 'source_url', 'source_file', 'source_sha256', 'format', 'published_at', 'document_period']}
        documents[doc_id].update(source_admission=copy.deepcopy(a), publication_review=date_rows[doc_id],
            independence_status='NOT_ESTABLISHED', original_group='EXTERNAL')
    require(rebuilt == reviews, 'External review order/content differs from lineage reconstruction')
    original, original_input, source_result, evidence_result, lineage = original_chain(incoming['original_analysis_reference'], incoming['review_context'], watched)
    source_rows = indexed(source_result['claim_results'], lambda r: r['claim_id'])
    evidence_rows = indexed(evidence_result['claim_assessments'], lambda r: r['claim_id'])
    for meta in original_input['document_metadata']:
        doc_id = meta['document_id']
        require(doc_id not in documents, 'Original/external document ID collision')
        pdf = find_original_pdf(meta['source_pdf_sha256'], [stage_data(1), runtime_root()])
        watched.add(pdf)
        documents[doc_id] = {'document_id': doc_id, 'title': meta['title'], 'source_url': None, 'source_file': str(pdf),
            'source_sha256': meta['source_pdf_sha256'], 'format': 'PDF', 'published_at': meta['published_at'],
            'publication_review': {'publication_status': 'ORIGINAL_METADATA_ONLY', 'provenance': meta.get('published_at_provenance')},
            'document_period': None, 'source_admission': {'analysis_admission': 'USER_PROVIDED', 'final_evidence_allowed': False},
            'independence_status': 'NOT_ESTABLISHED', 'original_group': 'USER'}
    for p in original_input['ocr_pages']:
        require(digest(p['text'].encode()) == p['ocr_text_sha256'], 'Original OCR hash mismatch')
    # STEP4's original source check also checked field meaning/context; retain that provenance.
    for image in original_input['image_manifest']:
        p = Path(image['path'])
        require(p.is_file() and digest(p.read_bytes()) == image['sha256'], 'Original source page image hash mismatch')
        watched.add(p.resolve())
    inventory = []
    for c in original['claims']:
        s, e = source_rows[c['claim_id']], evidence_rows[c['claim_id']]
        require(s['source_page'] == c['source_page'], 'Original source page changed')
        semantic = ('MISMATCH' if 'MISMATCH' in s['field_checks'].values() else 'UNCLEAR' if 'UNCLEAR' in s['field_checks'].values() else 'MATCH')
        if s['pdf_status'] != 'VERIFIED' and semantic == 'MATCH':
            semantic = 'UNCLEAR'
        row = {'source_status': s['pdf_status'], 'semantic_status': semantic, 'semantic_reason': s['reason'],
            'original_group': 'ACCEPTED', 'is_provisional': False, 'source_admission': 'USER_PROVIDED',
            'evidence': e, 'field_checks': s['field_checks'], 'original_quote': s['image_evidence_text'],
            'uncertainties': [] if s['pdf_status'] == 'VERIFIED' else s['error_types']}
        item = wrap_claim(c, row, documents[c['source_document_id']], 'USER')
        item['semantic_provenance'] = 'LEGACY_STEP4_SOURCE_AND_CONTEXT_CHECK'
        item['general_context'] = s['context_type'] == 'general_definition'
        inventory.append(item)
    for row in reviews:
        c = row['original_claim']
        item = wrap_claim(c, row, documents[c['source_document_id']], 'EXTERNAL')
        item['semantic_provenance'] = 'STEP8_2_SEMANTIC_AUDIT_AND_GUARDS'
        item['general_context'] = c.get('measurement_basis') == 'GENERAL_CONTEXT' and c['metric_type'] == 'other'
        inventory.append(item)
    indexed(inventory, lambda c: c['claim_key'])
    # Reconstruct global scope from validated registry, never from document 0's policy.
    context = copy.deepcopy(incoming['review_context'])
    repairs = [{'field': 'document_ids', 'before': context.get('document_ids'), 'after': list(documents), 'reason': 'Validated original and external document registry'}]
    for key in ['priority_pages', 'source_admission_policy', 'ocr_review_flags']:
        if key in context:
            repairs.append({'field': key, 'before': context.pop(key), 'after': 'DOCUMENT_SCOPED', 'reason': 'Document-specific field removed from global context'})
    context['document_ids'] = list(documents)
    for j in jobs:
        require(all(j['source_bundle']['analysis_handoff'][k] == context[k] for k in ['review_goal', 'target_projects', 'as_of_date', 'required_metrics']), 'Document Controller contexts differ')
    # Compare same-run split outputs as well as the full verification record.
    for name, rows, idfield in [('source_verification.json', read_json(source / 'source_verification.json')['claim_results'], 'claim_id'),
                              ('semantic_verification.json', read_json(source / 'semantic_verification.json')['claim_results'], 'claim_id'),
                              ('evidence_assessment.json', read_json(source / 'evidence_assessment.json')['claim_assessments'], 'claim_id')]:
        full = indexed(reviews, lambda r: r['claim_id'])
        require(Counter(r[idfield] for r in rows) == Counter(full.keys()), 'Split result Claim coverage mismatch: ' + name)
        for r in rows:
            target = full[r[idfield]]['evidence'] if name == 'evidence_assessment.json' else full[r[idfield]]
            require(all(target[k] == value for k, value in r.items()), 'Split result differs from same run: ' + name)
    scopes = {j['document_id']: j['source_bundle']['analysis_handoff'] for j in jobs}
    for doc in original_input['document_metadata']:
        doc_id = doc['document_id']
        scopes[doc_id] = {'document_ids': [doc_id], 'priority_pages': [p['source_page'] for p in original['analyzed_pages'] if p['document_id'] == doc_id],
            'source_admission_policy': documents[doc_id]['source_admission'], 'provenance': 'ORIGINAL_USER_ANALYSIS_AND_SOURCE_CHAIN'}
    return {'review_context': context, 'documents': documents, 'claims': inventory,
        'input_repairs': repairs, 'original_chain': lineage, 'source_run': str(source),
        'prior_missing_requirements': original['missing_required_information'],
        'prior_review_goal_status': evidence_result['review_goal_status'],
        'document_scopes': scopes,
        'file_aliases': {'source_semantic_verification.json': 'verification_results.json', 'evidence_sufficiency.json': 'evidence_assessment.json'},
        'watched_files': {str(p): digest(p.read_bytes()) for p in sorted(watched)}}
