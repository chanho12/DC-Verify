"""STEP8-3 boundary/regression tests. API mocks are not live search or evidence."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
from dc_verify.step.step8.code import cross_source as agent
from dc_verify.step.step8.code import cross_source_input as validator
from dc_verify.step.step8.code import pair_matcher as matcher


CONTEXT = {'review_goal': 'Verify current capacity and operating state', 'target_projects': ['Project A'],
    'as_of_date': '2026-11-11', 'required_metrics': ['operational capacity', 'operating state'],
    'supporting_metrics': ['contract capacity', 'occupancy', 'power']}


def item(doc, cid='c1', kind='provided_it_capacity', value=10, when='2026-06', **extra):
    c = {'claim_id': cid, 'source_document_id': doc, 'source_page': 1, 'project_name': 'Project A',
        'metric_type': kind, 'metric_name': kind, 'raw_value': f'{value} MW' if value is not None else 'full occupancy',
        'value': value, 'unit': 'MW', 'value_qualifier': 'exact', 'facility_scope': 'Project A',
        'temporal_basis': 'operating_claim', 'metric_as_of': when, 'evidence_quote': f'{kind}: {value} MW', 'notes': []}
    c.update(extra)
    return {'claim_key': doc + '::' + cid, 'claim': c, 'origin': 'EXTERNAL',
        'source_status': 'VERIFIED', 'semantic_status': 'MATCH', 'semantic_reason': '원문 해석 일치',
        'original_group': 'ACCEPTED', 'is_provisional': True, 'source_admission': 'CONDITIONAL',
        'comparison_eligibility': 'PROVISIONAL_STATEMENT', 'source_quote': c['evidence_quote'],
        'uncertainties': [], 'original_quarantine_reasons': [], 'general_context': False,
        'document': {'document_id': doc, 'source_sha256': 'hash-' + doc, 'source_file': '/source/' + doc,
            'published_at': None, 'document_period': {'value': '2026-09'}, 'independence_status': 'NOT_ESTABLISHED'}}


def pair(a, b):
    return matcher.make_pair(a, b, matcher.candidate_kind(a, b), CONTEXT)


def response(p, relationship='CONSISTENT_STATEMENTS'):
    return {'pair_id': p['pair_id'], 'relationship': relationship,
        'comparison_dimensions': {k: 'SAME' for k in agent.Dimensions.model_fields},
        'reason': '입력된 주장의 관계 확인', 'differences': [], 'additional_evidence_needed': [],
        'evidence_claim_keys': [p['left_key'], p['right_key']], 'related_requirements': []}


class MatcherTests(unittest.TestCase):
    def test_intake_and_it_load_are_different_metrics(self):
        self.assertEqual(matcher.candidate_kind(item('a', kind='power_intake_capacity'), item('b')), 'METRIC_BOUNDARY')

    def test_contract_and_occupancy_are_different_states(self):
        self.assertEqual(matcher.candidate_kind(item('a', kind='contract_ratio'), item('b', kind='occupancy_rate')), 'STATE_BOUNDARY')

    def test_different_targets_not_paired(self):
        self.assertIsNone(matcher.candidate_kind(item('a'), item('b', project_name='Another facility')))

    def test_publication_does_not_fill_metric_time(self):
        self.assertEqual(matcher.explicit_time(item('a', when=None))['status'], 'UNKNOWN')

    def test_halfyear_quarter_overlap_preserves_precision(self):
        r = matcher.time_relation(item('a', when='1H 2026'), item('b', when='2026년 2분기'))
        self.assertEqual(r['relation'], 'OVERLAPPING_DIFFERENT_PRECISION')
        self.assertEqual(r['left']['precision'], 'HALF_YEAR')

    def test_plan_event_date_can_be_explicit_in_claim(self):
        i = item('a', kind='planned_service_start', when=None, raw_value='2024년 4분기 서비스 개시')
        self.assertEqual(matcher.explicit_time(i)['basis'], 'EXPLICIT_EVENT_EXPRESSION')
        self.assertEqual(matcher.explicit_time(i)['start'], '2024-10-01')

    def test_multiple_event_months_not_collapsed(self):
        i = item('a', kind='planned_service_start', when=None, raw_value='2024년 8월 준공, 10월 오픈')
        self.assertEqual(matcher.explicit_time(i)['status'], 'UNKNOWN')

    def test_contract_progress_has_distinct_dates(self):
        p = pair(item('a', kind='contract_ratio', when='2025년 2분기'), item('b', kind='contract_status', when='2026년 2분기'))
        self.assertEqual(p['kind'], 'STATE_PROGRESS')
        self.assertEqual(p['time_comparison']['relation'], 'LEFT_BEFORE_RIGHT')

    def test_same_document_or_content_is_not_cross_source(self):
        a, b = item('a', cid='c1'), item('a', cid='c2')
        d = {'claims': [a, b], 'review_context': CONTEXT}
        self.assertEqual(matcher.select_pairs(d)['candidate_count'], 0)
        b = item('b');b['document']['source_sha256'] = a['document']['source_sha256']
        d['claims'] = [a, b]
        self.assertEqual(matcher.select_pairs(d)['candidate_count'], 0)

    def test_failed_claim_never_in_statement_api_pairs(self):
        a, b = item('a'), item('b')
        a.update(semantic_status='MISMATCH', comparison_eligibility='DIAGNOSTIC_ONLY')
        selected = matcher.select_pairs({'claims': [a, b], 'review_context': CONTEXT})
        self.assertFalse(selected['selected_pairs'])
        self.assertEqual(len(selected['local_diagnostics']), 1)

    def test_ambiguous_textual_ratio_has_diagnostic_pair(self):
        a = item('a', kind='occupancy_rate', value=None, when=None, unit=None)
        a.update(semantic_status='UNCLEAR', comparison_eligibility='DIAGNOSTIC_ONLY')
        p = matcher.select_pairs({'claims': [a, item('b', kind='occupancy_rate', value=50)], 'review_context': CONTEXT})
        self.assertEqual(p['selected_pairs'][0]['mode'], 'AMBIGUITY_DIAGNOSTIC')

    def test_deterministic_bounded_selection_and_no_mutation(self):
        d = {'claims': [item(str(n)) for n in range(10)], 'review_context': CONTEXT}
        original = copy.deepcopy(d)
        a, b = matcher.select_pairs(d, max_pairs=3), matcher.select_pairs(d, max_pairs=3)
        self.assertEqual(a, b)
        self.assertEqual(len(a['selected_pairs']), 3)
        self.assertEqual(d, original)


class GuardTests(unittest.TestCase):
    def check(self, a, b, relation='CONSISTENT_STATEMENTS', modify=None):
        p = pair(a, b);r = response(p, relation)
        if modify:
            modify(r)
        return agent.guard_pair(r, p, {a['claim_key']: a, b['claim_key']: b})

    def test_same_source_values_are_not_independent_truth(self):
        r = self.check(item('a'), item('b'))
        self.assertEqual(r['relationship'], 'CONSISTENT_STATEMENTS')
        self.assertFalse(r['independently_corroborated'])
        self.assertFalse(r['final_evidence_allowed'])
        self.assertEqual(r['comparison_mode'], 'PROVISIONAL_STATEMENTS')

    def test_unknown_time_does_not_become_same_interval(self):
        r = self.check(item('a', when=None), item('b', when=None))
        self.assertEqual(r['relationship'], 'SAME_VALUE_TIME_UNRESOLVED')

    def test_metric_boundary_cannot_be_consistent(self):
        r = self.check(item('a', kind='power_intake_capacity'), item('b'))
        self.assertEqual(r['relationship'], 'DIFFERENT_METRIC')

    def test_state_boundary_cannot_be_conflict(self):
        r = self.check(item('a', kind='contract_ratio', value=100), item('b', kind='occupancy_rate', value=50), 'POTENTIAL_CONFLICT')
        self.assertEqual(r['relationship'], 'DIFFERENT_STATE_BASIS')

    def test_full_occupancy_does_not_become_confirmed_ratio(self):
        a = item('a', kind='occupancy_rate', value=None, when=None, unit=None)
        a.update(semantic_status='UNCLEAR', comparison_eligibility='DIAGNOSTIC_ONLY')
        r = self.check(a, item('b', kind='occupancy_rate', value=50))
        self.assertEqual(r['relationship'], 'DEFINITION_UNCLEAR')
        self.assertEqual(r['comparison_mode'], 'DIAGNOSTIC_ONLY')

    def test_questioning_100_percent_does_not_erase_definition_uncertainty(self):
        a = item('a', kind='occupancy_rate', value=None, when=None, unit=None)
        a.update(semantic_status='UNCLEAR', comparison_eligibility='DIAGNOSTIC_ONLY')
        r = self.check(a, item('b', kind='occupancy_rate', value=50), 'DEFINITION_UNCLEAR',
            modify=lambda r: r.update(reason='full occupancy가 100%를 뜻하는지는 확인되지 않음'))
        self.assertEqual(r['relationship'], 'DEFINITION_UNCLEAR')
        self.assertFalse(r['final_evidence_allowed'])

    def test_same_metric_misreading_requires_focused_review(self):
        r = self.check(item('a', kind='backup_power_capacity'), item('b', kind='backup_power_capacity'), 'DIFFERENT_METRIC')
        self.assertEqual(r['relationship'], 'DEFINITION_UNCLEAR')
        self.assertTrue(r['retry_required'])

    def test_overlapping_halfyear_and_quarter_are_not_chronological_change(self):
        r = self.check(item('a', kind='occupancy_rate', value=50, unit='%', when='1H 2026'),
            item('b', kind='occupancy_rate', value=50, unit='%', when='2026년 2분기'), 'TEMPORAL_UPDATE_CANDIDATE')
        self.assertEqual(r['relationship'], 'SAME_VALUE_TIME_UNRESOLVED')

    def test_different_explicit_dates_allow_update_candidate(self):
        r = self.check(item('a', kind='contract_ratio', when='2025-06'), item('b', kind='contract_status', when='2026-06'), 'TEMPORAL_UPDATE_CANDIDATE')
        self.assertEqual(r['relationship'], 'TEMPORAL_UPDATE_CANDIDATE')

    def test_document_period_does_not_justify_update(self):
        r = self.check(item('a', when=None), item('b', when=None), 'TEMPORAL_UPDATE_CANDIDATE')
        self.assertEqual(r['relationship'], 'INSUFFICIENT_EVIDENCE')

    def test_different_dates_are_not_a_conflict(self):
        r = self.check(item('a', when='2025-06'), item('b', value=20, when='2026-06'), 'POTENTIAL_CONFLICT')
        self.assertEqual(r['relationship'], 'INSUFFICIENT_EVIDENCE')

    def test_actual_comparable_different_values_can_be_potential_conflict(self):
        r = self.check(item('a'), item('b', value=20), 'POTENTIAL_CONFLICT')
        self.assertEqual(r['relationship'], 'POTENTIAL_CONFLICT')
        self.assertFalse(r['final_evidence_allowed'])

    def test_inequality_qualifier_not_treated_as_exact_conflict(self):
        r = self.check(item('a', value_qualifier='at_least'), item('b', value=20), 'POTENTIAL_CONFLICT')
        self.assertEqual(r['relationship'], 'INSUFFICIENT_EVIDENCE')

    def test_fabricated_calculated_quantity_is_blocked(self):
        r = self.check(item('a'), item('b'), modify=lambda r: r.update(reason='따라서 실제 운영량은 5MW이다'))
        self.assertEqual(r['relationship'], 'INSUFFICIENT_EVIDENCE')

    def test_failed_input_cannot_be_promoted_even_with_wrong_pair_mode(self):
        a, b = item('a'), item('b')
        a.update(source_status='MISMATCH')
        r = self.check(a, b)
        self.assertEqual(r['comparison_mode'], 'DIAGNOSTIC_ONLY')
        self.assertEqual(r['relationship'], 'INSUFFICIENT_EVIDENCE')

    def test_wrong_evidence_key_rejected(self):
        with self.assertRaisesRegex(ValueError, 'evidence keys'):
            self.check(item('a'), item('b'), modify=lambda r: r.update(evidence_claim_keys=['invented', 'invented']))

    def test_focused_response_is_reused_without_api(self):
        a, b = item('a'), item('b');p = pair(a, b)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder/'api_response.json').write_text(json.dumps({'api_status':'completed','response_id':'test_response',
                'parsed':{'pair_results':[response(p,'SAME_VALUE_TIME_UNRESOLVED')]}}))
            with patch.object(agent,'request_structured_output',side_effect=AssertionError('Unexpected API')):
                r = agent.repair_pair(p,{a['claim_key']:a,b['claim_key']:b},CONTEXT,folder)
            self.assertEqual(r['focused_review_response_id'],'test_response')

    def test_original_fields_and_quotes_unchanged(self):
        a, b = item('a'), item('b');before = copy.deepcopy([a, b])
        r = self.check(a, b)
        self.assertEqual([a, b], before)
        self.assertEqual(r['evidence_references'][0]['claim_quote'], a['claim']['evidence_quote'])

    def test_api_schema_rejects_unselected_pair_or_requirement(self):
        a, b = item('a'), item('b');p = pair(a, b);schema = agent.response_schema([p], CONTEXT)
        r = response(p);r['pair_id'] = 'invented'
        with self.assertRaises(ValueError):schema.model_validate({'pair_results': [r]})
        r = response(p);r['related_requirements'] = ['shortened goal']
        with self.assertRaises(ValueError):schema.model_validate({'pair_results': [r]})


class InputTests(unittest.TestCase):
    def test_tampered_or_missing_snapshot_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'original';p.write_text('actual')
            s = Path(tmp)/'snapshot.json';s.write_text(json.dumps({str(p): 'wrong'}))
            with self.assertRaises(validator.InputBlocked):validator.check_snapshot(s, set())
            p.unlink()
            with self.assertRaises(validator.InputBlocked):validator.check_snapshot(s, set())

    def test_unavailable_source_is_blocked_without_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = agent.execute(source_run=Path(tmp)/'missing', output_dir=Path(tmp)/'output', client=object())
            self.assertEqual(r['status'], 'BLOCKED')
            self.assertFalse(r['api_attempted'])

    def test_rejected_resume_preserves_previous_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp);original = {'status':'SUCCESS','response_id':'prior_result'}
            (p/'run.json').write_text(json.dumps(original))
            r = agent.execute(source_run=p/'missing',resume=p,client=object())
            self.assertEqual(r['status'],'BLOCKED')
            self.assertEqual(json.loads((p/'run.json').read_text()),original)

    def test_newest_declared_success_is_validated_instead_of_falling_back(self):
        with patch.object(validator,'successful_runs',return_value=[(None,Path('/newest'),{}),(None,Path('/older'),{})]):
            self.assertEqual(validator.newest_source('/base'),Path('/newest'))

    def test_claim_keys_preserve_local_ids_but_are_document_scoped(self):
        a, b = item('a'), item('b')
        self.assertNotEqual(validator.claim_key(a['claim']), validator.claim_key(b['claim']))
        self.assertEqual(a['claim']['claim_id'], b['claim']['claim_id'])

    def test_deny_is_never_an_eligible_comparison(self):
        row = {'source_status': 'MISMATCH', 'semantic_status': 'MATCH', 'original_group': 'ACCEPTED', 'is_provisional': False}
        self.assertEqual(validator.eligibility(row), 'DIAGNOSTIC_ONLY')


class ActualArtifactRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (stage_data(8) / 'output/external_verification_20261111T100854380658Z').resolve()
        if not cls.source.exists():raise unittest.SkipTest('Local regression artifacts not installed')
        cls.data = validator.load_input(cls.source)

    def test_restores_seven_documents_and_document_policies(self):
        self.assertEqual(len(self.data['documents']), 7)
        self.assertEqual(set(self.data['document_scopes']),set(self.data['documents']))
        self.assertEqual(set(self.data['review_context']['document_ids']), set(self.data['documents']))
        self.assertNotIn('source_admission_policy', self.data['review_context'])
        self.assertEqual(len([d for d in self.data['documents'].values() if d['source_admission']['analysis_admission']=='CONDITIONAL']), 4)

    def test_preserves_ten_original_and_48_external_claims(self):
        self.assertEqual(Counter(c['origin'] for c in self.data['claims']), {'USER':10,'EXTERNAL':48})
        self.assertEqual(len([c for c in self.data['claims'] if c['original_group']=='QUARANTINED']),15)

    def test_selects_subset_and_full_occupancy_diagnostic(self):
        p = matcher.select_pairs(self.data)
        keys = {k for r in p['selected_pairs'] for k in [r['left_key'],r['right_key']]}
        self.assertLess(len(keys), len(self.data['claims']))
        full = next(c['claim_key'] for c in self.data['claims'] if c['claim']['raw_value']=='full occupancy')
        self.assertTrue(any(full in [r['left_key'],r['right_key']] and r['mode']=='AMBIGUITY_DIAGNOSTIC' for r in p['selected_pairs']))

    def test_mismatched_document_policy_cannot_be_silently_applied(self):
        actual = validator.read_json
        def altered(path):
            v = actual(path)
            if Path(path) == self.source / 'cross_check_input.json':
                v['original_document_jobs'][0]['source_admission']['analysis_admission'] = 'ALLOW'
            return v
        with patch.object(validator,'read_json',side_effect=altered):
            with self.assertRaisesRegex(validator.InputBlocked,'Admission lineage'):validator.load_input(self.source)

    def test_claim_original_change_detected_before_pairing(self):
        actual = validator.read_json
        def altered(path):
            v = actual(path)
            if Path(path) == self.source / 'cross_check_input.json':v['external_claim_reviews'][0]['original_claim']['value'] = 999
            return v
        with patch.object(validator,'read_json',side_effect=altered):
            with self.assertRaises(validator.InputBlocked):validator.load_input(self.source)


if __name__ == '__main__':
    unittest.main()
