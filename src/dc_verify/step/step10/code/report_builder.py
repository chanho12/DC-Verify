"""STEP10 offline report builder. No model, search, source promotion or workflow execution."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import argparse
import copy
import csv
import hashlib
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
from dc_verify.step.step8.code.source_admission import read_json, write_json, digest, now
from dc_verify.step.step8.code.cross_source_input import require, successful_runs, indexed
from dc_verify.step.step9.code.state_restoration import restore
from dc_verify.step.step6.code.controller_replan import normalize_post_evidence_metadata, validate_post_evidence_decision
from dc_verify.step.step10.code.prompt import CODEX_PROMPT


def safe_url(value):
    if not isinstance(value, str): return None
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme in {'https', 'http'} and parsed.hostname and not parsed.username and not parsed.password else None
    except ValueError:
        return None


def safe_csv(value):
    text = '' if value is None else str(value)
    return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text


def normalize_pair_time(pair):
    row = copy.deepcopy(pair)
    changes = []
    alignment = row['time_comparison']['relation']
    if alignment != 'SAME_INTERVAL' and row['comparison_dimensions']['temporal_basis'] == 'SAME':
        row['comparison_dimensions']['temporal_basis'] = 'UNKNOWN'
        changes.append({'pair_id': row['pair_id'], 'field': 'comparison_dimensions.temporal_basis', 'before': 'SAME',
            'after': 'UNKNOWN', 'reason': 'Actual metric time is unknown or has different interval/precision; equal values do not prove equal time.'})
    row['time_alignment'] = alignment
    return row, changes


def workflow_view(decision, pending):
    result = copy.deepcopy(decision['updated_plan'])
    terminal = decision['decision'] in {'DEFER', 'REQUEST_HUMAN_REVIEW', 'COMPLETE'}
    status = {'DEFER': 'DEFERRED', 'REQUEST_HUMAN_REVIEW': 'AWAITING_HUMAN_REVIEW', 'COMPLETE': 'CLOSED'}.get(decision['decision'], 'PLANNED')
    corrections = []
    for task in result:
        if terminal:
            corrections.append({'task_id': task['task_id'], 'field': 'execution_status', 'before': task['execution_status'], 'after': status,
                'reason': 'Terminal Controller decision; not scheduled executable work.'})
            task['execution_status'] = status
        task['runnable'] = not terminal and pending.get('downstream_execution_allowed', False)
    return {'status': status, 'tasks': result, 'automated_work_allowed': not terminal and pending.get('downstream_execution_allowed', False),
        'selected_action_executed': bool(pending.get('executed')), 'normalizations': corrections}


def load_inputs(source):
    source = Path(source).resolve()
    required = ['run.json', 'input_snapshot.json', 'controller_decision.json', 'controller_state.json', 'controller_api_input.json',
        'restored_controller_state.json', 'human_review_input.json', 'decision_provenance.json', 'api_calls.json',
        'cross_source_reconciled.json', 'next_action_input.json', 'execution_verification.json']
    require(all((source / n).is_file() for n in required), 'Required STEP9 report input missing')
    run = read_json(source / 'run.json')
    require(run['status'] == 'SUCCESS', 'Latest Controller run is not successful')
    snapshot = read_json(source / 'input_snapshot.json')
    watched = {str(Path(p).resolve()): sha for p, sha in snapshot['watched_files'].items()}
    for p, sha in watched.items():
        require(Path(p).is_file() and digest(Path(p).read_bytes()) == sha, 'STEP9 recorded input changed: ' + p)
    saved = read_json(source / 'restored_controller_state.json')
    runtime = [Path(p) for p in watched if Path(p).name == 'runtime_tools.json'
        and read_json(p).get('tools') == saved['runtime_tools']]
    require(bool(runtime), 'No hash-linked runtime snapshot for Controller state')
    restored, api_input, reconciled, _, inherited = restore(run['source_run'], runtime[0])
    require(saved == restored and read_json(source / 'controller_api_input.json') == api_input, 'Controller state cannot be restored from same lineage')
    require(read_json(source / 'cross_source_reconciled.json') == reconciled, 'Reconciled pairs differ from same lineage')
    decision = read_json(source / 'controller_decision.json')
    _, validation = validate_post_evidence_decision(decision, restored)
    require(validation['status'] == 'PASSED', 'Controller decision contract failed')
    provenance = read_json(source / 'decision_provenance.json')
    response_file = Path(provenance['file'])
    require(response_file.is_file() and source in response_file.parents, 'Selected response is outside Controller run')
    response = read_json(response_file)
    require(response['response_id'] == provenance['response_id'] and response['api_status'] == 'completed', 'Selected Controller API response mismatch')
    normalized, _ = normalize_post_evidence_metadata(response['parsed'], restored)
    require(normalized == decision, 'Controller decision differs from its recorded model choice/normalization')
    updated, pending = read_json(source / 'controller_state.json'), read_json(source / 'next_action_input.json')
    require(updated['decision'] == decision and updated['search_budget'] == saved['search_budget']
        and pending == updated['pending_action'] and pending['action'] == decision['next_action'], 'Controller state/decision/handoff mismatch')
    require(not pending['executed'] and not updated['selected_action_executed'], 'Report input unexpectedly includes executed followup work')
    cross_input = read_json(Path(run['source_run']) / 'controller_input.json')
    inventory, documents = cross_input['claim_inventory'], cross_input['documents']
    claims = indexed(inventory, lambda c: c['claim_key'])
    require(set(claims) == set(saved['known_claim_keys']) and set(documents) == set(saved['review_context']['document_ids']), 'Claim/document scope mismatch')
    human = read_json(source / 'human_review_input.json')
    for scope in human['source_review_issue_scopes']:
        for ref in scope['claims']:
            c = claims.get(ref['claim_key'])
            require(c and c['origin'] == 'USER' and ref['document_id'] == c['claim']['source_document_id']
                and ref['source_page'] == c['claim']['source_page'] and ref['evidence_quote'] == c['claim']['evidence_quote'], 'Human review source scope is ambiguous or changed')
    for p in source.rglob('*'):
        if p.is_file(): watched[str(p.resolve())] = digest(p.read_bytes())
    watched.update(inherited)
    return {'source': source, 'run': run, 'state': saved, 'decision': decision, 'pending': pending, 'human': human,
        'inventory': inventory, 'documents': documents, 'pairs': reconciled, 'watched': watched,
        'upstream_api_records': read_json(source / 'api_calls.json')}


def source_snapshots(documents, output):
    result = []
    import pymupdf
    from dc_verify.step.step7.code.document_preparer import parse_html
    folder = output / 'originals'; folder.mkdir()
    for index, (doc_id, d) in enumerate(sorted(documents.items()), 1):
        require(d['format'] in {'PDF', 'HTML'}, 'Unsupported original document format')
        raw = Path(d['source_file']).read_bytes()
        require(digest(raw) == d['source_sha256'], 'Original source hash mismatch')
        name = f'source_{index:02d}_' + digest(doc_id.encode())[:12]
        if d['format'] == 'PDF':
            name += '.pdf'
            with pymupdf.open(stream=raw, filetype='pdf') as pdf: page_count = len(pdf)
            text_path = None
        else:
            name += '.html.txt'; page_count = 1
            body, _ = parse_html(raw)
            text_path = 'originals/' + name + '.readable.txt'
            (output / text_path).write_text(body, encoding='utf-8')
        (folder / name).write_bytes(raw)
        result.append({'document_id': doc_id, 'title': d['title'], 'source_url': safe_url(d.get('source_url')),
            'format': d['format'], 'sha256': d['source_sha256'], 'local_path': 'originals/' + name, 'readable_path': text_path,
            'page_count': page_count, 'published_at': d.get('published_at'), 'document_period': d.get('document_period'),
            'publication_review': d.get('publication_review'), 'source_admission': d['source_admission']['analysis_admission'],
            'origin': d['original_group'], 'independence_status': d['independence_status'],
            'date_note': '공식 공개일 미확인' if d.get('published_at') is None else '원문 HTML 게시 메타데이터' if d['format'] == 'HTML' else '원래 문서 메타데이터 · 독립 확인 전'})
    return result


def evidence_rows(inventory, sources, policy):
    docs = {d['document_id']: d for d in sources}
    result = []
    for item in inventory:
        c = item['claim'];d = docs[c['source_document_id']]
        require(type(c['source_page']) is int and 1 <= c['source_page'] <= d['page_count'], 'Invalid source page')
        valid = item['source_status'] == 'VERIFIED' and item['semantic_status'] == 'MATCH' and item['original_group'] != 'QUARANTINED'
        result.append({'claim_key': item['claim_key'], 'claim_id': c['claim_id'], 'project_name': c['project_name'],
            'metric_type': c['metric_type'], 'metric_label': policy['metric_labels'].get(c['metric_type'], c['metric_type']),
            'metric_name': c['metric_name'], 'value': c['value'], 'unit': c['unit'], 'raw_value': c['raw_value'],
            'value_qualifier': c['value_qualifier'], 'measurement_basis': c.get('measurement_basis'),
            'basis_label': policy['basis_labels'].get(c.get('measurement_basis'), c.get('measurement_basis') or '기준 미기재'),
            'facility_scope': c.get('facility_scope'), 'statement_status': c.get('statement_status'), 'temporal_basis': c['temporal_basis'],
            'metric_as_of': c.get('metric_as_of'), 'source_published_at': c.get('source_published_at'),
            'source_status': item['source_status'], 'semantic_status': item['semantic_status'], 'semantic_reason': item['semantic_reason'],
            'evidence_level': item['evidence_level'], 'original_group': item['original_group'], 'origin': item['origin'],
            'is_provisional': item['is_provisional'], 'source_admission': item['source_admission'],
            'source_statement_usable': valid, 'final_evidence_allowed': item['final_evidence_allowed'],
            'document_id': c['source_document_id'], 'source_title': d['title'], 'source_page': c['source_page'],
            'source_location_label': ('p. ' + str(c['source_page'])) if d['format'] == 'PDF' else 'HTML 본문',
            'source_url': d['source_url'], 'local_source': d['local_path'] + ('#page=' + str(c['source_page']) if d['format'] == 'PDF' else ''),
            'readable_source': d['readable_path'], 'source_sha256': d['sha256'], 'evidence_quote': c['evidence_quote'],
            'source_verifier_quote': item['source_quote'], 'notes': c['notes'], 'uncertainties': item['uncertainties']})
    return result


def build_report(inputs, sources, policy):
    rows = evidence_rows(inputs['inventory'], sources, policy)
    lookup = {r['claim_key']: r for r in rows}
    decision, state = inputs['decision'], inputs['state']
    flow = workflow_view(decision, inputs['pending'])
    pairs, corrections = [], list(flow['normalizations'])
    for pair in inputs['pairs']['pair_results']:
        row, changes = normalize_pair_time(pair);corrections += changes
        row['evidence_references'] = [{**{k:v for k,v in ref.items() if k != 'source_file'},
            'local_source': lookup[ref['claim_key']]['local_source']} for ref in row['evidence_references']]
        row['relationship_label'] = policy['relationship_labels'].get(row['relationship'], row['relationship'])
        row['model_reason'] = row['reason']
        row['display_reason'] = ' '.join(row['local_gates']) if row.get('model_relationship') != row['relationship'] and row['local_gates'] else row['reason']
        pairs.append(row)
    metric_views = []
    for definition in policy['metric_views']:
        candidates = [r for r in rows if r['metric_type'] in definition['metric_types']]
        usable = [r for r in candidates if r['source_statement_usable']]
        metric_views.append({**definition, 'claim_keys': [r['claim_key'] for r in usable],
            'excluded_claim_keys': [r['claim_key'] for r in candidates if not r['source_statement_usable']],
            'status': 'SOURCE_STATEMENTS_ONLY' if usable else 'UNCONFIRMED',
            'values_as_stated': list(dict.fromkeys((str(r['value']).rstrip('0').rstrip('.') if r['value'] is not None and '.' in str(r['value']) else str(r['value']))
                + (' ' + r['unit'] if r['unit'] else '') if r['value'] is not None else r['raw_value'] for r in usable)),
            'final_fact_confirmed': any(r['final_evidence_allowed'] and not r['is_provisional']
                and r['evidence_level'] == 'INDEPENDENTLY_CORROBORATED' for r in usable)})
    base_issues = {i['issue_id']: i for i in state['issues']}
    scopes = {s['issue_id']: s['claims'] for s in inputs['human']['source_review_issue_scopes']}
    reviews, requirements = [], []
    for issue in decision['issue_decisions']:
        base = base_issues[issue['issue_id']]
        keys = list(dict.fromkeys(issue['supporting_claim_keys'] + [c['claim_key'] for c in scopes.get(issue['issue_id'], [])]))
        require(set(keys) <= set(lookup), 'Unknown review reference')
        references = [{'claim_key': k, 'document_id': lookup[k]['document_id'], 'source_page': lookup[k]['source_page'],
            'local_source': lookup[k]['local_source'], 'evidence_quote': lookup[k]['evidence_quote']} for k in keys]
        source_review = base['kind'] == 'SOURCE_REVIEW'
        title = '원문 문맥·대상·조건 검토' if source_review else base['description']
        question = ('아래 전체 Claim Key와 원문 페이지의 문맥·대상·조건을 확인해 주세요.' if source_review else
            f'“{base["description"]}”을 뒷받침하는 명시적 원문과 정보 기준일을 확인해 주세요.')
        review = {'issue_id': issue['issue_id'], 'priority': issue['priority'], 'kind': base['kind'], 'title': title,
            'question': question, 'controller_status': issue['status'], 'remaining_evidence': issue['remaining_evidence'],
            'references': references, 'annotation_effect': policy['review_annotation_effect']}
        reviews.append(review)
        if not source_review:
            requirements.append({**review, 'controller_reason': issue['reason'],
                'reason_kind': 'CONTROLLER_ASSESSMENT', 'executed_searches': base['executed_searches'], 'remaining_searches': base['remaining_searches']})
    report_id = 'review_' + digest(json.dumps({'source': inputs['run']['selected_response_id'], 'claims': [r['claim_key'] for r in rows]}, sort_keys=True).encode())[:16]
    return {'schema_version': 'DC_VERIFY_REVIEW_REPORT_1', 'report_id': report_id, 'generated_at': now(),
        'review_context': {k: state['review_context'][k] for k in ['review_goal', 'target_projects', 'as_of_date']},
        'decision': {'decision': decision['decision'], 'goal_status': decision['goal_status'], 'should_stop': decision['should_stop'],
            'reason': decision['decision_reason'], 'reason_kind': 'CONTROLLER_JUDGMENT', 'information_value_quantitatively_validated': False,
            'stop_condition': decision['stop_condition'], 'current_operation_independently_verified': decision['current_operation_independently_verified'],
            'comparison_status': decision['comparison_status']}, 'workflow': flow, 'metric_views': metric_views, 'requirements': requirements,
        'evidence': rows, 'sources': sources, 'cross_source_pairs': pairs,
        'local_diagnostics': [{**p, 'evidence_references': [{**{k:v for k,v in ref.items() if k != 'source_file'},
            'local_source': lookup[ref['claim_key']]['local_source']} for ref in p['evidence_references']]} for p in inputs['pairs']['diagnostic_results']],
        'human_review': reviews, 'normalizations': corrections, 'search_budget': state['search_budget'],
        'summary_counts': {'documents': len(sources), 'claims': len(rows), 'source_and_meaning_passed': sum(r['source_statement_usable'] for r in rows),
            'quarantined': sum(r['original_group'] == 'QUARANTINED' for r in rows), 'pairs': len(pairs),
            'partial_requirements': sum(r['controller_status'] == 'PARTIALLY_SUPPORTED' for r in requirements)},
        'execution': {'new_model_calls': 0, 'new_searches': 0, 'recorded_controller_responses': inputs['run']['api_responses'],
            'recorded_controller_usage': inputs['run']['usage'], 'prior_executed_searches': state['tool_call_statistics']['executed_search_requests'],
            'original_claims_modified': False, 'annotation_effect': policy['review_annotation_effect']}}


def render_html(report):
    payload = json.dumps(report, ensure_ascii=False, allow_nan=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    template = (HERE / 'report.html').read_text(encoding='utf-8')
    return template.replace('/*REPORT_CSS*/', (HERE / 'report.css').read_text(encoding='utf-8')).replace('/*REPORT_JS*/',
        (HERE / 'report.js').read_text(encoding='utf-8')).replace('REPORT_DATA_PLACEHOLDER', payload)


def validate_report(report, inputs):
    original = {c['claim_key']: c for c in inputs['inventory']}
    rows = indexed(report['evidence'], lambda c: c['claim_key'])
    checks = {'complete_claim_coverage': set(rows) == set(original),
        'original_claim_values_preserved': all(all(r[k] == original[key]['claim'].get(k) for k in
            ['metric_type', 'value', 'unit', 'raw_value', 'metric_as_of', 'evidence_quote']) for key, r in rows.items()),
        'admission_and_quarantine_preserved': all(r['source_admission'] == original[key]['source_admission'] and r['original_group'] == original[key]['original_group']
            and r['is_provisional'] == original[key]['is_provisional'] and r['final_evidence_allowed'] == original[key]['final_evidence_allowed'] for key,r in rows.items()),
        'no_new_model_or_search': report['execution']['new_model_calls'] == report['execution']['new_searches'] == 0,
        'controller_choice_unchanged': report['decision']['decision'] == inputs['decision']['decision'],
        'judgment_not_quantitative_fact': report['decision']['reason_kind'] == 'CONTROLLER_JUDGMENT' and not report['decision']['information_value_quantitatively_validated'],
        'unknown_time_not_same': all(p['comparison_dimensions']['temporal_basis'] != 'SAME' for p in report['cross_source_pairs'] if p['time_alignment'] != 'SAME_INTERVAL'),
        'defer_not_runnable': report['decision']['decision'] != 'DEFER' or (report['workflow']['status'] == 'DEFERRED' and not report['workflow']['automated_work_allowed']
            and all(t['execution_status'] == 'DEFERRED' and not t['runnable'] for t in report['workflow']['tasks'])),
        'human_review_has_scoped_keys': all('::' in ref['claim_key'] and ref['claim_key'] in rows for q in report['human_review'] for ref in q['references']),
        'no_metric_substitution': all(set(v['claim_keys']) <= {r['claim_key'] for r in rows.values() if r['source_statement_usable'] and r['metric_type'] in v['metric_types']} for v in report['metric_views'])}
    return {'status': 'PASSED' if all(checks.values()) else 'FAILED', 'checks': checks, 'scope': 'Report provenance and presentation invariants; not new source/fact verification.'}


def build(source_run=None, output_dir=stage_data(10) / 'output', policy_file=report_policy_path()):
    output = Path(output_dir) / ('review_report_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True)
    try:
        if source_run is None:
            candidates = successful_runs(stage_data(9) / 'output', 'controller_cycle_*', [])
            require(bool(candidates), 'No successful Controller execution found')
            source_run = candidates[0][1]
        inputs = load_inputs(source_run); policy = read_json(policy_file)
        inputs['watched'][str(Path(policy_file).resolve())] = digest(Path(policy_file).read_bytes())
        sources = source_snapshots(inputs['documents'], output)
        report = build_report(inputs, sources, policy)
        report['input_snapshot_digest'] = digest(json.dumps(inputs['watched'], sort_keys=True).encode())
        validation = validate_report(report, inputs)
        require(validation['status'] == 'PASSED', 'Report contract validation failed')
        write_json(output / 'report.json', report)
        write_json(output / 'human_review_template.json', {'report_id': report['report_id'], 'annotation_effect': policy['review_annotation_effect'],
            'reviews': [{'issue_id': q['issue_id'], 'claim_keys': [r['claim_key'] for r in q['references']], 'status': 'NOT_REVIEWED', 'comment': '', 'evidence_url': ''} for q in report['human_review']]})
        write_json(output / 'input_snapshot.json', inputs['watched'])
        write_json(output / 'normalizations.json', report['normalizations'])
        write_json(output / 'model_questions_audit.json', {'questions': inputs['human']['questions_from_model'],
            'displayed_questions_are_scoped_and_regenerated': True, 'source_scope_overrides_ambiguous_short_ids': True})
        write_json(output / 'execution_verification.json', validation)
        fields = ['claim_key', 'metric_label', 'raw_value', 'value', 'unit', 'basis_label', 'metric_as_of', 'source_published_at',
            'source_status', 'semantic_status', 'evidence_level', 'source_admission', 'is_provisional', 'original_group',
            'document_id', 'source_page', 'source_url', 'source_sha256', 'evidence_quote']
        with (output / 'evidence.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream);writer.writerow(fields)
            writer.writerows([[safe_csv(row.get(k)) for k in fields] for row in report['evidence']])
        (output / 'report.html').write_text(render_html(report), encoding='utf-8')
        (output / 'used_codex_prompt.txt').write_text(CODEX_PROMPT, encoding='utf-8')
        require(all(Path(p).is_file() and digest(Path(p).read_bytes()) == sha for p,sha in inputs['watched'].items()), 'Original inputs changed during report generation')
        require(all(digest((output / d['local_path']).read_bytes()) == d['sha256'] for d in sources), 'Copied original hashes differ')
        run = {'status': 'SUCCESS', 'stage': 'STEP10_OFFLINE_REPORT', 'finished_at': now(), 'source_run': str(Path(source_run).resolve()),
            'output_dir': str(output.resolve()), 'report_id': report['report_id'], 'counts': report['summary_counts'],
            'new_model_calls': 0, 'new_searches': 0, 'source_inputs_unchanged': True, 'original_snapshots_verified': len(sources),
            'html_sha256': digest((output / 'report.html').read_bytes()), 'browser_qa': 'NOT_YET_PERFORMED'}
    except Exception as exc:
        run = {'status': 'BLOCKED', 'stage': 'STEP10_OFFLINE_REPORT', 'finished_at': now(), 'error_type': type(exc).__name__,
            'reason': str(exc), 'output_dir': str(output.resolve()), 'new_model_calls': 0, 'new_searches': 0}
        # A blocked build must not leave a success-looking report.
        for name in ['report.html', 'report.json']:
            if (output / name).exists(): (output / name).unlink()
    write_json(output / 'run.json', run)
    (output / 'execution_log.jsonl').write_text(json.dumps(run, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(run, ensure_ascii=False), flush=True)
    return run


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-run', type=Path)
    p.add_argument('--output-dir', type=Path, default=stage_data(10) / 'output')
    p.add_argument('--policy-file', type=Path, default=report_policy_path())
    return 0 if build(**vars(p.parse_args()))['status'] == 'SUCCESS' else 1


if __name__ == '__main__': raise SystemExit(main())
