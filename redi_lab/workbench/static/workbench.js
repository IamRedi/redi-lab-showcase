'use strict';
const $=id=>document.getElementById(id), V=WorkbenchView;
let buildId=null,timer=null,snapshot=null,sourceSnapshot=null,graphData=null,selection=null,zoom=1,graphSignature='',layoutSignature='',polling=false,runtime=null,dialogTrigger=null;
let ownerBusy=false;
const desktop=()=>matchMedia('(min-width:1001px)').matches;
let panelRestore=null;
const esc=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const text=(id,v)=>{const el=$(id),value=String(v??'');if(el.textContent!==value)el.textContent=value;};
function html(id,value){if($(id)._rendered!==value){$(id).innerHTML=value;$(id)._rendered=value;}}
const badge=s=>'<span class="badge '+V.tone(s)+'">'+esc(V.state(s))+'</span>';
// Stable role glyphs: presentation identity never depends on provider/model.
const glyphs={zojz:'M12 2 22 12 12 22 2 12Z M7 8l5 10 5-10 M12 2v5',ora:'M12 8v5 M5 17v-4h14v4 M9 3h6v5H9Z M2 17h6v5H2Z M16 17h6v5h-6Z',agron:'M8 5 2 12l6 7 M16 5l6 7-6 7 M14 3l-4 18',teuta:'M3 4h18v16H3Z M3 9h18 M6 6h1 M9 6h1',andinus:'M3 6c0-5 18-5 18 0s-18 5-18 0v12c0 5 18 5 18 0V6 M3 12c0 5 18 5 18 0',bindus:'M3 8h14l-4-4 M21 16H7l4 4 M17 8l-4 4 M7 16l4-4',redon:'M8 8 4 12l4 4 M16 8l4 4-4 4 M10 18l4-12',bato:'M9 3h6 M10 3v7l-6 9q-1 2 2 2h12q3 0 2-2l-6-9V3 M8 15h8',monunius:'M12 2 21 6v6c0 5-9 10-9 10S3 17 3 12V6Z M8 12l3 3 5-6',medaurus:'M12 2 21 6v6c0 5-9 10-9 10S3 17 3 12V6Z M8 12l3 3 5-6',gentius:'M6 4v16 M6 12h12 M18 4v16 M3 2h6v4H3Z M3 18h6v4H3Z M15 10h6v4h-6Z'};
glyphs.zana='M12 2l2.7 6.3L21 11l-6.3 2.7L12 20l-2.7-6.3L3 11l6.3-2.7Z M12 7v8 M8 11h8';
const mark=p=>'<span class="persona-mark persona-'+esc(p.id)+'" aria-hidden="true">'+(glyphs[p.id]?'<svg viewBox="0 0 24 24"><path d="'+glyphs[p.id]+'"/></svg>':esc(p.name.slice(0,2).toUpperCase()))+'</span>';
const terminal=s=>s.status==='VERIFIED'||s.status==='FAILED'||s.status==='BLOCKED'||s.status==='OWNER_ACTION_REQUIRED';
async function j(url,opt){const r=await fetch(url,{cache:'no-store',...opt});const data=await r.json();if(!r.ok||data.error&&!data.status)throw Error(data.error||'Request failed');return data;}
function notice(message){text('notice',message);$('notice').hidden=!message;}
function normalizeActive(v){return String(v).toLowerCase()==='true';}
function backendOptionsHtml(backends){return Object.entries(backends).map(([id,m])=>'<option value="'+esc(id)+'"'+(normalizeActive(m.active)?'':' disabled')+'>'+esc(m.label)+(normalizeActive(m.active)?'':' (unavailable)')+'</option>').join('');}
function hydrateBackendOptions(backends){const sel=$('backend');if(sel.options.length){return;}sel.innerHTML=backendOptionsHtml(backends);}
function syncBackendUI(){const mode=$('backend').value,live=mode.startsWith('openai-live'),visual=mode==='openai-live-professional-product-v1';$('ceiling-row').hidden=!live;$('visual-budget-row').hidden=!visual;text('backend-help',visual?'Enter separate overall and visual ceilings before a professional live build.':live?'Live execution requires an enabled provider and an explicit overall ceiling.':'Dry execution uses deterministic fake workers.');}
function bindEventHandlers(){
 document.getElementById('backend').addEventListener('change',syncBackendUI);
 document.getElementById('build').addEventListener('click',startBuild);
}
function surface(name){document.body.dataset.surface=name;document.querySelectorAll('button[data-surface]').forEach(b=>b.classList.toggle('selected',b.dataset.surface===name));if(name==='run'){requestAnimationFrame(()=>fitGraph());}window.scrollTo({top:0,behavior:'instant'});}
async function startBuild(){
 const prompt=$('prompt').value.trim(),backend=$('backend').value;
 if(!prompt){$('prompt').reportValidity();$('prompt').focus();return;}
 const body={prompt,backend};
 const money=x=>/^\d+(?:\.\d{1,2})?$/.test(x)&&Number(x)>0;
 if(backend.startsWith('openai-live')){const ceiling=$('ceiling').value.trim();if(!money(ceiling)){$('build-settings').open=true;notice('Enter a positive overall product ceiling in USD.');$('ceiling').focus();return;}body.cost_ceiling_usd=ceiling;}
 if(backend==='openai-live-professional-product-v1'){
  const visual=$('visual-ceiling').value.trim(),calls=Number($('max-visual-calls').value),recovery=Number($('max-visual-recovery').value);
  const visualsOff=/^0(?:\.0{1,2})?$/.test(visual);
  if(!visualsOff&&(!money(visual)||Number(visual)>Number(body.cost_ceiling_usd)||Number(visual)<1)){$('build-settings').open=true;notice('Enter 0 to build without visuals, or a visual ceiling of at least USD 1.00 that does not exceed the overall ceiling.');$('visual-ceiling').focus();return;}
  if(!Number.isInteger(calls)||calls<1||calls>3||!Number.isInteger(recovery)||recovery<0||recovery>=calls){$('build-settings').open=true;$('visual-advanced').open=true;notice('Visual calls must be 1–3; recovery generations must be 0 through one less than the call limit.');$('max-visual-calls').focus();return;}
  body.visual_ceiling_usd=visual;body.max_visual_generation_calls=calls;body.max_visual_recovery_generations=recovery;
 }
 $('build').disabled=true;notice('');text('request-status','Submitting your request…');
 try{const r=await j('/api/builds',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});text('request-status','Request submitted.');await loadBuild(r.build_id);surface('run');}
 catch(e){notice(e.message);text('request-status','Request was not submitted.');}
 finally{$('build').disabled=false;}
}
function instrument([label,value]){return '<div class="instrument"><span class="label">'+esc(label)+'</span><span class="value" title="'+esc(value)+'">'+esc(value)+'</span></div>';}
async function loadBuild(id){
 if(desktop())$('build-settings').open=false;
 const saved=$('saved-build');if(![...saved.options].some(o=>o.value===id)){const option=document.createElement('option');option.value=id;option.textContent='wb-'+id;saved.append(option);}saved.value=id;
 clearTimeout(timer);buildId=id;snapshot=null;sourceSnapshot=null;selection=null;graphSignature='';layoutSignature='';notice('');history.replaceState(null,'','/?build='+encodeURIComponent(id));await poll();
}
async function poll(){
 if(polling||!buildId)return;polling=true;const requested=buildId;
 try{const s=await j('/api/builds/'+encodeURIComponent(requested));if(requested!==buildId)return;
  const source=s.presentation?.source_run;
  if(source&&!sourceSnapshot){try{sourceSnapshot=await j('/api/builds/'+source.build_id);}catch(e){notice('Original generation could not be read: '+e.message);}}
  if(requested!==buildId)return;snapshot=s;render(s);if(!terminal(s))timer=setTimeout(poll,1000);
 }catch(e){notice('Unable to refresh this run. '+e.message);timer=setTimeout(poll,4000);}
 finally{polling=false;if(requested!==buildId)timer=setTimeout(poll,0);}
}
function render(s){
 document.body.dataset.runStatus=s.status;document.body.dataset.hasRun='true';
 const p=s.presentation||{},crew=s.virtutes?.crew||[],ms=V.metrics(s),mode=s.execution_mode==='retained_qualification_offline'?'Retained qualification · offline':s.execution_mode==='injected_offline'||s.backend==='dry-fake'?'Offline execution · fake workers':s.execution_mode==='real_provider'?'Real provider execution':'Recorded execution';
 text('run-eyebrow',s.build_id?'RUN / '+s.build_id:'YOUR WORKSPACE');text('run-title',s.product_name||'Factory run');text('run-subtitle',mode);
 text('run-state',V.verdict(s));$('run-state').className='badge '+V.tone(s.status);$('fixture-banner').hidden=!s.visual_fixture;
 text('crew-active',(s.virtutes?.active_count??'—')+' active');
 const finished=Object.values(s.task_states||{}).filter(x=>x==='COMPLETED'||x==='VERIFIED').length,total=Object.keys(s.task_states||{}).length;
 const activeCrew=crew.filter(c=>V.active(c.state)&&c.persona.id!=='zojz');
 text('overview-copy',terminal(s)?V.verdict(s)+(total?' · '+finished+' / '+total+' tasks completed':''):(total?finished+' / '+total+' tasks completed. ':'')+(activeCrew.length?activeCrew.map(c=>c.persona.name+' · '+c.current_task).join(' / '):'Waiting for recorded task progress.'));
 html('compact-crew',crew.map(c=>'<span class="crew-chip '+V.tone(c.state)+'" title="'+esc(c.persona.role+' · '+c.state)+'">'+mark(c.persona)+'<span>'+esc(c.persona.name)+'</span><span class="sr-only">'+esc(c.state)+'</span></span>').join(''));
 html('telemetry-values',ms.map(instrument).join(''));html('metrics',[...ms,['Backend',s.backend||'—']].map(([k,v])=>'<div><dt>'+esc(k)+'</dt><dd>'+esc(v)+'</dd></div>').join(''));
 text('state-announcement',V.verdict(s)+'. '+crew.filter(c=>V.active(c.state)).map(c=>c.persona.name+': '+c.state).join('. '));
 renderResult(s,ms);renderGraph(sourceSnapshot||s);renderContext();renderEvents(s);
 const files=[...new Set([...(s.files||[]),...(p.files||[])])];html('files',files.length?files.map(f=>'<li>'+esc(f)+'</li>').join(''):'<li>No file paths recorded.</li>');
 html('evidence-details','<p>'+esc(mode)+'</p><p>'+esc(s.backend||'')+'</p><p>'+esc(p.evidence_path||'In-memory run snapshot')+'</p>'+(p.source_run?'<p>Original generation: <a href="/?build='+esc(p.source_run.build_id)+'">wb-'+esc(p.source_run.build_id)+'</a> · '+esc(p.source_run.status)+'</p><p>Source evidence SHA-256<br>'+esc(p.source_run.sha256)+'</p>':'')+(p.prompt?'<details><summary>Recorded request</summary><p>'+esc(p.prompt)+'</p></details>':''));
}
function renderGraph(s){
 const data=V.graph({...s,presentation:{personas:WORKBENCH_PERSONAS,...s.presentation}},{desktop:desktop(),height:$('graph-viewport').clientHeight});graphData=data;
 $('graph-stage').style.setProperty('--station-height',data.nodeHeight+'px');$('graph-stage').classList.toggle('compact-stations',data.compact);
 const inherited=!!sourceSnapshot;
 html('graph-provenance',!snapshot?'The execution graph appears when a plan is recorded.':inherited?'Original generation · <a href="/?build='+esc(s.build_id)+'">wb-'+esc(s.build_id)+'</a> · '+esc(s.status)+'. Qualification is separate.':data.hasManifest?'Recorded dependencies · '+data.nodes.filter(n=>n.task_id).length+' tasks':'Station evidence · task dependencies were not recorded.');
 const signature=JSON.stringify(data);
 if(signature===graphSignature)return;graphSignature=signature;
 const root=$('graph-nodes'),existing=new Map([...root.children].map(e=>[e.dataset.node,e]));
 for(const n of data.nodes){let el=existing.get(n.id);if(!el){el=document.createElement('button');el.type='button';el.dataset.node=n.id;el.addEventListener('click',()=>selectNode(n.id));root.append(el);}existing.delete(n.id);
  el.className='node '+V.tone(n.state)+(n.kind==='orchestration'?' orchestration':'')+(['bato','monunius','medaurus'].includes(n.persona.id)?' assurance':n.persona.id==='gentius'?' integration':'');el.style.left=n.x+'px';el.style.top=n.y+'px';el.setAttribute('aria-pressed',String(selection===n.id));el.setAttribute('aria-label',n.persona.name+', '+n.persona.role+', '+n.title+', '+n.state);el.title=n.persona.role+' · '+n.title;
  const content=mark(n.persona)+'<span class="node-copy"><span class="node-name">'+esc(n.persona.name)+'</span><span class="node-task">'+esc(n.title)+'</span>'+badge(n.state)+'</span>';
  if(el._rendered!==content){el.innerHTML=content;el._rendered=content;}
 }
 for(const el of existing.values())el.remove();
 const edges=data.edges.map(e=>{const x=e.from.x+105,y=e.from.y+data.nodeHeight,tx=e.to.x+105,ty=e.to.y,mid=(y+ty)/2;return '<path class="graph-edge'+(e.active?' active':'')+'" d="M '+x+' '+y+' V '+mid+' H '+tx+' V '+ty+'"/>';}).join('');
 html('graph-edges',edges);$('graph-edges').setAttribute('width',data.width);$('graph-edges').setAttribute('height',data.height);
 html('task-list',data.nodes.map(n=>'<div class="task-row"><button data-task="'+esc(n.id)+'">'+esc(n.persona.name)+' '+badge(n.state)+'</button><p>'+esc(n.persona.role)+'</p><p>'+esc(n.title)+'</p><p>Dependencies: '+esc(n.dependencies.map(x=>data.nodes.find(v=>v.id===x)?.persona.name||x).join(', ')||'None recorded')+'</p></div>').join(''));
 $('task-list').querySelectorAll('button').forEach(b=>b.onclick=()=>selectNode(b.dataset.task));
 $('graph-empty').hidden=!!snapshot;$('graph-viewport').classList.toggle('empty',!snapshot);
 const layout=JSON.stringify(data.nodes.map(n=>[n.id,n.x,n.y]));if(layout!==layoutSignature){layoutSignature=layout;fitGraph();}
}
function fitGraph(){if(!graphData)return;const viewport=$('graph-viewport'),width=viewport.clientWidth;if(!width)return;const minimum=matchMedia('(max-width:600px)').matches?.65:.85;zoom=Math.max(minimum,Math.min(1,(width-20)/graphData.width,(viewport.clientHeight-6)/graphData.height));applyZoom();viewport.scrollLeft=Math.max(0,(viewport.scrollWidth-width)/2);viewport.scrollTop=0;}
function applyZoom(){if(!graphData)return;const stage=$('graph-stage');stage.style.width=graphData.width*zoom+'px';stage.style.height=graphData.height*zoom+'px';for(const id of ['graph-nodes','graph-edges']){$(id).style.transform='scale('+zoom+')';$(id).style.transformOrigin='top left';}text('fit-graph','Fit');}
function selectNode(id){selection=selection===id?null:id;document.querySelectorAll('.node').forEach(n=>n.setAttribute('aria-pressed',String(n.dataset.node===selection)));renderContext();selectTab('now');if(matchMedia('(max-width:600px)').matches)$('context-title').scrollIntoView({block:'start',behavior:'instant'});}
function renderContext(){
 if(!snapshot)return;const s=snapshot,crew=s.virtutes?.crew||[],chosen=graphData?.nodes.find(n=>n.id===selection),allWorking=crew.filter(c=>V.active(c.state)),working=allWorking.some(c=>c.persona.id!=='zojz')?allWorking.filter(c=>c.persona.id!=='zojz'):allWorking;
 let content='';
 if(chosen){const deps=chosen.dependencies.map(id=>graphData.nodes.find(n=>n.id===id)).filter(Boolean),next=graphData.nodes.filter(n=>n.dependencies.includes(chosen.id));content='<p class="eyebrow">'+(sourceSnapshot?'ORIGINAL GENERATION / ':'')+'SELECTED TASK</p><h3>'+esc(chosen.persona.name)+'</h3>'+badge(chosen.state)+'<p class="subtle">'+esc(chosen.persona.role)+'</p><p class="subtle">'+esc(chosen.purpose||chosen.title)+'</p><p class="next">Depends on: '+esc(deps.map(n=>n.persona.name+' · '+n.state).join(', ')||'None recorded')+'<br>Next: '+esc(next.map(n=>n.persona.name).join(', ')||'No dependent task recorded')+'</p>'+(chosen.model?'<p class="field-hint">Model · '+esc(chosen.model)+'</p>':'');}
 else if(working.length){content='<p class="eyebrow">CURRENT ACTIVITY</p>'+working.map(c=>'<h3>'+esc(c.persona.name)+'</h3>'+badge(c.state)+'<p class="subtle">'+esc(graphData?.nodes.find(n=>n.persona.id===c.persona.id&&V.active(n.state))?.purpose||c.current_task||c.persona.role)+'</p>').join('');const next=graphData?.nodes.filter(n=>['WAITING','NOT_RUN'].includes(n.state)&&n.dependencies.length).slice(0,2)||[];content+='<p class="next">Next in plan: '+esc(next.map(n=>n.persona.name+' · '+n.title).join('; ')||'Awaiting the next recorded stage')+'</p>';}
 else if(s.status==='VERIFIED'){content='<p class="eyebrow">RUN COMPLETE</p><h3>Ready for the owner</h3><p class="subtle">'+esc(s.can_open_app?'The qualified package is available through OPEN APP.':s.verdict||s.status)+'</p>'+(sourceSnapshot?'<p class="next">This qualification reused retained bytes. The graph shows the original generation; no worker generation is implied here.</p>':'');}
 else if(['FAILED','BLOCKED','OWNER_ACTION_REQUIRED'].includes(s.status)){const f=V.failure(s);content='<p class="eyebrow">'+esc(s.status)+'</p><h3>'+esc(f.stage.replaceAll('_',' '))+'</h3><p class="subtle">'+esc(f.reason.slice(0,240))+'</p><p class="next">'+esc(s.owner_action?.decision_needed||'No automatic retry. Review the retained evidence.')+'</p>';}
 else content='<p class="eyebrow">CURRENT ACTIVITY</p><h3>Waiting for evidence</h3><p class="subtle">No worker activity is recorded yet.</p>';
 html('current-task',content);
}
function renderEvents(s){const events=s.presentation?.events||(s.live_events||[]).map(event=>({event}));text('event-count',events.length+' recorded');const meaningful=events.filter(e=>!String(e.event).startsWith('artifact_retained:'));html('events',meaningful.length?meaningful.slice(-6).reverse().map(e=>'<li class="event-item">'+(e.ts?'<time datetime="'+new Date(e.ts*1000).toISOString()+'">'+esc(new Date(e.ts*1000).toLocaleTimeString())+'</time>':'')+esc(V.eventText(e))+'</li>').join(''):'<li class="subtle">No events recorded.</li>');html('activity-events',meaningful.map(e=>'<li class="event-item">'+esc(V.eventText(e))+'</li>').join('')||'<li>No events recorded.</li>');text('logs',events.map(e=>(e.ts?new Date(e.ts*1000).toISOString()+' ':'')+e.event+(e.detail?' — '+e.detail:'')).join('\n')||'No recorded events.');}
function renderResult(s,ms){
 const gateNames={structured_output_schema:'Schema',scope_security:'Scope',public_verification:'Public',private_verification:'Private',integration:'Integration',reviewer:'Review'};
 const recordedGates=Object.entries(s.gate_states||{});
 $('factory-checks').hidden=!recordedGates.length;
 html('factory-checks','<span class="eyebrow">Selected run · final checks</span><div class="check-stages">'+recordedGates.map(([name,state])=>'<span class="check-stage '+V.tone(state)+'" title="'+esc(name+' · '+state)+'"><i aria-hidden="true">'+(V.state(state)==='VERIFIED'?'✓':V.state(state)==='FAILED'?'×':'○')+'</i><span>'+esc(gateNames[name]||name.replaceAll('_',' '))+'</span><span class="sr-only">'+esc(state)+'</span></span>').join('')+'</div>');
 $('result').hidden=!terminal(s);if(!terminal(s)){$('owner-decision-panel').hidden=true;return;}
 const ready=s.status==='VERIFIED',blocked=['BLOCKED','OWNER_ACTION_REQUIRED'].includes(s.status)||s.status==='OWNER_ACTION_REQUIRED';$('result').className='panel result-panel '+(ready?'verified':blocked?'blocked':'failed');text('verdict',V.verdict(s));text('result-icon',ready?'✓':blocked?'!':'×');text('result-kicker',ready?'VERIFIED DELIVERY':blocked?'EXECUTION BLOCKED':'RETAINED FAILURE');
 const f=V.failure(s);text('result-summary',ready?s.execution_mode==='retained_qualification_offline'?'Retained product qualified offline. The original generation and its verdict remain preserved.':s.can_open_app?'Your verified product is packaged and ready to launch.':s.detail||'The factory recorded a verified result.':f.stage.replaceAll('_',' ')+' · '+f.reason.slice(0,300)+(f.reason.length>300?'…':''));
 $('launch-actions').hidden=!s.can_open_app;$('launch').hidden=!s.can_open_app;text('app-path',s.project_path);text('launch-command',s.launch_command);text('app-url',s.local_url||'Launch locally when you are ready.');$('stop-app').disabled=!s.local_url;
 html('result-instruments',ms.filter(([label])=>['Calls','Cost','Elapsed'].includes(label)).map(instrument).join(''));
 const gates=Object.entries(s.gate_states||{});if(s.packaging)gates.push(['packaging / launch / persistence',s.packaging.status]);if(s.product_review)gates.push(['product_review',s.product_review.verdict]);
 html('gate-results',gates.map(([k,v])=>'<div class="gate-row"><span>'+esc(k.replaceAll('_',' '))+'</span>'+badge(v)+'</div>').join('')||'<p class="subtle">No final gate results recorded.</p>');
 html('failure-details',ready?'':'<h3>Retained reason · '+esc(f.stage)+'</h3><pre>'+esc(f.reason)+'</pre>');text('participating-personas','Recorded participants: '+(s.virtutes?.crew||[]).filter(c=>!['WAITING','NOT_RUN'].includes(c.state)).map(c=>c.persona.name).join(', '));
 renderOwnerDecision(s);
}
function renderOwnerDecision(s){
 const context=s.owner_decision,shown=s.status==='OWNER_ACTION_REQUIRED'&&context;
 $('owner-decision-panel').hidden=!shown;if(!shown)return;
 const unresolved=s.product_review?.unresolved||[];
 const reason=unresolved.length&&s.owner_action?.reason==='MANUAL_APPROVAL_REQUIRED'
  ?'Product review left '+unresolved.length+' required checks unresolved: '+unresolved.map(r=>r.requirement_id).join(', ')+'. The exact retained exception is available in Details.'
  :(s.owner_action?.cause||s.detail||'');
 text('owner-decision-reason',reason+' '+(s.owner_action?.decision_needed||''));
 const budget=context.budget_before_resume||{};
 const decision=context.decision,choices=context.choices||[];
 const recovery=choices.includes('AUTHORIZE_RECOVERY_CONTINUATION')||decision?.decision_type==='AUTHORIZE_RECOVERY_CONTINUATION';
 text('owner-decision-context',recovery?'Same logical run wb-'+s.build_id+' will re-enter ProductSpec generation under the repaired visual placement contract. Previous '+budget.calls+' calls and $'+budget.spent_usd+' remain counted against $'+budget.ceiling_usd+'. The OpenAI visual worker and independent reviewer remain within the recorded $3.00 visual and $5.00 product ceilings, with two required assets and at most three generation calls. This decision does not approve the product. Evidence SHA-256: '+context.evidence_sha256:'Evidence SHA-256: '+context.evidence_sha256+'; prior spend $'+budget.spent_usd+' remains counted against $'+budget.ceiling_usd+'. Previous evidence stays preserved. This continues wb-'+s.build_id+'.');
 const choicesHtml=choices.map((choice,i)=>'<label><input type="radio" name="owner-choice" value="'+esc(choice)+'"'+(i===0?' checked':'')+'> '+esc(choice==='AUTHORIZE_RECOVERY_CONTINUATION'?'AUTHORIZE RECOVERY CONTINUATION - resume the same ZANA smoke run; this does not approve the product':choice==='AUTHORIZE_EVALUATOR_REPAIR'?'AUTHORIZE EVALUATOR REPAIR - run real checks; this does not approve the product':choice==='RESOLVE_SPEC_DECISION'?'RESOLVE SPEC DECISION - use the answers below for a new ProductSpec':'Unsupported decision')+'</label>').join('');
 const questions=choices.includes('RESOLVE_SPEC_DECISION')?(context.uncertainties||[]).map((q,i)=>'<label for="owner-resolution-'+i+'">'+esc(q)+'</label><textarea id="owner-resolution-'+i+'" data-question="'+esc(q)+'" rows="2" required></textarea>').join(''):'';
 html('owner-decision-fields',decision?'<p>Recorded decision: '+esc(decision.decision_type)+'</p>':choicesHtml+questions);
 $('owner-decision-note').hidden=!!decision;$('submit-owner-decision').hidden=!!decision||!choices.length;
 $('resume-run').hidden=!decision||!!context.segments.find(x=>x.segment_id===(context.continuation_segment_id||'0001')&&x.status!=='DECISION_RECORDED');
 $('submit-owner-decision').disabled=ownerBusy;$('resume-run').disabled=ownerBusy;
 html('owner-segment-history',(context.segments||[]).map(x=>'<li>Segment '+esc(x.segment_id)+' · '+esc(x.status)+' · '+esc(x.evidence_sha256||'execution pending')+'</li>').join(''));
}
async function submitOwnerDecision(){
 if(ownerBusy||!buildId||!snapshot?.owner_decision)return;const context=snapshot.owner_decision;
 const choice=document.querySelector('input[name="owner-choice"]:checked')?.value;if(!choice)return;
 const resolutions={};for(const el of $('owner-decision-fields').querySelectorAll('textarea[data-question]')){if(!el.value.trim()){el.focus();text('owner-decision-status','Answer every recorded specification uncertainty.');return;}resolutions[el.dataset.question]=el.value.trim();}
 ownerBusy=true;$('submit-owner-decision').disabled=true;text('owner-decision-status','Recording the owner decision…');
 try{await j('/api/builds/'+buildId+'/decisions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision_type:choice,blocker_id:context.blocker_id,evidence_sha256:context.evidence_sha256,owner_text:$('owner-decision-note').value,resolutions,visual_authorization:choice==='AUTHORIZE_RECOVERY_CONTINUATION'?context.visual_authorization:null})});text('owner-decision-status','Decision persisted. Resume this same run when ready.');await poll();}
 catch(e){text('owner-decision-status',e.message);}finally{ownerBusy=false;if(snapshot)renderOwnerDecision(snapshot);}
}
async function resumeOwnerRun(){
 if(ownerBusy||!buildId)return;ownerBusy=true;$('resume-run').disabled=true;clearTimeout(timer);text('owner-decision-status','Starting the recorded continuation…');
 try{await j('/api/builds/'+buildId+'/resume',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});text('owner-decision-status','Continuation started under the same run ID.');await poll();}
 catch(e){text('owner-decision-status',e.message);await poll();}finally{ownerBusy=false;if(snapshot)renderOwnerDecision(snapshot);}
}
async function openApp(){const target=window.open('about:blank','virtutes-product');$('open-app').disabled=true;try{const r=await j('/api/builds/'+buildId+'/open',{method:'POST',headers:{'Content-Type':'application/json'}});text('app-url',r.url);text('launch-status','Running from the verified project directory.');if(target)target.location.href=r.url;else location.href=r.url;await poll();}catch(e){if(target)target.close();text('launch-status',e.message);}finally{$('open-app').disabled=false;}}
function selectTab(name){for(const tab of document.querySelectorAll('[role=tab]')){const selected=tab.id==='tab-'+name;tab.setAttribute('aria-selected',selected);tab.tabIndex=selected?0:-1;$(tab.getAttribute('aria-controls')).hidden=!selected;}if(desktop()&&name!=='now')openPanel($('pane-'+name),$('tab-'+name).textContent,$('tab-'+name));}
function openPanel(content,title,trigger){
 if($('panel-dialog').open)return;
 const anchor=document.createComment('detail surface');content.before(anchor);const wasHidden=content.hidden;
 panelRestore=()=>{anchor.replaceWith(content);content.hidden=wasHidden;if(content.tagName==='DETAILS')content.open=false;if(content.id.startsWith('pane-'))selectTab('now');trigger?.focus();};
 text('panel-dialog-title',title);$('panel-dialog-content').append(content);content.hidden=false;if(content.tagName==='DETAILS')content.open=true;$('panel-dialog').showModal();
}
function inspect(title,value,trigger){dialogTrigger=trigger;text('inspector-title',title);text('inspector-content',JSON.stringify(value,null,2));$('inspector').showModal();}
async function init(){
 bindEventHandlers();syncBackendUI();
 document.querySelectorAll('button[data-surface]').forEach(b=>b.onclick=()=>surface(b.dataset.surface));
 $('open-app').onclick=openApp;$('stop-app').onclick=async()=>{try{await j('/api/builds/'+buildId+'/stop',{method:'POST',headers:{'Content-Type':'application/json'}});text('launch-status','App stopped. Your data is saved.');await poll();}catch(e){text('launch-status',e.message);}};
 $('submit-owner-decision').onclick=submitOwnerDecision;$('resume-run').onclick=resumeOwnerRun;
 $('close-panel-dialog').onclick=()=>$('panel-dialog').close();$('panel-dialog').onclose=()=>{const restore=panelRestore;panelRestore=null;restore?.();};
 for(const [id,title] of [['build-settings','Build settings'],['delivery-details','Delivery & verification']])$(id).querySelector('summary').addEventListener('click',e=>{if(desktop()){e.preventDefault();openPanel($(id),title,e.currentTarget);}});
 $('view-build').onclick=()=>{if($('saved-build').value)loadBuild($('saved-build').value);};
 $('zoom-in').onclick=()=>{zoom=Math.min(1.5,zoom+.15);applyZoom();};$('zoom-out').onclick=()=>{zoom=Math.max(.5,zoom-.15);applyZoom();};$('fit-graph').onclick=fitGraph;
 $('graph-list').onclick=()=>{if(desktop()){openPanel($('task-list'),'Recorded tasks & dependencies',$('graph-list'));return;}const list=$('task-list').hidden;$('task-list').hidden=!list;$('graph-viewport').hidden=list;$('graph-list').setAttribute('aria-pressed',String(list));if(!list)fitGraph();};
 const tabs=[...document.querySelectorAll('[role=tab]')];tabs.forEach((t,i)=>{t.onclick=()=>selectTab(t.id.slice(4));t.onkeydown=e=>{let next;if(e.key==='ArrowRight')next=(i+1)%tabs.length;if(e.key==='ArrowLeft')next=(i+tabs.length-1)%tabs.length;if(e.key==='Home')next=0;if(e.key==='End')next=tabs.length-1;if(next!=null){e.preventDefault();selectTab(tabs[next].id.slice(4));tabs[next].focus();}};});
 $('runtime-info').onclick=()=>inspect('Workspace runtime',runtime||{status:'Not recorded'},$('runtime-info'));$('inspect-state').onclick=()=>inspect('Run evidence',snapshot||{status:'No run selected'},$('inspect-state'));$('close-inspector').onclick=()=>$('inspector').close();$('inspector').onclose=()=>dialogTrigger?.focus();
 // The desktop canvas never grows with its contents. Reflow only when its
 // available dimensions change, so polling and user zoom retain their position.
 let canvasSize='';new ResizeObserver(entries=>{const r=entries[0].contentRect,key=r.width+':'+r.height;if(key!==canvasSize){canvasSize=key;if(r.width&&r.height){renderGraph(sourceSnapshot||snapshot||{});fitGraph();}}}).observe($('graph-viewport'));
 const canvas=$('graph-viewport');let drag=null;
 canvas.addEventListener('pointerdown',e=>{if(!desktop()||e.target.closest('button')||e.button!==0)return;drag={x:e.clientX,y:e.clientY,left:canvas.scrollLeft,top:canvas.scrollTop};canvas.setPointerCapture(e.pointerId);canvas.classList.add('panning');});
 canvas.addEventListener('pointermove',e=>{if(drag){canvas.scrollLeft=drag.left+drag.x-e.clientX;canvas.scrollTop=drag.top+drag.y-e.clientY;}});
 for(const event of ['pointerup','pointercancel','lostpointercapture'])canvas.addEventListener(event,()=>{drag=null;canvas.classList.remove('panning');});
 canvas.addEventListener('keydown',e=>{if(!desktop()||e.target!==canvas)return;const delta={ArrowLeft:[-50,0],ArrowRight:[50,0],ArrowUp:[0,-50],ArrowDown:[0,50]}[e.key];if(delta){e.preventDefault();canvas.scrollBy(...delta);}});
 try{const b=await j('/api/backends');hydrateBackendOptions(b.backends);syncBackendUI();}catch(e){/* Server-rendered options remain authoritative. */}
 try{runtime=await j('/api/server');$('runtime-info').title=runtime.normal_app_runtime?.fresh?'Runtime source is current':'Runtime source has changed; restart before building';}catch(e){$('runtime-info').title='Runtime identity unavailable';}
 try{const saved=await j('/api/builds');for(const id of saved.builds||[]){if(!/^[a-f0-9]{12}$/.test(id))continue;const o=document.createElement('option');o.value=id;o.textContent='wb-'+id;$('saved-build').append(o);}}catch(e){notice(e.message);}
 const id=new URLSearchParams(location.search).get('build');if(id){await loadBuild(id);}else{renderGraph({});}
}
init();
