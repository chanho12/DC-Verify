"""Controller policy boundaries; synthetic decisions are not actual verification."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
from dc_verify.step.step9.code.controller import execute
from dc_verify.step.step9.code.state_restoration import restore, restore_search_history
from dc_verify.step.step6.code.controller_replan import validate_post_evidence_decision as validate, normalize_post_evidence_metadata as normalize


def fixture():
    context = {'review_goal':'Check requested metric', 'target_projects':['Project A'],'as_of_date':'2026-11-11'}
    issues = [{'issue_id':i,'kind':'GOAL_EVIDENCE' if i!='source_c' else 'SOURCE_REVIEW','goal_completion_allowed':False}
        for i in ['issue_a','issue_b','source_c']]
    state = {'review_context':context,'issues':issues,'known_claim_keys':['doc::c1'],
        'evidence_observation':{'execution_statement_claim_keys':['doc::c1'],'current_operation_independently_verified':False},
        'search_budget':{'remaining_attempts':{'issue_a':1,'issue_b':0}},'goal_evidence_sufficient':False,'comparison_target_supplied':False,
        'runtime_tools':[{'tool_id':'WEB','available':True,'supported_actions':['SEARCH_OFFICIAL']},
                         {'tool_id':'LOCAL','available':True,'supported_actions':['SEARCH_LOCAL','REVERIFY_SOURCE']}],
        'execution_history':{'action_history':[{'executed':True,'type':'SEARCH_OFFICIAL','target':'Project A','issue_ids':['issue_a'],
            'finished_at':'2026-01-01T00:00:00+00:00'}]},
        'new_information_events':[{'event_id':'new_source','status':'SUCCESS','operation':'SOURCE_REVIEW_COMPLETED','finished_at':'2026-02-01T00:00:00+00:00'}],
        'available_documents':[{'document_id':'doc','all_pages':[1,2],'unprocessed_pages':[2]}]}
    action = {'type':'SEARCH_OFFICIAL','target':'Project A','reason':'New evidence changes the search need','issue_ids':['issue_a'],
        'required_evidence':['Direct proof'],'query_suggestions':['Project A original metric definition'],'local_scope':[],
        'search_constraints':{'official_sources_only':True,'published_on_or_before':'2026-11-11','same_project_required':True,
            'metric_definition_required':True,'metric_as_of_required':True,'scope_and_status_required':True,'reverify_before_use':True},
        'retry_justification':'A newly reviewed source revealed a definition gap','new_information_event_ids':['new_source']}
    rows = [{'issue_id':i,'priority':n,'status':'UNRESOLVED','reason':'Evidence is incomplete','supporting_claim_keys':[],
        'remaining_evidence':['Direct proof']} for n,i in enumerate(['issue_a','issue_b','source_c'],1)]
    result = {'decision':'REPLAN','goal_status':'INCOMPLETE',**context,'observations':['A completion statement exists but is not independent current proof'],
        'observed_execution_claim_keys':['doc::c1'],'current_operation_independently_verified':False,'issue_decisions':rows,
        'next_action':action,'selected_tool_id':'WEB','expected_information_gain':'Clarify the newly identified gap',
        'updated_plan':[{'task_id':'next1','priority':1,'action':'SEARCH_OFFICIAL','issue_ids':['issue_a'],
            'description':'Find direct proof','execution_status':'PLANNED'}],
        'remaining_issue_ids':['issue_a','issue_b','source_c'],'human_review_questions':[],
        'comparison_status':'NO_COMPARISON_TARGET','should_stop':False,'stop_condition':'Stop after the remaining attempt if no direct proof',
        'decision_reason':'Specific new evidence justifies a bounded search'}
    return state,result


def terminal(result, decision='DEFER'):
    result['decision']=decision;result['should_stop']=True;result['selected_tool_id']=None
    action = 'ASK_USER' if decision=='REQUEST_HUMAN_REVIEW' else 'FINALIZE' if decision=='COMPLETE' else 'DEFER'
    result['next_action']['type']=action;result['updated_plan'][0]['action']=action
    result['human_review_questions']=['Please obtain direct dated original evidence']
    return result


class PolicyTests(unittest.TestCase):
    def check(self,state,r): return validate(r,state)[1]
    def bad(self,state,r): self.assertEqual(self.check(state,r)['status'],'FAILED')

    def test_replan_with_real_new_evidence_and_remaining_budget(self):
        s,r=fixture();self.assertEqual(self.check(s,r)['status'],'PASSED')

    def test_exhausted_issue_search_is_blocked(self):
        s,r=fixture();s['search_budget']['remaining_attempts']['issue_a']=0;self.bad(s,r)

    def test_followup_search_cannot_bypass_zero_budget(self):
        s,r=fixture();r['updated_plan'].append({'task_id':'later','priority':2,'action':'SEARCH_OFFICIAL','issue_ids':['issue_b'],
            'description':'Search later','execution_status':'PLANNED'});self.bad(s,r)

    def test_defer_is_allowed_even_when_some_budget_remains(self):
        s,r=fixture();terminal(r);self.assertEqual(self.check(s,r)['status'],'PASSED')

    def test_defer_after_budget_exhaustion(self):
        s,r=fixture();s['search_budget']['remaining_attempts']={'issue_a':0,'issue_b':0};terminal(r)
        self.assertEqual(self.check(s,r)['status'],'PASSED')

    def test_human_review_is_a_valid_terminal_action(self):
        s,r=fixture();terminal(r,'REQUEST_HUMAN_REVIEW');r['goal_status']='NEEDS_INPUT'
        self.assertEqual(self.check(s,r)['status'],'PASSED')

    def test_complete_without_evidence_is_rejected(self):
        s,r=fixture();terminal(r,'COMPLETE');r['goal_status']='COMPLETE';self.bad(s,r)

    def test_valid_complete_requires_all_required_issues_resolved(self):
        s,r=fixture();terminal(r,'COMPLETE');r['goal_status']='COMPLETE';s['goal_evidence_sufficient']=True
        for x,y in zip(s['issues'],r['issue_decisions']):
            if x['kind']=='GOAL_EVIDENCE':x['goal_completion_allowed']=True;y['status']='RESOLVED'
        r['remaining_issue_ids']=['source_c'];self.assertEqual(self.check(s,r)['status'],'PASSED')

    def test_issuer_statement_is_not_independent_operation_proof(self):
        s,r=fixture();r['current_operation_independently_verified']=True;self.bad(s,r)

    def test_existing_completion_statement_cannot_be_omitted(self):
        s,r=fixture();r['observed_execution_claim_keys']=[];self.bad(s,r)

    def test_new_issue_id_cannot_reset_budget(self):
        s,r=fixture();r['issue_decisions'][0]['issue_id']='new_alias';self.bad(s,r)

    def test_no_repetition_without_later_real_event(self):
        s,r=fixture();r['next_action']['new_information_event_ids']=[];self.bad(s,r)

    def test_model_call_does_not_count_as_new_evidence(self):
        s,r=fixture();s['new_information_events'][0]['operation']='MODEL_API';self.bad(s,r)

    def test_older_event_cannot_justify_retry(self):
        s,r=fixture();s['new_information_events'][0]['finished_at']='2025-01-01T00:00:00+00:00';self.bad(s,r)

    def test_unavailable_tool_is_rejected(self):
        s,r=fixture();s['runtime_tools'][0]['available']=False;self.bad(s,r)

    def test_tool_action_mismatch_is_rejected(self):
        s,r=fixture();r['selected_tool_id']='LOCAL';self.bad(s,r)

    def test_source_review_cannot_receive_fresh_search_budget(self):
        s,r=fixture();r['next_action']['issue_ids']=['source_c'];r['updated_plan'][0]['issue_ids']=['source_c'];self.bad(s,r)

    def test_local_search_must_use_unprocessed_pages(self):
        s,r=fixture();r['next_action'].update(type='SEARCH_LOCAL',local_scope=[{'document_id':'doc','pages':[1]}])
        r['selected_tool_id']='LOCAL';r['updated_plan'][0]['action']='SEARCH_LOCAL';self.bad(s,r)
        r['next_action']['local_scope'][0]['pages']=[2];self.assertEqual(self.check(s,r)['status'],'PASSED')

    def test_reverify_uses_existing_local_scope_without_search_charge(self):
        s,r=fixture();r['next_action'].update(type='REVERIFY_SOURCE',local_scope=[{'document_id':'doc','pages':[1]}])
        r['selected_tool_id']='LOCAL';r['updated_plan'][0]['action']='REVERIFY_SOURCE';s['search_budget']['remaining_attempts']['issue_a']=0
        self.assertEqual(self.check(s,r)['status'],'PASSED')

    def test_terminal_decision_needs_human_due_diligence_questions(self):
        s,r=fixture();terminal(r);r['human_review_questions']=[];self.bad(s,r)

    def test_terminal_decision_cannot_schedule_another_search(self):
        s,r=fixture();terminal(r);r['updated_plan'].append({'task_id':'later','priority':2,'action':'SEARCH_OFFICIAL','issue_ids':['issue_a'],
            'description':'Search','execution_status':'PLANNED'});self.bad(s,r)

    def test_terminal_state_cannot_claim_to_continue(self):
        s,r=fixture();terminal(r);r['should_stop']=False;self.bad(s,r)

    def test_missing_comparison_target_is_not_a_comparison(self):
        s,r=fixture();r['comparison_status']='CONDITIONAL';self.bad(s,r)

    def test_metadata_fixes_preserve_model_decision_and_source_statuses(self):
        s,r=fixture();terminal(r);r['remaining_issue_ids']=['issue_a','issue_b'];r['next_action']['local_scope']=[{'document_id':'doc','pages':[1]}]
        before=copy.deepcopy(r);n,changes=normalize(r,s)
        self.assertEqual(r,before);self.assertEqual(n['decision'],r['decision']);self.assertEqual(n['issue_decisions'],r['issue_decisions'])
        self.assertEqual(n['remaining_issue_ids'],['issue_a','issue_b','source_c']);self.assertEqual(len(changes),2)
        self.assertEqual(self.check(s,n)['status'],'PASSED')

    def test_normalizer_does_not_fix_budget_or_completion_violations(self):
        s,r=fixture();s['search_budget']['remaining_attempts']['issue_a']=0;n,_=normalize(r,s);self.bad(s,n)
        terminal(r,'COMPLETE');r['goal_status']='COMPLETE';n,_=normalize(r,s);self.bad(s,n)

    def test_duplicate_priorities_are_stably_presented(self):
        s,r=fixture();r['issue_decisions'][1]['priority']=1
        n,_=normalize(r,s);self.assertEqual([x['priority'] for x in n['issue_decisions']],[1,2,3])

    def test_unknown_claim_references_are_not_repaired(self):
        s,r=fixture();r['issue_decisions'][0]['supporting_claim_keys']=['invented::claim'];n,_=normalize(r,s);self.bad(s,n)

    def test_negative_priority_is_not_silently_repaired(self):
        s,r=fixture();r['issue_decisions'][0]['priority']=-1;n,_=normalize(r,s);self.bad(s,n)

    def test_missing_source_run_blocks_before_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            r=execute(source_run=Path(tmp)/'missing',output_dir=Path(tmp)/'out',client=object())
            self.assertEqual(r['status'],'BLOCKED');self.assertFalse(r['api_attempted'])


class ArtifactRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=stage_data(8) / 'output/cross_source_20261111T114634884568Z'
        if not cls.source.exists():raise unittest.SkipTest('Local artifact regression not installed')
        cls.s,cls.a,cls.r,cls.c,cls.w=restore(cls.source,stage_data(9) / 'runtime_tools.json')

    def test_actual_search_counts_and_remaining_budget(self):
        self.assertEqual(sum(self.s['search_budget']['executed_attempts'].values()),5)
        self.assertEqual(sorted(self.s['search_budget']['remaining_attempts'].values()),[0,1,1,1])

    def test_all_previous_issue_ids_preserved(self):
        from dc_verify.step.step8.code.source_admission import read_json
        old=read_json(Path(self.s['lineage']['prior_controller'])/'controller_input.json')['issue_inventory']
        self.assertEqual({i['issue_id'] for i in old},{i['issue_id'] for i in self.s['issues']})

    def test_definition_unknown_is_not_same_measurement_basis(self):
        for r in self.r['pair_results']:
            if r['relationship']=='DEFINITION_UNCLEAR':
                self.assertNotEqual(r['comparison_dimensions']['metric_definition'],'SAME')
                self.assertNotEqual(r['comparison_dimensions']['measurement_basis'],'SAME')

    def test_identical_context_definition_is_consistent_but_insufficient(self):
        rows=[r for r in self.r['pair_results'] if r['goal_role']=='CONTEXT_ONLY'];self.assertTrue(rows)
        self.assertTrue(all(r['relationship']=='CONSISTENT_STATEMENTS' and r['goal_evidence_status']=='INSUFFICIENT' for r in rows))

    def test_completion_statement_exists_without_independent_current_verification(self):
        self.assertTrue(self.s['evidence_observation']['execution_statement_exists'])
        self.assertFalse(self.s['evidence_observation']['current_operation_independently_verified'])
        self.assertTrue(any(i['execution_statement_claim_keys'] for i in self.s['issues']))

    def test_independently_corroborated_historical_opening_is_not_current_operation(self):
        from dc_verify.step.step9.code.state_restoration import evidence_observation
        from dc_verify.step.step8.code.cross_source_input import load_input
        from dc_verify.step.step8.code.source_admission import read_json
        data=load_input(read_json(self.source/'run.json')['source_run'])
        for c in data['claims']:
            if c['claim']['metric_type']=='service_start':c['evidence_level']='INDEPENDENTLY_CORROBORATED';c['is_provisional']=False
        self.assertFalse(evidence_observation(data,self.r)['current_operation_independently_verified'])

    def test_historical_absence_reason_not_sent_as_current_fact(self):
        self.assertTrue(any('prior_reason' in i for i in self.s['issues']))
        self.assertTrue(all('prior_reason' not in i for i in self.a['issues']))

    def test_receipt_hash_corruption_blocks_restoration(self):
        from dc_verify.step.step8.code.source_admission import read_json
        from dc_verify.step.step9.code import state_restoration as module
        search=Path(self.s['lineage']['search_execution']);actual=module.read_json
        def altered(path):
            v=actual(path)
            if Path(path)==search/'state.json':v['attempts'][0]['receipt_sha256']='wrong'
            return v
        prior={i['issue_id']:i for i in self.s['issues']}
        with patch.object(module,'read_json',side_effect=altered):
            with self.assertRaisesRegex(ValueError,'receipt hash'):restore_search_history(search,read_json(search/'executor_input.json'),prior)


if __name__=='__main__':unittest.main()
