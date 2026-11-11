"""Lineage-bound restoration and derived corrections; never edits earlier outputs."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
from dc_verify.step.step8.code.source_admission import digest, read_json
from dc_verify.step.step8.code.cross_source_input import load_input, successful_runs, indexed, require
from dc_verify.step.step8.code.cross_source import guard_pair, references
from dc_verify.step.step8.code.pair_matcher import explicit_time
from dc_verify.step.step6.code.controller_replan import validate_decision


def load_cross_source(source):
    source = Path(source).resolve()
    run, incoming = read_json(source / 'run.json'), read_json(source / 'controller_input.json')
    require(run['status'] == 'SUCCESS' and incoming['execution_ready'], 'Cross-source run is not successful/ready')
    data = load_input(run['source_run'])
    saved = read_json(source / 'normalized_input.json')
    require(saved == data, 'Cross-source normalized inputs differ from validated upstream lineage')
    require(incoming['claim_inventory'] == data['claims'] and incoming['documents'] == data['documents']
        and incoming['review_context'] == data['review_context'], 'Controller handoff inventory/context changed')
    result, plan = read_json(source / 'cross_source_verification.json'), read_json(source / 'pair_selection.json')
    require(result == incoming['cross_source_result'], 'Cross-source handoff/result from different runs')
    inventory = indexed(data['claims'], lambda c: c['claim_key'])
    pairs = indexed(plan['selected_pairs'], lambda p: p['pair_id'])
    require(Counter(r['pair_id'] for r in result['pair_results']) == Counter(pairs.keys()), 'Cross-source pair coverage differs')
    watched = {Path(p): sha for p, sha in data['watched_files'].items()}
    for p in source.rglob('*.json'):
        watched[p.resolve()] = digest(p.read_bytes())
    raw = {}
    for p in source.glob('batches/*/api_response.json'):
        response = read_json(p)
        require(response['api_status'] == 'completed', 'Incomplete cross-source API response')
        for r in response['parsed']['pair_results']:
            require(r['pair_id'] not in raw, 'Duplicate pair response')
            raw[r['pair_id']] = r
    for p in source.glob('focused_reviews/*/api_response.json'):
        response = read_json(p)
        require(response['api_status'] == 'completed', 'Incomplete focused review')
        for r in response['parsed']['pair_results']:
            require(r['pair_id'] in raw, 'Focused response is not linked to an original pair')
            raw[r['pair_id']] = r
    require(set(raw) == set(pairs), 'Original cross-source API coverage differs')
    corrected, corrections = [], []
    for old in result['pair_results']:
        pid = old['pair_id']; pair = pairs[pid]
        require(old['evidence_references'] == references(pair, inventory), 'Cross-source evidence bindings differ')
        require(old['model_relationship'] == raw[pid]['relationship'], 'Final relation not linked to raw API result')
        row = guard_pair(raw[pid], pair, inventory)
        corrected.append(row)
        changes = {k: {'before': old.get(k), 'after': row.get(k)} for k in
            ['relationship', 'comparison_dimensions', 'relation_scope', 'goal_role', 'goal_evidence_status'] if old.get(k) != row.get(k)}
        if changes: corrections.append({'pair_id': pid, 'changes': changes, 'reason': row['local_gates']})
    return data, {**result, 'pair_results': corrected}, corrections, watched


def restore_search_history(search, incoming, prior_issues):
    state, history, budget = (read_json(search / n) for n in ['state.json', 'search_history_updated.json', 'search_budget_updated.json'])
    require(state['run_mode'] == 'LIVE' and state['history'] == history, 'Search history is mock or differs from recorded state')
    require(digest(Path(state['input_file']).read_bytes()) == state['input_sha256']
        and read_json(state['input_file']) == incoming, 'Search Executor input hash/context changed')
    records = indexed(history['action_history'], lambda a: a.get('attempt_id') or json.dumps(a, sort_keys=True))
    executed = [a for a in records.values() if a.get('executed') is True and a['type'] in {'SEARCH_OFFICIAL', 'SEARCH_LOCAL'}]
    attempts = indexed(state['attempts'], lambda a: a['attempt_id'])
    initial_actions = incoming['search_history']['action_history']
    require(history['action_history'][:len(initial_actions)] == initial_actions, 'Historical action prefix changed')
    new_ids = {a['attempt_id'] for a in executed if a not in initial_actions}
    require(new_ids == set(attempts), 'Search receipts and executed actions differ')
    watched = {Path(state['input_file']).resolve(): state['input_sha256']}
    for aid, a in attempts.items():
        p = Path(a['raw_response_file'])
        require(p.is_file() and digest(p.read_bytes()) == a['receipt_sha256'], 'Actual search receipt hash mismatch')
        receipt = read_json(p)
        require(receipt.get('executed') is True and receipt['provenance'] in {'LIVE_CODEX_TOOL', 'LIVE_PYTHON_API'}
            and receipt['query'] == a['query_executed'] and receipt['finished_at'] == a['finished_at'], 'Non-executed/mock/unbound search receipt')
        action = records[aid]
        require(action['issue_ids'] == [a['issue_id']] and action['query'] == a['query_executed'], 'Search issue attribution differs')
        require(a['issue_id'] in prior_issues, 'Search assigned to unknown prior issue')
        watched[p.resolve()] = a['receipt_sha256']
    limit = incoming['search_budget']['max_attempts_per_issue']
    require(limit == history['search_policy']['max_attempts_per_issue'], 'Search limits differ across history')
    counts = Counter(i for a in executed for i in a['issue_ids'])
    owners = set(incoming['search_budget']['remaining_attempts'])
    remaining = {i: limit - counts[i] for i in sorted(owners)}
    require(all(v >= 0 for v in remaining.values()) and remaining == budget['remaining_attempts'], 'Restored search budget differs from actual executed requests')
    require(read_json(search / 'run.json')['search_request_count'] == len(attempts), 'Search request total differs from receipts')
    return history, {'max_attempts_per_issue': limit, 'executed_attempts': {i: counts[i] for i in sorted(owners)},
        'remaining_attempts': remaining, 'counts_only_executed_search_actions': True}, watched


def evidence_observation(data, reconciled):
    valid = [c for c in data['claims'] if c['comparison_eligibility'] != 'DIAGNOSTIC_ONLY']
    executions = [c for c in valid if c['claim']['metric_type'] in {'service_start', 'completion'}]
    def brief(c):
        return {k: c[k] for k in ['claim_key', 'source_status', 'semantic_status', 'is_provisional', 'source_admission']} | {
            'claim': {k: c['claim'].get(k) for k in ['metric_type', 'raw_value', 'metric_as_of', 'source_document_id', 'source_page', 'evidence_quote']},
            'published_at': c['document'].get('published_at')}
    cutoff = data['review_context']['as_of_date']
    independently_verified = any(c['evidence_level'] == 'INDEPENDENTLY_CORROBORATED' and not c['is_provisional']
        and c['claim']['metric_type'] in {'operational_it_capacity', 'actual_it_power_usage'}
        and explicit_time(c).get('precision') == 'DAY' and explicit_time(c).get('start') == cutoff for c in valid)
    return {'execution_statement_claim_keys': [c['claim_key'] for c in executions],
        'execution_statement_observations': [brief(c) for c in executions],
        'execution_statement_exists': bool(executions), 'current_operation_independently_verified': independently_verified,
        'current_use_metric_claim_keys': [c['claim_key'] for c in valid if c['claim']['metric_type'] in {'operational_it_capacity', 'actual_it_power_usage'}],
        'statement_and_current_proof_are_distinct': True,
        'capacity_and_contract_claims': [brief(c) for c in valid if c['claim']['metric_type'].endswith('_capacity') or c['claim']['metric_type'] in {'contract_ratio', 'contract_status', 'occupancy_rate'}],
        'relation_counts': dict(Counter(r['relationship'] for r in reconciled['pair_results'])),
        'context_only_pair_ids': [r['pair_id'] for r in reconciled['pair_results'] if r['goal_role'] == 'CONTEXT_ONLY'],
        'definition_unresolved_pair_ids': [r['pair_id'] for r in reconciled['pair_results'] if r['relationship'] == 'DEFINITION_UNCLEAR'],
        'unconfirmed_publication_document_ids': [d['document_id'] for d in data['documents'].values() if d.get('published_at') is None],
        'note': '후속 원문에 실행·완료 주장이 존재한다. 해당 문서의 공개일·검토 기준일 현재 상태·실제 사용량·독립 입증은 별도로 확인해야 한다.'}


def restore(source, runtime_file):
    source = Path(source).resolve()
    data, reconciled, corrections, watched = load_cross_source(source)
    # Collection lineage is derived from original file locations, then verified by hashes.
    collections = set()
    for d in data['documents'].values():
        if d['original_group'] == 'EXTERNAL':
            roots = [p for p in Path(d['source_file']).parents if (p / 'collected_documents.json').is_file()]
            require(len(roots) == 1, 'Ambiguous/missing original search collection lineage')
            collections.add(roots[0])
    require(len(collections) == 1, 'Multiple collection histories need explicit reconciliation; not silently mixed')
    search = next(iter(collections))
    incoming = read_json(search / 'executor_input.json')
    collected = indexed(read_json(search / 'collected_documents.json'), lambda d: d['document_id'])
    for d in data['documents'].values():
        if d['original_group'] == 'EXTERNAL':
            require(collected[d['document_id']]['sha256'] == d['source_sha256'], 'Collection/document lineage mismatch')
    planner = None
    for _, p, _ in successful_runs(stage_data(7) / 'output', 'search_plan_*', []):
        if read_json(p / 'search_plan.json') == incoming['search_plan']:
            planner = p; break
    require(planner is not None, 'No matching Search Planner run')
    planner_input = read_json(planner / 'planner_input.json')
    prior = None
    for _, p, _ in successful_runs(stage_data(6) / 'output', 'controller_replan_*', []):
        if read_json(p / 'controller_decision.json') == planner_input['controller_decision']:
            prior = p; break
    require(prior is not None, 'No matching previous Controller state')
    previous_input, previous_decision = read_json(prior / 'controller_input.json'), read_json(prior / 'controller_decision.json')
    _, validation = validate_decision(previous_decision, previous_input)
    require(validation['status'] == 'PASSED', 'Previous Controller state failed its own validator')
    for k in ['review_goal', 'target_projects', 'as_of_date']:
        require(data['review_context'][k] == previous_input['initial_plan'][k] == incoming['search_plan'][k], 'Controller goal context differs across lineage')
    prior_issues = indexed(previous_input['issue_inventory'], lambda i: i['issue_id'])
    history, budget, checked = restore_search_history(search, incoming, prior_issues)
    watched.update(checked)
    for folder in [search, planner, prior]:
        for p in folder.glob('*.json'): watched[p.resolve()] = digest(p.read_bytes())
    runtime_file = Path(runtime_file).resolve()
    runtime = read_json(runtime_file)
    require(runtime.get('checked_at') and runtime.get('tools'), 'Runtime Tool capabilities not checked')
    watched[runtime_file] = digest(runtime_file.read_bytes())
    observation = evidence_observation(data, reconciled)
    external = read_json(Path(data['source_run']) / 'cross_check_input.json')['external_claim_reviews']
    supports = {r['original_claim']['source_document_id'] + '::' + r['claim_id']: r['requirement_support'] for r in external}
    issues = []
    admitted = [r for r in external if r.get('final_evidence_allowed') is True and r['source_status'] == 'VERIFIED'
        and r['semantic_status'] == 'MATCH' and r['original_group'] != 'QUARANTINED' and not r['is_provisional']
        and r['evidence']['supports_review_goal']]
    for old in prior_issues.values():
        item = copy.deepcopy(old)
        item['prior_reason'] = item.pop('reason')
        findings = [c['claim_key'] for c in data['claims'] if c['comparison_eligibility'] != 'DIAGNOSTIC_ONLY'
            and any(s['requirement'] == old['description'] and s['support'] != 'NONE' for s in supports.get(c['claim_key'], []))]
        events = [k for k in observation['execution_statement_claim_keys'] if k in findings]
        complete = any(any(s['requirement'] == old['description'] and s['support'] == 'DIRECT' for s in r['requirement_support']) for r in admitted)
        if old['kind'] == 'SOURCE_REVIEW':
            linked = [c for c in data['claims'] if c['origin'] == 'USER' and c['claim']['claim_id'] in old['claim_ids']]
            complete = bool(linked) and all(c['source_status'] == 'VERIFIED' and c['semantic_status'] == 'MATCH' for c in linked)
        item.update(new_claim_keys=findings, execution_statement_claim_keys=events, goal_completion_allowed=complete,
            status='NEEDS_HUMAN_REVIEW' if old['kind'] == 'SOURCE_REVIEW' else 'PARTIALLY_SUPPORTED' if findings else 'UNRESOLVED',
            current_reason=('후속 자료에 실행·완료 문서상 주장이 발견됨. 공개일 미확인 및 기준일 현재의 독립 검증과는 구분해야 함.' if events else
                '검토 관련 문서상 정보가 추가되었으나 최종 목적 증빙은 아직 충분하지 않음.' if findings else '해당 쟁점의 채택 가능한 추가 증빙이 확인되지 않아 미해결 상태를 유지함.'),
            executed_searches=budget['executed_attempts'].get(old['issue_id'], 0),
            remaining_searches=budget['remaining_attempts'].get(old['issue_id'], 0),
            search_budget_owner=old['issue_id'] if old['issue_id'] in budget['remaining_attempts'] else None)
        issues.append(item)
    available = []
    import pymupdf
    for d in data['documents'].values():
        if d['format'] == 'PDF':
            with pymupdf.open(d['source_file']) as pdf: pages = list(range(1, len(pdf) + 1))
        else: pages = [1]
        processed = data['document_scopes'][d['document_id']].get('priority_pages', [])
        available.append({'document_id': d['document_id'], 'source_url': d.get('source_url'), 'source_sha256': d['source_sha256'],
            'all_pages': pages, 'processed_pages': processed, 'unprocessed_pages': [p for p in pages if p not in processed]})
    cross_run = read_json(source / 'run.json')
    source_run = read_json(Path(data['source_run']) / 'run.json')
    new_events = [
        {'event_id': 'source_collection:' + search.name, 'operation': 'SOURCE_ADDED', 'status': 'SUCCESS',
            'finished_at': read_json(search / 'run.json')['updated_at'], 'source_file': str(search / 'collected_documents.json')},
        {'event_id': 'source_review:' + Path(data['source_run']).name, 'operation': 'SOURCE_REVIEW_COMPLETED', 'status': 'SUCCESS',
            'finished_at': source_run['finished_at'], 'source_file': str(Path(data['source_run']) / 'verification_results.json')},
        {'event_id': 'cross_check:' + source.name, 'operation': 'CROSS_CHECK_COMPLETED', 'status': 'SUCCESS',
            'finished_at': cross_run['finished_at'], 'source_file': str(source / 'cross_source_verification.json')}]
    full_state = {'review_context': data['review_context'], 'issues': issues, 'known_claim_keys': [c['claim_key'] for c in data['claims']],
        'evidence_observation': observation, 'execution_history': history, 'search_budget': budget,
        'new_information_events': new_events, 'runtime_tools': runtime['tools'], 'available_documents': available,
        'goal_evidence_sufficient': all(i['goal_completion_allowed'] for i in issues if i['kind'] == 'GOAL_EVIDENCE'),
        'comparison_target_supplied': bool(previous_input['initial_plan'].get('comparison_project')),
        'lineage': {'cross_source': str(source), 'search_execution': str(search), 'search_planner': str(planner), 'prior_controller': str(prior)},
        'tool_call_statistics': {'executed_search_requests': len(read_json(search / 'state.json')['attempts']),
            'recorded_document_open_events': len(read_json(search / 'state.json')['retrieval_events']),
            'downloaded_originals': len(collected), 'planning_consumes_search_attempts': False},
        'prior_state_preserved': True, 'source_claims_modified': False}
    # Historical absence statements remain in the artifact, not the current API observation.
    api_state = copy.deepcopy(full_state)
    for i in api_state['issues']: i.pop('prior_reason', None)
    api_state['execution_history'] = {'action_history': history['action_history'], 'search_policy': history['search_policy']}
    return full_state, api_state, reconciled, corrections, {str(p): sha for p, sha in watched.items()}
