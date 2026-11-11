"""One command review runner. Existing stage APIs perform extraction and validation."""
from __future__ import annotations
import copy
import hashlib
import html
import json
import os
import shutil
from datetime import date,datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from pydantic import BaseModel,ConfigDict

from dc_verify import codex
from dc_verify.settings import runtime_root
from dc_verify.step.step8.code.source_admission import read_json,write_json,now,digest


class Scope(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    target_projects:list[str]
    clarification:str


class PipelineBlocked(ValueError):pass


class TrackedResponses:
    def __init__(self,responses,owner):self.responses,self.owner=responses,owner
    def __getattr__(self,name):
        operation=getattr(self.responses,name)
        def request(**kwargs):
            self.owner.status['model_requests']+=1;self.owner.save()
            response=operation(**kwargs)
            self.owner.status['model_responses'].append({'response_id':response.id,'model':response.model,
                'status':response.status,'usage':response.usage.model_dump() if response.usage else None})
            self.owner.save();return response
        return request


class TrackedClient:
    def __init__(self,client,owner):self.responses=TrackedResponses(client.responses,owner)


class Runner:
    def __init__(self,pdf,request,output_dir=None,targets=None,as_of=None,published_at=None,document_id=None,
                 official_domains=None,pages=None,client=None):
        self.pdf=Path(pdf).expanduser().resolve();self.request=request.strip();self.targets=targets or []
        self.as_of=as_of or datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat();date.fromisoformat(self.as_of)
        self.published_at=published_at;self.document_id=document_id
        self.domains=list(dict.fromkeys(official_domains or []));self.pages=pages
        self.injected_client=client
        base=Path(output_dir).expanduser().resolve() if output_dir else runtime_root()
        self.root=base/('review_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'));self.root.mkdir(parents=True)
        self.status={'status':'RUNNING','run_mode':getattr(client,'dc_verify_run_mode','LIVE'),
            'started_at':now(),'model_requests':0,'model_responses':[],
            'stages':{str(n):{'status':'PENDING'} for n in range(11)},'report':None,
            'source_input':str(self.pdf),'request':self.request,'as_of_date':self.as_of}
        self.save()
    def save(self):
        self.status['token_usage_reported']={key:sum((r.get('usage') or {}).get(key,0) for r in self.status['model_responses'])
            for key in ['input_tokens','output_tokens','total_tokens']}
        write_json(self.root/'pipeline.json',self.status)
    def event(self,stage,status,**details):
        if status=='RUNNING':self.status['active_stage']=str(stage)
        self.status['stages'][str(stage)]={'status':status,**details};self.save()
        print(f'[STEP {stage}] {status}'+(' · '+details['reason'] if details.get('reason') else ''),flush=True)
    def folder(self,n):
        p=self.root/f'step{n}';p.mkdir(exist_ok=True);return p
    def put(self,stage,name,value):
        p=self.folder(stage)/name;write_json(p,value);return p
    def copied(self,stage,name,source):
        p=self.folder(stage)/name;p.write_bytes(Path(source).read_bytes());return p
    def run_stage(self,n,operation):
        self.event(n,'RUNNING')
        for attempt in range(1,3):
            result=operation();log,path=result if isinstance(result,tuple) else (result,None)
            if log['status']=='SUCCESS':
                if path is None and log.get('output_dir'):path=Path(log['output_dir'])
                self.event(n,'SUCCESS',output=str(path) if path else None,attempts=attempt);return path
            self.event(n,'RETRYING' if attempt==1 else 'FAILED',attempts=attempt,
                reason=log.get('local_validation_error') or (log.get('error') or {}).get('message') or log['status'])
        raise PipelineBlocked(f'STEP {n} did not pass validation; inspect its run.json/validation.json')
    def fail_report(self,error):
        reason=str(error) if isinstance(error,PipelineBlocked) else codex.safe_error(error)['message']
        self.status.update(status='BLOCKED',reason=reason,error_type=type(error).__name__,finished_at=now())
        self.event(self.status.get('active_stage','0'),'FAILED',reason=reason)
        active=self.status.get('active_stage','0')
        if '-' in active:self.event(active.split('-')[0],'FAILED',reason=reason)
        for item in self.status['stages'].values():
            if item['status']=='PENDING':item.update(status='SKIPPED_UPSTREAM_FAILURE',reason='앞 단계가 완료되지 않았습니다.')
        p=self.root/'report.html'
        p.write_text('<!doctype html><meta charset="utf-8"><title>DC-Verify · BLOCKED</title><h1>검토 실행 중단</h1><p>'+html.escape(reason)+'</p><p>완료된 단계와 실패 원인은 pipeline.json과 단계별 run.json에 저장했습니다. 확인되지 않은 항목은 검증 완료로 처리하지 않았습니다.</p>')
        self.status['report']=str(p);self.save()
    def execute(self):
        previous=os.environ.get('DC_VERIFY_DATA_DIR');owned=None
        try:
            self.event(0,'RUNNING')
            codex.read_pdf_file(self.pdf)
            if not self.request:raise PipelineBlocked('검토 질문을 --request로 입력하세요.')
            if self.injected_client is None:
                try:config=codex.load_config()
                except ValueError:raise PipelineBlocked('API.env의 OPENAI_API_KEY에 본인 API 키를 입력하세요.')
                owned=codex.create_client(config,timeout=600.0);transport=owned
            else:transport=self.injected_client
            self.client=TrackedClient(transport,self)
            os.environ['DC_VERIFY_DATA_DIR']=str(self.root)
            self.event(0,'SUCCESS')
            self.run()
        except KeyboardInterrupt:
            self.fail_report(PipelineBlocked('사용자가 실행을 중단했습니다.'));self.status['status']='INTERRUPTED';self.save()
        except Exception as error:self.fail_report(error)
        finally:
            if owned is not None:owned.close()
            if previous is None:os.environ.pop('DC_VERIFY_DATA_DIR',None)
            else:os.environ['DC_VERIFY_DATA_DIR']=previous
        print(json.dumps({k:self.status.get(k) for k in ['status','controller_decision','goal_status','report','reason','run_mode','model_requests','token_usage_reported']},ensure_ascii=False,indent=2),flush=True)
        return self.status
    def run(self):
        from dc_verify.step.step1.code import OCR
        from dc_verify.step.step2.code import controller as planner
        from dc_verify.step.step3.code import analysis as extractor
        from dc_verify.step.step4.code import source_verifier as verifier
        from dc_verify.step.step5.code import assess_evidence as assessor
        from dc_verify.step.step6.code import controller_replan as controller
        pdf=self.folder(1)/'input.pdf';shutil.copy2(self.pdf,pdf)
        self.original_pdf=pdf
        p1=self.run_stage(1,lambda:OCR.run_ocr(pdf,self.folder(1)/'output',client=self.client))
        ocr=read_json(p1/'ocr.json');self.ocr=ocr
        self.document_id=self.document_id or 'document_'+ocr['source_sha256'][:16]
        if not self.targets:
            self.event(2,'RUNNING',reason='요청과 OCR에서 검토 대상 식별')
            from dc_verify.workflow_prompt import SCOPE_SYSTEM_PROMPT
            response=codex.request_structured_output(SCOPE_SYSTEM_PROMPT,json.dumps({'request':self.request,
                'ocr_previews':[p['text'][:1800] for p in ocr['pages'][:4]]},ensure_ascii=False),Scope,client=self.client)
            self.put(2,'scope_response.json',response)
            if response['api_status']!='completed' or not response['parsed']:raise PipelineBlocked('Controller가 검토 대상을 식별하지 못했습니다.')
            self.targets=response['parsed']['target_projects']
            if not self.targets:raise PipelineBlocked(response['parsed']['clarification'] or '--target로 검토 대상을 지정하세요.')
        context={'review_goal':self.request,'target_projects':self.targets,'as_of_date':self.as_of}
        req=self.put(2,'review_request.json',{'user_request':self.request,'review_context':context,
            'document_metadata':{'document_id':self.document_id,'title':self.pdf.stem,'published_at':self.published_at,
                'published_at_provenance':'USER_SPECIFIED' if self.published_at else 'UNCONFIRMED'}})
        self.copied(2,'ocr.json',p1/'ocr.json')
        p2=self.run_stage(2,lambda:planner.run_controller(p1/'ocr.json',req,self.folder(2)/'output',client=self.client))
        plan=read_json(p2/'controller_plan.json');self.plan=plan
        if plan['plan_status']!='READY':raise PipelineBlocked('Controller가 추가 사용자 입력을 요청했습니다. controller_plan.json을 확인하세요.')
        bundle=self.copied(3,'analysis_input.json',p2/'analysis_input.json')
        selected=self.pages or plan['analysis_handoff']['priority_pages']
        selection=self.put(3,'analysis_request.json',{'selected_pages':selected,'selection_authority':'PIPELINE_WITHIN_CONTROLLER_SCOPE'})
        p3=self.run_stage(3,lambda:extractor.run_analysis(bundle,selection,self.folder(3)/'output',client=self.client,
            bind_whitespace=True,quarantine_invalid=True))
        a=read_json(p3/'analysis_result.json');self.a=a
        if not a['claims']:raise PipelineBlocked('원문 검사를 통과한 Claim이 없습니다. extraction_handling.json을 확인하세요.')
        p4=self.run_stage(4,lambda:verifier.run_verifier(p3/'analysis_result.json',p3/'analysis_input.json',pdf,self.folder(4)/'output',client=self.client))
        source=read_json(p4/'source_verification.json');self.source=source
        context_file=self.put(5,'review_context.json',{**context,'comparison_project':None})
        if source['controller_observation']['verified_claim_ids']:
            p5=self.run_stage(5,lambda:assessor.assess_evidence(p3/'analysis_result.json',p4/'source_verification.json',context_file,self.folder(5)/'output',client=self.client))
            evidence=read_json(p5/'evidence_assessment.json')
        else:
            self.event(5,'RUNNING')
            base={'assessment_status':'COMPLETED','review_goal_status':'INCOMPLETE',**context,'claim_assessments':[],
                'unresolved_requirement_details':[{'requested_metric':m['requested_metric'],'reason':m['reason'],'needed_evidence':m['needed_evidence']} for m in a['missing_required_information']],
                'controller_observation':{'supported_claim_ids':[],'recheck_claim_ids':[],
                    'unresolved_requirements':[m['requested_metric'] for m in a['missing_required_information']],
                    'additional_evidence_needed':['원문 재검토 필요']}}
            evidence,_=assessor.merge_not_assessed(base,a,source)
            evidence,validation=assessor.validate_assessment(evidence,a,source,context)
            if validation['status']!='PASSED':raise PipelineBlocked('미검증 Claim의 NOT_ASSESSED 처리 검사 실패')
            self.put(5,'evidence_assessment.json',evidence);self.put(5,'validation.json',validation)
            self.event(5,'SUCCESS',reason='원문 통과 Claim이 없어 API 없이 NOT_ASSESSED 유지')
        self.evidence=evidence
        analyzed={p['source_page'] for p in a['analyzed_pages']};all_pages=list(range(1,ocr['total_pdf_pages']+1))
        history={'action_history':[],'events':[],'search_policy':{'max_attempts_per_issue':2},'available_documents':[
            {'document_id':self.document_id,'published_at':self.published_at,'all_pages':all_pages,
                'processed_pages':sorted(analyzed),'unprocessed_pages':[p for p in all_pages if p not in analyzed]}]}
        self.history=history
        for name,value in [('initial_plan.json',plan),('analysis_result.json',a),('source_verification.json',source),
                           ('evidence_assessment.json',evidence),('execution_history.json',history)]:self.put(6,name,value)
        p6=self.run_stage(6,lambda:controller.run_replan(self.folder(6),self.folder(6)/'output',client=self.client))
        self.replan_path=p6;self.replan_input=read_json(p6/'controller_input.json')
        external=self.external(p6)
        if external:
            from dc_verify.step.step9.code import controller as final_controller
            registry=self.put(9,'runtime_tools.json',{'checked_at':now(),'tools':[{'tool_id':'TAVILY','available':False,'supported_actions':['SEARCH_OFFICIAL']}],
                'reason':'Bounded pass complete; final Controller records further work without dispatching another batch.'})
            p9=self.run_stage(9,lambda:final_controller.execute(external,registry,self.folder(9)/'output',client=self.client))
            from dc_verify.step.step10.code import report_builder
            self.event(10,'RUNNING');report_run=report_builder.build(p9,self.folder(10)/'output')
            if report_run['status']!='SUCCESS':raise PipelineBlocked('Report Builder failed; inspect STEP10 run.json')
            report=Path(report_run['output_dir'])/'report.html'
            model=read_json(report.with_name('report.json'))
            model['execution'].update(pipeline_model_requests=self.status['model_requests'],
                pipeline_searches=read_json(p9/'restored_controller_state.json')['tool_call_statistics']['executed_search_requests'],
                pipeline_run_mode=self.status['run_mode'])
            model['workflow_stages']=copy.deepcopy(self.status['stages']);model['workflow_stages']['10']={'status':'SUCCESS'}
            write_json(report.with_name('report.json'),model);report.write_text(report_builder.render_html(model))
            run_file=report.parent/'run.json';run_meta=read_json(run_file);run_meta['html_sha256']=digest(report.read_bytes());write_json(run_file,run_meta)
            self.event(10,'SUCCESS',output=str(report.parent))
        else:report=self.local_finish()
        decision=read_json(Path(report).with_name('report.json'))['decision']
        warnings=[{'stage':n,**s} for n,s in self.status['stages'].items() if s['status'] in {'PARTIAL','FAILED'}]
        self.status.update(status='SUCCESS_WITH_WARNINGS' if warnings else 'SUCCESS',report=str(report),finished_at=now(),
            controller_decision=decision['decision'],goal_status=decision['goal_status'],warnings=warnings);self.save()

    def external(self,p6):
        from dc_verify.workflow_search import execute_external
        return execute_external(self,p6)

    def local_finish(self):
        from dc_verify.workflow_view import local_inputs,render
        from dc_verify.step.step6.code.controller_replan import decide_after_cross_check
        from dc_verify.step.step9.code.prompt import CONTROLLER_POST_EVIDENCE_PROMPT,build_user_prompt
        inputs=local_inputs(self.plan,self.a,self.source,self.evidence,self.replan_input,self.original_pdf,self.ocr,self.history)
        inputs['state']['execution_policy']['external_branch']=self.status['stages'].get('7')
        self.event(9,'RUNNING');state=inputs['state'];decision=None
        first=len(self.status['model_responses'])
        for attempt in range(2):
            response,decision,validation=decide_after_cross_check(state,CONTROLLER_POST_EVIDENCE_PROMPT,
                build_user_prompt(state),client=self.client)
            self.put(9,f'controller_response_{attempt+1}.json',response);self.put(9,f'validation_{attempt+1}.json',validation)
            if validation['status']=='PASSED':break
        if validation['status']!='PASSED':raise PipelineBlocked('Final Controller response did not pass its state/evidence guards.')
        self.put(9,'controller_state_input.json',state);self.put(9,'controller_decision.json',decision)
        self.event(9,'SUCCESS',output=str(self.folder(9)))
        responses=self.status['model_responses'][first:]
        usage={k:sum((r.get('usage') or {}).get(k,0) for r in responses) for k in ['input_tokens','output_tokens']}
        inputs.update(decision=decision,pending={'downstream_execution_allowed':False,'executed':False},
            run={'selected_response_id':responses[-1]['response_id'],'api_responses':len(responses),'usage':usage})
        inputs['watched']={str(p.resolve()):digest(p.read_bytes()) for p in self.root.rglob('*.json') if p.name!='pipeline.json'}
        inputs['watched'][str(self.original_pdf)]=digest(self.original_pdf.read_bytes())
        self.event(10,'RUNNING');report=render(inputs,self.folder(10)/'report',self.status)
        self.event(10,'SUCCESS',output=str(report.parent));return report


def execute(**kwargs):
    try:return Runner(**kwargs).execute()
    except (OSError,ValueError) as error:
        result={'status':'BLOCKED','error_type':type(error).__name__,'reason':codex.safe_error(error)['message'],'model_requests':0}
        print(json.dumps(result,ensure_ascii=False));return result
