"""Packaging checks and neutral synthetic fixtures only; no network or live LLM calls."""
import contextlib
import copy
import importlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dc_verify import cli
from dc_verify.codex import load_config
from dc_verify.demo import fixture, execute
from dc_verify.settings import runtime_root,stage_data
from dc_verify.step.step3.code.analysis import AnalysisResult,check_result,prepare_input
from dc_verify.step.step8.code.source_admission import guard_claims
from dc_verify.step.step2.code.controller import build_document_index


class PackageTests(unittest.TestCase):
    def test_all_stage_modules_import_without_path_mutation(self):
        import sys
        before=list(sys.path)
        for module in cli.STAGES.values():importlib.import_module('dc_verify.step.'+module)
        self.assertEqual(sys.path,before)

    def test_runtime_paths_are_configuration_based(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'DC_VERIFY_DATA_DIR':temp}):
            self.assertEqual(runtime_root(),Path(temp).resolve())
            self.assertEqual(stage_data(7),Path(temp).resolve()/'step7')

    def test_runtime_setting_can_be_read_from_api_env(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'API.env';path.write_text('DC_VERIFY_DATA_DIR='+temp+'\nOPENAI_API_KEY=\n')
            with patch.dict(os.environ,{'DC_VERIFY_ENV_FILE':str(path)},clear=True):
                self.assertEqual(runtime_root(),Path(temp).resolve())

    def test_missing_key_does_not_contact_api(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{},clear=True),patch('socket.socket.connect',side_effect=AssertionError('Network forbidden')):
            with self.assertRaises(ValueError):load_config(Path(temp)/'absent.env')

    def test_secret_is_not_in_config_repr(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'SYNTHETIC_TEST_SECRET_NOT_A_REAL_KEY'}):
            config=load_config('/nonexistent.env')
            self.assertNotIn('SYNTHETIC_TEST_SECRET_NOT_A_REAL_KEY',repr(config))

    def test_example_config_with_no_key_is_not_a_success(self):
        with patch.dict(os.environ,{},clear=True),tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'.env';path.write_text('OPENAI_API_KEY=\n')
            with self.assertRaises(ValueError):load_config(path)

    def test_fixture_is_schema_compatible(self):
        bundle,selection,result,_=fixture()
        AnalysisResult.model_validate(result)
        self.assertEqual(check_result(result,prepare_input(bundle,selection))[1]['status'],'PASSED')

    def test_fixture_quote_change_is_rejected(self):
        bundle,selection,result,_=fixture();result['claims'][0]['evidence_quote']='Invented source quote'
        self.assertEqual(check_result(result,prepare_input(bundle,selection))[1]['status'],'FAILED')

    def test_fixture_provided_capacity_is_not_operational(self):
        bundle,selection,result,_=fixture()
        result['claims'][0]['metric_type']='operational_it_capacity'
        self.assertEqual(check_result(result,prepare_input(bundle,selection))[1]['status'],'FAILED')

    def test_conditional_guard_keeps_provisional_and_denies_evidence(self):
        _,_,result,admission=fixture()
        row=guard_claims(result['claims'],[admission])[0]
        self.assertTrue(row['is_provisional']);self.assertFalse(row['final_evidence_allowed'])
        self.assertEqual(row['claim_verification_status'],'PENDING')

    def test_guard_rejects_promoted_synthetic_claim(self):
        _,_,result,admission=fixture();result['claims'][0]['fact_verified']=True
        with self.assertRaises(ValueError):guard_claims(result['claims'],[admission])

    def test_synthetic_demo_is_offline_and_not_agent_execution(self):
        with tempfile.TemporaryDirectory() as temp,patch('socket.socket.connect',side_effect=AssertionError('Network forbidden')):
            summary=execute(temp);output=json.loads((Path(temp)/'demo_result.json').read_text())
        self.assertEqual(summary['status'],'SUCCESS');self.assertFalse(output['agents_executed'])
        self.assertEqual(output['data_classification'],'SYNTHETIC_TEST_DATA')
        self.assertFalse(output['guarded_claims'][0]['final_evidence_allowed'])

    def test_demo_does_not_mutate_fixture_claim_values(self):
        _,_,result,admission=fixture();before=copy.deepcopy(result)
        guard_claims(result['claims'],[admission]);self.assertEqual(before,result)

    def test_cli_stage_list_is_read_only(self):
        stream=io.StringIO()
        with contextlib.redirect_stdout(stream):code=cli.main(['stages'])
        self.assertEqual(code,0);self.assertIn('report',json.loads(stream.getvalue()))

    def test_cli_help_for_every_implemented_stage(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for stage in cli.STAGES:
                with self.assertRaises(SystemExit) as result:cli.main(['run',stage,'--help'])
                self.assertEqual(result.exception.code,0)

    def test_cli_config_error_is_safe_and_does_not_call_network(self):
        with tempfile.TemporaryDirectory() as temp,patch.dict(os.environ,{'DC_VERIFY_ENV_FILE':str(Path(temp)/'missing')},clear=True),contextlib.redirect_stdout(io.StringIO()) as text:
            self.assertEqual(cli.main(['check-config']),1)
            self.assertEqual(json.loads(text.getvalue())['status'],'FAILED')

    def test_packaged_report_assets_exist(self):
        from dc_verify.step.step10.code import report_builder
        html=report_builder.render_html({'data_classification':'SYNTHETIC_TEST_DATA'})
        self.assertIn('SYNTHETIC_TEST_DATA',html)
        self.assertNotIn('REPORT_DATA_PLACEHOLDER',html)
        self.assertNotIn('/*REPORT_CSS*/',html)

    def test_document_lexical_index_preserves_all_pages_and_is_not_verified(self):
        ocr={'total_pdf_pages':2,'pages':[{'input_page':1,'text':'SYNTHETIC_TEST_DATA operating capacity'},
            {'input_page':2,'text':'SYNTHETIC_TEST_DATA unrelated background'}]}
        result=build_document_index(ocr,{'document_id':'synthetic_document'},query='operating capacity')
        self.assertEqual([p['page'] for p in result['pages']],[1,2])
        self.assertEqual(result['suggested_priority_pages'][0],1)
        self.assertFalse(result['ocr_verified_against_source'])

    def test_no_bare_project_imports_even_inside_functions(self):
        import ast
        import dc_verify
        package=Path(dc_verify.__file__).parent
        local_names={p.stem for p in (package/'step').rglob('*.py')}|{'codex','step'}
        for path in package.rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node,ast.ImportFrom) and node.module:
                    self.assertNotIn(node.module.split('.')[0],local_names,str(path))
                elif isinstance(node,ast.Import):
                    for name in node.names:self.assertNotIn(name.name.split('.')[0],local_names,str(path))

    def test_successful_run_paths_are_canonical_through_symlinks(self):
        from dc_verify.step.step8.code.cross_source_input import successful_runs
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);actual=root/'actual';run=actual/'run_1';run.mkdir(parents=True)
            (run/'run.json').write_text(json.dumps({'status':'SUCCESS','finished_at':'2026-01-01T00:00:00+00:00'}))
            alias=root/'alias';alias.symlink_to(actual,target_is_directory=True)
            self.assertEqual(successful_runs(alias,'run_*',[])[0][1],run.resolve())

    def test_ocr_entrypoint_with_synthetic_response_preserves_page(self):
        import pymupdf
        from dc_verify.step.step1.code import OCR
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(temp);pdf=root/'synthetic.pdf'
            with pymupdf.open() as document:
                document.new_page().insert_text((72,72),'SYNTHETIC_TEST_DATA')
                document.save(pdf)
            response={'api_status':'completed','text':'--- PAGE 1 ---\nSYNTHETIC_TEST_DATA','refusals':[],
                'model':'TEST_DOUBLE_NOT_LIVE','usage':None}
            with patch.object(OCR,'load_config',return_value=object()),patch.object(OCR,'request_pdf_ocr',return_value=response) as request:
                result,output=OCR.run_ocr(pdf,root/'output')
            self.assertEqual(result['status'],'SUCCESS');self.assertEqual(result['page_markers'],[1])
            self.assertEqual((output/'page_0001.txt').read_text(),'SYNTHETIC_TEST_DATA')
            self.assertEqual(request.call_count,1)

    def test_initial_controller_entrypoint_dry_run_never_calls_api(self):
        from dc_verify.step.step2.code import controller
        with tempfile.TemporaryDirectory() as temp,contextlib.redirect_stdout(io.StringIO()):
            root=Path(temp);ocr=root/'ocr.json';request=root/'request.json'
            ocr.write_text(json.dumps({'total_pdf_pages':1,'pages':[{'input_page':1,'text':'SYNTHETIC_TEST_DATA Example Facility capacity'}]}))
            request.write_text(json.dumps({'user_request':'Review Example Facility','review_context':{'target_projects':['Example Facility'],'as_of_date':'2026-01-01'},
                'document_metadata':{'document_id':'synthetic_document','title':'SYNTHETIC_TEST_DATA','published_at':None}}))
            with patch.object(controller,'request_structured_output') as api:
                result,output=controller.run_controller(ocr,request,root/'output',dry_run=True)
            api.assert_not_called();self.assertEqual(result['status'],'PREPARED')
            self.assertEqual(json.loads((output/'controller_input.json').read_text())['ocr_document_index'][0]['document_id'],'synthetic_document')


if __name__=='__main__':unittest.main()
