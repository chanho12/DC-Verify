"""Boundary tests; synthetic responses are not actual API verification."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
from dc_verify.step.step8.code import external_verification as v


def fixture(group='ACCEPTED', conditional=False, html=False):
    claim = {'claim_id': 'doc_test:c1', 'source_document_id': 'doc_test', 'source_page': 1,
        'evidence_quote': 'IT Load 10MW', 'raw_value': '10MW', 'metric_name': 'IT Load',
        'metric_type': 'provided_it_capacity', 'value': 10.0, 'unit': 'MW', 'temporal_basis': 'unspecified',
        'measurement_basis': 'CUSTOMER_PROVIDED_CAPACITY', 'statement_status': 'ANNOUNCEMENT'}
    admission = {'analysis_admission': 'CONDITIONAL' if conditional else 'ALLOW',
        'provisional_claims_required': conditional, 'published_at': None if conditional else '2026-01-01', 'source_sha256': 'hash'}
    job = {'document_id': 'doc_test', 'source_admission': admission,
        'review_claims': [{'claim': claim, 'original_group': group, 'quarantine_reasons': []}],
        'source_bundle': {'analysis_handoff': {'required_metrics': ['capacity', 'operation']}}}
    prepared = {'ORIGINAL_HTML': {'text': 'IT Load\n10MW'} if html else None}
    row = {'claim_id': claim['claim_id'], 'source_page': 1, 'source_status': 'VERIFIED',
        'original_quote': 'IT Load 10MW', 'quote_relation': 'EXACT', 'source_reason': '수치·인용 확인',
        'semantic_status': 'MATCH', 'field_checks': {k: 'MATCH' for k in v.FieldChecks.model_fields},
        'semantic_reason': '제공 가능 용량이며 실제 소비 전력과 구분됨', 'interpreted_metric': 'provided_it_capacity',
        'uncertainties': [], 'quarantine_classification': 'NOT_QUARANTINED' if group == 'ACCEPTED' else 'FORMAT_ONLY',
        'correction_proposals': [], 'requirement_support': [
            {'requirement': k, 'support': 'PARTIAL', 'reason': '참고 지표'} for k in ['capacity', 'operation']],
        'evidence': {'claim_id': claim['claim_id'], 'statement_type': 'ANNOUNCEMENT', 'evidence_level': 'SOURCE_STATEMENT_ONLY',
            'supports_review_goal': False, 'reason': '기업 설명', 'missing_evidence': ['실제 소비 전력'], 'review_required': False}}
    result = {'document_id': 'doc_test', 'cover_period_observation': None, 'publication_uncertainties': [], 'claim_results': [row]}
    return job, prepared, [{'source_page': 1}], result


class ReviewTests(unittest.TestCase):
    def review(self, args):
        job, prepared, images, result = args
        return v.enforce_review(result, job, prepared, images)[0]['claim_results'][0]

    def test_missing_original_image_blocks_grounding(self):
        job, p, _, result = fixture()
        row = self.review((job, p, [], result))
        self.assertEqual(row['source_status'], 'NOT_VERIFIED')
        self.assertEqual(row['evidence']['evidence_level'], 'NOT_ASSESSED')

    def test_html_does_not_require_pdf_image(self):
        job, p, _, result = fixture(html=True)
        row = self.review((job, p, [], result))
        self.assertEqual(row['source_status'], 'VERIFIED')

    def test_html_fabricated_quote_is_not_verified(self):
        args = fixture(html=True)
        args[3]['claim_results'][0]['original_quote'] = 'IT Load 100MW'
        row = self.review(args)
        self.assertEqual(row['source_status'], 'NEEDS_MANUAL_REVIEW')

    def test_semantic_mismatch_keeps_source_axis_separate(self):
        args = fixture()
        args[3]['claim_results'][0]['semantic_status'] = 'MISMATCH'
        row = self.review(args)
        self.assertEqual(row['source_status'], 'VERIFIED')
        self.assertEqual(row['evidence']['evidence_level'], 'NOT_ASSESSED')

    def test_wrong_scope_cannot_pass_semantics(self):
        args = fixture()
        args[3]['claim_results'][0]['field_checks']['project_scope'] = 'MISMATCH'
        self.assertEqual(self.review(args)['semantic_status'], 'MISMATCH')

    def test_ambiguous_time_cannot_pass_semantics(self):
        args = fixture()
        args[3]['claim_results'][0]['field_checks']['temporal_conditions'] = 'UNCLEAR'
        self.assertEqual(self.review(args)['evidence']['evidence_level'], 'NOT_ASSESSED')

    def test_conditional_grounding_is_not_admission(self):
        args = fixture(conditional=True)
        args[3]['claim_results'][0]['evidence']['supports_review_goal'] = True
        row = self.review(args)
        self.assertTrue(row['is_provisional'])
        self.assertFalse(row['evidence']['supports_review_goal'])
        self.assertFalse(row['final_evidence_allowed'])
        self.assertIsNone(row['published_at'])

    def test_format_error_is_not_auto_released(self):
        row = self.review(fixture(group='QUARANTINED'))
        self.assertEqual(row['quarantine_classification'], 'FORMAT_ONLY')
        self.assertFalse(row['quarantine_released'])
        self.assertFalse(row['correction_applied'])

    def test_independent_evidence_cannot_be_invented(self):
        args = fixture()
        args[3]['claim_results'][0]['evidence']['evidence_level'] = 'INDEPENDENTLY_CORROBORATED'
        self.assertEqual(self.review(args)['evidence']['evidence_level'], 'SOURCE_STATEMENT_ONLY')

    def test_additional_document_support_is_out_of_scope(self):
        args = fixture()
        args[3]['claim_results'][0]['evidence']['evidence_level'] = 'ADDITIONAL_DOCUMENT_SUPPORT'
        self.assertEqual(self.review(args)['evidence']['evidence_level'], 'SOURCE_STATEMENT_ONLY')

    def test_unknown_quote_relation_cannot_pass_source(self):
        args = fixture()
        args[3]['claim_results'][0]['quote_relation'] = 'UNCLEAR'
        self.assertEqual(self.review(args)['source_status'], 'NEEDS_MANUAL_REVIEW')

    def test_claim_ids_must_cover_input_exactly(self):
        args = fixture()
        args[3]['claim_results'] = []
        with self.assertRaisesRegex(ValueError, 'coverage'):
            self.review(args)

    def test_duplicate_claim_ids_rejected(self):
        args = fixture()
        args[3]['claim_results'] *= 2
        with self.assertRaisesRegex(ValueError, 'coverage'):
            self.review(args)

    def test_page_change_rejected(self):
        args = fixture()
        args[3]['claim_results'][0]['source_page'] = 2
        with self.assertRaisesRegex(ValueError, 'page'):
            self.review(args)

    def test_quarantine_group_change_rejected(self):
        args = fixture(group='QUARANTINED')
        args[3]['claim_results'][0]['quarantine_classification'] = 'NOT_QUARANTINED'
        row = self.review(args)
        self.assertEqual(row['original_group'], 'QUARANTINED')
        self.assertEqual(row['quarantine_classification'], 'UNRESOLVED')
        self.assertFalse(row['quarantine_released'])

    def test_missing_requirement_rejected(self):
        args = fixture()
        args[3]['claim_results'][0]['requirement_support'].pop()
        with self.assertRaisesRegex(ValueError, 'Requirement'):
            self.review(args)

    def test_api_response_and_claim_immutable(self):
        args = fixture()
        before = copy.deepcopy(args)
        self.review(args)
        self.assertEqual(args, before)

    def test_units_are_not_normalized_or_converted(self):
        self.assertNotEqual(v.normalize('20kW+'), v.normalize('20MW'))
        self.assertEqual(v.normalize('20\nkW+'), v.normalize('20kW+'))

    def test_efficiency_is_not_it_capacity_even_if_model_passes(self):
        args = fixture(group='QUARANTINED')
        args[0]['review_claims'][0]['claim'].update(metric_type='design_it_capacity', unit=None, value=1.3)
        row = self.review(args)
        self.assertEqual(row['semantic_status'], 'MISMATCH')
        self.assertEqual(row['evidence']['evidence_level'], 'NOT_ASSESSED')

    def test_contract_it_load_is_not_operational_capacity(self):
        args = fixture()
        args[0]['review_claims'][0]['claim'].update(metric_type='operational_it_capacity', measurement_basis='OPERATIONAL_CAPACITY')
        self.assertEqual(self.review(args)['semantic_status'], 'MISMATCH')

    def test_completion_is_not_a_planned_start(self):
        args = fixture()
        args[0]['review_claims'][0]['claim'].update(metric_type='planned_service_start', raw_value='Service launched in December 2024', statement_status='EXECUTION_CLAIM')
        self.assertEqual(self.review(args)['semantic_status'], 'MISMATCH')

    def test_html_linebreak_is_format_only_and_stays_quarantined(self):
        args = fixture(group='QUARANTINED', html=True)
        claim = args[0]['review_claims'][0]['claim']
        claim.update(evidence_quote='20\nkW+\nHigh power', raw_value='20kW+', metric_name='20kW+', value=20.0, unit='kW', metric_type='other')
        args[1]['ORIGINAL_HTML']['text'] = claim['evidence_quote']
        args[1]['mechanical_ocr_checks'] = {claim['claim_id']: {'issues': ['RAW_VALUE_MISMATCH', 'METRIC_NAME_MISMATCH']}}
        args[3]['claim_results'][0].update(source_status='MISMATCH', semantic_status='MISMATCH')
        row = self.review(args)
        self.assertEqual(row['quarantine_classification'], 'FORMAT_ONLY')
        self.assertEqual(row['source_status'], 'VERIFIED')
        self.assertEqual(row['semantic_status'], 'UNCLEAR')
        self.assertFalse(row['quarantine_released'])

    def test_percent_symbol_omission_requires_original_symbol(self):
        c = {'evidence_quote': '99.999\n%\nAvailability', 'raw_value': '99.999', 'metric_name': '99.999', 'unit': '%'}
        self.assertTrue(v.html_format_proof(c, {'text': c['evidence_quote']}, {'issues': ['OCR_UNIT_MISMATCH']}))
        self.assertFalse(v.html_format_proof(c, {'text': '99.999 Availability'}, {'issues': ['OCR_UNIT_MISMATCH']}))

    def test_textual_occupancy_does_not_establish_a_ratio(self):
        args = fixture()
        args[0]['review_claims'][0]['claim'].update(metric_type='occupancy_rate', measurement_basis='OCCUPANCY_RATIO', value=None, raw_value='full occupancy')
        self.assertEqual(self.review(args)['semantic_status'], 'UNCLEAR')

    def test_cached_assessment_cannot_grant_new_eligibility(self):
        args = fixture()
        row = self.review(args)
        old = {'REVIEW_CONTEXT': {}, 'SOURCE_ADMISSION': {}, 'AVAILABLE_EVIDENCE': {},
            'SOURCE_AND_SEMANTIC_RESULT': {'claim_results': [row]}}
        stricter = copy.deepcopy(old)
        stricter['SOURCE_AND_SEMANTIC_RESULT']['claim_results'][0]['semantic_status'] = 'UNCLEAR'
        self.assertTrue(v.reusable_evidence(old, stricter))
        self.assertFalse(v.reusable_evidence(stricter, old))

    def test_tampered_original_blocks_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'source.pdf'
            p.write_bytes(b'changed')
            images, errors = v.render_original_pages(p,
                {'document_id': 'doc_test', 'source_pdf_sha256': 'wrong'}, [{'source_page': 1}], Path(tmp) / 'pages')
            self.assertFalse(images)
            self.assertEqual(errors[1]['status'], 'VERIFICATION_FAILED')

    def test_pdf_period_does_not_establish_posting_date(self):
        job, p, _, result = fixture(conditional=True)
        job['source_admission']['as_of_date'] = '2026-11-11'
        job['source_bundle']['documents'] = [{'source_sha256': 'hash', 'document_period': {'value': '2026-09'}}]
        result['cover_period_observation'] = '2026년 9월'
        date = v.review_date(job, p, result)
        self.assertIsNone(date['published_at'])
        self.assertEqual(date['publication_status'], 'UNCONFIRMED')
        self.assertEqual(date['analysis_admission_unchanged'], 'CONDITIONAL')


if __name__ == '__main__':
    unittest.main()
