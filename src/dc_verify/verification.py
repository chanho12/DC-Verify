"""Mechanical rule IDs plus independent challenge/final reviews. No fact promotion."""
from __future__ import annotations
import json
from typing import Literal
from pathlib import Path
from pydantic import BaseModel, ConfigDict
from dc_verify import codex
from dc_verify.runtime import fingerprint,RuntimeDenied
from dc_verify.step.step8.code.source_admission import read_json, write_json, relation_to_cutoff

RuleID = Literal['SOURCE_LOCATION', 'QUOTE_EXACT', 'NUMERIC_VALUE', 'UNIT_EXPLICIT',
                 'ORIGINAL_VERIFIED', 'DATE_CUTOFF', 'EVIDENCE_SCOPE', 'GOAL_DEFER', 'KNOWN_CLAIM_ID']


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Concern(Strict):
    rule_id: RuleID
    claim_keys: list[str]
    reason: str
    required_evidence: list[str]


class CounterReview(Strict):
    status: Literal['COMPLETED']
    concerns: list[Concern]
    limitations: list[str]


class FinalReview(Strict):
    status: Literal['PASS', 'BLOCKED', 'NEEDS_HUMAN_REVIEW']
    concerns: list[Concern]
    reason: str


def checked_review(runner, stage, prompt, payload, schema, known):
    runner.event(stage, 'RUNNING')
    for attempt in range(3):
        try:
            response = codex.request_structured_output(prompt, json.dumps(payload, ensure_ascii=False), schema, client=runner.client)
            runner.put(stage, f'response_{attempt+1}.json', response)
            if response['api_status'] != 'completed' or response['parsed'] is None: raise ValueError('Incomplete review response')
            parsed = schema.model_validate(response['parsed']).model_dump(mode='json')
            refs = {k for c in parsed['concerns'] for k in c['claim_keys']}
            if not refs <= known: raise ValueError('Unknown claim keys in review')
            runner.validation_feedback = None
            runner.put(stage, 'result.json', parsed)
            runner.ledger.record(runner.root.name, 'verifier' if stage=='final-verifier' else 'counterevidence', stage, parsed)
            runner.event(stage, 'SUCCESS'); return parsed
        except RuntimeDenied:raise
        except ValueError as error:
            runner.validation_feedback = str(error)
            runner.ledger.record(runner.root.name, 'extraction_retry', stage, {'attempt': attempt+1, 'reason': str(error)})
            if attempt == 2: raise


def mechanical_report_checks(model, expected_claims=None):
    rows = []; keys = [r['claim_key'] for r in model['evidence']]
    def add(rule, key, passed, reason, severity='ERROR'):
        rows.append({'rule_id': rule, 'claim_key': key, 'status': 'PASS' if passed else 'FAIL', 'severity': severity, 'reason': reason})
    add('KNOWN_CLAIM_ID', None, len(keys)==len(set(keys)), 'Claim keys are unique')
    expected = {c['source_document_id']+'::'+c['claim_id']:c for c in (expected_claims or [])}
    if expected:add('KNOWN_CLAIM_ID',None,set(keys)==set(expected),'Report covers exactly the validated claim inventory')
    for r in model['evidence']:
        key = r['claim_key']
        add('SOURCE_LOCATION', key, bool(r.get('document_id') and r.get('source_page') and r.get('source_sha256')), 'Document, page and original hash required')
        add('QUOTE_EXACT', key, bool(r.get('evidence_quote')), 'Quotation must be present; STEP 3/4 checks original grounding')
        if key in expected:
            original=expected[key]
            add('NUMERIC_VALUE',key,r.get('value')==original.get('value') and r['raw_value']==original['raw_value'],'Value and raw value equal the guarded Claim')
            add('UNIT_EXPLICIT',key,r.get('unit')==original.get('unit'),'Unit equals the guarded Claim; no implicit conversion')
            add('QUOTE_EXACT',key,r['evidence_quote']==original['evidence_quote'],'Quote equals the grounded Claim')
            add('SOURCE_LOCATION',key,r['source_page']==original['source_page'] and r['document_id']==original['source_document_id'],'Original document and page are unchanged')
        if r.get('final_evidence_allowed'):
            add('ORIGINAL_VERIFIED', key, r['source_status']=='VERIFIED' and r['semantic_status']=='MATCH', 'Final evidence requires source and semantic checks')
            add('DATE_CUTOFF', key, relation_to_cutoff(r.get('source_published_at'), model['review_context']['as_of_date'])=='WITHIN_CUTOFF', 'Unconfirmed publication dates cannot become final evidence')
            add('EVIDENCE_SCOPE', key, not r.get('is_provisional'), 'Provisional claims cannot become final facts')
    decision = model['decision']
    insufficient = any(r.get('controller_status') not in {'RESOLVED', 'SATISFIED'} for r in model['requirements'])
    add('GOAL_DEFER', None, not (insufficient and decision['goal_status']=='COMPLETE'), 'Unresolved requirements prevent goal completion')
    return {'status': 'PASSED' if all(r['status']=='PASS' for r in rows) else 'FAILED', 'rule_results': rows}


def incremental_source_verification(runner, analysis_path, ocr_path, pdf, output):
    """Cache each original page + exact claim semantics; only changed pages call Vision.

    PDF binary/version may change while an unchanged page's pixels and OCR remain identical.
    Cached rows are rebound only to current claim IDs after matching the entire claim payload.
    """
    import pymupdf
    from dc_verify.step.step4.code import source_verifier as v
    analysis, payload = v.load_inputs(analysis_path, ocr_path)
    if v.sha256(Path(pdf).read_bytes()) != payload['documents'][0].get('source_pdf_sha256'):
        raise ValueError('Original PDF hash mismatch; cached verification cannot be reused')
    output = Path(output)/('source_verification_'+v.datetime.now(v.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    current = {}; reused = []; runs = []
    images,failures=v.render_original_pages(pdf,payload['documents'][0],analysis['claims'],output/'original_pages')
    if failures:raise ValueError('Original page rendering failed; verification cannot be reused')
    checks=v.check_ocr(analysis['claims'],payload['ocr_pages'])
    write_json(output/'verification_input.json',{'analysis_result':analysis,'ocr_pages':payload['ocr_pages'],
        'document_metadata':payload['documents'],'ocr_checks':checks,'image_manifest':images})
    with pymupdf.open(pdf) as doc:
        for page in sorted({c['source_page'] for c in analysis['claims']}):
            claims = [c for c in analysis['claims'] if c['source_page']==page]
            texts = [p for p in payload['ocr_pages'] if p['source_page']==page]
            pixels = doc[page-1].get_pixmap(matrix=pymupdf.Matrix(2,2)).tobytes('png')
            normalized = [{k: value for k,value in c.items() if k not in {'claim_id','source_document_id'}} for c in claims]
            cache_key = 'source-page:'+fingerprint([normalized, [p['text'] for p in texts], v.sha256(pixels), runner.code_hash, codex.DEFAULT_MODEL,runner.status['run_mode']])
            row = runner.ledger.db.execute('SELECT payload FROM verification_cache WHERE cache_key=?',(cache_key,)).fetchone()
            if row and not runner.refresh and page not in getattr(runner,'force_review_pages',set()):
                cached = json.loads(row[0])
                if len(cached)==len(claims):
                    for claim, result in zip(claims, cached): current[claim['claim_id']]={**result,'claim_id':claim['claim_id']}
                    reused.append(page); continue
            subset = {**analysis, 'claims': claims}
            input_path = output/f'page_{page}_analysis.json'; write_json(input_path, subset)
            log, run = v.run_verifier(input_path, ocr_path, pdf, output, client=runner.client)
            runs.append(str(run))
            if log['status'] != 'SUCCESS': return log, output
            result = read_json(run/'source_verification.json')['claim_results']
            current.update({r['claim_id']:r for r in result})
            with runner.ledger.db:
                runner.ledger.db.execute('INSERT OR REPLACE INTO verification_cache VALUES (?,?)',(cache_key,json.dumps(result,ensure_ascii=False)))
    final = v.assemble_results(analysis['claims'],None,list(current.values()))
    write_json(output/'source_verification.json',final)
    write_json(output/'controller_observation.json',final['controller_observation'])
    log={'status':'SUCCESS','reused_pages':reused,'reverified_pages':len(runs),'source_runs':runs,
         'finished_at':v.datetime.now(v.timezone.utc).isoformat(),
         'input_hashes':{'analysis':v.sha256(Path(analysis_path).read_bytes()),'ocr_source':v.sha256(Path(ocr_path).read_bytes())},
         'policy':'exact claims + OCR + page pixels + verifier code/model; original bytes rechecked upstream'}
    write_json(output/'run.json',log)
    runner.ledger.record(runner.root.name,'incremental_verification','4',log)
    return log,output
