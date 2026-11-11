"""STEP9 orchestration. Reuses the STEP6 Controller; selects work without running it."""
from __future__ import annotations

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import argparse
import copy
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
from dc_verify.codex import DEFAULT_MODEL, safe_error
from dc_verify.step.step8.code.source_admission import digest, now, read_json, write_json
from dc_verify.step.step8.code.cross_source_input import successful_runs, require
from dc_verify.step.step9.code.state_restoration import restore
from dc_verify.step.step9.code.prompt import CONTROLLER_POST_EVIDENCE_PROMPT, CODEX_PROMPT, build_user_prompt
from dc_verify.step.step6.code.controller_replan import (decide_after_cross_check, validate_post_evidence_decision,
    build_action_input, signature, normalize_post_evidence_metadata)


def execute(source_run=None, runtime_file=stage_data(9) / 'runtime_tools.json', output_dir=stage_data(9) / 'output',
            resume=None, dry_run=False, client=None):
    output = Path(resume).resolve() if resume else Path(output_dir) / ('controller_cycle_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    output.mkdir(parents=True, exist_ok=bool(resume))
    def event(name, **values):
        with (output / 'execution_log.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': now(), 'event': name, **values}, ensure_ascii=False) + '\n')
    event('STARTED', model=DEFAULT_MODEL, dry_run=dry_run)
    try:
        if source_run is None:
            candidates = successful_runs(stage_data(8) / 'output', 'cross_source_*', [])
            require(bool(candidates), 'No successful Cross-source execution')
            source_run = candidates[0][1]
        state, api_state, reconciled, corrections, watched = restore(source_run, runtime_file)
        fingerprint = {'watched_files': watched, 'api_input_sha256': digest(json.dumps(api_state, ensure_ascii=False, sort_keys=True).encode()),
            'model': DEFAULT_MODEL, 'system_prompt_sha256': digest(CONTROLLER_POST_EVIDENCE_PROMPT.encode())}
        snap = output / 'input_snapshot.json'
        require(not snap.exists() or read_json(snap) == fingerprint, 'Resume state or prompt changed; use a separate execution')
        write_json(snap, fingerprint)
        write_json(output / 'restored_controller_state.json', state)
        write_json(output / 'controller_api_input.json', api_state)
        write_json(output / 'cross_source_reconciled.json', reconciled)
        write_json(output / 'input_corrections.json', {'original_files_modified': False, 'corrections': corrections,
            'observation_update': state['evidence_observation'], 'search_budget_restored': state['search_budget']})
        (output / 'used_codex_prompt.txt').write_text(CODEX_PROMPT, encoding='utf-8')
    except Exception as exc:
        error = safe_error(exc)
        if isinstance(exc, ValueError): error['message'] = str(exc)
        elif isinstance(exc, OSError): error['message'] = '필수 입력·원문 파일을 읽을 수 없습니다. 원본 경로와 접근 권한을 확인하세요.'
        result = {'status': 'BLOCKED', 'error': error, 'api_attempted': False, 'output_dir': str(output), 'finished_at': now()}
        target = output / ('resume_rejected_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.json') if resume else output / 'run.json'
        write_json(target, result);event('INPUT_BLOCKED', error=error)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result
    user = build_user_prompt(api_state)
    decision = None
    selected_response = None
    responses, validation, new_requests = [], None, 0
    if not dry_run:
        feedback = ''
        for number in range(1, 3):
            folder = output / 'attempts' / f'attempt_{number:02d}'
            folder.mkdir(parents=True, exist_ok=True)
            f = folder / 'api_response.json'
            try:
                if f.exists():
                    response = read_json(f)
                    require(response.get('api_status') == 'completed' and response.get('model', '').startswith(DEFAULT_MODEL)
                        and response.get('response_id'), 'Cached Controller response is incomplete or uses a different model')
                    normalized, changes = normalize_post_evidence_metadata(response['parsed'], state)
                    decision, validation = validate_post_evidence_decision(normalized, state)
                    validation['metadata_normalizations'] = changes
                    event('CACHED_RESPONSE_REUSED', response_id=response.get('response_id'))
                else:
                    (folder / 'system_prompt.txt').write_text(CONTROLLER_POST_EVIDENCE_PROMPT, encoding='utf-8')
                    (folder / 'user_prompt.txt').write_text(user + feedback, encoding='utf-8')
                    new_requests += 1
                    print(json.dumps({'stage': 'CONTROLLER_API_REQUESTED', 'attempt': number, 'model': DEFAULT_MODEL}), flush=True)
                    event('MODEL_API_REQUEST_STARTED', attempt=number)
                    response, decision, validation = decide_after_cross_check(state, CONTROLLER_POST_EVIDENCE_PROMPT, user + feedback, client=client)
                    write_json(f, response)
                    event('MODEL_API_RESPONSE', attempt=number, response_id=response.get('response_id'), usage=response.get('usage'))
                responses.append(response)
                _, raw_validation = validate_post_evidence_decision(response['parsed'], state)
                write_json(folder / 'raw_validation.json', raw_validation)
                write_json(folder / 'validation.json', validation)
                if validation['status'] == 'PASSED':
                    selected_response = {'file': str(f.resolve()), 'response_id': response['response_id'],
                        'decision_unchanged': response['parsed']['decision'] == decision['decision'],
                        'action_type_unchanged': response['parsed']['next_action']['type'] == decision['next_action']['type'],
                        'metadata_normalizations': validation.get('metadata_normalizations', [])}
                    break
                feedback = '\n이전 응답의 계약 검사 오류를 수정하라. 원래 상태·예산은 바꾸지 않는다:\n' + json.dumps(validation['errors'], ensure_ascii=False)
            except Exception as exc:
                error = safe_error(exc)
                if isinstance(exc, ValueError): error['message'] = str(exc)
                write_json(folder / 'failure.json', error);event('MODEL_OR_VALIDATION_FAILED', attempt=number, error=error)
                validation = {'status': 'FAILED', 'errors': [error]}
                break
    unchanged = all(Path(p).is_file() and digest(Path(p).read_bytes()) == sha for p, sha in watched.items())
    status = 'PREPARED' if dry_run else 'SUCCESS' if validation and validation['status'] == 'PASSED' and unchanged else 'FAILED'
    if status == 'SUCCESS':
        write_json(output / 'controller_decision.json', decision)
        write_json(output / 'decision_provenance.json', selected_response)
        history = copy.deepcopy(state['execution_history'])
        history['events'] += state['new_information_events']
        compatible_issues = [{**i, 'reason': i['current_reason']} for i in state['issues']]
        handoff = build_action_input(decision, {'initial_plan': {'analysis_handoff': state['review_context']},
            'issue_inventory': compatible_issues, 'execution_history': history}, output)
        handoff.update(schema_version='STEP9_ACTION_HANDOFF_V1', selected_tool_id=decision['selected_tool_id'],
            runtime_tools=state['runtime_tools'], search_budget_before=state['search_budget'],
            should_stop=decision['should_stop'], downstream_execution_allowed=decision['decision'] == 'REPLAN',
            new_tool_calls_performed=False, budget_consumed=False,
            controller_state_file=str((output / 'controller_state.json').resolve()))
        write_json(output / 'next_action_input.json', handoff)
        updated = {**state, 'decision': decision, 'pending_action': handoff, 'cycle_status': decision['decision'],
            'search_budget': copy.deepcopy(state['search_budget']), 'execution_history': history,
            'selected_action_executed': False}
        decisions = {i['issue_id']: i for i in decision['issue_decisions']}
        updated['issues'] = [{**i, 'controller_observation': decisions[i['issue_id']]} for i in state['issues']]
        write_json(output / 'controller_state.json', updated)
        write_json(output / 'controller_observation.json', {'decision': decision['decision'], 'goal_status': decision['goal_status'],
            'execution_statement_observations': state['evidence_observation']['execution_statement_observations'],
            'current_operation_independently_verified': decision['current_operation_independently_verified'],
            'remaining_issue_ids': decision['remaining_issue_ids'], 'human_review_questions': decision['human_review_questions'],
            'next_action': decision['next_action'], 'selected_tool_id': decision['selected_tool_id'],
            'search_budget_unchanged': True, 'selected_action_executed': False, 'should_stop': decision['should_stop']})
        original_inventory = read_json(Path(source_run) / 'controller_input.json')['claim_inventory']
        scopes = [{'issue_id': i['issue_id'], 'description': i['description'], 'claims': [
            {'claim_key': c['claim_key'], 'document_id': c['claim']['source_document_id'], 'source_page': c['claim']['source_page'],
             'source_file': c['document']['source_file'], 'evidence_quote': c['claim']['evidence_quote']}
            for c in original_inventory if c['origin'] == 'USER' and c['claim']['claim_id'] in i['claim_ids']]}
            for i in state['issues'] if i['kind'] == 'SOURCE_REVIEW']
        write_json(output / 'human_review_input.json', {'questions_from_model': decision['human_review_questions'],
            'source_review_issue_scopes': scopes, 'review_context': state['review_context'],
            'scope_note': '간략 cN 표기와 후속 문서라는 모델 표현으로 문서를 추측하지 않는다. Source Review 대상은 이 파일의 검증된 사용자 원문 문서·페이지 연결을 따른다.',
            'required_metric_substitution_allowed': False, 'goal_completed': decision['goal_status'] == 'COMPLETE'})
    responses = [read_json(p) for p in output.glob('attempts/*/api_response.json')]
    write_json(output / 'api_calls.json', {'responses': [{'file': str(p.resolve()), **{k: read_json(p).get(k) for k in ['response_id','model','usage','api_status']}}
        for p in output.glob('attempts/*/api_response.json')], 'includes_preserved_earlier_attempts': True})
    run = {'status': status, 'stage': 'STEP9_CONTROLLER_OBSERVE_DECIDE_REPLAN', 'model': DEFAULT_MODEL,
        'source_run': str(Path(source_run).resolve()), 'output_dir': str(output.resolve()), 'finished_at': now(),
        'decision': decision['decision'] if status == 'SUCCESS' else None,
        'next_action': decision['next_action']['type'] if status == 'SUCCESS' else None,
        'api_responses': len(responses), 'api_requests_this_invocation': new_requests,
        'selected_response_id': selected_response['response_id'] if selected_response else None,
        'usage': {k: sum((r.get('usage') or {}).get(k, 0) for r in responses) for k in ['input_tokens', 'output_tokens', 'total_tokens']},
        'prior_search_requests': state['tool_call_statistics']['executed_search_requests'], 'new_search_requests': 0,
        'selected_action_executed': False, 'source_inputs_unchanged': unchanged, 'validation': validation}
    write_json(output / 'run.json', run)
    if status == 'SUCCESS':
        if verify_execution(output)['status'] != 'PASSED':
            run['status'] = 'FAILED'
            write_json(output / 'run.json', run)
            handoff['downstream_execution_allowed'] = False
            write_json(output / 'next_action_input.json', handoff)
    event('FINISHED', **run)
    print(json.dumps(run, ensure_ascii=False), flush=True)
    return run


def verify_execution(output):
    output = Path(output)
    state, decision, updated, handoff = (read_json(output / n) for n in
        ['restored_controller_state.json', 'controller_decision.json', 'controller_state.json', 'next_action_input.json'])
    _, validation = validate_post_evidence_decision(decision, state)
    snapshot = read_json(output / 'input_snapshot.json')
    checks = {'controller_contract_passed': validation['status'] == 'PASSED',
        'source_inputs_unchanged': all(Path(p).is_file() and digest(Path(p).read_bytes()) == sha for p, sha in snapshot['watched_files'].items()),
        'issue_ids_preserved': {i['issue_id'] for i in state['issues']} == {i['issue_id'] for i in updated['issues']},
        'search_budget_unchanged': state['search_budget'] == updated['search_budget'] == handoff['search_budget_before'],
        'actual_search_actions_unchanged': state['execution_history']['action_history'] == updated['execution_history']['action_history'],
        'new_action_planned_not_executed': not handoff['executed'] and handoff['execution_status'] == 'PLANNED' and not updated['selected_action_executed'],
        'execution_statement_presence_preserved': set(decision['observed_execution_claim_keys']) == set(state['evidence_observation']['execution_statement_claim_keys']),
        'no_unsupported_current_independent_verification': decision['current_operation_independently_verified'] == state['evidence_observation']['current_operation_independently_verified'],
        'no_claim_or_admission_mutation': state['source_claims_modified'] is False,
        'termination_consistent': decision['should_stop'] == handoff['should_stop']}
    corrected = read_json(output / 'cross_source_reconciled.json')['pair_results']
    checks['unclear_definition_not_same_basis'] = all(r['comparison_dimensions']['metric_definition'] != 'SAME'
        and r['comparison_dimensions']['measurement_basis'] != 'SAME' for r in corrected if r['relationship'] == 'DEFINITION_UNCLEAR')
    checks['context_consistency_separate_from_goal_sufficiency'] = all(r['relationship'] == 'CONSISTENT_STATEMENTS'
        and r['goal_evidence_status'] == 'INSUFFICIENT' for r in corrected if r['goal_role'] == 'CONTEXT_ONLY')
    provenance = read_json(output / 'decision_provenance.json')
    checks['model_decision_and_action_preserved'] = provenance['decision_unchanged'] and provenance['action_type_unchanged']
    calls = read_json(output / 'api_calls.json')['responses']
    checks['actual_fixed_model_responses'] = bool(calls) and all(c['api_status'] == 'completed' and c['response_id']
        and c['model'].startswith(DEFAULT_MODEL) for c in calls) and len({c['response_id'] for c in calls}) == len(calls)
    result = {'status': 'PASSED' if all(checks.values()) else 'FAILED', 'checks': checks,
        'scope': 'Controller state/decision/termination invariants, not proof of real-world investment information.'}
    write_json(output / 'execution_verification.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', type=Path)
    parser.add_argument('--runtime-file', type=Path, default=stage_data(9) / 'runtime_tools.json')
    parser.add_argument('--output-dir', type=Path, default=stage_data(9) / 'output')
    parser.add_argument('--resume', type=Path)
    parser.add_argument('--dry-run', action='store_true')
    result = execute(**vars(parser.parse_args()))
    return 0 if result['status'] in {'SUCCESS', 'PREPARED'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
