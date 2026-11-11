"""Offline report boundaries and regression against the preserved experiment lineage."""

from dc_verify.settings import stage_data, runtime_root, env_path, report_policy_path
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
from dc_verify.step.step10.code import report_builder as b


class PresentationBoundaries(unittest.TestCase):
    def pair(self, relation='UNKNOWN', temporal='SAME'):
        return {'pair_id': 'synthetic_pair', 'time_comparison': {'relation': relation},
                'comparison_dimensions': {'temporal_basis': temporal}, 'value': 17}

    def test_unknown_time_is_not_same(self):
        result, changes = b.normalize_pair_time(self.pair())
        self.assertEqual(result['comparison_dimensions']['temporal_basis'], 'UNKNOWN')
        self.assertEqual(len(changes), 1)

    def test_different_precision_is_not_same(self):
        result, _ = b.normalize_pair_time(self.pair('OVERLAPPING_DIFFERENT_PRECISION'))
        self.assertEqual(result['comparison_dimensions']['temporal_basis'], 'UNKNOWN')

    def test_explicit_same_interval_is_preserved(self):
        result, changes = b.normalize_pair_time(self.pair('SAME_INTERVAL'))
        self.assertEqual(result['comparison_dimensions']['temporal_basis'], 'SAME')
        self.assertFalse(changes)

    def test_atemporal_definition_is_preserved(self):
        result, changes = b.normalize_pair_time(self.pair('UNKNOWN', 'NOT_APPLICABLE'))
        self.assertEqual(result['comparison_dimensions']['temporal_basis'], 'NOT_APPLICABLE')
        self.assertFalse(changes)

    def test_pair_view_does_not_mutate_original(self):
        pair = self.pair(); before = copy.deepcopy(pair)
        result, _ = b.normalize_pair_time(pair)
        self.assertEqual(pair, before)
        self.assertEqual(result['value'], 17)

    def test_defer_cannot_be_executed_even_if_pending_flag_true(self):
        decision = {'decision':'DEFER', 'updated_plan':[{'task_id':'t', 'execution_status':'PLANNED'}]}
        result = b.workflow_view(decision, {'downstream_execution_allowed':True})
        self.assertEqual(result['tasks'][0]['execution_status'], 'DEFERRED')
        self.assertFalse(result['tasks'][0]['runnable'])
        self.assertFalse(result['automated_work_allowed'])
        self.assertEqual(decision['updated_plan'][0]['execution_status'], 'PLANNED')

    def test_other_terminal_states_do_not_schedule_work(self):
        for action, expected in [('COMPLETE','CLOSED'), ('REQUEST_HUMAN_REVIEW','AWAITING_HUMAN_REVIEW')]:
            result = b.workflow_view({'decision':action, 'updated_plan':[]}, {})
            self.assertEqual(result['status'], expected)
            self.assertFalse(result['automated_work_allowed'])

    def test_replan_requires_downstream_permission(self):
        decision = {'decision':'REPLAN', 'updated_plan':[{'task_id':'t', 'execution_status':'PLANNED'}]}
        self.assertFalse(b.workflow_view(decision, {})['tasks'][0]['runnable'])
        self.assertTrue(b.workflow_view(decision, {'downstream_execution_allowed':True})['tasks'][0]['runnable'])

    def test_unsafe_source_urls_are_not_links(self):
        for url in ['javascript:alert(1)', 'file:///etc/passwd', 'https://user:secret@example.com', '//example.com', 'https://[invalid', None]:
            self.assertIsNone(b.safe_url(url))
        self.assertEqual(b.safe_url('https://example.org/source?a=1'), 'https://example.org/source?a=1')

    def test_csv_formula_cells_are_neutralized(self):
        for cell in ['=1+1', '+SUM(A1)', '-2+3', '@SUM(A1)', '  =HYPERLINK("url")']:
            self.assertTrue(b.safe_csv(cell).startswith("'"))
        self.assertEqual(b.safe_csv('10MW'), '10MW')
        self.assertEqual(b.safe_csv(None), '')

    def test_embedded_data_cannot_close_script(self):
        html = b.render_html({'untrusted':'</script><img src=x onerror=alert(1)>'})
        self.assertNotIn('</script><img', html)
        self.assertIn('\\u003c/script\\u003e', html)

    def test_non_finite_values_are_rejected(self):
        with self.assertRaises(ValueError): b.render_html({'value': float('nan')})

    def test_ui_has_no_remote_code_or_network_client(self):
        from bs4 import BeautifulSoup
        html = BeautifulSoup(b.render_html({}), 'html.parser')
        self.assertFalse(html.select('script[src], link[rel=stylesheet], iframe'))
        js = (HERE / 'report.js').read_text()
        for unsafe in ['innerHTML', 'eval(', 'fetch(', 'XMLHttpRequest', 'sendBeacon']:
            self.assertNotIn(unsafe, js)

    def test_missing_inputs_block_without_success_report(self):
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            result = b.build(Path(temp) / 'missing', Path(temp) / 'output')
            self.assertEqual(result['status'], 'BLOCKED')
            self.assertFalse((Path(result['output_dir']) / 'report.html').exists())

    def test_tampered_source_blocks_before_snapshot_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); raw = p / 'source'; raw.write_bytes(b'changed')
            output = p / 'output'; output.mkdir()
            with self.assertRaises(ValueError):
                b.source_snapshots({'synthetic': {'format':'PDF', 'source_file':str(raw), 'source_sha256':'0'*64}}, output)
            self.assertFalse(list((output / 'originals').iterdir()))


class PreservedLineageRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        candidates = b.successful_runs(stage_data(9) / 'output', 'controller_cycle_*', [])
        if not candidates: raise unittest.SkipTest('No preserved Controller run available')
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        with patch('socket.socket.connect', side_effect=AssertionError('Network forbidden in report build')), contextlib.redirect_stdout(io.StringIO()):
            cls.build_result = b.build(candidates[0][1], Path(cls.temp.name))
        if cls.build_result['status'] != 'SUCCESS': raise AssertionError(cls.build_result)
        cls.output = Path(cls.build_result['output_dir'])
        cls.report = b.read_json(cls.output / 'report.json')
        cls.inputs = b.load_inputs(candidates[0][1])

    def test_actual_lineage_loads_offline(self):
        self.assertEqual(self.build_result['status'], 'SUCCESS')
        self.assertEqual(self.report['execution']['new_model_calls'], 0)
        self.assertEqual(self.report['execution']['new_searches'], 0)

    def test_all_claims_and_pairs_are_preserved(self):
        self.assertEqual(self.report['summary_counts']['claims'], 58)
        self.assertEqual(len(self.report['cross_source_pairs']), 24)
        self.assertEqual(len(self.report['local_diagnostics']), 12)
        self.assertEqual(self.report['summary_counts']['quarantined'], 15)

    def test_actual_claim_values_and_verdicts_are_unchanged(self):
        self.assertEqual(b.validate_report(self.report, self.inputs)['status'], 'PASSED')

    def test_actual_source_inputs_remain_hash_identical(self):
        self.assertTrue(all(b.digest(Path(p).read_bytes()) == sha for p, sha in self.inputs['watched'].items()))

    def test_all_source_copies_match_downloaded_bytes(self):
        self.assertEqual(len(self.report['sources']), 7)
        self.assertTrue(all(b.digest((self.output / d['local_path']).read_bytes()) == d['sha256'] for d in self.report['sources']))

    def test_source_links_are_portable_and_exist(self):
        for row in self.report['evidence']:
            local = row['local_source'].split('#')[0]
            self.assertFalse(Path(local).is_absolute())
            self.assertTrue((self.output / local).is_file())

    def test_unpublished_pdfs_remain_conditional(self):
        pdfs = [d for d in self.report['sources'] if d['format']=='PDF' and d['origin']!='USER']
        self.assertEqual(len(pdfs), 4)
        self.assertTrue(all(d['published_at'] is None and d['source_admission']=='CONDITIONAL' for d in pdfs))

    def test_provided_capacity_does_not_fill_operating_or_usage(self):
        views = {v['id']:v for v in self.report['metric_views']}
        self.assertTrue(views['provided']['claim_keys'])
        for key in ['installed_operational', 'actual_usage']:
            self.assertFalse(views[key]['claim_keys'])
            self.assertEqual(views[key]['status'], 'UNCONFIRMED')
            self.assertFalse(views[key]['final_fact_confirmed'])

    def test_quarantine_is_diagnostic_only(self):
        rows = [r for r in self.report['evidence'] if r['original_group']=='QUARANTINED']
        self.assertTrue(all(not r['source_statement_usable'] and not r['final_evidence_allowed'] for r in rows))

    def test_analysis_or_adoption_permission_is_not_independent_proof(self):
        inputs = copy.deepcopy(self.inputs)
        for item in inputs['inventory']:
            if item['claim']['metric_type']=='provided_it_capacity':
                item['source_status']='VERIFIED'; item['semantic_status']='MATCH'
                item['original_group']='ACCEPTED'; item['final_evidence_allowed']=True
                item['is_provisional']=False; item['evidence_level']='SOURCE_STATEMENT_ONLY'
        report = b.build_report(inputs, self.report['sources'], b.read_json(report_policy_path()))
        self.assertFalse(next(v for v in report['metric_views'] if v['id']=='provided')['final_fact_confirmed'])

    def test_human_source_review_points_to_original_document(self):
        by_id = {r['issue_id']:r for r in self.report['human_review']}
        for issue_id, key, page in [('source_review_c7','kinx_ir_240925::c7',23), ('source_review_c10','kinx_ir_240925::c10',7)]:
            refs = by_id[issue_id]['references']
            self.assertEqual([r['claim_key'] for r in refs], [key])
            self.assertEqual(refs[0]['source_page'], page)

    def test_search_count_budget_and_subjective_reason_are_preserved(self):
        self.assertEqual(self.report['execution']['prior_executed_searches'], 5)
        self.assertEqual(self.report['search_budget'], self.inputs['state']['search_budget'])
        self.assertFalse(self.report['decision']['information_value_quantitatively_validated'])

    def test_actual_terminal_workflow_is_deferred(self):
        self.assertEqual(self.report['decision']['decision'], 'DEFER')
        self.assertEqual(self.report['decision']['goal_status'], 'INCOMPLETE')
        self.assertTrue(all(t['execution_status']=='DEFERRED' and not t['runnable'] for t in self.report['workflow']['tasks']))

    def test_unknown_comparison_time_never_says_same(self):
        for pair in self.report['cross_source_pairs']:
            if pair['time_alignment']!='SAME_INTERVAL':
                self.assertNotEqual(pair['comparison_dimensions']['temporal_basis'], 'SAME')

    def test_rendered_html_embeds_exact_report(self):
        from bs4 import BeautifulSoup
        html = BeautifulSoup((self.output / 'report.html').read_text(), 'html.parser')
        self.assertEqual(json.loads(html.select_one('#report-data').string), self.report)

    def test_annotation_template_cannot_promote_verification(self):
        template = b.read_json(self.output / 'human_review_template.json')
        self.assertEqual(template['annotation_effect'], 'ANNOTATION_ONLY_REVALIDATION_REQUIRED')
        self.assertTrue(all(r['status']=='NOT_REVIEWED' for r in template['reviews']))

    def test_claim_value_tampering_is_detected(self):
        changed = copy.deepcopy(self.report); changed['evidence'][0]['value'] = 'fabricated'
        self.assertEqual(b.validate_report(changed, self.inputs)['status'], 'FAILED')

    def test_invalid_source_page_is_rejected(self):
        changed = copy.deepcopy(self.inputs['inventory']); changed[0]['claim']['source_page'] = 999999
        with self.assertRaises(ValueError): b.evidence_rows(changed, self.report['sources'], b.read_json(report_policy_path()))


if __name__ == '__main__': unittest.main()
