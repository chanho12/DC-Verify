"""STEP 8-3: validated inventory -> deterministic pairs -> fixed-model cross-check.

No new search, extraction, Claim correction, independent-source promotion,
final facility comparison, or Controller decision is performed here.
"""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path

import argparse
import copy
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model, Field

HERE = Path(__file__).resolve().parent
from dc_verify.codex import DEFAULT_MODEL, request_structured_output, safe_error
from dc_verify.step.step8.code.source_admission import digest, now, read_json, write_json
from dc_verify.step.step8.code.cross_source_input import load_input, newest_source, InputBlocked, require
from dc_verify.step.step8.code.pair_matcher import select_pairs
from dc_verify.step.step8.code.prompt import CROSS_CHECK_SYSTEM_PROMPT, CROSS_CHECK_USER_PROMPT


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


Dimension = Literal['SAME', 'DIFFERENT', 'UNKNOWN', 'NOT_APPLICABLE']
Relationship = Literal['CONSISTENT_STATEMENTS', 'SAME_VALUE_TIME_UNRESOLVED', 'TEMPORAL_UPDATE_CANDIDATE',
    'PLAN_EXECUTION_FOLLOWUP', 'DIFFERENT_METRIC', 'DIFFERENT_STATE_BASIS', 'DIFFERENT_SCOPE',
    'DEFINITION_UNCLEAR', 'POTENTIAL_CONFLICT', 'INSUFFICIENT_EVIDENCE', 'NOT_COMPARABLE']


class Dimensions(StrictModel):
    metric_definition: Dimension
    facility_scope: Dimension
    measurement_basis: Dimension
    temporal_basis: Dimension
    statement_status: Dimension
    unit: Dimension


def response_schema(pairs, context):
    keys = sorted({k for p in pairs for k in [p['left_key'], p['right_key']]})
    Row = create_model('ScopedCrossCheckPair', __base__=StrictModel,
        pair_id=(Literal[tuple(p['pair_id'] for p in pairs)], ...), relationship=(Relationship, ...),
        comparison_dimensions=(Dimensions, ...), reason=(str, ...), differences=(list[str], ...),
        additional_evidence_needed=(list[str], ...), evidence_claim_keys=(list[Literal[tuple(keys)]], Field(min_length=2, max_length=2)),
        related_requirements=(list[Literal[tuple(context['required_metrics'])]], ...))
    return create_model('CrossCheckBatch', __base__=StrictModel,
        pair_results=(list[Row], Field(min_length=len(pairs), max_length=len(pairs))))


def pair_payload(pair, inventory):
    def brief(key):
        c = inventory[key]
        d = c['document']
        return {k: c[k] for k in ['claim_key', 'claim', 'source_status', 'semantic_status', 'semantic_reason',
            'original_group', 'source_admission', 'comparison_eligibility', 'is_provisional', 'source_quote', 'uncertainties']} | {
                'document': {k: d.get(k) for k in ['document_id', 'title', 'source_url', 'source_sha256', 'published_at',
                    'document_period', 'publication_review', 'independence_status']}}
    return {**pair, 'left': brief(pair['left_key']), 'right': brief(pair['right_key'])}


def references(pair, inventory):
    values = []
    for k in [pair['left_key'], pair['right_key']]:
        i = inventory[k]; c, d = i['claim'], i['document']
        values.append({'claim_key': k, 'claim_id': c['claim_id'], 'document_id': c['source_document_id'],
            'source_page': c['source_page'], 'source_file': d['source_file'], 'source_url': d.get('source_url'),
            'source_sha256': d['source_sha256'], 'claim_quote': c['evidence_quote'], 'source_verifier_quote': i['source_quote'],
            'source_status': i['source_status'], 'semantic_status': i['semantic_status'], 'original_group': i['original_group'],
            'source_admission': i['source_admission'], 'is_provisional': i['is_provisional'], 'final_evidence_allowed': False})
    return values


def quantities(text):
    return {(m.group(1).replace(',', ''), m.group(2).casefold()) for m in re.finditer(r'(\d[\d,]*(?:\.\d+)?)\s*(MW|kW|GW|%)(?![a-zA-Z])', text, re.I)}


def guard_pair(raw, pair, inventory):
    """Pure conservative guards. Original response and Claims are retained separately."""
    row = copy.deepcopy(raw)
    row['model_comparison_dimensions'] = copy.deepcopy(raw['comparison_dimensions'])
    keys = [pair['left_key'], pair['right_key']]
    if Counter(row['evidence_claim_keys']) != Counter(keys):
        raise ValueError('Pair evidence keys do not match its two Claims')
    left, right = (inventory[k] for k in keys)
    relation = row['relationship']
    gates = []
    retry_required = False
    def restrict(value, why):
        nonlocal relation
        relation = value; gates.append(why)
    hard_failed = any(c['source_status'] != 'VERIFIED' or c['semantic_status'] == 'MISMATCH' or c['original_group'] == 'QUARANTINED' for c in [left, right])
    if hard_failed:
        restrict('INSUFFICIENT_EVIDENCE', '원문·의미 검증 실패 또는 격리 항목은 확정 비교에서 제외')
    elif pair['mode'] == 'AMBIGUITY_DIAGNOSTIC' or any(c['semantic_status'] != 'MATCH' for c in [left, right]):
        restrict('DEFINITION_UNCLEAR', '의미 불명확 Claim은 정의·분모 확인 전 진단 정보로만 사용')
    elif pair['kind'] == 'STATE_BOUNDARY':
        restrict('DIFFERENT_STATE_BASIS', '계약 상태와 입주 상태를 같은 분모·상태로 취급하지 않음')
    elif pair['kind'] == 'METRIC_BOUNDARY':
        restrict('DEFINITION_UNCLEAR' if 'other' in pair['metric_types'] else 'DIFFERENT_METRIC',
                 '다른 용량·사용량 유형은 같은 지표로 대체하지 않음')
    dims = row['comparison_dimensions']
    temporal = pair['time_comparison']['relation']
    context_definition = (not hard_failed and left.get('general_context') and right.get('general_context')
        and left['claim'].get('value') is None and right['claim'].get('value') is None
        and re.sub(r'\s+', '', left['claim']['evidence_quote']) == re.sub(r'\s+', '', right['claim']['evidence_quote']))
    if context_definition:
        restrict('CONSISTENT_STATEMENTS', '검증된 동일한 일반 정의의 문장 관계를 목표 증빙 충분성과 분리')
        dims.update(metric_definition='SAME', facility_scope='NOT_APPLICABLE', measurement_basis='NOT_APPLICABLE',
            temporal_basis='NOT_APPLICABLE', statement_status='NOT_APPLICABLE', unit='NOT_APPLICABLE')
    if relation == 'CONSISTENT_STATEMENTS':
        timeless = left.get('general_context') and right.get('general_context') and dims['temporal_basis'] == 'NOT_APPLICABLE'
        if any(v not in {'SAME', 'NOT_APPLICABLE'} for v in dims.values()) or (temporal != 'SAME_INTERVAL' and not timeless):
            restrict('SAME_VALUE_TIME_UNRESOLVED' if pair['same_explicit_number_and_unit'] else 'INSUFFICIENT_EVIDENCE',
                     '지표·범위·정의·시점 확인 없이 동일 기준 일치로 올리지 않음')
    if relation == 'SAME_VALUE_TIME_UNRESOLVED' and not pair['same_explicit_number_and_unit']:
        restrict('INSUFFICIENT_EVIDENCE', '같은 명시적 숫자·단위가 없는 쌍의 수치 일치 추정 금지')
    if relation == 'TEMPORAL_UPDATE_CANDIDATE' and (temporal not in {'LEFT_BEFORE_RIGHT', 'RIGHT_BEFORE_LEFT'} or len(set(pair['families'])) != 1):
        if temporal == 'OVERLAPPING_DIFFERENT_PRECISION' and pair['same_explicit_number_and_unit']:
            restrict('SAME_VALUE_TIME_UNRESOLVED', '분기·반기 등 겹치는 기간을 시간순 정보 변화로 단정하지 않음')
        else:
            restrict('INSUFFICIENT_EVIDENCE', '미기재 지표 기준일을 표지·게시일이나 주변 정보로 보충해 정보 변화로 확정하지 않음')
    if relation == 'PLAN_EXECUTION_FOLLOWUP':
        planned = [c['claim']['metric_type'].startswith('planned_') or c['claim']['temporal_basis'] in {'planned', 'expected'} for c in [left, right]]
        if pair['kind'] != 'EVENT_PROGRESS' or planned[0] == planned[1]:
            restrict('NOT_COMPARABLE', '계획과 실행 발표가 실제로 구분되는 사건 쌍인지 확인 필요')
    if relation == 'POTENTIAL_CONFLICT':
        a, b = left['claim'], right['claim']
        comparable = (left['comparison_eligibility'] != 'DIAGNOSTIC_ONLY' and right['comparison_eligibility'] != 'DIAGNOSTIC_ONLY'
            and a['metric_type'] == b['metric_type'] and temporal == 'SAME_INTERVAL'
            and all(v in {'SAME', 'NOT_APPLICABLE'} for v in dims.values()) and a.get('unit') and b.get('unit')
            and a['unit'].casefold() == b['unit'].casefold() and a.get('value') is not None and b.get('value') is not None
            and a['value'] != b['value'] and a['value_qualifier'] == b['value_qualifier'] == 'exact')
        if not comparable:
            restrict('INSUFFICIENT_EVIDENCE', '동일 지표·범위·정의·시점의 서로 다른 명시 값이 확인되지 않아 충돌 판정 보류')
    prose = ' '.join([row['reason']] + row['differences'] + row['additional_evidence_needed'])
    supplied = json.dumps(pair_payload(pair, inventory), ensure_ascii=False)
    if quantities(prose) - quantities(supplied):
        if relation in {'DEFINITION_UNCLEAR', 'INSUFFICIENT_EVIDENCE', 'NOT_COMPARABLE'}:
            gates.append('입력에 없는 수치 언급은 확정 증빙으로 사용하지 않음')
        else:
            restrict('INSUFFICIENT_EVIDENCE', '입력에 없는 수치·단위 조합이 응답에 있어 추가 검토 필요')
    if relation == 'DIFFERENT_METRIC' and left['claim']['metric_type'] == right['claim']['metric_type'] != 'other' and not hard_failed:
        restrict('DEFINITION_UNCLEAR', '검증된 동일 지표 유형을 다른 지표로 읽은 응답은 선택 Claim에 집중해 재검토 필요')
        retry_required = True
    if relation == 'DEFINITION_UNCLEAR':
        for field in ['metric_definition', 'measurement_basis']:
            if dims[field] == 'SAME':
                dims[field] = 'UNKNOWN'
        gates.append('동일 지표 계열과 동일 측정 정의를 구분; 정의 미확인을 SAME으로 기록하지 않음')
    provisional = left['is_provisional'] or right['is_provisional']
    row.update(relationship=relation, model_relationship=raw['relationship'], local_gates=gates,
        comparison_mode='DIAGNOSTIC_ONLY' if pair['mode'] != 'STATEMENT_COMPARISON' or hard_failed else 'PROVISIONAL_STATEMENTS' if provisional else 'SOURCE_STATEMENTS_ONLY',
        retry_required=retry_required,
        relation_scope='GENERAL_DEFINITION' if context_definition else 'CLAIM_RELATIONSHIP',
        goal_role='CONTEXT_ONLY' if context_definition else 'DIAGNOSTIC_ONLY' if pair['mode'] != 'STATEMENT_COMPARISON' or hard_failed else 'PARTIAL_SUPPORT_ONLY',
        goal_evidence_status='INSUFFICIENT' if context_definition else 'NOT_DETERMINED_BY_PAIR_RELATIONSHIP',
        final_evidence_allowed=False, independently_corroborated=False, claims_modified=False,
        evidence_references=references(pair, inventory), time_comparison=pair['time_comparison'],
        definition_status='UNRESOLVED' if relation == 'DEFINITION_UNCLEAR' else 'SEE_COMPARISON_DIMENSIONS')
    return row


def repair_pair(pair, inventory, context, folder, client=None):
    """At most one focused API review of a contradictory typed-metric reading."""
    folder.mkdir(parents=True, exist_ok=True)
    payload = pair_payload(pair, inventory)
    focus = {k: {f: inventory[k]['claim'].get(f) for f in ['metric_type', 'metric_name', 'raw_value', 'value', 'unit']}
        for k in [pair['left_key'], pair['right_key']]}
    user = CROSS_CHECK_USER_PROMPT.format(context_json=json.dumps(context, ensure_ascii=False), pairs_json=json.dumps([payload], ensure_ascii=False))
    user += '\nFOCUS_SELECTED_CLAIMS_ONLY:\n' + json.dumps(focus, ensure_ascii=False)
    user += '\n긴 인용 안의 다른 지표를 선택 Claim과 혼동하지 말고 두 선택 Claim의 관계를 독립적으로 다시 판단하라.'
    f = folder / 'api_response.json'
    schema = response_schema([pair], context)
    if f.exists():
        response = read_json(f)
    else:
        write_json(folder / 'request.json', {'pair': payload, 'focus': focus})
        (folder / 'system_prompt.txt').write_text(CROSS_CHECK_SYSTEM_PROMPT, encoding='utf-8')
        (folder / 'user_prompt.txt').write_text(user, encoding='utf-8')
        response = request_structured_output(CROSS_CHECK_SYSTEM_PROMPT, user, schema, client=client, max_output_tokens=4000)
        write_json(f, response)
    require(response.get('api_status') == 'completed' and response.get('parsed'), 'Focused review incomplete')
    value = schema.model_validate(response['parsed']).model_dump(mode='json')['pair_results']
    require(len(value) == 1 and value[0]['pair_id'] == pair['pair_id'], 'Focused review pair identity mismatch')
    result = guard_pair(value[0], pair, inventory)
    result['focused_review_response_id'] = response['response_id']
    write_json(folder / 'validated_result.json', result)
    return result


def local_diagnostic(pair, inventory):
    reasons = [{'claim_key': k, 'source_status': inventory[k]['source_status'],
        'semantic_status': inventory[k]['semantic_status'], 'original_group': inventory[k]['original_group'],
        'reason': inventory[k]['semantic_reason'], 'quarantine_reasons': inventory[k]['original_quarantine_reasons']}
        for k in [pair['left_key'], pair['right_key']] if inventory[k]['comparison_eligibility'] == 'DIAGNOSTIC_ONLY']
    return {'pair_id': pair['pair_id'], 'relationship': 'INSUFFICIENT_EVIDENCE', 'comparison_mode': 'DIAGNOSTIC_ONLY',
        'reason': '원문·의미 검증 미통과 또는 격리 항목이 포함되어 확정 비교에서 제외', 'failed_claims': reasons,
        'evidence_references': references(pair, inventory), 'llm_called': False,
        'final_evidence_allowed': False, 'independently_corroborated': False, 'claims_modified': False}


def execute(source_run=None, output_dir=stage_data(8) / 'output', resume=None, dry_run=False,
            max_pairs=24, max_diagnostics=12, batch_size=6, client=None, revalidate=False):
    require(1 <= batch_size <= 8, 'Batch size must be between 1 and 8')
    output = Path(resume).resolve() if resume else Path(output_dir) / ('cross_source_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True, exist_ok=bool(resume))
    def event(name, **values):
        with (output / 'execution_log.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': now(), 'event': name, **values}, ensure_ascii=False) + '\n')
    event('STARTED', dry_run=dry_run, model=DEFAULT_MODEL)
    try:
        source = Path(source_run).resolve() if source_run else newest_source(stage_data(8) / 'output')
        data = load_input(source)
        signature = {'source_run': str(source), 'watched_files': data['watched_files'],
            'max_pairs': max_pairs, 'max_diagnostics': max_diagnostics, 'batch_size': batch_size,
            'implementation_sha256': digest(Path(__file__).read_bytes()),
            'matcher_sha256': digest((HERE / 'pair_matcher.py').read_bytes()),
            'prompt_sha256': digest(CROSS_CHECK_SYSTEM_PROMPT.encode())}
        snapshot = output / 'input_snapshot.json'
        plan = select_pairs(data, max_pairs=max_pairs, max_diagnostics=max_diagnostics)
        if snapshot.exists() and read_json(snapshot) != signature:
            old = read_json(snapshot)
            core = ['source_run', 'watched_files', 'max_pairs', 'max_diagnostics', 'batch_size']
            require(revalidate and all(old[k] == signature[k] for k in core)
                and read_json(output / 'pair_selection.json') == plan, 'Resume inputs/configuration/code changed; use a new run or explicit local revalidation')
            write_json(output / 'revalidation_history' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json'),
                {'previous_signature': old, 'current_signature': signature, 'same_inputs_and_pairs': True,
                 'original_api_requests_and_prompts_preserved': True})
        elif not snapshot.exists():
            write_json(snapshot, signature)
        write_json(output / 'normalized_input.json', data)
        write_json(output / 'input_validation.json', {'status': 'PASSED', 'source_run': str(source), 'document_count': len(data['documents']),
            'claim_count': len(data['claims']), 'input_repairs': data['input_repairs'], 'original_chain': data['original_chain'],
            'file_aliases': data['file_aliases'], 'all_document_hashes_verified': True})
        write_json(output / 'pair_selection.json', plan)
    except Exception as exc:
        error = safe_error(exc)
        if isinstance(exc, (InputBlocked, ValueError)):
            error['message'] = str(exc)
        result = {'status': 'BLOCKED', 'error': error, 'model': DEFAULT_MODEL, 'api_attempted': False,
            'output_dir': str(output.resolve()), 'finished_at': now()}
        if resume and (output / 'run.json').exists():
            write_json(output / ('resume_rejected_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json'), result)
        else:
            write_json(output / 'input_validation.json', result)
            write_json(output / 'run.json', result)
        event('INPUT_BLOCKED', error=error)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    inventory = {c['claim_key']: c for c in data['claims']}
    pairs = plan['selected_pairs']
    results, failures, api_keys = [], [], set()
    new_api_requests = 0
    for start in range(0, len(pairs), batch_size):
        group = pairs[start:start + batch_size]
        folder = output / 'batches' / f'batch_{start // batch_size + 1:03d}'
        folder.mkdir(parents=True, exist_ok=True)
        payload = [pair_payload(p, inventory) for p in group]
        prompt = CROSS_CHECK_USER_PROMPT.format(context_json=json.dumps(data['review_context'], ensure_ascii=False), pairs_json=json.dumps(payload, ensure_ascii=False))
        if not (folder / 'api_response.json').exists():
            write_json(folder / 'request.json', {'review_context': data['review_context'], 'pairs': payload})
            (folder / 'system_prompt.txt').write_text(CROSS_CHECK_SYSTEM_PROMPT, encoding='utf-8')
            (folder / 'user_prompt.txt').write_text(prompt, encoding='utf-8')
        api_keys |= {k for p in group for k in [p['left_key'], p['right_key']]}
        if dry_run:
            continue
        try:
            schema = response_schema(group, data['review_context'])
            response_file = folder / 'api_response.json'
            if response_file.exists():
                require(read_json(folder / 'request.json') == {'review_context': data['review_context'], 'pairs': payload}, 'Cached API request does not match current pair inputs')
                response = read_json(response_file)
                event('CACHED_RESPONSE_REUSED', batch=folder.name, response_id=response.get('response_id'))
            else:
                new_api_requests += 1
                event('API_REQUEST_STARTED', batch=folder.name, pair_count=len(group))
                print(json.dumps({'batch': folder.name, 'pairs': len(group), 'stage': 'API_REQUESTED'}), flush=True)
                response = request_structured_output(CROSS_CHECK_SYSTEM_PROMPT, prompt, schema, client=client, max_output_tokens=10000)
                write_json(response_file, response)
                event('API_RESPONSE_RECEIVED', batch=folder.name, response_id=response.get('response_id'), usage=response.get('usage'))
            require(response.get('api_status') == 'completed' and response.get('parsed'), 'Cross-check response incomplete/unparsed')
            parsed = schema.model_validate(response['parsed']).model_dump(mode='json')
            require(Counter(r['pair_id'] for r in parsed['pair_results']) == Counter(p['pair_id'] for p in group), 'Response pair coverage mismatch')
            pair_map = {p['pair_id']: p for p in group}
            checked = [guard_pair(r, pair_map[r['pair_id']], inventory) for r in parsed['pair_results']]
            results += checked
            write_json(folder / 'validated_result.json', {'status': 'PASSED', 'pair_results': checked, 'response_id': response['response_id']})
            print(json.dumps({'batch': folder.name, 'stage': 'SUCCESS', 'relationships': dict(Counter(r['relationship'] for r in checked))}), flush=True)
        except Exception as exc:
            error = safe_error(exc)
            if isinstance(exc, ValueError):
                error['message'] = str(exc)
            failure = {'batch': folder.name, 'pair_ids': [p['pair_id'] for p in group], 'error': error}
            failures.append(failure)
            write_json(folder / 'failure.json', failure)
            event('BATCH_FAILED', **failure)
    pair_map = {p['pair_id']: p for p in pairs}
    for n, r in enumerate(results):
        if r['retry_required']:
            try:
                event('FOCUSED_PAIR_REVIEW_STARTED', pair_id=r['pair_id'])
                if not (output / 'focused_reviews' / r['pair_id'] / 'api_response.json').exists():
                    new_api_requests += 1
                results[n] = repair_pair(pair_map[r['pair_id']], inventory, data['review_context'], output / 'focused_reviews' / r['pair_id'], client=client)
                event('FOCUSED_PAIR_REVIEW_FINISHED', pair_id=r['pair_id'], response_id=results[n]['focused_review_response_id'])
            except Exception as exc:
                r['focused_review_failure'] = safe_error(exc)
                event('FOCUSED_PAIR_REVIEW_FAILED', pair_id=r['pair_id'], error=safe_error(exc))
    diagnostics = [local_diagnostic(p, inventory) for p in plan['local_diagnostics']]
    unchanged = all(Path(p).is_file() and digest(Path(p).read_bytes()) == sha for p, sha in data['watched_files'].items())
    status = 'PREPARED' if dry_run else 'SUCCESS' if len(results) == len(pairs) and not failures and unchanged else 'PARTIAL'
    checked = {'status': status, 'pair_results': results, 'diagnostic_results': diagnostics, 'final_evidence_allowed': False,
        'independent_corroboration_performed': False, 'original_claims_unchanged': unchanged}
    write_json(output / 'cross_source_verification.json', checked)
    observation = {'cross_check_status': status, 'prior_review_goal_status': data['prior_review_goal_status'],
        'goal_decision': 'DEFERRED_TO_CONTROLLER', 'relationship_counts': dict(Counter(r['relationship'] for r in results)),
        'diagnostic_only_pair_ids': [r['pair_id'] for r in results + diagnostics if r['comparison_mode'] == 'DIAGNOSTIC_ONLY'],
        'unresolved_requirements': data['prior_missing_requirements'],
        'additional_evidence_needed': sorted({s for r in results for s in r['additional_evidence_needed']}),
        'publication_unconfirmed_document_ids': [d['document_id'] for d in data['documents'].values() if d.get('published_at') is None],
        'unpaired_claims': plan['unpaired_claims'], 'technical_failures': failures, 'controller_replanned': False,
        'note': 'Selected-pair statement relationships only. Unselected Claims were not cross-checked; no claim of independent proof or final goal completion.'}
    write_json(output / 'controller_observation.json', observation)
    write_json(output / 'controller_input.json', {'next_stage': 'STEP_9_CONTROLLER', 'execution_ready': status == 'SUCCESS',
        'review_context': data['review_context'], 'documents': data['documents'], 'claim_inventory': data['claims'],
        'cross_source_result': checked, 'controller_observation': observation, 'controller_replanned': False})
    response_paths = list(output.glob('batches/*/api_response.json')) + list(output.glob('focused_reviews/*/api_response.json'))
    responses = [read_json(p) for p in response_paths]
    calls = [{'file': str(p.resolve()), **{k: read_json(p).get(k) for k in ['response_id', 'model', 'api_status', 'usage']}}
        for p in response_paths]
    run = {'status': status, 'model': DEFAULT_MODEL, 'source_run': str(source), 'output_dir': str(output.resolve()),
        'finished_at': now(), 'document_count': len(data['documents']), 'claim_count': len(inventory),
        'candidate_count': plan['candidate_count'], 'llm_pair_count': len(pairs), 'llm_unique_claim_count': len(api_keys),
        'local_diagnostic_pair_count': len(diagnostics), 'api_responses': len(responses), 'api_attempted': bool(responses),
        'api_requests_this_invocation': new_api_requests,
        'token_usage_recorded_for_all_responses': all(r.get('usage') is not None for r in responses),
        'usage': {k: sum((r.get('usage') or {}).get(k, 0) for r in responses) for k in ['input_tokens', 'output_tokens', 'total_tokens']},
        'originals_unchanged': unchanged, 'searches_executed': False, 'claims_reextracted': False,
        'independent_corroboration_performed': False, 'controller_replanned': False, 'cross_check_performed': not dry_run and bool(results)}
    write_json(output / 'api_calls.json', {'responses': calls, 'scope': 'Actual API responses; unit-test doubles are stored only in temporary test directories.'})
    write_json(output / 'run.json', run)
    if not dry_run:
        verification = verify_execution(output)
        if verification['status'] != 'PASSED':
            run['status'] = 'PARTIAL'
            write_json(output / 'run.json', run)
            controller = read_json(output / 'controller_input.json')
            controller['execution_ready'] = False
            write_json(output / 'controller_input.json', controller)
    event('FINISHED', **run)
    print(json.dumps(run, ensure_ascii=False), flush=True)
    return run


def verify_execution(output):
    """Audit saved results and source bindings without model or search calls."""
    output = Path(output)
    data, plan, result, run = (read_json(output / n) for n in ['normalized_input.json', 'pair_selection.json', 'cross_source_verification.json', 'run.json'])
    inventory = {c['claim_key']: c for c in data['claims']}
    pairs = {p['pair_id']: p for p in plan['selected_pairs']}
    rows = result['pair_results']
    checks = {
        'global_scope_restored': set(data['review_context']['document_ids']) == set(data['documents']) == set(data['document_scopes']),
        'no_document_policy_in_global_context': 'source_admission_policy' not in data['review_context'],
        'pair_coverage_exact': Counter(r['pair_id'] for r in rows) == Counter(pairs.keys()),
        'all_original_files_unchanged': all(Path(p).is_file() and digest(Path(p).read_bytes()) == sha for p, sha in data['watched_files'].items()),
        'all_document_hashes_verified': all(digest(Path(d['source_file']).read_bytes()) == d['source_sha256'] for d in data['documents'].values()),
        'no_final_or_independent_promotion': all(not r['final_evidence_allowed'] and not r['independently_corroborated'] and not r['claims_modified'] for r in rows + result['diagnostic_results']),
        'diagnostics_not_sent_as_confirmed_evidence': all(r['comparison_mode'] == 'DIAGNOSTIC_ONLY' for r in result['diagnostic_results']),
        'pair_budget_respected': len(pairs) <= plan['max_pairs'] and len(result['diagnostic_results']) <= plan['max_diagnostics'],
        'no_search_reextraction_or_controller_action': not run['searches_executed'] and not run['claims_reextracted'] and not run['controller_replanned'],
    }
    bound = True
    for r in rows + result['diagnostic_results']:
        for ref in r['evidence_references']:
            c = inventory[ref['claim_key']]
            bound &= (ref['claim_id'] == c['claim']['claim_id'] and ref['document_id'] == c['claim']['source_document_id']
                and ref['source_page'] == c['claim']['source_page'] and ref['claim_quote'] == c['claim']['evidence_quote']
                and ref['source_sha256'] == c['document']['source_sha256'] and ref['source_verifier_quote'] == c['source_quote'])
    checks['evidence_ids_quotes_pages_hashes_bound'] = bool(bound)
    checks['all_model_results_pass_current_guards'] = all(guard_pair(r, pairs[r['pair_id']], inventory)['relationship'] == r['relationship'] for r in rows)
    checks['failed_or_quarantined_claims_are_diagnostic_only'] = all(r['comparison_mode'] == 'DIAGNOSTIC_ONLY'
        for r in rows for ref in r['evidence_references'] if inventory[ref['claim_key']]['comparison_eligibility'] == 'DIAGNOSTIC_ONLY')
    calls = read_json(output / 'api_calls.json')['responses']
    checks['actual_fixed_model_responses'] = bool(calls) and all(c['response_id'] and c['api_status'] == 'completed' and c['model'].startswith(DEFAULT_MODEL) for c in calls)
    checks['response_ids_unique'] = len({c['response_id'] for c in calls}) == len(calls)
    checks['controller_inventory_unmodified'] = read_json(output / 'controller_input.json')['claim_inventory'] == data['claims']
    observed = {'status': 'PASSED' if all(checks.values()) and run['status'] == 'SUCCESS' else 'FAILED', 'checks': checks,
        'scope': 'Selected-pair contract and provenance checks; not proof of factual truth or source independence.'}
    write_json(output / 'execution_verification.json', observed)
    return observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--output-dir', type=Path, default=stage_data(8) / 'output')
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--max-pairs', type=int, default=24)
    parser.add_argument('--max-diagnostics', type=int, default=12)
    parser.add_argument('--batch-size', type=int, default=6)
    parser.add_argument('--revalidate', action='store_true', help='Reapply code guards to unchanged cached inputs/pairs, preserving actual API prompts')
    result = execute(**vars(parser.parse_args()))
    return 0 if result['status'] in {'SUCCESS', 'PREPARED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
