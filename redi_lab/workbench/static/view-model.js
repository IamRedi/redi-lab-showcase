/* Pure presentation functions, shared by the browser and offline contract tests. */
const WorkbenchView = (() => {
  const active = s => ['ACTIVE','WORKING','RUNNING','TESTING','REVIEWING','VERIFYING','RECOVERY','REPLANNING','ESCALATED','ANALYZING','RECOVERY DECISION'].includes(s);
  const state = s => ({COMPLETED:'VERIFIED',PASS:'VERIFIED',FAIL:'FAILED',RUNNING:'WORKING',ACTIVE:'WORKING',PENDING:'WAITING'}[s] || s || 'NOT_RUN');
  const tone = s => ['OWNER_ACTION_REQUIRED','FAILED_INTERNAL'].includes(s)?'blocked':active(s)?'working':state(s).toLowerCase();
  const verdict = s => s.status === 'OWNER_ACTION_REQUIRED' ? 'OWNER ACTION REQUIRED' : s.status === 'BLOCKED' ? 'BLOCKED' : s.verdict || s.app_state || s.status;
  const duration = v => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v) < 60 ? Number(v).toFixed(1).replace(/\.0$/,'')+'s' : Math.floor(Number(v)/60)+'m '+Math.round(Number(v)%60)+'s';
  const number = v => v == null ? '—' : Number(v).toLocaleString('en-US');
  const cost = v => v == null || v === '' || !Number.isFinite(Number(v)) ? '—' : '$'+Number(v).toFixed(Number(v)===0?2:6).replace(/(\.\d{2,}?)0+$/,'$1');
  function metrics(s) {
    const p=s.presentation||{};
    const elapsed=Object.hasOwn(p,'elapsed_seconds')?p.elapsed_seconds:s.execution_mode==='retained_qualification_offline'&&!s.elapsed_seconds?null:s.elapsed_seconds;
    const rows=[['Run',s.build_id?'wb-'+s.build_id:'—'],['Provider',p.provider||'—'],['Model',p.model||'—'],['Calls',number(Object.hasOwn(p,'provider_calls')?p.provider_calls:s.provider_calls)],['Tokens · in / out',number(p.input_tokens)+' / '+number(p.output_tokens)],['Cost',cost(Object.hasOwn(p,'cost_usd')?p.cost_usd:s.cost_usd)],['Elapsed',duration(elapsed)]];
    const vf=s.visual_facts;
    if(vf){const z=(s.virtutes?.crew||[]).find(c=>c.persona.id==='zana');rows.push(['Visual worker',z?.technical?.provider&&z.worker_model?z.technical.provider+' / '+z.worker_model:z?.worker_model||'-'],['Visual calls',number(vf.provider_calls)],['Visual cost',cost(vf.cost_usd)],['Visual reserved',cost(vf.reserved_cost_usd)],['Visual duration',duration(vf.duration_seconds)],['Visual assets',number(vf.retained_assets)],['Total calls',number(s.total_provider_calls)],['Product cost upper bound',cost(s.total_cost_upper_bound_usd)]);}
    const b=s.budget_telemetry;
    if(b)rows.push(['Authorized overall',cost(b.authorized_overall_usd)],['Authorized visual',cost(b.authorized_visual_usd)],['Spent total',cost(b.spent_total_usd)],['Spent visual',cost(b.spent_visual_usd)],['Reserved / uncertain total',cost(b.reserved_total_usd)],['Reserved / uncertain visual',cost(b.reserved_visual_usd)],['Remaining total',cost(b.remaining_total_usd)],['Remaining visual',cost(b.remaining_visual_usd)]);
    return rows;
  }
  function graph(s, layout={}) {
    const crew=s.virtutes?.crew||[];
    const personas=new Map((s.presentation?.personas||crew.map(c=>c.persona)).map(p=>[p.id,p]));
    const cards=new Map(crew.map(c=>[c.persona.id,c]));
    const manifest=s.presentation?.manifest||s.manifest;
    const nodes=[];
    function add(id,pid,title,st,deps,detail={}) {
      const p=personas.get(pid)||{id:pid,name:pid||id,role:'Recorded task'};
      nodes.push({id,persona:p,title,state:state(st),dependencies:deps,...detail});
    }
    for(const pid of ['zojz','ora']) {
      const c=cards.get(pid);
      add('@'+pid,pid,pid==='zojz'?'Supreme Orchestrator':'Planning / Scheduling',c?.state||'WAITING',pid==='ora'?['@zojz']:[],{kind:'orchestration',purpose:c?.current_task});
    }
    const tasks=manifest?.tasks||[];
    for(const t of tasks) {
      const live=(s.tasks||[]).find(x=>x.node_id===t.task_id);
      const st=s.task_states?.[t.task_id]||live?.status||'NOT_RUN';
      add(t.task_id,t.persona,t.purpose||t.station||t.task_id,st,t.dependencies?.length?t.dependencies:['@ora'],{purpose:t.purpose,task_id:t.task_id,files:t.owned_paths||(t.artifact?[t.artifact]:[]),model:s.recovery_profiles?.[t.task_id]?.model||t.worker_profile?.model});
    }
    // Legacy records without a retained DAG stay an explicit station view.
    if(!tasks.length) for(const c of crew.filter(c=>!['zojz','ora'].includes(c.persona.id)&&c.state!=='WAITING'))
      add('@'+c.persona.id,c.persona.id,c.current_task||c.persona.role,c.state,[],{purpose:c.current_task,kind:'station'});
    const levels=new Map(),visiting=new Set();
    const byId=new Map(nodes.map(n=>[n.id,n]));
    function depth(id){if(levels.has(id))return levels.get(id);if(visiting.has(id))return 0;visiting.add(id);const n=byId.get(id);const d=n?.dependencies.length?1+Math.max(...n.dependencies.map(x=>byId.has(x)?depth(x):0)):0;visiting.delete(id);levels.set(id,d);return d;}
    nodes.forEach(n=>depth(n.id));
    const rows=[];for(const n of nodes){const d=n.kind==='station'?2:levels.get(n.id);(rows[d]||=[]).push(n);}
    const width=Math.max(470,...rows.map(r=>(r?.length||0)*234+24));
    // Density is a view concern: task IDs, states and edges stay unchanged.
    const pitch=layout.desktop&&layout.height>0?Math.max(58,Math.min(110,(layout.height-24)/rows.length)):94;
    const nodeHeight=layout.desktop?Math.max(48,Math.min(90,pitch-12)):76;
    rows.forEach((row,i)=>row.forEach((n,j)=>{n.x=(width-row.length*234)/2+j*234+12;n.y=i*pitch+12;}));
    const edges=[];
    for(const n of nodes)for(const dep of n.dependencies){const parent=byId.get(dep);if(parent)edges.push({from:parent,to:n,active:active(n.state)&&parent.state==='VERIFIED'});}
    return {nodes,edges,width,height:rows.length*pitch+12,nodeHeight,compact:nodeHeight<70,hasManifest:!!tasks.length};
  }
  function failure(s){
    if(s.owner_action)return {stage:s.owner_action.reason,reason:s.owner_action.cause+' '+s.owner_action.decision_needed};
    if(s.status==='BLOCKED')return {stage:'Build preflight',reason:s.detail||s.error||(s.missing||[]).join(', ')||'The factory recorded a blocker.'};
    const failedTask=Object.entries(s.task_states||{}).find(([,v])=>v==='FAILED');
    const failedGate=Object.entries(s.gate_states||{}).find(([,v])=>v==='FAIL'||v==='FAILED');
    const stage=failedTask?.[0]||failedGate?.[0]||s.presentation?.failure_stage||(s.product_review?.verdict==='FAIL'?'Product review':s.detail?.startsWith('Delivery')?'Delivery':'Factory');
    const reason=s.presentation?.failures?.[stage]||s.presentation?.gate_reasons?.[stage]||s.product_review?.reasons?.[0]||s.error||s.detail||'No further reason was recorded.';
    return {stage,reason:typeof reason==='string'?reason:JSON.stringify(reason)};
  }
  function eventText(e){const name=typeof e==='string'?e:e.event||'';const names={planning_started:'Planning started',scope_resolved:'Execution plan recorded',product_review_started:'Product review started','product_review_finished:pass':'Product review passed',run_finished:'Run finished',product_integrity_passed:'Product integrity passed'};return names[name]||name.replace('component_task_started:','Task started · ').replace('component_task_completed:','Task verified · ').replace('component_task_failed:','Task failed · ').replaceAll('_',' ').replaceAll(':',' · ');}
  return {active,state,tone,verdict,duration,number,cost,metrics,graph,failure,eventText};
})();
if(typeof module!=='undefined')module.exports=WorkbenchView;
