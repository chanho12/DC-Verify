"""SYNTHETIC_TEST_DATA: exercise existing guards, not model extraction or factual verification."""
import copy
import hashlib
import json
from pathlib import Path

from dc_verify.step.step3.code.analysis import prepare_input, check_result
from dc_verify.step.step8.code.source_admission import guard_claims
from dc_verify.step.step10.code.report_builder import workflow_view, normalize_pair_time


def fixture():
    text='SYNTHETIC_TEST_DATA. Example Facility customer-provided IT capacity is 12 MW (planned).'
    context={'review_goal':'Check operational IT capacity','target_projects':['Example Facility'],'as_of_date':'2026-01-01'}
    handoff={**context,'document_ids':['synthetic_document'],'priority_pages':[1],'required_metrics':['operational_it_capacity'],
        'source_admission_policy':{'analysis_admission':'CONDITIONAL','final_evidence_allowed':False}}
    bundle={'analysis_handoff':handoff,'documents':[{'document_id':'synthetic_document','published_at':None,
        'pages':[{'source_page':1,'text':text,'ocr_text_sha256':hashlib.sha256(text.encode()).hexdigest()}]}]}
    selection={'selected_pages':[1],'selection_authority':'SYNTHETIC_TEST_DATA'}
    claim={'claim_id':'synthetic_document:c1','project_name':'Example Facility','metric_type':'provided_it_capacity',
        'metric_name':'customer-provided IT capacity','value':12.0,'unit':'MW','raw_value':'12 MW','value_qualifier':'exact',
        'facility_scope':'Example Facility','development_status':None,'source_published_at':None,'metric_as_of':None,
        'source_document_id':'synthetic_document','source_page':1,'evidence_quote':text,'temporal_basis':'planned',
        'notes':['SYNTHETIC_TEST_DATA; not an official document or API response'],
        'measurement_basis':'CUSTOMER_PROVIDED_CAPACITY','statement_status':'PLAN'}
    result={'analysis_status':'INCOMPLETE',**context,'analyzed_pages':[{'document_id':'synthetic_document','source_page':1}],
        'unprocessed_controller_pages':[],'claims':[claim],
        'required_metric_coverage':[{'requested_metric':'operational_it_capacity','status':'NOT_FOUND','claim_ids':[],
            'reason':'Provided capacity does not establish operational capacity'}],
        'missing_required_information':[{'requested_metric':'operational_it_capacity','metric_type':'operational_it_capacity',
            'reason':'No directly stated operational capacity','needed_evidence':'Direct original operational capacity evidence'}],
        'ambiguities':[],'ocr_review_flags':[],'recommendations_for_controller':['Human review required']}
    admission={'document_id':'synthetic_document','analysis_admission':'CONDITIONAL','analysis_allowed':True,
        'final_evidence_allowed':False,'claims_verified':False,'evidence_admission':'BLOCKED_UNTIL_CONDITIONS_RESOLVED',
        'published_at':None,'allowed_source_pages':[1],'source_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'source_warnings':['SYNTHETIC_TEST_DATA'],'conditions_to_resolve':['VERIFY_SOURCE_AND_PUBLICATION']}
    return bundle,selection,result,admission


def execute(output_dir):
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    bundle,selection,candidate,admission=fixture()
    original=copy.deepcopy(candidate)
    payload=prepare_input(bundle,selection)
    parsed,validation=check_result(candidate,payload)
    if validation['status']!='PASSED':raise ValueError(validation)
    envelopes=guard_claims(parsed['claims'],[admission])
    workflow=workflow_view({'decision':'DEFER','updated_plan':[{'task_id':'synthetic_review','execution_status':'PLANNED'}]}, {})
    pair,changes=normalize_pair_time({'pair_id':'synthetic_pair','comparison_dimensions':{'temporal_basis':'SAME'},
        'time_comparison':{'relation':'UNKNOWN'}})
    assert candidate==original
    data={'data_classification':'SYNTHETIC_TEST_DATA','new_model_calls':0,'new_searches':0,'agents_executed':False,
        'extraction':'NOT_EXECUTED_HAND_AUTHORED_FIXTURE','original_pdf_verification':'NOT_PERFORMED',
        'analysis_result':parsed,'validation':validation,'guarded_claims':envelopes,'workflow':workflow,
        'comparison_view':pair,'normalizations':changes}
    for name,value in [('demo_result.json',data),('analysis_input.json',bundle),('analysis_request.json',selection)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    return {'status':'SUCCESS','data_classification':'SYNTHETIC_TEST_DATA','output':'demo_result.json',
        'new_model_calls':0,'new_searches':0,'guard_tests':'PASSED','goal_status':'INCOMPLETE','workflow':'DEFERRED'}
