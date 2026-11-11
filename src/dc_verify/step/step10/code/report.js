/* All source/user strings are rendered with textContent or form values. No remote requests. */
"use strict";
const R=JSON.parse(document.getElementById('report-data').textContent);
const $=id=>document.getElementById(id);
const E=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined&&text!==null)n.textContent=String(text);if(cls)n.className=cls;return n;};
const add=(parent,...nodes)=>nodes.filter(Boolean).forEach(n=>parent.append(n));
const badge=(text,style='gray')=>E('span',text,'badge '+style);
const label={PARTIALLY_SUPPORTED:'부분 지원',UNRESOLVED:'미해결',NEEDS_HUMAN_REVIEW:'사람 검토 필요',RESOLVED:'해결',VERIFIED:'원문 일치',MATCH:'의미 일치',MISMATCH:'불일치',UNCLEAR:'불명확',NEEDS_MANUAL_REVIEW:'원문 재검토',NOT_ASSESSED:'미평가',SOURCE_STATEMENT_ONLY:'문서상 주장',INSUFFICIENT:'증빙 부족',NOT_VERIFIED:'미검증',VERIFICATION_FAILED:'검증 실패'};
const L=v=>label[v]||v||'미기재';
const decisionLabels={DEFER:'판단 보류',COMPLETE:'목적 검증 완료',REPLAN:'추가 조사 계획',REQUEST_HUMAN_REVIEW:'사람 검토 요청'};
const rowsByKey=new Map(R.evidence.map(r=>[r.claim_key,r]));
const sourceById=new Map(R.sources.map(s=>[s.document_id,s]));
function link(text,url){const a=E('a',text);if(typeof url==='string'&&(url.startsWith('originals/')||url.startsWith('#')||/^https?:\/\//i.test(url))){a.href=url;if(!url.startsWith('#')){a.target='_blank';a.rel='noopener noreferrer';}}return a;}
function refs(parent,items){items.forEach(ref=>{const r=rowsByKey.get(ref.claim_key);if(!r)return;const box=E('div',null,'review-reference');add(box,E('div',r.claim_key,'claim-key'),link(r.source_title+' · '+r.source_location_label,r.local_source));parent.append(box);});}
function list(items){const ul=E('ul');items.forEach(t=>ul.append(E('li',t)));return ul;}
function chips(parent,values){const c=E('div',null,'chips');values.forEach(v=>c.append(badge(v[0],v[1])));parent.append(c);}
function setupSummary(){
 $('project').textContent=R.review_context.target_projects.join(' · ');document.title=$('project').textContent+' · DC-Verify';
 $('goal').textContent=R.review_context.review_goal;$('asof').textContent='검토 기준일 '+R.review_context.as_of_date;
 $('generated').textContent='생성 '+R.generated_at.slice(0,19).replace('T',' ')+' UTC';$('report-id').textContent=R.report_id;
 $('decision-badge').textContent=(decisionLabels[R.decision.decision]||R.decision.decision)+' · '+R.decision.decision;
 $('decision-title').textContent=decisionLabels[R.decision.decision]||R.decision.decision;$('decision-reason').textContent=R.decision.reason;
 $('workflow-status').textContent='후속 작업 '+R.workflow.status;$('goal-status').textContent='목표 '+R.decision.goal_status;
 chips($('boundary-summary'),[[R.decision.current_operation_independently_verified?'현재 운영 독립 입증 있음':'현재 운영 독립 입증 미확인','amber'],[R.workflow.automated_work_allowed?'후속 실행 계획':'자동 실행 중지','gray']]);
 [['문서',R.summary_counts.documents],['전체 Claim',R.summary_counts.claims],['원문·의미 통과 주장',R.summary_counts.source_and_meaning_passed],['부분 지원 필수 요건',R.summary_counts.partial_requirements]].forEach(([t,n])=>{const c=E('div',null,'stat');add(c,E('strong',n),E('span',t));$('stats').append(c);});
 R.requirements.forEach((q,i)=>{const c=E('article',null,'requirement');const top=E('div',null,'card-top');add(top,E('span','REQUIREMENT '+String(i+1).padStart(2,'0'),'count-tag'),badge(L(q.controller_status),'amber'));add(c,top,E('h3',q.title),E('p',q.controller_reason),list(q.remaining_evidence));const foot=E('div',null,'cell-sub');foot.textContent='실제 검색 '+q.executed_searches+'회 · 남은 예산 '+q.remaining_searches+'회';c.append(foot);$('requirements-grid').append(c);});
 R.metric_views.forEach(v=>{const values=new Map();v.values_as_stated.forEach(s=>{if(!values.has(s.toLowerCase()))values.set(s.toLowerCase(),s);});const c=E('article',null,'metric-card');add(c,E('h3',v.label),E('div',values.size?[...values.values()].join(' / '):'확인되지 않음','metric-value'),E('p',v.note));if(v.claim_keys.length)c.append(E('p',v.claim_keys.length+'개 문서상 주장 · 최종 실적 확정과 별개'));if(v.excluded_claim_keys.length)c.append(E('p','검증 미통과 후보 '+v.excluded_claim_keys.length+'개 제외'));c.append(badge(v.status==='UNCONFIRMED'?'직접 근거 미확인':v.final_fact_confirmed?'채택 근거 있음':'문서상 주장 · 잠정 상태 유지',v.final_fact_confirmed?'teal':'amber'));$('metric-grid').append(c);});
}
let page=0;let printMode=false;const pageSize=10;
function renderEvidence(){
 const q=$('search').value.toLocaleLowerCase();const status=$('status-filter').value;const metric=$('metric-filter').value;
 const filtered=printMode?R.evidence:R.evidence.filter(r=>(metric==='all'||r.metric_type===metric)&&(!q||[r.claim_key,r.metric_label,r.metric_name,r.raw_value,r.document_id,r.source_title].join(' ').toLocaleLowerCase().includes(q))&&(status==='all'||status==='usable'&&r.source_statement_usable||status==='review'&&!r.source_statement_usable||status==='quarantine'&&r.original_group==='QUARANTINED'||status==='provisional'&&r.is_provisional));
 const pages=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(page,pages-1);$('evidence-body').replaceChildren();
 (printMode?filtered:filtered.slice(page*pageSize,(page+1)*pageSize)).forEach(r=>{
  const tr=E('tr');const a=E('td');add(a,E('div',r.metric_label,'cell-title'),E('div',r.raw_value,'raw-value'),E('div',r.metric_name,'cell-sub'));const b=E('td');add(b,E('div',r.basis_label),E('div','지표 기준일: '+(r.metric_as_of||'미기재'),'cell-sub'),E('div','범위: '+(r.facility_scope||'미기재'),'cell-sub'),E('div','상태: '+(r.statement_status||r.temporal_basis),'cell-sub'));
  const c=E('td');chips(c,[[L(r.source_status),r.source_status==='VERIFIED'?'teal':'red'],[L(r.semantic_status),r.semantic_status==='MATCH'?'teal':'amber']]);chips(c,[[L(r.evidence_level),'gray'],...(r.original_group==='QUARANTINED'?[['격리 유지','red']]:[]),...(r.is_provisional?[['잠정 · 공개일 확인 필요','amber']]:[])]);c.append(E('div',r.final_evidence_allowed?'증빙 채택 허용':'최종 증빙 채택 전','cell-sub'));
  const d=E('td');add(d,link(r.document_id+' · '+r.source_location_label,r.local_source),E('div',r.claim_key,'claim-key'));const detail=E('details');detail.append(E('summary','인용·검토 사유'));add(detail,E('div',r.evidence_quote,'quote'),E('p',r.semantic_reason,'cell-sub'));if(r.notes.length)detail.append(E('div',r.notes.join(' · '),'cell-sub'));add(detail,E('div','SHA-256 '+r.source_sha256,'hash'));d.append(detail);add(tr,a,b,c,d);$('evidence-body').append(tr);
 });
 $('evidence-count').textContent=filtered.length+' / '+R.evidence.length+'개 Claim';$('empty').hidden=filtered.length!==0;
 $('page-info').textContent=(page+1)+' / '+pages+' 페이지';$('prev').disabled=page===0;$('next').disabled=page>=pages-1;
}
function setupSources(){R.sources.forEach(s=>{const c=E('article',null,'source-card');chips(c,[[s.format,'gray'],[s.origin==='USER'?'사용자 원문':'외부 수집 원문','gray'],[s.source_admission==='CONDITIONAL'?'공개일 확인 전':s.source_admission==='ALLOW'?'분석 허용':'사용자 자료',s.source_admission==='CONDITIONAL'?'amber':'teal']]);add(c,E('h3',s.title),E('div',s.document_id,'source-id'));const p=E('div',null,'source-meta');add(p,E('div','공개일: '+(s.published_at||'미확인')),E('div',s.date_note));if(s.document_period)p.append(E('div','표지 기간: '+s.document_period.value+' · 공개일과 별개'));c.append(p);const links=E('div',null,'source-links');links.append(link(s.format==='PDF'?'원본 PDF':'원본 HTML 스냅샷',s.local_path));if(s.readable_path)links.append(link('읽기용 텍스트',s.readable_path));if(s.source_url)links.append(link('공식 원문 URL ↗',s.source_url));add(c,links,E('div','SHA-256 '+s.sha256,'hash'));$('source-grid').append(c);});}
const dimLabels={metric_definition:'지표 정의',facility_scope:'시설 범위',measurement_basis:'측정 기준',temporal_basis:'기준 시점',statement_status:'사업 상태',unit:'단위'};
const dimText={SAME:'동일',DIFFERENT:'다름',UNKNOWN:'미확인',NOT_APPLICABLE:'해당 없음'};
const timeLabels={UNKNOWN:'기준 시점 미확인',SAME_INTERVAL:'같은 명시 기간',OVERLAPPING_DIFFERENT_PRECISION:'기간이 겹치나 정밀도 다름',LEFT_BEFORE_RIGHT:'왼쪽이 앞선 명시 시점',RIGHT_BEFORE_LEFT:'오른쪽이 앞선 명시 시점'};
function setupPairs(){
 $('pair-count').textContent=R.cross_source_pairs.length+'개 API 검토 쌍';const counts=new Map();R.cross_source_pairs.forEach(p=>counts.set(p.relationship_label,(counts.get(p.relationship_label)||0)+1));counts.forEach((n,k)=>$('relationship-summary').append(badge(k+' '+n,'gray')));
 R.cross_source_pairs.forEach(p=>{const c=E('article',null,'pair-card');chips(c,[[p.relationship_label,p.relationship==='CONSISTENT_STATEMENTS'?'teal':'amber'],[p.goal_role==='CONTEXT_ONLY'?'일반 정의 · 목적 증빙 부족':p.comparison_mode==='DIAGNOSTIC_ONLY'?'진단 정보':'문서상 주장 관계','gray']]);
  const ref=E('div',null,'pair-ref');p.evidence_references.forEach((r,i)=>{if(i)ref.append(E('div','↕'));ref.append(E('div',r.claim_key));});c.append(ref);add(c,E('p',p.display_reason),E('div',timeLabels[p.time_alignment]||p.time_alignment,'cell-sub'));const ds=E('details');ds.append(E('summary','비교 차원·원문 확인'));const dims=E('div',null,'dimensions');Object.entries(p.comparison_dimensions).forEach(([k,v])=>dims.append(E('span',(dimLabels[k]||k)+' · '+(dimText[v]||v))));ds.append(dims);refs(ds,p.evidence_references);if(p.additional_evidence_needed.length)ds.append(list(p.additional_evidence_needed));if(p.model_reason!==p.display_reason){ds.append(E('div','원 모델 설명 · 최종 판정과 다를 수 있음','cell-sub'));ds.append(E('div',p.model_reason,'quote'));}c.append(ds);$('pair-grid').append(c);});
 $('diagnostic-summary').textContent='확정 비교에서 제외한 로컬 진단 '+R.local_diagnostics.length+'쌍';R.local_diagnostics.forEach(p=>{const c=E('div',null,'diagnostic-item');add(c,E('div',p.reason));refs(c,p.evidence_references);$('diagnostics').append(c);});
}
const storageKey='dc-verify-review:'+R.report_id;const controls=new Map();let saved=new Map();let persistent=true;
try{const x=JSON.parse(localStorage.getItem(storageKey)||'null');if(x&&x.report_id===R.report_id&&Array.isArray(x.reviews))saved=new Map(x.reviews.map(r=>[r.issue_id,r]));}catch(e){persistent=false;}
function annotation(){return {schema_version:'DC_VERIFY_HUMAN_ANNOTATION_1',report_id:R.report_id,input_snapshot_digest:R.input_snapshot_digest,exported_at:new Date().toISOString(),annotation_effect:R.execution.annotation_effect,verification_state_changed:false,reviews:R.human_review.map(q=>{const c=controls.get(q.issue_id);return {issue_id:q.issue_id,claim_keys:q.references.map(r=>r.claim_key),status:c.status.value,comment:c.comment.value,evidence_url:c.url.value};})};}
function save(){const payload=annotation();try{localStorage.setItem(storageKey,JSON.stringify(payload));$('save-status').textContent='검토 메모 저장됨 · 검증 상태는 변경되지 않습니다.';}catch(e){persistent=false;$('save-status').textContent='브라우저 저장을 사용할 수 없습니다. JSON 내보내기로 메모를 보관하세요.';}return payload;}
function setupReviews(){
 R.human_review.forEach(q=>{const c=E('article',null,'review-card');const top=E('div',null,'card-top');add(top,E('span','REVIEW '+String(q.priority).padStart(2,'0'),'count-tag'),badge(L(q.controller_status),'amber'));add(c,top,E('h3',q.title),E('div',q.issue_id,'claim-key'),E('p',q.question),list(q.remaining_evidence));refs(c,q.references);
  const prior=saved.get(q.issue_id)||{};const status=E('select');status.setAttribute('aria-label','검토 메모 상태 '+q.issue_id);[['NOT_REVIEWED','미검토'],['NEEDS_MORE_EVIDENCE','추가 자료 필요'],['COMMENTED','의견 작성 완료']].forEach(([v,t])=>{const o=E('option',t);o.value=v;status.append(o);});status.value=['NOT_REVIEWED','NEEDS_MORE_EVIDENCE','COMMENTED'].includes(prior.status)?prior.status:'NOT_REVIEWED';const statusLabel=E('label','내 검토 메모 상태');statusLabel.append(status);
  const comment=E('textarea');comment.setAttribute('aria-label','검토 메모 '+q.issue_id);comment.placeholder='원문 해석, 추가 확인 결과, 필요한 자료를 기록하세요.';comment.maxLength=10000;comment.value=typeof prior.comment==='string'?prior.comment.slice(0,10000):'';const commentLabel=E('label','검토 메모');commentLabel.append(comment);
  const url=E('input');url.type='url';url.placeholder='https://';url.maxLength=4000;url.setAttribute('aria-label','추가 근거 URL '+q.issue_id);url.value=typeof prior.evidence_url==='string'?prior.evidence_url.slice(0,4000):'';const urlLabel=E('label','추가 근거 URL · 자동 조회하지 않음');urlLabel.append(url);controls.set(q.issue_id,{status,comment,url});add(c,statusLabel,commentLabel,urlLabel);$('review-grid').append(c);
 });
 if(!persistent)$('save-status').textContent='브라우저 저장이 제한될 수 있습니다. JSON 내보내기를 이용하세요.';
 $('save-review').addEventListener('click',save);$('export-review').addEventListener('click',()=>{const payload=save();const blob=new Blob([JSON.stringify(payload,null,2)],{type:'application/json;charset=utf-8'});const a=E('a');a.href=URL.createObjectURL(blob);a.download='human_review_annotations_'+R.report_id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000);});
}
function setupExecution(){
 const cards=[['WORKFLOW',R.workflow.status,'자동 후속 실행 '+(R.workflow.automated_work_allowed?'허용':'중지')+' · 검토 목표 '+R.decision.goal_status],['MODEL & SEARCH','새 호출 없음','이 리포트의 LLM '+R.execution.new_model_calls+'회 · 검색 '+R.execution.new_searches+'회. 이전 Controller 응답 '+R.execution.recorded_controller_responses+'개는 저장 기록입니다.'],['EVIDENCE GATE','원래 판정 유지','격리 '+R.summary_counts.quarantined+'개 · 원문·의미 통과는 최종 증빙 채택과 별개입니다.']];
 cards.forEach(([k,t,p])=>{const c=E('article',null,'execution-card');add(c,E('span',k,'section-kicker'),E('h3',t),E('p',p));$('execution-grid').append(c);});
 const budget=E('article',null,'execution-card');add(budget,E('span','SEARCH BUDGET','section-kicker'),E('h3','기존 검색 '+R.execution.prior_executed_searches+'회 유지'));
 Object.entries(R.search_budget.remaining_attempts).forEach(([id,n])=>{const q=R.requirements.find(r=>r.issue_id===id);budget.append(E('p',(q?q.title:id)+' · 남은 '+n+'회'));});$('execution-grid').append(budget);
 const trace=E('article',null,'execution-card');add(trace,E('span','AUDIT TRAIL','section-kicker'),E('h3','저장 파일과 연결'));[['report.json','리포트 JSON'],['evidence.csv','전체 Evidence CSV'],['normalizations.json','표시 보완 내역'],['execution_verification.json','출력 검사 기록']].forEach(([path,t])=>{const a=E('a',t);a.href=path;a.target='_blank';a.rel='noopener';trace.append(E('p'));trace.lastChild.append(a);});$('execution-grid').append(trace);
 const end=E('article',null,'execution-card');add(end,E('span','STOP CONDITION','section-kicker'),E('h3','Controller의 종료 이유'),E('p',R.decision.stop_condition));$('execution-grid').append(end);
}
setupSummary();setupSources();setupPairs();setupReviews();setupExecution();
const metricOptions=new Map(R.evidence.map(r=>[r.metric_type,r.metric_label]));metricOptions.forEach((t,v)=>{const o=E('option',t);o.value=v;$('metric-filter').append(o);});
['search','status-filter','metric-filter'].forEach(id=>$(id).addEventListener(id==='search'?'input':'change',()=>{page=0;renderEvidence();}));
$('clear').addEventListener('click',()=>{$('search').value='';$('status-filter').value='all';$('metric-filter').value='all';page=0;renderEvidence();});
$('prev').addEventListener('click',()=>{page--;renderEvidence();});$('next').addEventListener('click',()=>{page++;renderEvidence();});$('print').addEventListener('click',()=>window.print());renderEvidence();
window.addEventListener('beforeprint',()=>{printMode=true;renderEvidence();});window.addEventListener('afterprint',()=>{printMode=false;renderEvidence();});
