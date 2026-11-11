"""Adapt a local-document run to the existing Controller and Report Builder schemas."""
import copy
import json
from pathlib import Path

from dc_verify.step.step8.code.source_admission import read_json, write_json, digest, now


def local_inputs(plan, analysis, source, evidence, replan_input, pdf, ocr, history):
    """No external source/claim or independent corroboration is fabricated for skipped branches."""
    doc_id=plan['analysis_handoff']['document_ids'][0]
    doc={'document_id':doc_id,'title':pdf.stem,'source_file':str(pdf.resolve()),'source_url':None,
        'source_sha256':ocr['source_sha256'],'format':'PDF','published_at':analysis['claims'][0]['source_published_at'] if analysis['claims'] else None,
        'document_period':None,'publication_review':{'status':'USER_INPUT_METADATA'},
        'source_admission':{'analysis_admission':'USER_PROVIDED','final_evidence_allowed':False},
        'original_group':'USER','independence_status':'NOT_ESTABLISHED'}
    verified={r['claim_id']:r for r in source['claim_results']}
    assessments={r['claim_id']:r for r in evidence['claim_assessments']}
    inventory=[]
    for claim in analysis['claims']:
        s=verified[claim['claim_id']];e=assessments[claim['claim_id']]
        semantic='MATCH' if s['pdf_status']=='VERIFIED' and all(v in {'MATCH','NOT_APPLICABLE'} for v in s['field_checks'].values()) else 'UNCLEAR'
        inventory.append({'claim_key':doc_id+'::'+claim['claim_id'],'claim':copy.deepcopy(claim),'origin':'USER',
            'source_status':s['pdf_status'],'semantic_status':semantic,'semantic_reason':s['reason'],
            'semantic_provenance':'SOURCE_VERIFIER_FIELD_CHECKS','original_group':'ACCEPTED','is_provisional':False,
            'source_admission':'USER_PROVIDED','evidence_level':e['evidence_level'],'final_evidence_allowed':False,
            'source_quote':s.get('image_evidence_text'),'field_checks':s['field_checks'],
            'uncertainties':e['missing_evidence'],'comparison_eligibility':'VERIFIED_SOURCE_STATEMENT' if semantic=='MATCH' else 'DIAGNOSTIC_ONLY',
            'document':doc})
    by_id={c['claim']['claim_id']:c for c in inventory}
    counts={}
    for event in history['action_history']:
        if event.get('executed') and event['type'] in {'SEARCH_OFFICIAL','SEARCH_LOCAL'}:
            for issue in event['issue_ids']:counts[issue]=counts.get(issue,0)+1
    issues=[];scopes=[]
    limit=history['search_policy']['max_attempts_per_issue']
    for original in replan_input['issue_inventory']:
        item=copy.deepcopy(original);kind=item['kind'];key=item['issue_id']
        linked=[by_id[c] for c in item['claim_ids']]
        if kind=='SOURCE_REVIEW':
            scopes.append({'issue_id':key,'claims':[{'claim_key':c['claim_key'],'document_id':doc_id,
                'source_page':c['claim']['source_page'],'evidence_quote':c['claim']['evidence_quote']} for c in linked]})
        item.update(goal_completion_allowed=False,executed_searches=counts.get(key,0),
            remaining_searches=max(0,limit-counts.get(key,0)) if kind=='GOAL_EVIDENCE' else 0,
            status='NEEDS_HUMAN_REVIEW' if kind=='SOURCE_REVIEW' else 'UNRESOLVED')
        issues.append(item)
    goal_issues=[i for i in issues if i['kind']=='GOAL_EVIDENCE']
    # Source statements alone never become independently admitted evidence here.
    executions=[c['claim_key'] for c in inventory if c['claim']['metric_type'] in {'service_start','completion'} and c['comparison_eligibility']!='DIAGNOSTIC_ONLY']
    context={k:plan[k] for k in ['review_goal','target_projects','as_of_date']}
    state={'review_context':context,'issues':issues,'known_claim_keys':[c['claim_key'] for c in inventory],
        'evidence_observation':{'execution_statement_claim_keys':executions,'current_operation_independently_verified':False},
        'execution_history':history,'search_budget':{'max_attempts_per_issue':limit,
            'executed_attempts':{i['issue_id']:i['executed_searches'] for i in goal_issues},
            'remaining_attempts':{i['issue_id']:i['remaining_searches'] for i in goal_issues}},
        'new_information_events':[],'runtime_tools':[],
        'available_documents':history['available_documents'],'goal_evidence_sufficient':False,
        'comparison_target_supplied':bool(plan.get('comparison_project')),
        'execution_policy':{'followup_enabled':False,'reason':'Bounded single review pass; external/local gaps must remain explicit.'},
        'tool_call_statistics':{'executed_search_requests':sum(1 for e in history['action_history'] if e.get('executed') and e['type']=='SEARCH_OFFICIAL')}}
    return {'inventory':inventory,'documents':{doc_id:doc},'state':state,'pairs':{'pair_results':[],'diagnostic_results':[]},
        'human':{'source_review_issue_scopes':scopes,'questions_from_model':[]}}


def render(inputs, output, pipeline_status):
    from dc_verify.step.step10.code import report_builder as report
    from dc_verify.settings import report_policy_path
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    sources=report.source_snapshots(inputs['documents'],output)
    model=report.build_report(inputs,sources,read_json(report_policy_path()))
    model['input_snapshot_digest']=digest(json.dumps(inputs['watched'],sort_keys=True).encode())
    model['execution'].update(pipeline_model_requests=pipeline_status['model_requests'],
        pipeline_searches=inputs['state']['tool_call_statistics']['executed_search_requests'],
        pipeline_run_mode=pipeline_status['run_mode'])
    model['workflow_stages']=copy.deepcopy(pipeline_status['stages'])
    model['workflow_stages']['10']={'status':'SUCCESS'}
    validation=report.validate_report(model,inputs)
    if validation['status']!='PASSED':raise ValueError('Final report invariants failed')
    if any(digest(Path(p).read_bytes())!=h for p,h in inputs['watched'].items()):raise ValueError('Inputs changed before reporting')
    write_json(output/'report.json',model);write_json(output/'execution_verification.json',validation)
    (output/'report.html').write_text(report.render_html(model))
    import csv
    fields=['claim_key','metric_type','raw_value','value','unit','metric_as_of','source_status','semantic_status','evidence_level','document_id','source_page','evidence_quote']
    with (output/'evidence.csv').open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.writer(stream);writer.writerow(fields)
        writer.writerows([[report.safe_csv(c.get(k)) for k in fields] for c in model['evidence']])
    write_json(output/'normalizations.json',model['normalizations'])
    return output/'report.html'
