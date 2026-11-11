"""Checkpointed LangGraph controller around the preserved STEP implementations."""
from __future__ import annotations
import copy
import hashlib
import json
import inspect
from importlib.metadata import version
import os
import shutil
from pathlib import Path
from typing import Literal
from typing_extensions import TypedDict
from pydantic import BaseModel, ConfigDict
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from dc_verify import codex
from dc_verify.pipeline import Runner, Scope, PipelineBlocked
from dc_verify.runtime import Ledger, RuntimeGuard, GuardedClient, fingerprint, transient
from dc_verify.evidence_store import EvidenceStore
from dc_verify.step.step8.code.source_admission import read_json, write_json, now, digest


class State(TypedDict, total=False):
    context: dict
    route: str
    report: str
    artifacts: dict


class Route(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    route: Literal['analysis', 'source_verification', 'external', 'counterevidence']
    reason: str


class Retrieval(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str


class GraphRunner(Runner):
    def __init__(self, *args, resume=None, refresh=False, max_calls=64, max_tokens=2_000_000, max_searches=8, **kwargs):
        super().__init__(*args, **kwargs)
        implementation={str(p.relative_to(Path(__file__).parent)): digest(p.read_bytes())
            for p in sorted(Path(__file__).parent.rglob('*')) if p.is_file() and p.suffix in {'.py','.json','.html','.css','.js'}}
        self.code_hash = fingerprint({'implementation':implementation,'dependencies':{p:version(p) for p in
            ['openai','pydantic','PyMuPDF','langgraph','langgraph-checkpoint-sqlite','qdrant-client']}})
        self.input_key = fingerprint({'pdf':digest(self.pdf.read_bytes()) if self.pdf.is_file() and self.pdf.stat().st_size<50_000_000 else None,'pdf_path':str(self.pdf),
            'request':self.request,'targets':self.targets,'as_of':self.as_of,'published':self.published_at,
            'document_id':self.document_id,'domains':self.domains,'pages':self.pages,'code':self.code_hash,
            'models':[codex.PLANNING_MODEL,codex.DEFAULT_MODEL], 'budgets':[max_calls,max_tokens,max_searches],
            'run_mode':self.status['run_mode']})
        self.resuming = bool(resume); self.refresh=refresh; self.rounds=0; self.store=None
        if resume:
            root = Path(resume).expanduser().resolve()
            manifest = read_json(root/'graph_manifest.json')
            if manifest['input_key'] != self.input_key: raise PipelineBlocked('Resume 입력·코드·모델·예산이 기존 실행과 다릅니다. 새 실행을 시작하세요.')
            generated = self.root
            self.root = root; self.status = read_json(root/'pipeline.json')
            (generated/'pipeline.json').unlink(); generated.rmdir()
        if self.status['run_mode'] not in {'LIVE','TEST_DOUBLE'}:raise PipelineBlocked('Unknown execution mode')
        self.state_dir = self.root.parent/'.dc_verify'
        if self.status['run_mode']=='TEST_DOUBLE':self.state_dir=self.state_dir/'test_double'
        self.state_dir.mkdir(parents=True,exist_ok=True)
        self.ledger = Ledger(self.state_dir/'ledger.sqlite')
        self.guard = RuntimeGuard(self.ledger,self.root.name,self.as_of,self.domains,max_calls,max_tokens,max_searches)
        prior = self.ledger.db.execute('SELECT kind,stage,payload FROM records WHERE run_id=?',(self.root.name,)).fetchall()
        self.guard.calls = sum(k=='call' for k,_,_ in prior)
        self.guard.searches = sum(k=='search_request' for k,_,_ in prior)
        self.guard.tokens = sum((json.loads(p).get('usage') or {}).get('total_tokens',0) for k,_,p in prior if k=='call_result')
        write_json(self.root/'graph_manifest.json',{'input_key':self.input_key,'code_hash':self.code_hash,
            'thread_id':self.root.name,'ledger':str(self.ledger.path),'checkpoints':str(self.state_dir/'checkpoints.sqlite'),
            'models':{'planning':codex.PLANNING_MODEL,'extraction':codex.DEFAULT_MODEL,'embedding':'text-embedding-3-large'},
            'max_calls':max_calls,'max_tokens':max_tokens,'max_searches':max_searches})
        self.validation_feedback=None

    def wrap_client(self, transport):
        self.transport=transport
        return GuardedClient(transport,self)

    def execute(self):
        try: return super().execute()
        finally:
            if self.store: self.store.close()
            self.ledger.close()

    def event(self, stage, status, **details):
        super().event(stage,status,**details)
        if hasattr(self,'ledger'): self.ledger.record(self.root.name,'stage',stage,{'status':status,**details})

    def run_stage(self, n, operation, dependencies=None):
        scope=self.input_key
        if str(n)=='1':
            from dc_verify.step.step1.code import OCR,prompt
            scope=fingerprint([digest(self.pdf.read_bytes()),digest(Path(OCR.__file__).read_bytes()),
                digest(Path(prompt.__file__).read_bytes()),inspect.getsource(codex.request_pdf_ocr),codex.DEFAULT_MODEL,self.status['run_mode']])
        key = fingerprint([scope,str(n),dependencies])
        cacheable = dependencies is not None and str(n) not in {'7-plan','8-admission','8-analysis','8-verification','8-cross','9'}
        cached = self.ledger.cached(key) if cacheable and not self.refresh else None
        if cached:
            destination=self.folder(n)/'output'/cached.name
            if not destination.exists():shutil.copytree(cached,destination)
            cached=destination
            self.event(n,'SUCCESS',output=str(cached),reused=True,reason='동일 입력·코드·모델과 산출물 해시 확인')
            self.ledger.record(self.root.name,'cache_hit',n,{'cache_key':key,'path':str(cached)}); return cached
        self.event(n,'RUNNING')
        for attempt in range(3):
            result=operation(); log,path=result if isinstance(result,tuple) else (result,None)
            if log['status']=='SUCCESS':
                path=Path(path or log['output_dir'])
                handling=path/'extraction_handling.json'
                if str(n)=='3' and handling.is_file():
                    details=read_json(handling)
                    if details.get('rejected_claims') and attempt<2:
                        self.validation_feedback='Correct these extraction/quote errors using only original source text: '+json.dumps(details,ensure_ascii=False)
                        self.ledger.record(self.root.name,'extraction_retry',n,{'attempt':attempt+1,'handling':details})
                        continue
                if cacheable:self.ledger.cache(key,path)
                self.validation_feedback=None
                self.event(n,'SUCCESS',output=str(path),attempts=attempt+1); return path
            # Transport errors were already retried by ResponsesProxy; never multiply that budget here.
            validation=log.get('local_validation_error') or log.get('validation_error')
            if str(n)=='1' and log.get('api_status')=='completed' and log.get('status')=='PARTIAL':
                validation='OCR page markers/text incomplete: '+json.dumps(log.get('missing_page_markers',[]))
            self.ledger.record(self.root.name,'validation_failure' if validation else 'technical_failure',n,log)
            if not validation or attempt==2: break
            self.validation_feedback=validation
            self.event(n,'RETRYING',reason=validation,attempts=attempt+1)
        raise PipelineBlocked(f'STEP {n} 검증/실행 실패. 원인과 인계 사항은 SQLite 및 run.json에 보존했습니다.')

    def snapshot(self):
        names=['ocr','plan','a','source','evidence','history','replan_input','targets','document_id','rounds','counter',
               'original_pdf','p1','p2','p3','p4','replan_path','external_path','report_path','pending_local_action']
        return {k:str(getattr(self,k)) if isinstance(getattr(self,k),Path) else getattr(self,k)
                for k in names if hasattr(self,k)}

    def restore(self, context):
        paths={'original_pdf','p1','p2','p3','p4','replan_path','external_path','report_path'}
        for key,value in context.items(): setattr(self,key,Path(value) if key in paths and value else value)

    def node(self, method):
        def execute(state):
            for path,sha in state.get('artifacts',{}).items():
                if not Path(path).is_file() or digest(Path(path).read_bytes())!=sha:
                    raise PipelineBlocked('Checkpoint artifact changed: '+path)
            self.restore(state.get('context',{}))
            result=method() or {}
            artifacts={str(p):digest(p.read_bytes()) for p in self.root.rglob('*') if p.is_file() and p.name!='pipeline.json'}
            return {'context':self.snapshot(),'artifacts':artifacts,**result}
        return execute

    def run(self):
        # No server is needed: Qdrant local and SQLite both live outside the Git repository index.
        self.store=EvidenceStore(self.state_dir/'qdrant',self.ledger,self.client,self.guard)
        graph=StateGraph(State)
        nodes={'ocr':self.ocr_node,'plan':self.plan_node,'analysis':self.analysis_node,
            'source_verification':self.source_node,'evidence':self.evidence_node,'observe':self.observe_node,
            'dispatch':self.dispatch_node,'external':self.external_node,'counterevidence':self.counter_node,
            'controller':self.controller_node,'final_verifier':self.final_verifier_node}
        for name,method in nodes.items(): graph.add_node(name,self.node(method))
        edges=[(START,'ocr'),('ocr','plan'),('plan','analysis'),('analysis','source_verification'),
            ('source_verification','evidence'),('evidence','observe'),('observe','dispatch'),
            ('external','counterevidence'),('counterevidence','controller'),('controller','final_verifier'),('final_verifier',END)]
        for left,right in edges:graph.add_edge(left,right)
        graph.add_conditional_edges('dispatch',lambda s:s['route'],{k:k for k in ['analysis','source_verification','external','counterevidence']})
        (self.root/'workflow.mmd').write_text(graph.compile().get_graph().draw_mermaid())
        with SqliteSaver.from_conn_string(str(self.state_dir/'checkpoints.sqlite')) as saver:
            app=graph.compile(checkpointer=saver)
            config={'configurable':{'thread_id':self.root.name},'recursion_limit':60}
            state=app.get_state(config)
            if self.resuming and not state.values: raise PipelineBlocked('복구할 체크포인트가 없습니다.')
            if self.resuming:
                for path,sha in state.values.get('artifacts',{}).items():
                    if not Path(path).is_file() or digest(Path(path).read_bytes())!=sha:
                        raise PipelineBlocked('Checkpoint artifact changed: '+path)
            result=app.invoke(None if self.resuming else {'context':{}},config)
            self.restore(result['context'])
            self.ledger.record(self.root.name,'checkpoint_complete','graph',{'next':list(app.get_state(config).next)})
        model=read_json(self.report_path.with_name('report.json')); decision=model['decision']
        warnings=[{'stage':k,**v} for k,v in self.status['stages'].items() if v['status'] in {'PARTIAL','FAILED'}]
        self.status.update(status='SUCCESS_WITH_WARNINGS' if warnings or model['final_verifier']['status']=='NEEDS_HUMAN_REVIEW' else 'SUCCESS',
            controller_decision=decision['decision'],goal_status=decision['goal_status'],report=str(self.report_path),
            finished_at=now(),warnings=warnings,ledger=str(self.ledger.path),checkpoint_thread=self.root.name,
            total_model_calls_including_embeddings=self.guard.calls,total_model_tokens=self.guard.tokens)
        self.save()

    def ocr_node(self):
        from dc_verify.step.step1.code import OCR
        self.original_pdf=self.folder(1)/'input.pdf';shutil.copy2(self.pdf,self.original_pdf)
        if self.published_at:self.guard.check('retrieve_local_evidence',published_at=self.published_at)
        self.p1=self.run_stage(1,lambda:OCR.run_ocr(self.original_pdf,self.folder(1)/'output',client=self.client),
            dependencies={'pdf':digest(self.pdf.read_bytes())})
        self.ocr=read_json(self.p1/'ocr.json')
        self.document_id=self.document_id or 'document_'+fingerprint(str(self.pdf))[:16]
        index=self.store.index(self.document_id,self.ocr['source_sha256'],self.ocr['pages'],
            {'title':self.pdf.stem,'source_path':str(self.original_pdf),'source_sha256':self.ocr['source_sha256'],
             'url':None,'published_at':self.published_at,'admission':'USER_PROVIDED','version_type':'SOURCE_SHA256'})
        self.put(1,'store_index.json',index)

    def plan_node(self):
        from dc_verify.step.step2.code import controller
        if not self.targets:
            from dc_verify.workflow_prompt import SCOPE_SYSTEM_PROMPT
            self.event(2,'RUNNING')
            response=codex.request_structured_output(SCOPE_SYSTEM_PROMPT,json.dumps({'request':self.request,
                'ocr_previews':[p['text'][:1800] for p in self.ocr['pages'][:4]]},ensure_ascii=False),Scope,client=self.client)
            self.put(2,'scope_response.json',response)
            if response['api_status']!='completed' or not response['parsed']:raise PipelineBlocked('검토 대상 식별 실패')
            self.targets=response['parsed']['target_projects']
            if not self.targets:raise PipelineBlocked(response['parsed']['clarification'] or '--target를 지정하세요.')
        context={'review_goal':self.request,'target_projects':self.targets,'as_of_date':self.as_of}
        req=self.put(2,'review_request.json',{'user_request':self.request,'review_context':context,
            'document_metadata':{'document_id':self.document_id,'title':self.pdf.stem,'published_at':self.published_at,
                'published_at_provenance':'USER_SPECIFIED' if self.published_at else 'UNCONFIRMED'}})
        self.p2=self.run_stage(2,lambda:controller.run_controller(self.p1/'ocr.json',req,self.folder(2)/'output',client=self.client),dependencies=context)
        self.plan=read_json(self.p2/'controller_plan.json')
        self.ledger.record(self.root.name,'plan','2',self.plan)
        if self.plan['plan_status']!='READY':raise PipelineBlocked('Controller가 추가 사용자 입력을 요청했습니다.')

    def analysis_node(self):
        from dc_verify.step.step3.code import analysis
        bundle=self.copied(3,'analysis_input.json',self.p2/'analysis_input.json')
        selected=self.pages or self.plan['analysis_handoff']['priority_pages']
        if self.rounds:
            decision=read_json(self.replan_path/'controller_decision.json')
            extra=[p for s in decision['next_action']['local_scope'] if s['document_id']==self.document_id for p in s['pages']]
            selected=sorted({p['source_page'] for p in self.a['analyzed_pages']}|set(extra))
            # Controller STEP 6 may authorize previously unselected pages; bind their actual OCR text.
            payload=read_json(bundle)
            payload['analysis_handoff']['priority_pages']=selected
            source_pages={p['input_page']:p['text'] for p in self.ocr['pages']}
            if any(p not in source_pages for p in selected):raise PipelineBlocked('Controller selected an unavailable OCR page')
            payload['documents'][0]['pages']=[{'source_page':p,'text':text,'ocr_text_sha256':digest(text.encode())}
                for p,text in source_pages.items()]
            write_json(bundle,payload)
        selection=self.put(3,'analysis_request.json',{'selected_pages':selected,'selection_authority':'CONTROLLER_AUTHORIZED_SCOPE'})
        self.p3=self.run_stage(3,lambda:analysis.run_analysis(bundle,selection,self.folder(3)/'output',client=self.client,
            bind_whitespace=True,quarantine_invalid=True),dependencies=[self.plan,selected])
        self.a=read_json(self.p3/'analysis_result.json')
        if not self.a['claims']:raise PipelineBlocked('원문 검사를 통과한 Claim이 없습니다.')
        self.ledger.record(self.root.name,'claims','3',self.a)
        if getattr(self,'pending_local_action',None) and self.pending_local_action['type']=='SEARCH_LOCAL':self.finish_local_action()

    def source_node(self):
        from dc_verify.verification import incremental_source_verification
        recheck=bool(getattr(self,'pending_local_action',None) and self.pending_local_action['type']=='REVERIFY_SOURCE')
        self.force_review_pages={r['source_page'] for r in self.source['claim_results'] if r['pdf_status']!='VERIFIED'} if recheck else set()
        if recheck and not self.force_review_pages:self.force_review_pages={c['source_page'] for c in self.a['claims']}
        self.p4=self.run_stage(4,lambda:incremental_source_verification(self,self.p3/'analysis_result.json',
            self.p3/'analysis_input.json',self.original_pdf,self.folder(4)/'output'),dependencies=None if recheck else self.a)
        self.source=read_json(self.p4/'source_verification.json')
        self.ledger.record(self.root.name,'source_verification','4',self.source)
        if recheck:self.finish_local_action()
        if self.rounds:
            self.history['events'].append({'event_id':'source_review_round_'+str(self.rounds),'operation':'SOURCE_REVIEW_COMPLETED',
                'status':'SUCCESS','finished_at':now(),'source_file':str(self.p4/'source_verification.json')})

    def finish_local_action(self):
        action={**self.pending_local_action,'executed':True,'finished_at':now()}
        self.history['action_history'].append(action);self.pending_local_action=None
        self.ledger.record(self.root.name,'local_action',action['type'],action)

    def evidence_node(self):
        from dc_verify.step.step5.code import assess_evidence as assessor
        context={'review_goal':self.request,'target_projects':self.targets,'as_of_date':self.as_of,'comparison_project':None}
        context_file=self.put(5,'review_context.json',context)
        if self.source['controller_observation']['verified_claim_ids']:
            p5=self.run_stage(5,lambda:assessor.assess_evidence(self.p3/'analysis_result.json',self.p4/'source_verification.json',
                context_file,self.folder(5)/'output',client=self.client),dependencies=[self.a,self.source])
            self.evidence=read_json(p5/'evidence_assessment.json')
        else:
            base={'assessment_status':'COMPLETED','review_goal_status':'INCOMPLETE',
                **{k:context[k] for k in ['review_goal','target_projects','as_of_date']},'claim_assessments':[],
                'unresolved_requirement_details':[{k:m[k] for k in ['requested_metric','reason','needed_evidence']} for m in self.a['missing_required_information']],
                'controller_observation':{'supported_claim_ids':[],'recheck_claim_ids':[],
                    'unresolved_requirements':[m['requested_metric'] for m in self.a['missing_required_information']],
                    'additional_evidence_needed':['원문 재검토 필요']}}
            self.evidence,_=assessor.merge_not_assessed(base,self.a,self.source)
            self.evidence,validation=assessor.validate_assessment(self.evidence,self.a,self.source,context)
            if validation['status']!='PASSED':raise PipelineBlocked('NOT_ASSESSED 검사 실패')
            self.put(5,'evidence_assessment.json',self.evidence);self.event(5,'SUCCESS')
        self.ledger.record(self.root.name,'evidence','5',self.evidence)

    def observe_node(self):
        from dc_verify.step.step6.code import controller_replan
        analyzed={p['source_page'] for p in self.a['analyzed_pages']};all_pages=list(range(1,self.ocr['total_pdf_pages']+1))
        if not hasattr(self,'history'):
            self.history={'action_history':[],'events':[],'search_policy':{'max_attempts_per_issue':2},'available_documents':[]}
        self.history['available_documents']=[{'document_id':self.document_id,'published_at':self.published_at,'all_pages':all_pages,
            'processed_pages':sorted(analyzed),'unprocessed_pages':[p for p in all_pages if p not in analyzed]}]
        for name,value in [('initial_plan',self.plan),('analysis_result',self.a),('source_verification',self.source),
            ('evidence_assessment',self.evidence),('execution_history',self.history)]:self.put(6,name+'.json',value)
        self.replan_path=self.run_stage(6,lambda:controller_replan.run_replan(self.folder(6),self.folder(6)/'output',client=self.client),
            dependencies=[self.a,self.source,self.evidence,self.history])
        self.replan_input=read_json(self.replan_path/'controller_input.json')
        self.ledger.record(self.root.name,'issues','6',self.replan_input['issue_inventory'])
        self.ledger.record(self.root.name,'plan','6',read_json(self.replan_path/'controller_decision.json'))

    def dispatch_node(self):
        from dc_verify.agent_prompt import DISPATCH_PROMPT
        decision=read_json(self.replan_path/'controller_decision.json'); action=decision['next_action']
        allowed=['counterevidence']
        if action['type']=='SEARCH_OFFICIAL':allowed.append('external')
        if self.rounds<2 and action['type']=='SEARCH_LOCAL':allowed.append('analysis')
        if self.rounds<2 and action['type']=='REVERIFY_SOURCE':allowed.append('source_verification')
        tools=[{'type':'function','name':name,'description':description,'parameters':schema.model_json_schema(),'strict':True}
            for name,description,schema in [('retrieve_local_evidence','Search saved source chunks using BM25 and dense RRF; candidates only.',Retrieval),
                ('choose_verification_route','Schedule exactly one allowed LangGraph node; runtime executes it after this function.',Route)]]
        self.event('dispatch','RUNNING')
        conversation=[{'role':'user','content':json.dumps({'controller_decision':decision,'allowed_routes':allowed,
            'review_goal':self.request,'as_of_date':self.as_of,'local_store_available':True},ensure_ascii=False)}]
        chosen=None
        for turn in range(4):
            response=self.client.responses.create(model=codex.PLANNING_MODEL,instructions=DISPATCH_PROMPT,input=conversation,
                tools=tools,tool_choice='required' if chosen is None else 'none',parallel_tool_calls=False,max_output_tokens=2048,store=False)
            conversation.extend(response.output)
            calls=[o for o in response.output if getattr(o,'type',None)=='function_call']
            if not calls:
                if chosen:break
                continue
            for call in calls:
                self.guard.check(call.name)
                if chosen:raise PipelineBlocked('Controller selected more than one route')
                if call.name=='retrieve_local_evidence':
                    args=Retrieval.model_validate_json(call.arguments)
                    result=self.store.search(args.query,self.as_of)
                    # Retained only as retrieval candidates; no insertion into verified inventory.
                    self.put('dispatch',f'local_candidates_{turn}.json',result)
                elif call.name=='choose_verification_route':
                    args=Route.model_validate_json(call.arguments)
                    if args.route not in allowed:raise PipelineBlocked('Controller route outside authoritative STEP 6 scope')
                    chosen=args.route;result={'scheduled_node':chosen,'execution_status':'SCHEDULED','reason':args.reason}
                else:raise PipelineBlocked('Unregistered function')
                self.ledger.record(self.root.name,'tool','dispatch',{'tool':call.name,'arguments':json.loads(call.arguments),'result':result})
                conversation.append({'type':'function_call_output','call_id':call.call_id,'output':json.dumps(result,ensure_ascii=False)})
            if chosen:
                # Close the Responses tool loop with actual tool output; no new tools may be selected.
                self.client.responses.create(model=codex.PLANNING_MODEL,instructions=DISPATCH_PROMPT,input=conversation,
                    tools=tools,tool_choice='none',max_output_tokens=512,store=False)
                break
        if not chosen:raise PipelineBlocked('Controller did not select a valid route within the tool budget')
        if chosen in {'analysis','source_verification'}:
            self.rounds+=1
            self.pending_local_action={**copy.deepcopy(action),'executed':False,
                'action_id':'local_'+str(self.rounds),'query':None,'started_at':now()}
        if chosen=='counterevidence':
            self.event(7,'SKIPPED',reason='Controller 선택 또는 실행 가능한 후속 작업 없음')
            self.event(8,'SKIPPED',reason='새 외부 자료 없음')
        self.event('dispatch','SUCCESS',route=chosen)
        return {'route':chosen}

    def external_node(self):
        self.external_path=self.external(self.replan_path)

    def counter_node(self):
        from dc_verify.agent_prompt import COUNTER_PROMPT
        from dc_verify.verification import CounterReview,checked_review
        from dc_verify.workflow_view import local_inputs
        inventory=local_inputs(self.plan,self.a,self.source,self.evidence,self.replan_input,self.original_pdf,self.ocr,self.history)['inventory']
        payload={'review_goal':self.request,'as_of_date':self.as_of,'claims':inventory,'missing':self.a['missing_required_information']}
        if getattr(self,'external_path',None):
            data=read_json(self.external_path/'normalized_input.json')
            payload['claims']=data['claims']
            payload['external_cross_check']=read_json(self.external_path/'cross_source_verification.json')
        known={c['claim_key'] for c in inventory}
        if getattr(self,'external_path',None):
            normalized=self.external_path/'normalized_input.json'
            if normalized.is_file():
                data=read_json(normalized)
                # External keys are admitted only if present in the actual cross-check input inventory.
                for row in data.get('claims',[]):known.add(row['claim_key'])
        self.counter=checked_review(self,'counterevidence',COUNTER_PROMPT,payload,CounterReview,known)

    def controller_node(self):
        if getattr(self,'external_path',None):
            from dc_verify.step.step9.code import controller
            from dc_verify.step.step10.code import report_builder
            registry=self.put(9,'runtime_tools.json',{'checked_at':now(),'tools':[{'tool_id':'TAVILY','available':False,'supported_actions':['SEARCH_OFFICIAL']}],
                'reason':'Bounded graph pass complete; unresolved requirements require human review or a new run.'})
            p9=self.run_stage(9,lambda:controller.execute(self.external_path,registry,self.folder(9)/'output',client=self.client,counterevidence=self.counter))
            self.event(10,'RUNNING');run=report_builder.build(p9,self.folder(10)/'output')
            if run['status']!='SUCCESS':raise PipelineBlocked('Report Builder failed')
            self.report_path=Path(run['output_dir'])/'report.html';self.event(10,'SUCCESS')
        else:self.report_path=self.local_finish()
        self.ledger.record(self.root.name,'decision','9',read_json(self.report_path.with_name('report.json'))['decision'])

    def final_verifier_node(self):
        from dc_verify.agent_prompt import FINAL_VERIFIER_PROMPT
        from dc_verify.verification import mechanical_report_checks,FinalReview,checked_review
        from dc_verify.step.step10.code import report_builder
        model=read_json(self.report_path.with_name('report.json'))
        claims=self.a['claims']
        if getattr(self,'external_path',None):claims=[r['claim'] for r in read_json(self.external_path/'normalized_input.json')['claims']]
        checks=mechanical_report_checks(model,claims);self.put('final-verifier','rule_checks.json',checks)
        known={r['claim_key'] for r in model['evidence']}
        review=checked_review(self,'final-verifier',FINAL_VERIFIER_PROMPT,
            {'report':model,'mechanical_checks':checks,'counterevidence':self.counter},FinalReview,known)
        model.update(counterevidence=self.counter,final_verifier=review,runtime_rule_checks=checks,
            storage={'ledger':str(self.ledger.path),'checkpoint_thread':self.root.name,'qdrant':str(self.store.path)})
        model['execution'].update(pipeline_model_requests=self.status['model_requests'],pipeline_run_mode=self.status['run_mode'],
            total_model_calls_including_embeddings=self.guard.calls,total_model_tokens=self.guard.tokens)
        model['workflow_stages']=copy.deepcopy(self.status['stages'])
        write_json(self.report_path.with_name('report.json'),model)
        self.report_path.write_text(report_builder.render_html(model))
        run_file=self.report_path.with_name('run.json')
        if run_file.is_file():
            run_meta=read_json(run_file);run_meta['html_sha256']=digest(self.report_path.read_bytes());write_json(run_file,run_meta)
        self.ledger.record(self.root.name,'rules','final-verifier',checks)
        if checks['status']!='PASSED' or review['status']=='BLOCKED':raise PipelineBlocked('최종 Verifier가 리포트를 승인하지 않았습니다. 규칙별 사유를 확인하세요.')
        return {'report':str(self.report_path)}


def execute(**kwargs):
    try:return GraphRunner(**kwargs).execute()
    except (OSError,ValueError) as error:
        result={'status':'BLOCKED','error_type':type(error).__name__,'reason':str(error),'model_requests':0}
        print(json.dumps(result,ensure_ascii=False));return result
