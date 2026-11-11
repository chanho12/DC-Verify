"""Connect the existing Planner/Executor/Fetcher and external verification stages."""
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit

from dc_verify.step.step8.code.source_admission import read_json,write_json,now


def execute_external(runner,p6):
    from dc_verify.step.step7.code import search_planner,search_executor,search_api_adapter,search_fetcher
    from dc_verify.step.step7.code.document_preparer import Preparer
    from dc_verify.step.step8.code import source_admission,external_analysis,external_verification,cross_source
    from dc_verify.pipeline import PipelineBlocked
    decision=read_json(p6/'controller_decision.json')
    if decision['next_action']['type']!='SEARCH_OFFICIAL':
        runner.event(7,'SKIPPED',reason='Controller가 외부 검색을 선택하지 않았습니다: '+decision['next_action']['type'])
        runner.event(8,'SKIPPED',reason='새 외부 자료 없음');return None
    runner.event(7,'RUNNING')
    key=search_api_adapter.load_tavily_key()
    hosts=[]
    for value in runner.domains:
        host=urlsplit(value if '://' in value else 'https://'+value).hostname
        if not host:raise PipelineBlocked('--official-domain에 실제 공식 호스트를 입력하세요.')
        hosts.append(host)
    source_types=['OFFICIAL_PRESS_RELEASE','OFFICIAL_IR','REGULATORY_FILING','FINANCIAL_STATEMENT',
        'CONTRACT_DISCLOSURE','OFFICIAL_PROJECT_DOCUMENT','OFFICIAL_METHODOLOGY']
    available=bool(key and hosts)
    tool={'tool_id':'TAVILY','available':available,'execution_context':'PYTHON' if available else 'UNCONNECTED',
        'standalone_available':available,'adapter':'TAVILY_HTTP','kind':'WEB_SEARCH','source_types':source_types}
    runner.copied(7,'controller_decision.json',p6/'controller_decision.json')
    runner.copied(7,'controller_handoff.json',p6/'next_action_input.json')
    runner.put(7,'search_history.json',runner.history)
    runner.put(7,'available_tools.json',{'checked_at':now(),'tools':[tool],'verified_domains':hosts})
    p7=runner.run_stage('7-plan',lambda:search_planner.run_planner(runner.folder(7),runner.folder(7)/'output',client=runner.client))
    plan=read_json(p7/'search_plan.json')
    if not plan['search_plan']:
        reason='TAVILY_API_KEY 미설정' if not key else '공식 출처 범위가 없습니다. --official-domain으로 지정하세요.' if not hosts else 'Controller 쟁점의 가용 검색 예산 없음'
        runner.event(7,'SKIPPED_TOOL_UNAVAILABLE',reason=reason,output=str(p7))
        runner.event(8,'SKIPPED',reason='새 외부 자료 없음');return None
    runtime=runner.put(7,'runtime_tools.json',{'execution_context':'PYTHON','tools':[tool],'official_hosts':hosts,
        'official_host_provenance':{'method':'USER_DECLARED_OFFICIAL_DOMAINS','observed_urls':[]},'reviewed_documents':[]})
    search=search_executor.init_run(p7/'search_executor_input.json',runtime,runner.folder(7)/'output')
    # Archive real pages, not a generated assertion that a host was visited.
    registry=read_json(search/'runtime_tools.json');observed=[]
    for index,host in enumerate(hosts):
        try:
            response=search_fetcher.fetch_bytes('https://'+host+'/',max_bytes=2_000_000,allowed_hosts=set(hosts))
            if response['status']==200:
                observed.append(response['url'])
                write_json(search/'raw_retrieval'/f'origin_{index}.json',{'operation':'OPEN_DOCUMENT',
                    'response':response['url']+'\n'+response['body'].decode('utf-8',errors='replace'),'http_status':200})
        except Exception as error:
            write_json(search/'raw_retrieval'/f'origin_{index}_failure.json',{'operation':'OPEN_DOCUMENT','status':'FAILED','error_type':type(error).__name__})
    registry['official_host_provenance']['observed_urls']=observed;write_json(search/'runtime_tools.json',registry)
    if getattr(runner,'guard',None):search_api_adapter.execute_api(search,key=key,guard=runner.guard)
    else:search_api_adapter.execute_api(search,key=key)
    state=read_json(search/'state.json')
    urls=[d['url'] for d in state['documents'] if urlsplit(d['url']).hostname in set(hosts)][:12]
    if getattr(runner,'guard',None):
        for url in urls:runner.guard.check('retrieve_local_evidence',url=url)
    if urls:search_fetcher.collect(search,urls)
    runner.history=read_json(search/'search_history_updated.json')
    search_run=read_json(search/'run.json')
    runner.event(7,'PARTIAL' if search_run['search_failures'] else 'SUCCESS',output=str(search),
        search_requests=len(read_json(search/'state.json')['attempts']),
        reason='검색 API 실패가 있어 실패 영수증과 미해결 쟁점을 보존했습니다.' if search_run['search_failures'] else '')
    if not read_json(search/'collected_documents.json'):
        runner.event(8,'SKIPPED_NO_DOCUMENTS',reason='검색 실행은 기록했으나 다운로드한 원문이 없습니다.');return None
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    preparation=runner.folder(7)/'output'/('document_preparation_'+stamp)
    runner.event('7-prepare','RUNNING')
    prepared=Preparer(search,preparation,allow_vision=True,client=runner.client).run()
    if prepared['status']!='SUCCESS':raise PipelineBlocked('외부 문서 전처리 실패')
    runner.event('7-prepare','SUCCESS',output=str(preparation))
    runner.event(8,'RUNNING')
    gate=runner.run_stage('8-admission',lambda:source_admission.run_gate(preparation,runner.folder(8)/'output'))
    jobs=read_json(gate/'analysis_input.json')['analysis_jobs']
    if not jobs:
        runner.event(8,'SKIPPED_NO_ADMITTED_DOCUMENTS',reason='확보 원문이 출처·날짜·범위 검사를 통과하지 못했습니다.',output=str(gate));return None
    if getattr(runner,'store',None):
        # Admission permits analysis; retrieved chunks remain unverified candidates.
        for job in jobs:
            doc=job['bundle']['documents'][0]
            runner.store.index(doc['document_id'],doc['source_sha256'],doc['pages'],
                {'title':doc['title'],'source_path':doc['source_file'],'source_sha256':doc['source_sha256'],
                 'url':doc.get('source_url'),'published_at':doc.get('published_at'),
                 'admission':job['analysis_admission'],'version_type':'SOURCE_SHA256'})
    external=runner.run_stage('8-analysis',lambda:external_analysis.execute(gate,runner.folder(8)/'output',client=runner.client))
    verified=runner.run_stage('8-verification',lambda:external_verification.execute(external,runner.folder(8)/'output',client=runner.client))
    pairs=runner.run_stage('8-cross',lambda:cross_source.execute(verified,runner.folder(8)/'output',client=runner.client))
    runner.event(8,'SUCCESS',output=str(pairs));return pairs
