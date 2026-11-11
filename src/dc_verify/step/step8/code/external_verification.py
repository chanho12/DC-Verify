"""STEP 8-2. Immutable Claim review against downloaded originals, never cross-checks.

Reuses STEP4 OCR checks/rendering/FieldChecks, STEP5 ClaimAssessment and policy,
STEP8 Admission guard, and the existing fixed-model Responses API adapter.
"""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import copy
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, create_model, Field

HERE = Path(__file__).resolve().parent
# Legacy modules import their prompt by its short name.
from dc_verify.codex import DEFAULT_MODEL, request_image_verification, request_structured_output, safe_error
from dc_verify.step.step4.code.source_verifier import check_ocr, render_original_pages, FieldChecks
from dc_verify.step.step4.code.source_verifier_prompt import SOURCE_VERIFIER_SYSTEM_PROMPT
from dc_verify.step.step5.code.assess_evidence import ClaimAssessment
from dc_verify.step.step5.code.evidence_prompt import EVIDENCE_SYSTEM_PROMPT
from dc_verify.step.step7.code.document_preparer import parse_html
from dc_verify.step.step7.code.search_fetcher import publication_date
from dc_verify.step.step8.code.source_admission import guard_claims, digest, now, read_json, write_json
from dc_verify.step.step8.code.external_analysis import latest_run
from dc_verify.step.step3.code.analysis import external_semantic_issues
from dc_verify.step.step8.code.prompt import (SOURCE_SEMANTIC_INSTRUCTIONS, SOURCE_SEMANTIC_USER_PROMPT,
    SEMANTIC_AUDIT_INSTRUCTIONS, EXTERNAL_EVIDENCE_INSTRUCTIONS)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Correction(StrictModel):
    field: str
    proposed_value: str
    reason: str


class RequirementSupport(StrictModel):
    requirement: str
    support: Literal['DIRECT', 'PARTIAL', 'NONE']
    reason: str


class ClaimReview(StrictModel):
    claim_id: str
    source_page: int
    source_status: Literal['VERIFIED', 'MISMATCH', 'NEEDS_MANUAL_REVIEW', 'NOT_VERIFIED', 'VERIFICATION_FAILED']
    original_quote: Optional[str]
    quote_relation: Literal['EXACT', 'WHITESPACE_ONLY', 'TRANSCRIPTION_DIFFERENCE', 'NOT_FOUND', 'UNCLEAR']
    source_reason: str
    semantic_status: Literal['MATCH', 'MISMATCH', 'UNCLEAR', 'NOT_ASSESSED']
    field_checks: FieldChecks
    semantic_reason: str
    interpreted_metric: str
    uncertainties: list[str]
    quarantine_classification: Literal['NOT_QUARANTINED', 'FORMAT_ONLY', 'REFERENCE_ERROR', 'SEMANTIC_ERROR', 'MIXED', 'UNRESOLVED']
    correction_proposals: list[Correction]
    requirement_support: list[RequirementSupport]
    evidence: ClaimAssessment


class DocumentReview(StrictModel):
    document_id: str
    cover_period_observation: Optional[str]
    publication_uncertainties: list[str]
    claim_results: list[ClaimReview]


class SemanticAuditRow(StrictModel):
    claim_id: str
    semantic_status: Literal['MATCH', 'MISMATCH', 'UNCLEAR', 'NOT_ASSESSED']
    field_checks: FieldChecks
    semantic_reason: str
    interpreted_metric: str
    uncertainties: list[str]
    quarantine_classification: Literal['NOT_QUARANTINED', 'FORMAT_ONLY', 'REFERENCE_ERROR', 'SEMANTIC_ERROR', 'MIXED', 'UNRESOLVED']
    correction_proposals: list[Correction]


class SemanticAudit(StrictModel):
    claim_results: list[SemanticAuditRow]


def normalize(text):
    """Whitespace only; never normalize punctuation, digits, labels or units."""
    return re.sub(r'\s+', '', text or '')


def html_format_proof(claim, html, check):
    """Narrow deterministic proof of whitespace/unit-field extraction failures.

    Does not establish scope/meaning or release quarantine. No unit conversion.
    """
    if html is None or check is None or not check['issues']:
        return False
    if not set(check['issues']) <= {'RAW_VALUE_MISMATCH', 'METRIC_NAME_MISMATCH', 'OCR_UNIT_MISMATCH'}:
        return False
    quote = normalize(claim['evidence_quote'])
    if not quote or quote not in normalize(html['text']):
        return False
    return (normalize(claim['metric_name']) in quote and normalize(claim['raw_value']) in quote
        and (not claim.get('unit') or normalize(claim['raw_value'] + claim['unit']) in quote
             or normalize(claim['unit']) in normalize(claim['raw_value'])))


def system_prompt():
    return ('\n\n'.join([
        SOURCE_VERIFIER_SYSTEM_PROMPT.split('출력 형식:')[0],
        EVIDENCE_SYSTEM_PROMPT.split('[출력]')[0], SOURCE_SEMANTIC_INSTRUCTIONS]))


def load_jobs(source):
    source = Path(source)
    incoming = read_json(source / 'verification_input.json')
    accepted = read_json(source / 'external_claims.json')['claims']
    quarantined = read_json(source / 'quarantined_claims.json')['claims']
    admissions = read_json(source / 'source_admission_snapshot.json')
    jobs = copy.deepcopy(incoming['verification_jobs'])
    if not jobs or len({j['document_id'] for j in jobs}) != len(jobs):
        raise ValueError('Empty/duplicate verification document jobs')
    expected = [c['claim']['claim_id'] for c in accepted + quarantined]
    if len(expected) != len(set(expected)):
        raise ValueError('Duplicate accepted/quarantined Claim IDs')
    for job in jobs:
        doc_id = job['document_id']
        doc = job['source_bundle']['documents'][0]
        admission = job['source_admission']
        if admission not in admissions or admission['analysis_admission'] == 'DENY':
            raise ValueError('Invalid source admission snapshot')
        if doc['document_id'] != doc_id or digest(Path(doc['source_file']).read_bytes()) != admission['source_sha256']:
            raise ValueError('Original document identity/hash mismatch')
        if doc['source_sha256'] != admission['source_sha256'] or doc['published_at'] != admission['published_at']:
            raise ValueError('Document metadata differs from admission')
        for proof in admission.get('archived_origin_proof_files', []):
            if digest(Path(proof['file']).read_bytes()) != proof['sha256']:
                raise ValueError('Archived origin provenance hash mismatch')
        originals = job['analysis_result']['claims']
        if guard_claims(originals, admissions) != job['guarded_claims']:
            raise ValueError('Accepted Claim guard snapshot differs')
        group = [c for c in accepted if c['claim']['source_document_id'] == doc_id]
        if group != job['guarded_claims']:
            raise ValueError('Accepted Claim inventory differs')
        rejected = [c for c in quarantined if c['document_id'] == doc_id]
        job['review_claims'] = ([{'claim': c['claim'], 'original_group': 'ACCEPTED', 'quarantine_reasons': []} for c in group]
            + [{'claim': c['claim'], 'original_group': 'QUARANTINED', 'quarantine_reasons': c['reasons']} for c in rejected])
        allowed = set(admission['allowed_source_pages'])
        for item in job['review_claims']:
            c = item['claim']
            if c['source_document_id'] != doc_id or c['source_page'] not in allowed:
                raise ValueError('Claim outside admitted source scope')
        for p in doc['pages']:
            if digest(p['text'].encode()) != p['ocr_text_sha256']:
                raise ValueError('OCR page hash mismatch')
    actual = [c['claim']['claim_id'] for j in jobs for c in j['review_claims']]
    if Counter(actual) != Counter(expected):
        raise ValueError('Not all accepted/quarantined Claims were scheduled')
    return incoming, jobs


def prepare_original(job, folder):
    doc = job['source_bundle']['documents'][0]
    raw = Path(doc['source_file']).read_bytes()
    if digest(raw) != doc['source_sha256']:
        raise ValueError('Original changed before source preparation')
    claims = [x['claim'] for x in job['review_claims']]
    pages = [{**p, 'document_id': job['document_id']} for p in doc['pages']]
    checks = check_ocr(claims, pages)
    images, failures, html = [], {}, None
    if doc['format'].upper() == 'PDF':
        # Cover is date evidence only, never an additional extraction target.
        render_claims = claims + [{'source_page': 1}]
        images, failures = render_original_pages(doc['source_file'],
            {'document_id': job['document_id'], 'source_pdf_sha256': doc['source_sha256']},
            render_claims, folder / 'original_pages', dpi=170)
    elif doc['format'].upper() == 'HTML':
        text, selector = parse_html(raw)
        html = {'text': text, 'selector': selector, 'source_sha256': digest(raw),
            'locator': 'Original saved HTML DOM, virtual text page 1; not a PDF image'}
    else:
        raise ValueError('Unsupported original source type')
    write_json(folder / 'original_manifest.json', {'source_file': doc['source_file'],
        'source_sha256': digest(raw), 'images': images, 'render_failures': failures, 'original_html': html})
    return {'document_id': job['document_id'], 'review_context': job['source_bundle']['analysis_handoff'],
        'document_metadata': {k: v for k, v in doc.items() if k != 'pages'},
        'claims': job['review_claims'], 'ocr_pages': pages, 'mechanical_ocr_checks': checks,
        'ORIGINAL_HTML': html, 'render_failures': failures,
        'available_evidence': {'additional_documents': [], 'independent_evidence': []}}, images


def enforce_review(value, job, prepared, images, require_support=True):
    """Validate coverage and apply conservative gates, keeping API response separately."""
    result = DocumentReview.model_validate(value).model_dump(mode='json')
    originals = {x['claim']['claim_id']: x for x in job['review_claims']}
    if result['document_id'] != job['document_id'] or Counter(r['claim_id'] for r in result['claim_results']) != Counter(originals.keys()):
        raise ValueError('Response Claim/document coverage mismatch')
    required = job['source_bundle']['analysis_handoff']['required_metrics']
    image_pages = {i['source_page'] for i in images}
    html = prepared['ORIGINAL_HTML']
    admission = job['source_admission']
    gates = []
    for row in result['claim_results']:
        entry = originals[row['claim_id']]
        c = entry['claim']
        if row['source_page'] != c['source_page'] or row['evidence']['claim_id'] != c['claim_id']:
            raise ValueError('Response changed source page/evidence Claim ID')
        local = []
        # Bind a uniquely matching omitted parenthetical qualification only.
        # No support/reason judgment is filled in or inferred by this binding.
        for support in row['requirement_support']:
            label = support['requirement']
            if label not in required:
                candidates = [r for r in required if label == re.split(r'[（(]', r, maxsplit=1)[0].strip()]
                if len(candidates) == 1:
                    support['requirement'] = candidates[0]
                    local.append('UNIQUE_REQUIREMENT_LABEL_BOUND: ' + label)
        if require_support and Counter(x['requirement'] for x in row['requirement_support']) != Counter(required):
            raise ValueError('Requirement support coverage mismatch')
        if entry['original_group'] == 'ACCEPTED' and row['quarantine_classification'] != 'NOT_QUARANTINED':
            row['quarantine_classification'] = 'NOT_QUARANTINED'
            local.append('ACCEPTED_GROUP_PRESERVED_SEMANTIC_RESULT_UNCHANGED')
        if entry['original_group'] == 'QUARANTINED' and row['quarantine_classification'] == 'NOT_QUARANTINED':
            row['quarantine_classification'] = 'UNRESOLVED'
            local.append('QUARANTINE_GROUP_PRESERVED_CLASSIFICATION_UNRESOLVED')
        if not row['source_reason'].strip() or not row['semantic_reason'].strip():
            raise ValueError('Missing review reason')
        if html is None and c['source_page'] not in image_pages:
            row['source_status'] = 'NOT_VERIFIED'
            local.append('ORIGINAL_PDF_IMAGE_MISSING')
        if row['source_status'] == 'VERIFIED' and not row['original_quote']:
            row['source_status'] = 'NEEDS_MANUAL_REVIEW'
            local.append('ORIGINAL_QUOTE_MISSING')
        if html is not None and row['source_status'] == 'VERIFIED' and normalize(row['original_quote']) not in normalize(html['text']):
            row['source_status'] = 'NEEDS_MANUAL_REVIEW'
            local.append('QUOTE_NOT_BOUND_TO_ORIGINAL_HTML')
        if row['source_status'] == 'VERIFIED' and row['quote_relation'] in {'NOT_FOUND', 'UNCLEAR', 'TRANSCRIPTION_DIFFERENCE'}:
            row['source_status'] = 'NEEDS_MANUAL_REVIEW'
            local.append('SOURCE_QUOTE_RELATION_NOT_MATCHED')
        if row['semantic_status'] == 'MATCH' and any(s in {'MISMATCH', 'UNCLEAR'} for s in row['field_checks'].values()):
            row['semantic_status'] = 'MISMATCH' if 'MISMATCH' in row['field_checks'].values() else 'UNCLEAR'
            local.append('FIELD_CHECKS_NOT_ALL_MATCHED')
        contract_errors = external_semantic_issues(c)
        if contract_errors:
            row['semantic_status'] = 'MISMATCH'
            row['uncertainties'] = list(dict.fromkeys(row['uncertainties'] + contract_errors))
            if entry['original_group'] == 'QUARANTINED':
                row['quarantine_classification'] = ('MIXED' if row['quarantine_classification'] in {'FORMAT_ONLY', 'REFERENCE_ERROR', 'MIXED'} else 'SEMANTIC_ERROR')
            local.append('SHARED_ANALYSIS_METRIC_CONTRACT')
        if c['metric_type'] in {'occupancy_rate', 'contract_ratio'} and c.get('value') is None and row['semantic_status'] == 'MATCH':
            row['semantic_status'] = 'UNCLEAR'
            row['uncertainties'].append('TEXTUAL_RATIO_WITHOUT_EXPLICIT_VALUE_OR_DENOMINATOR: 정성 표현의 정의·분모를 확인해야 함')
            local.append('TEXTUAL_RATIO_IS_NOT_A_CONFIRMED_NUMERIC_RATIO')
        format_proof = html_format_proof(c, html, prepared.get('mechanical_ocr_checks', {}).get(c['claim_id']))
        if format_proof and not contract_errors and entry['original_group'] == 'QUARANTINED':
            row.update(source_status='VERIFIED', original_quote=c['evidence_quote'], quote_relation='WHITESPACE_ONLY')
            row['source_reason'] += ' [로컬 검사: 원본 HTML에 숫자·단위·인용이 공백 제거만으로 일치함]'
            row['quarantine_classification'] = 'FORMAT_ONLY'
            # The model disagreed; format proof is deliberately not semantic approval.
            if row['semantic_status'] == 'MISMATCH':
                row['semantic_status'] = 'UNCLEAR'
            local.append('ORIGINAL_HTML_FORMAT_PROOF_NO_SEMANTIC_PROMOTION')
        evidence = row['evidence']
        if row['source_status'] != 'VERIFIED' or row['semantic_status'] != 'MATCH':
            evidence.update(evidence_level='NOT_ASSESSED', supports_review_goal=False, review_required=True)
            evidence['missing_evidence'] = list(dict.fromkeys(evidence['missing_evidence'] + ['원문·의미 재검토 완료 필요']))
            local.append('SOURCE_OR_SEMANTIC_GATE')
        if evidence['evidence_level'] in {'ADDITIONAL_DOCUMENT_SUPPORT', 'INDEPENDENTLY_CORROBORATED'}:
            evidence['evidence_level'] = 'SOURCE_STATEMENT_ONLY' if row['source_status'] == 'VERIFIED' and row['semantic_status'] == 'MATCH' else 'NOT_ASSESSED'
            local.append('NO_CROSS_DOCUMENT_OR_INDEPENDENT_EVIDENCE')
        if admission['analysis_admission'] == 'CONDITIONAL' or entry['original_group'] == 'QUARANTINED':
            evidence['supports_review_goal'] = False
            evidence['review_required'] = True
            local.append('ADMISSION_OR_QUARANTINE_ADOPTION_BLOCK')
        if evidence['evidence_level'] in {'NOT_ASSESSED', 'INSUFFICIENT'}:
            evidence['supports_review_goal'] = False
        row.update(original_group=entry['original_group'], original_claim=c,
            original_quarantine_reasons=entry['quarantine_reasons'], source_admission=admission['analysis_admission'],
            is_provisional=admission['provisional_claims_required'], published_at=admission['published_at'],
            source_sha256=admission['source_sha256'], final_evidence_allowed=False,
            correction_applied=False, quarantine_released=False, local_gates=local)
        row['semantic_contract_errors'] = contract_errors
        row['mechanical_ocr_check'] = prepared.get('mechanical_ocr_checks', {}).get(c['claim_id'])
        row['source_format'] = 'HTML' if html is not None else 'PDF'
        row['html_format_proof'] = format_proof
        if local:
            gates.append({'claim_id': c['claim_id'], 'gates': local})
    return result, gates


def reusable_evidence(previous, current):
    """Reuse only unchanged claims/context with unchanged or stricter eligibility.

    Local gates may downgrade old assessments, never grant new support from cache.
    """
    if any(previous[k] != current[k] for k in ['REVIEW_CONTEXT', 'SOURCE_ADMISSION', 'AVAILABLE_EVIDENCE']):
        return False
    def state(payload):
        rows = payload['SOURCE_AND_SEMANTIC_RESULT']['claim_results']
        return ({r['claim_id']: r['original_claim'] for r in rows},
            {r['claim_id'] for r in rows if r['source_status'] == 'VERIFIED' and r['semantic_status'] == 'MATCH'})
    old_claims, old_eligible = state(previous)
    new_claims, new_eligible = state(current)
    return old_claims == new_claims and new_eligible <= old_eligible


def assess_document(job, grounded, folder, client=None):
    """A separate STEP5 assessment call; no cross-document evidence is supplied.

    The source pass's free-form requirement labels are not used for adoption.
    Exact requirements are schema-constrained in this pass.
    """
    required = job['source_bundle']['analysis_handoff']['required_metrics']
    ids = [c['claim']['claim_id'] for c in job['review_claims']]
    Support = create_model('ScopedRequirementSupport', __base__=StrictModel,
        requirement=(Literal[tuple(required)], ...), support=(Literal['DIRECT', 'PARTIAL', 'NONE'], ...), reason=(str, ...))
    Row = create_model('ScopedEvidenceRow', __base__=StrictModel,
        claim_id=(Literal[tuple(ids)], ...), evidence=(ClaimAssessment, ...),
        requirement_support=(list[Support], Field(min_length=len(required), max_length=len(required))))
    Schema = create_model('DocumentEvidenceReview', __base__=StrictModel,
        claim_results=(list[Row], Field(min_length=len(ids), max_length=len(ids))))
    system = EVIDENCE_SYSTEM_PROMPT.split('[출력]')[0] + '\n' + EXTERNAL_EVIDENCE_INSTRUCTIONS
    payload = {'REVIEW_CONTEXT': job['source_bundle']['analysis_handoff'],
        'SOURCE_AND_SEMANTIC_RESULT': grounded,
        'SOURCE_ADMISSION': job['source_admission'],
        'AVAILABLE_EVIDENCE': {'additional_documents': [], 'independent_evidence': []}}
    prompt = json.dumps(payload, ensure_ascii=False)
    api_file = folder / 'evidence_api_response.json'
    if api_file.exists() and not reusable_evidence(read_json(folder / 'evidence_request.json'), payload):
        archive = folder / 'evidence_attempts' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        archive.mkdir(parents=True)
        for name in ['evidence_api_response.json', 'evidence_request.json', 'evidence_system_prompt.txt']:
            shutil.move(str(folder / name), str(archive / name))
    if not (folder / 'evidence_api_response.json').exists():
        (folder / 'evidence_system_prompt.txt').write_text(system, encoding='utf-8')
        write_json(folder / 'evidence_request.json', payload)
    if api_file.exists():
        api = read_json(api_file)
    else:
        api = request_structured_output(system, prompt, Schema, client=client, max_output_tokens=18000)
        write_json(api_file, api)
    if api.get('api_status') != 'completed' or not api.get('parsed'):
        raise ValueError('Evidence API response incomplete/unparsed')
    data = Schema.model_validate(api['parsed']).model_dump(mode='json')
    if Counter(r['claim_id'] for r in data['claim_results']) != Counter(ids):
        raise ValueError('Evidence response Claim coverage mismatch')
    for r in data['claim_results']:
        if r['claim_id'] != r['evidence']['claim_id'] or Counter(x['requirement'] for x in r['requirement_support']) != Counter(required):
            raise ValueError('Evidence response requirement/Claim coverage mismatch')
    return {r['claim_id']: r for r in data['claim_results']}


def audit_semantics(job, prepared, api_result, folder, client=None):
    payload = {'TARGETS_AND_GOAL': prepared['review_context'], 'ORIGINAL_CLAIMS': job['review_claims'],
        'SOURCE_PAGES': prepared['ocr_pages'], 'ORIGINAL_HTML': prepared['ORIGINAL_HTML'],
        'INDEPENDENT_SOURCE_TRANSCRIPTION': [{k: r[k] for k in ['claim_id', 'source_page', 'source_status', 'original_quote']}
            for r in api_result['claim_results']]}
    f = folder / 'semantic_audit_api_response.json'
    if f.exists():
        api = read_json(f)
    else:
        write_json(folder / 'semantic_audit_request.json', payload)
        (folder / 'semantic_audit_system_prompt.txt').write_text(SEMANTIC_AUDIT_INSTRUCTIONS, encoding='utf-8')
        api = request_structured_output(SEMANTIC_AUDIT_INSTRUCTIONS, json.dumps(payload, ensure_ascii=False),
            SemanticAudit, client=client, max_output_tokens=14000)
        write_json(f, api)
    if api.get('api_status') != 'completed' or not api.get('parsed'):
        raise ValueError('Semantic audit API incomplete/unparsed')
    data = SemanticAudit.model_validate(api['parsed']).model_dump(mode='json')
    if Counter(r['claim_id'] for r in data['claim_results']) != Counter(c['claim']['claim_id'] for c in job['review_claims']):
        raise ValueError('Semantic audit Claim coverage mismatch')
    audited = {r['claim_id']: r for r in data['claim_results']}
    merged = copy.deepcopy(api_result)
    for r in merged['claim_results']:
        r.update(audited[r['claim_id']])
    return merged


def review_date(job, prepared, model_result):
    doc = job['source_bundle']['documents'][0]
    admission = job['source_admission']
    observed = None
    if prepared['ORIGINAL_HTML'] is not None:
        observed = publication_date(Path(doc['source_file']).read_text(encoding='utf-8', errors='replace'))
        if observed != admission['published_at']:
            raise ValueError('Saved HTML publication metadata changed')
    return {'document_id': job['document_id'], 'source_sha256': doc['source_sha256'],
        'published_at': admission['published_at'], 'observed_saved_html_publication': observed,
        'publication_status': 'STATED_IN_SAVED_HTML' if observed else 'UNCONFIRMED',
        'document_period': doc.get('document_period'), 'cover_observation': model_result.get('cover_period_observation'),
        'model_uncertainties': model_result.get('publication_uncertainties', []),
        'analysis_admission_unchanged': admission['analysis_admission'], 'final_evidence_allowed': False,
        'cutoff_status': ('BEFORE_OR_ON_CUTOFF' if observed <= admission['as_of_date'] else 'AFTER_CUTOFF') if observed else 'UNKNOWN',
        'scope': 'Saved originals and archived provenance only; cover/filename/PDF metadata are not official posting proof.'}


def failed_document(job, error):
    rows = []
    for entry in job['review_claims']:
        c = entry['claim']
        rows.append({'claim_id': c['claim_id'], 'source_page': c['source_page'], 'source_status': 'VERIFICATION_FAILED',
            'semantic_status': 'NOT_ASSESSED', 'original_group': entry['original_group'], 'original_claim': c,
            'quarantine_classification': 'UNRESOLVED' if entry['original_group'] == 'QUARANTINED' else 'NOT_QUARANTINED',
            'source_reason': error['message'], 'semantic_reason': '기술 오류로 미평가', 'uncertainties': ['TECHNICAL_FAILURE'],
            'correction_proposals': [], 'correction_applied': False, 'quarantine_released': False,
            'source_admission': job['source_admission']['analysis_admission'], 'is_provisional': job['source_admission']['provisional_claims_required'],
            'final_evidence_allowed': False, 'evidence': {'claim_id': c['claim_id'], 'statement_type': 'UNKNOWN',
            'evidence_level': 'NOT_ASSESSED', 'supports_review_goal': False, 'reason': '기술 오류로 미평가',
            'missing_evidence': ['검증 재실행 필요'], 'review_required': True}})
    return {'document_id': job['document_id'], 'claim_results': rows, 'execution_status': 'FAILED', 'error': error}


def execute(source_run=None, output_dir=stage_data(8) / 'output', resume=None, dry_run=False, client=None):
    source = Path(source_run).resolve() if source_run else latest_run(stage_data(8) / 'output', 'external_analysis_*',
        ['verification_input.json', 'external_claims.json', 'quarantined_claims.json']).resolve()
    incoming, jobs = load_jobs(source)
    output = Path(resume).resolve() if resume else Path(output_dir) / ('external_verification_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True, exist_ok=bool(resume))
    watched = list(source.rglob('*.json')) + [Path(j['source_bundle']['documents'][0]['source_file']) for j in jobs]
    for j in jobs:
        watched += [Path(p['file']) for p in j['source_admission'].get('archived_origin_proof_files', [])]
    snapshot = {str(p.resolve()): digest(p.read_bytes()) for p in watched}
    existing = output / 'input_snapshot.json'
    if existing.exists() and read_json(existing) != snapshot:
        raise ValueError('Resume input changed')
    write_json(existing, snapshot)
    write_json(output / 'accepted_claims_snapshot.json', read_json(source / 'external_claims.json'))
    write_json(output / 'quarantined_claims_snapshot.json', read_json(source / 'quarantined_claims.json'))
    def event(name, **values):
        with (output / 'execution_log.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': now(), 'event': name, **values}, ensure_ascii=False) + '\n')
    event('STARTED', source=str(source), model=DEFAULT_MODEL, dry_run=dry_run)
    results, dates, technical = [], [], []
    for job in jobs:
        doc_id = job['document_id']
        folder = output / 'documents' / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        print(json.dumps({'document_id': doc_id, 'stage': 'PREPARING', 'claims': len(job['review_claims'])}), flush=True)
        try:
            prepared, images = prepare_original(job, folder)
            write_json(folder / 'verification_request.json', prepared)
            text = SOURCE_SEMANTIC_USER_PROMPT.format(input_json=json.dumps(prepared, ensure_ascii=False))
            if not (folder / 'api_response.json').exists():
                (folder / 'system_prompt.txt').write_text(system_prompt(), encoding='utf-8')
                (folder / 'user_prompt.txt').write_text(text, encoding='utf-8')
            if dry_run:
                continue
            api_file = folder / 'api_response.json'
            if api_file.exists():
                api = read_json(api_file)
                event('CACHED_API_RESPONSE_REUSED', document_id=doc_id)
            else:
                event('API_REQUEST_STARTED', document_id=doc_id)
                if prepared['ORIGINAL_HTML'] is None:
                    if not images or any(c['claim']['source_page'] not in {i['source_page'] for i in images} for c in job['review_claims']):
                        raise ValueError('Original PDF pages unavailable; API not called')
                    api = request_image_verification(system_prompt(), text, images, DocumentReview, client=client, max_output_tokens=22000)
                else:
                    api = request_structured_output(system_prompt(), text, DocumentReview, client=client, max_output_tokens=22000)
                write_json(api_file, api)
                event('API_RESPONSE_RECEIVED', document_id=doc_id, response_id=api.get('response_id'), usage=api.get('usage'))
            if api.get('api_status') != 'completed' or not api.get('parsed'):
                raise ValueError('API response incomplete/unparsed')
            event('SEMANTIC_AUDIT_STARTED', document_id=doc_id)
            audited = audit_semantics(job, prepared, api['parsed'], folder, client=client)
            grounded, _ = enforce_review(audited, job, prepared, images, require_support=False)
            write_json(folder / 'grounding_before_assessment.json', grounded)
            event('EVIDENCE_ASSESSMENT_STARTED', document_id=doc_id)
            assessed = assess_document(job, grounded, folder, client=client)
            merged = copy.deepcopy(audited)
            for r in merged['claim_results']:
                r['evidence'] = assessed[r['claim_id']]['evidence']
                r['requirement_support'] = assessed[r['claim_id']]['requirement_support']
            result, gates = enforce_review(merged, job, prepared, images)
            result.update(execution_status='SUCCESS', response_id=api['response_id'], local_gates=gates)
            result['model_trace'] = {name: read_json(folder / file)['response_id'] for name, file in [
                ('source', 'api_response.json'), ('semantic_audit', 'semantic_audit_api_response.json'), ('evidence', 'evidence_api_response.json')]}
            write_json(folder / 'verification_result.json', result)
            if (folder / 'technical_failure.json').exists():
                old = read_json(folder / 'technical_failure.json')
                old['resolved_by_successful_retry'] = True
                write_json(folder / 'technical_failure.json', old)
            date_result = review_date(job, prepared, result)
            write_json(folder / 'publication_date_review.json', date_result)
            dates.append(date_result)
            results.append(result)
            event('DOCUMENT_VERIFIED', document_id=doc_id, count=len(result['claim_results']))
            print(json.dumps({'document_id': doc_id, 'stage': 'SUCCESS', 'source': dict(Counter(r['source_status'] for r in result['claim_results'])),
                'semantic': dict(Counter(r['semantic_status'] for r in result['claim_results']))}), flush=True)
        except Exception as exc:
            error = safe_error(exc)
            if isinstance(exc, ValueError):
                error['message'] = str(exc)  # Controlled local messages, never secrets.
            technical.append({'document_id': doc_id, **error})
            failure = failed_document(job, error)
            write_json(folder / 'technical_failure.json', failure)
            results.append(failure)
            event('DOCUMENT_FAILED', document_id=doc_id, error=error)
            print(json.dumps({'document_id': doc_id, 'stage': 'FAILED', 'error': error}), flush=True)
    rows = [r for d in results for r in d['claim_results']]
    expected = [c['claim']['claim_id'] for j in jobs for c in j['review_claims']]
    immutable = all(p.is_file() and digest(p.read_bytes()) == h for p, h in ((Path(p), h) for p, h in snapshot.items()))
    complete = not dry_run and Counter(r['claim_id'] for r in rows) == Counter(expected)
    if not immutable:
        raise ValueError('Original/input files changed during verification')
    claims = {'verification_status': 'COMPLETED' if complete and not technical else 'PARTIAL', 'claim_count': len(rows),
        'claim_results': rows, 'cross_check_performed': False, 'final_evidence_allowed': False}
    write_json(output / 'verification_results.json', claims)
    write_json(output / 'source_verification.json', {'claim_results': [{k: r.get(k) for k in
        ['claim_id', 'source_page', 'source_status', 'original_quote', 'quote_relation', 'source_reason', 'original_group']} for r in rows]})
    write_json(output / 'semantic_verification.json', {'claim_results': [{k: r.get(k) for k in
        ['claim_id', 'semantic_status', 'field_checks', 'semantic_reason', 'interpreted_metric', 'uncertainties', 'correction_proposals']} for r in rows]})
    write_json(output / 'quarantine_review.json', {'quarantine_released': False, 'claims': [r for r in rows if r['original_group'] == 'QUARANTINED']})
    write_json(output / 'evidence_assessment.json', {'review_goal_status': 'INCOMPLETE', 'claim_assessments': [r['evidence'] for r in rows],
        'final_evidence_allowed': False, 'scope': 'Same-document issuer statements only; requirement support is separate from final adoption.'})
    write_json(output / 'publication_date_review.json', {'documents': dates})
    observation = {'review_goal_status': 'INCOMPLETE', 'source_status_counts': dict(Counter(r['source_status'] for r in rows)),
        'semantic_status_counts': dict(Counter(r['semantic_status'] for r in rows)),
        'quarantine_review_counts': dict(Counter(r['quarantine_classification'] for r in rows if r['original_group'] == 'QUARANTINED')),
        'recheck_claim_ids': [r['claim_id'] for r in rows if r['source_status'] != 'VERIFIED' or r['semantic_status'] != 'MATCH'],
        'unresolved_requirements': jobs[0]['source_bundle']['analysis_handoff']['required_metrics'],
        'technical_failures': technical, 'publication_unconfirmed_document_ids': [d['document_id'] for d in dates if d['published_at'] is None],
        'recommended_next_stage': 'STEP_8_3_CROSS_CHECK', 'controller_replanned': False, 'searches_executed': False}
    write_json(output / 'controller_observation.json', observation)
    write_json(output / 'cross_check_input.json', {'next_stage': 'STEP_8_3', 'execution_ready': complete and not technical,
        'review_context': jobs[0]['source_bundle']['analysis_handoff'], 'original_analysis_reference': incoming['original_analysis_reference'],
        'external_claim_reviews': rows, 'publication_date_review': dates,
        'original_document_jobs': [{'document_id': j['document_id'], 'source_bundle': j['source_bundle'], 'source_admission': j['source_admission']} for j in jobs],
        'constraints': ['Preserve quarantine and provisional status', 'No automatic correction/adoption', 'Source match is not independent corroboration'],
        'cross_check_performed': False})
    usage = [read_json(p).get('usage') for p in (output / 'documents').rglob('*api_response.json')]
    run = {'status': 'PREPARED' if dry_run else 'SUCCESS' if complete and not technical else 'PARTIAL', 'model': DEFAULT_MODEL,
        'source_run': str(source), 'output_dir': str(output.resolve()), 'finished_at': now(), 'documents': len(jobs), 'claims': len(rows),
        'api_responses': len(usage), 'usage': {k: sum((u or {}).get(k, 0) for u in usage) for k in ['input_tokens', 'output_tokens', 'total_tokens']},
        'originals_unchanged': immutable, 'claim_coverage_complete': complete, 'searches_executed': False, 'cross_check_performed': False}
    write_json(output / 'run.json', run)
    api_records = [{'file': str(p.resolve()), 'response_id': read_json(p).get('response_id'),
        'model': read_json(p).get('model'), 'api_status': read_json(p).get('api_status'), 'usage': read_json(p).get('usage')}
        for p in sorted((output / 'documents').rglob('*api_response.json'))]
    write_json(output / 'api_calls.json', {'responses': api_records, 'response_count': len(api_records),
        'usage': run['usage'], 'usage_recorded_for_all_responses': all(x['usage'] is not None for x in api_records),
        'includes_preserved_earlier_assessment_attempts': True})
    if not dry_run:
        verify_execution(output)
    event('FINISHED', **run)
    print(json.dumps(run, ensure_ascii=False), flush=True)
    return run


def verify_execution(output):
    """Read-only artifact audit, never calls an API or equates source match with truth."""
    output = Path(output)
    run = read_json(output / 'run.json')
    rows = read_json(output / 'verification_results.json')['claim_results']
    accepted = read_json(output / 'accepted_claims_snapshot.json')['claims']
    quarantine = read_json(output / 'quarantined_claims_snapshot.json')['claims']
    originals = {x['claim']['claim_id']: x['claim'] for x in accepted + quarantine}
    qids = {x['claim']['claim_id'] for x in quarantine}
    checks = {}
    checks['complete_unique_claim_coverage'] = Counter(r['claim_id'] for r in rows) == Counter(originals.keys())
    checks['original_claim_fields_unchanged'] = all(r['original_claim'] == originals[r['claim_id']] for r in rows)
    checks['quarantine_preserved'] = all((r['original_group'] == 'QUARANTINED') == (r['claim_id'] in qids)
        and not r['quarantine_released'] and not r['correction_applied'] for r in rows)
    checks['no_final_evidence_promotion'] = all(r['final_evidence_allowed'] is False for r in rows)
    checks['no_unsupported_corroboration'] = all(r['evidence']['evidence_level'] not in {'ADDITIONAL_DOCUMENT_SUPPORT', 'INDEPENDENTLY_CORROBORATED'} for r in rows)
    checks['source_and_semantic_gate'] = all(r['evidence']['evidence_level'] == 'NOT_ASSESSED'
        and not r['evidence']['supports_review_goal'] for r in rows if r['source_status'] != 'VERIFIED' or r['semantic_status'] != 'MATCH')
    checks['conditional_and_quarantined_not_goal_complete'] = all(not r['evidence']['supports_review_goal'] for r in rows
        if r['source_admission'] == 'CONDITIONAL' or r['claim_id'] in qids)
    checks['shared_semantic_invariants'] = all(not external_semantic_issues(r['original_claim']) for r in rows if r['semantic_status'] == 'MATCH')
    checks['no_unclear_fields_on_semantic_match'] = all(not {'MISMATCH', 'UNCLEAR'}.intersection(r['field_checks'].values()) for r in rows if r['semantic_status'] == 'MATCH')
    snapshot = read_json(output / 'input_snapshot.json')
    checks['all_original_inputs_unchanged'] = all(Path(p).is_file() and digest(Path(p).read_bytes()) == h for p, h in snapshot.items())
    bound = True
    for r in rows:
        if r['source_status'] != 'VERIFIED':
            continue
        manifest = read_json(output / 'documents' / r['original_claim']['source_document_id'] / 'original_manifest.json')
        if manifest['original_html'] is not None:
            bound &= bool(r['original_quote']) and normalize(r['original_quote']) in normalize(manifest['original_html']['text'])
        else:
            matches = [i for i in manifest['images'] if i['source_page'] == r['source_page']]
            bound &= len(matches) == 1 and digest(Path(matches[0]['path']).read_bytes()) == matches[0]['sha256']
    checks['verified_sources_bound_to_original_html_or_pdf_images'] = bool(bound)
    dates = read_json(output / 'publication_date_review.json')['documents']
    checks['unknown_publication_not_filled_from_period'] = all(d['published_at'] is None and d['cutoff_status'] == 'UNKNOWN'
        and d['analysis_admission_unchanged'] == 'CONDITIONAL' for d in dates if d['publication_status'] == 'UNCONFIRMED')
    calls = read_json(output / 'api_calls.json')['responses']
    checks['actual_fixed_model_responses'] = bool(calls) and all(c['response_id'] and c['model'].startswith(DEFAULT_MODEL)
        and c['api_status'] == 'completed' for c in calls) and len({c['response_id'] for c in calls}) == len(calls)
    checks['within_requested_stage'] = not run['searches_executed'] and not run['cross_check_performed']
    result = {'status': 'PASSED' if all(checks.values()) and run['status'] == 'SUCCESS' else 'FAILED',
        'checks': checks, 'original_accepted_count': len(accepted), 'original_quarantined_count': len(quarantine),
        'actual_response_count': len(calls), 'scope': 'Artifact/contract checks; not independent factual corroboration.'}
    write_json(output / 'execution_verification.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--output-dir', type=Path, default=stage_data(8) / 'output')
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    result = execute(**vars(args))
    return 0 if result['status'] in {'SUCCESS', 'PREPARED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
