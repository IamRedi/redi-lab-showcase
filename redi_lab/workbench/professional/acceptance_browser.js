// Runs only inside an isolated acceptance host. No provider or external network.
(async()=>{
const spec=SPEC, journey=JOURNEY, verifyVisuals=VERIFY_VISUALS;
const report={version:'browser_product_quality_v2',checks:[],assertions:[],failures:[]};
const check=(id,observed,expected,pass)=>{
 const row={assertion_id:id,status:pass?'PASS':'FAIL',evidence_type:'real_browser_observation',evidence_location:'report.json',observed:observed??null,expected,failure_reason:pass?'':'Observed browser behavior differs from the declared requirement'};
 report.assertions.push(row);if(!pass)report.failures.push(row);
};
const state=async()=>await(await fetch('/api/state')).json();
const frame=document.createElement('iframe');frame.title='Isolated product acceptance';frame.style.cssText='display:block;border:0;width:1280px;height:844px';document.body.appendChild(frame);
const load=path=>new Promise((resolve,reject)=>{frame.onload=resolve;frame.onerror=reject;frame.src=path;});
const pause=()=>new Promise(r=>setTimeout(r,10));
const wait=async fn=>{for(let n=0;n<500;n++){if(fn())return;await pause();}throw Error('Browser observation timed out');};
const d=()=>frame.contentDocument,w=()=>frame.contentWindow;
const visible=el=>{if(el.closest('details:not([open])')&&!el.matches('summary')&&!el.closest('summary'))return false;const r=el.getBoundingClientRect(),s=w().getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none'};
const field=(form,name)=>form.elements.namedItem(name);
const fill=(form,values)=>{for(const [name,value] of Object.entries(values)){const el=field(form,name);if(!el)throw Error('Missing form field '+name);el.value=value??'';el.dispatchEvent(new (w().Event)('input',{bubbles:true}));}};
const submit=async(form,expectError=false)=>{
 let loaded=false;frame.onload=()=>loaded=true;
 form.querySelector('button[type=submit]').click();
 check('state:loading',{busy:form.getAttribute('aria-busy'),text:form.querySelector('.loading')?.textContent},'busy form and saving feedback',form.getAttribute('aria-busy')==='true'&&!!form.querySelector('[role=status]'));
 await wait(()=>loaded||!!d().querySelector('[role=alert]'));
 if(expectError){const alert=d().querySelector('[role=alert]');check('state:validation_error',alert?.textContent,'accessible user-facing validation',!!alert&&!/Traceback|KeyError|sqlite3\./.test(alert.textContent));}
 else check('state:success',d().querySelector('[role=status]')?.textContent,'visible successful submission',loaded&&!!d().querySelector('[role=status]'));
};
const viewFor=name=>spec.views.find(v=>v.entity===name).id;
// Independent exact arithmetic for monetary browser assertions and edit inputs.
const minor=value=>{const text=String(value),negative=text.startsWith('-'),parts=text.replace(/^-/,'').split('.');return (negative?-1n:1n)*(BigInt(parts[0])*100n+BigInt((parts[1]||'').padEnd(2,'0')));};
const money=cents=>{const magnitude=cents<0n?-cents:cents;return (cents<0n?'-':'')+String(magnitude/100n)+'.'+String(magnitude%100n).padStart(2,'0');};
const valuesFor=(ent,ids,suffix='')=>Object.fromEntries(ent.fields.map(f=>{
 let val=f.type==='reference'?ids[f.references]:f.sample;
 if(suffix&&f.type==='email')val='qa'+suffix+'@example.test';
 else if(suffix&&(f.unique||f.id===ent.display_field)&&['text','long_text'].includes(f.type))val=String(val)+' '+suffix;
 const flow=spec.workflows.find(x=>x.entity===ent.id&&x.field===f.id);if(flow)val=flow.initial;
 return [f.id,val];
}));
const criterion=pattern=>spec.acceptance_criteria.some(text=>pattern.test(text.toLowerCase()));
// A prohibition ("cannot move backward") is not a requirement to move back.
const NEGATED_REVERSE=/(cannot|can't|can not|must not|may not|never|not allowed to|no)\s+(be\s+)?(move[ds]?|go(es)?|return(s|ed)?)\s*(back|backward)/;
const NEGATED_REVERSE_SHORT=/\b(not|never)\s+(back|backward)\b/;
// The structured workflow wins: a reverse obligation needs declared reverse transitions.
const hasReverseTransition=flow=>{const order=[flow.initial,...Object.keys(flow.transitions)];return Object.entries(flow.transitions).some(([from,targets])=>targets.some(to=>order.indexOf(to)>-1&&order.indexOf(to)<order.indexOf(from)));};
const reverseRequired=flow=>hasReverseTransition(flow)&&spec.acceptance_criteria.some(text=>{const lower=text.toLowerCase();return /back to.*earlier|earlier stage|move.*back/.test(lower)&&!NEGATED_REVERSE.test(lower)&&!NEGATED_REVERSE_SHORT.test(lower);});
const collectionIds=()=>[...d().querySelectorAll('.record-card a[href*="id="],tbody a[href*="id="]')].map(el=>Number(new URL(el.href).searchParams.get('id'))).sort((a,b)=>a-b);
const metricExpected=(metric,rows)=>{const selected=rows.filter(row=>!metric.where||metric.where.values.includes(row[metric.where.field]));if(metric.operation==='count')return String(selected.length);const field=spec.entities.find(ent=>ent.id===metric.entity).fields.find(item=>item.id===metric.field);if(field.type==='money')return money(selected.reduce((sum,row)=>sum+(row[metric.field]==null?0n:minor(row[metric.field])),0n));return String(selected.reduce((sum,row)=>sum+(row[metric.field]??0),0));};
const shownMetrics=()=>Object.fromEntries(spec.metrics.map(metric=>[metric.id,d().querySelector('[data-metric="'+metric.id+'"]')?.textContent?.trim()]));
const params=(view,extras={})=>'/?'+new URLSearchParams({view,...extras});
let browserJourneySteps=0, deleteConfirmations=[];
try{
 await load('/?view='+spec.navigation[0]);
 if(journey){
  const initial=await state();check('state:empty',initial.records,'empty acceptance database',Object.values(initial.records).every(rows=>rows.length===0)&&(!spec.entities.length||!!d().querySelector('.empty')));
  const ids={};let pending=[...spec.entities];
  while(pending.length){
   const ready=pending.filter(e=>e.fields.filter(f=>f.type==='reference').every(f=>ids[f.references]));if(!ready.length)throw Error('Unresolvable relationships');
   for(const ent of ready){
    await load('/?view='+viewFor(ent.id));let form=d().querySelector('form');const values=valuesFor(ent,ids);
    fill(form,values);await submit(form);
    browserJourneySteps++;
    const rows=(await state()).records[ent.id];const row=rows[rows.length-1];ids[ent.id]=row.id;
    check('create:'+ent.id,row,values,Object.entries(values).every(([k,v])=>String(row[k])===String(v)));
    check('read:'+ent.id,d().querySelector('h1')?.textContent,String(row[ent.display_field]),d().querySelector('h1')?.textContent===String(row[ent.display_field]));
    if(ent.fields.some(f=>f.type==='reference'))check('relationships:'+ent.id,row,'declared parent IDs',ent.fields.filter(f=>f.type==='reference').every(f=>row[f.id]===ids[f.references]));
    const edit=ent.fields.find(f=>['text','long_text','email'].includes(f.type)&&!f.append)||ent.fields.find(f=>['money','integer','date','boolean'].includes(f.type));
    if(edit){
     form=d().querySelector('form');let value=row[edit.id];
     if(edit.type==='email')value='edited'+ent.id+'@example.test';
     else if(['text','long_text'].includes(edit.type))value=String(value)+' updated';
     else if(edit.type==='boolean')value=!value;
     else if(edit.type==='date')value=String(value).slice(0,8)+(String(value).endsWith('01')?'02':'01');
     else if(edit.type==='integer')value=Number(value)+(edit.max!=null&&Number(value)>=edit.max?-1:1);
     else {const cents=minor(value),step=edit.max!=null&&cents>=minor(edit.max)?-1n:1n;value=money(cents+step);}
     fill(form,{[edit.id]:value});await submit(form);const actual=(await state()).records[ent.id].find(x=>x.id===row.id)[edit.id];check('edit:'+ent.id,actual,value,String(actual)===String(value));
    }else{
     // Enum/reference-only records can still be saved; workflow changes are checked separately.
     form=d().querySelector('form');await submit(form);check('edit:'+ent.id,(await state()).records[ent.id].find(x=>x.id===row.id),row,!!(await state()).records[ent.id].find(x=>x.id===row.id));
    }
    const required=ent.fields.find(f=>f.required&&['text','long_text','email'].includes(f.type));
    if(required){form=d().querySelector('form');fill(form,{[required.id]:required.type==='email'?' ': '   '});form.noValidate=true;const before=await state();await submit(form,true);const unchanged=JSON.stringify(await state())===JSON.stringify(before);check('validation:'+ent.id,await state(),before,unchanged);if(criterion(/blank|whitespace/))check('required_text_validation',{entity:ent.id,field:required.id,alert:d().querySelector('[role=alert]')?.textContent,unchanged},'clear validation message and unchanged record',unchanged&&!!d().querySelector('[role=alert]'));await load('/?view='+viewFor(ent.id)+'&id='+row.id);}
    else{
     const requiredField=ent.fields.find(f=>f.required);if(requiredField){form=d().querySelector('form');fill(form,{[requiredField.id]:''});form.noValidate=true;const before=await state();await submit(form,true);check('validation:'+ent.id,await state(),before,JSON.stringify(await state())===JSON.stringify(before));}
    }
    const bounded=ent.fields.find(f=>['text','long_text'].includes(f.type)&&Number.isInteger(f.max)&&f.max>0);
    if(bounded&&criterion(/over.limit|too long|maximum length|truncat/)){
     await load('/?view='+viewFor(ent.id)+'&id='+row.id);form=d().querySelector('form');fill(form,{[bounded.id]:'x'.repeat(bounded.max+1)});form.noValidate=true;const before=await state();await submit(form,true);check('text_length_validation',{entity:ent.id,field:bounded.id,alert:d().querySelector('[role=alert]')?.textContent,unchanged:JSON.stringify(await state())===JSON.stringify(before)},'over-limit value rejected with message, no truncation',!!d().querySelector('[role=alert]')&&JSON.stringify(await state())===JSON.stringify(before));
    }
    const optionalDate=ent.fields.find(f=>f.type==='date'&&!f.required);
    if(optionalDate&&criterion(/date/)){
     await load('/?view='+viewFor(ent.id)+'&id='+row.id);form=d().querySelector('form');fill(form,{[optionalDate.id]:''});await submit(form);const actual=(await state()).records[ent.id].find(x=>x.id===row.id)?.[optionalDate.id];check('optional_date',{entity:ent.id,field:optionalDate.id,stored:actual},'empty date accepted as null or empty',actual==null||actual==='');
    }
    pending=pending.filter(x=>x!==ent);
   }
  }
   for(const flow of spec.workflows){
    const rid=ids[flow.entity],declared=spec.entities.find(item=>item.id===flow.entity),status=declared.fields.find(item=>item.id===flow.field);
    let current=flow.initial,observed=[];
    const pathTo=target=>{const queue=[[current]];const visited=new Set();while(queue.length){const path=queue.shift(),last=path.at(-1);if(last===target)return path;if(visited.has(last))continue;visited.add(last);for(const next of flow.transitions[last]||[])queue.push([...path,next]);}return null;};
    const move=async target=>{await load('/?view='+viewFor(flow.entity)+'&id='+rid);const form=d().querySelector('form');const values={[flow.field]:target};
     for(const rule of spec.business_rules.filter(item=>item.kind==='required_when'&&item.entity===flow.entity&&item.when_value===target))values[rule.field]=declared.fields.find(item=>item.id===rule.field).sample;
     fill(form,values);await submit(form);const row=(await state()).records[flow.entity].find(item=>item.id===rid);observed.push(row[flow.field]);if(row[flow.field]!==target)throw Error('Transition failed');current=target;};
    const unreachable=[];
    for(const target of status.choices){const path=pathTo(target);if(!path){unreachable.push(target);continue;}for(const step of path.slice(1))await move(step);}
    const reverse=pathTo(flow.initial);if(reverse)for(const step of reverse.slice(1))await move(step);
    check('workflow:'+flow.entity,{observed,unreachable,current},'all declared reachable stages and visible selected status',observed.length>0&&!unreachable.length&&field(d().querySelector('form'),flow.field).value===current);
    if(reverseRequired(flow))check('workflow_reverse',{observed,current},'a later stage returns to the initial stage through declared transitions',!!reverse&&reverse.length>1&&current===flow.initial);
   }
  const populated=await state();check('state:populated',populated,'persisted records visible',Object.values(populated.records).every(rows=>rows.length>0));
  for(const view of spec.views.filter(item=>item.kind==='board')){
   const flow=spec.workflows.find(item=>item.entity===view.entity);
   if(!flow)continue;
   await load(params(view.id));const field=spec.entities.find(item=>item.id===view.entity).fields.find(item=>item.id===flow.field);
   const observed=field.choices.map(choice=>({choice,count:Number(d().querySelector('[data-stage-count="'+choice+'"]')?.textContent),present:!!d().querySelector('[data-stage="'+choice+'"]')}));
   check('board_columns',observed,'one visible column per workflow stage with exact record counts',observed.every(item=>item.present&&item.count===populated.records[view.entity].filter(row=>row[flow.field]===item.choice).length));
  }
  if(spec.metrics.length){
   const metricViews=[];for(const view of spec.views){await load('/?view='+view.id);if(spec.metrics.every(m=>d().querySelector('[data-metric="'+m.id+'"]')))metricViews.push(view);}
   if(!metricViews.length)check('dashboard',{metric_ids:spec.metrics.map(m=>m.id),views:spec.views.map(v=>v.id)},'declared metrics visibly rendered on a reachable view',false);
   for(const view of metricViews){await load('/?view='+view.id);for(const metric of spec.metrics){const rows=populated.records[metric.entity];const selected=rows.filter(r=>!metric.where||metric.where.values.includes(r[metric.where.field]));let expected=selected.length;if(metric.operation==='sum'){const field=spec.entities.find(e=>e.id===metric.entity).fields.find(f=>f.id===metric.field);if(field.type==='money'){expected=money(selected.reduce((sum,r)=>sum+(r[metric.field]==null?0n:minor(r[metric.field])),0n));}else expected=selected.reduce((sum,r)=>sum+(r[metric.field]??0),0);}check('dashboard',{view:view.id,id:metric.id,rendered:d().querySelector('[data-metric="'+metric.id+'"]')?.textContent,stored:populated.metrics[metric.id]},expected,String(d().querySelector('[data-metric="'+metric.id+'"]')?.textContent)===String(expected)&&populated.metrics[metric.id]===expected);}
   }
   if(criterion(/filter.*(summar|metric|count|value)|(summar|metric|count|value).*filter/)){
    const view=metricViews[0];if(view)await load('/?view='+view.id);
    const search=d().querySelector('input[type=search],input[name=search],input[name=q]');
    if(!search)check('filtered_metrics',{view:view?.id,search_control:false},'filter controls update metric values from matching records',false);
    else{search.value='redi-no-result-'+Date.now();search.dispatchEvent(new (w().Event)('input',{bubbles:true}));if(search.form?.method==='get'){let loaded=false;frame.onload=()=>loaded=true;search.form.requestSubmit();await wait(()=>loaded);}else await pause();const observed=spec.metrics.map(m=>({id:m.id,value:d().querySelector('[data-metric="'+m.id+'"]')?.textContent?.trim()}));check('filtered_metrics',observed,'zero metrics for a filter matching no records',observed.every(x=>x.value==='0'||x.value==='0.00'));}
   }
  }
  if(criterion(/empty search|no (search )?results?|nothing matches/)){
   const view=spec.views.find(v=>v.id===spec.navigation[0]&&v.entity)||spec.views.find(v=>v.entity);
   await load('/?view='+view.id);const search=d().querySelector('input[type=search],input[name=search],input[name=q]');
   if(!search)check('empty_search_state',{view:view.id,search_control:false},'search control and empty-result state',false);
   else{search.value='redi-no-result-'+Date.now();search.dispatchEvent(new (w().Event)('input',{bubbles:true}));const searchForm=search.form;if(searchForm){if(searchForm.method==='get'){let loaded=false;frame.onload=()=>loaded=true;searchForm.requestSubmit();await wait(()=>loaded);}else await submit(searchForm);}else await pause();check('empty_search_state',{view:view.id,text:d().body.innerText,empty:!!d().querySelector('.empty')},'visible empty search-result state',!!d().querySelector('.empty')&&/no |empty|nothing|match/i.test(d().querySelector('.empty').innerText));}
  }
  // Deletion is exercised on a disposable second graph, preserving the main
  // graph for reload/restart. Confirmation is observed in this QA frame only.
  const extra={};pending=[...spec.entities];
  while(pending.length){for(const ent of [...pending]){
   if(!ent.fields.filter(f=>f.type==='reference').every(f=>extra[f.references]))continue;
   await load('/?view='+viewFor(ent.id));const details=d().querySelector('details');if(details)details.open=true;
   const form=d().querySelector('form');fill(form,valuesFor(ent,extra,'disposable'));await submit(form);
   const rows=(await state()).records[ent.id];extra[ent.id]=Number(new URL(frame.contentWindow.location.href).searchParams.get('id'));pending=pending.filter(x=>x!==ent);
  }}
  for(const view of spec.views.filter(item=>item.entity)){
   const ent=spec.entities.find(item=>item.id===view.entity),data=await state();
   const main=data.records[ent.id].find(row=>row.id===ids[ent.id]),second=data.records[ent.id].find(row=>row.id===extra[ent.id]);
   if(['board','gallery','feed'].includes(view.kind)){
    await load(params(view.id));const card=[...d().querySelectorAll('.record-card')].find(item=>new URL(item.querySelector('a').href).searchParams.get('id')===String(second.id));
    const fields=ent.fields.filter(item=>item.type!=='long_text'&&second[item.id]!=null&&second[item.id]!=='');
    const visibleFields=!!card&&fields.every(item=>item.type==='reference'?card.textContent.includes(String(data.records[item.references].find(row=>row.id===second[item.id])?.[spec.entities.find(parent=>parent.id===item.references).display_field])):card.textContent.includes(String(second[item.id])));
    check('board_card_fields',{view:view.id,fields:fields.map(item=>item.id),card:card?.innerText},'card shows its declared populated summary fields',visibleFields);
    await load(params(view.id,{id:second.id}));const panel=d().querySelector('[role=dialog][data-detail-panel]');
    check('detail_panel',{view:view.id,present:!!panel,close:!!panel?.querySelector('a.back')},'card opens accessible detail panel',!!panel&&!!panel.querySelector('form')&&!!panel.querySelector('a.back'));
    const editable=ent.fields.find(item=>['text','long_text','email'].includes(item.type));
    if(panel&&editable){const before=JSON.stringify(await state());fill(panel.querySelector('form'),{[editable.id]:String(second[editable.id]||'')+' unsaved'});let loaded=false;frame.onload=()=>loaded=true;panel.querySelector('a.back').click();await wait(()=>loaded);
     check('cancel_edit',{view:view.id,unchanged:JSON.stringify(await state())===before},'closing without save preserves data',JSON.stringify(await state())===before);
     await load(params(view.id,{id:second.id}));loaded=false;frame.onload=()=>loaded=true;d().dispatchEvent(new (w().KeyboardEvent)('keydown',{key:'Escape',bubbles:true}));await wait(()=>loaded);
     check('detail_escape',{view:view.id,panel:!!d().querySelector('[data-detail-panel]')},'Escape closes detail panel',!d().querySelector('[data-detail-panel]'));
    }
   }
   const display=ent.fields.find(item=>item.id===ent.display_field);
   if(['text','long_text','email'].includes(display.type)){
    await load(params(view.id,{search:String(second[display.id])}));
    check('search',{view:view.id,ids:collectionIds()},'declared display-field search selects the matching record',JSON.stringify(collectionIds())===JSON.stringify([second.id]));
   }
   for(const field of ent.fields.filter(item=>item.type==='reference'&&main[item.id]!==second[item.id])){
    await load(params(view.id,{['filter_'+field.id]:String(second[field.id])}));
    check('filter_reference',{view:view.id,field:field.id,ids:collectionIds()},'declared reference filter selects linked records',collectionIds().includes(second.id)&&!collectionIds().includes(main.id));
   }
   await load(params(view.id));
   check('clear_filters',{view:view.id,ids:collectionIds()},'reset restores the full collection',collectionIds().includes(main.id)&&collectionIds().includes(second.id));
  }
  const board=spec.views.find(item=>item.kind==='board'&&item.entity&&spec.workflows.some(flow=>flow.entity===item.entity));
  if(board){
   const ent=spec.entities.find(item=>item.id===board.entity),flow=spec.workflows.find(item=>item.entity===ent.id);
   const next=flow.transitions[flow.initial]?.[0];
   if(next){await load(params(board.id,{id:extra[ent.id]}));fill(d().querySelector('form'),{[flow.field]:next});await submit(d().querySelector('form'));}
   const data=await state(),main=data.records[ent.id].find(row=>row.id===ids[ent.id]),second=data.records[ent.id].find(row=>row.id===extra[ent.id]);
   await load(params(board.id));
   const card=[...d().querySelectorAll('.record-card')].find(item=>new URL(item.querySelector('a').href).searchParams.get('id')===String(second.id));
   const references=ent.fields.filter(item=>item.type==='reference');
   const cardFields=ent.fields.filter(item=>item.type!=='long_text'&&second[item.id]!=null&&second[item.id]!=='');
   const needsSwatch=references.some(item=>{const target=spec.entities.find(entity=>entity.id===item.references),linked=data.records[item.references].find(row=>row.id===second[item.id]);return target.fields.some(field=>(/colou?r/i.test(field.id))&&/^#[0-9a-f]{6}$/i.test(String(linked?.[field.id]||'')));});
   const cardValid=!!card&&cardFields.every(item=>item.type==='reference'?card.textContent.includes(String(data.records[item.references].find(row=>row.id===second[item.id])?.[spec.entities.find(entity=>entity.id===item.references).display_field])):card.textContent.includes(String(second[item.id])))&&(!needsSwatch||!!card.querySelector('.swatch'));
   check('board_card_fields',{fields:cardFields.map(item=>item.id),card:card?.innerText},'card shows declared populated summary fields',cardValid);
   await load(params(board.id,{id:second.id}));
   const panel=d().querySelector('[role=dialog][data-detail-panel]');
   check('detail_panel',{role:panel?.getAttribute('role'),modal:panel?.getAttribute('aria-modal'),close:!!panel?.querySelector('a.back'),form:!!panel?.querySelector('form')},'accessible detail panel with edit and close',!!panel&&panel.getAttribute('aria-modal')==='true'&&!!panel.querySelector('a.back')&&!!panel.querySelector('form'));
   const beforeCancel=JSON.stringify(await state()),editField=ent.fields.find(item=>item.id===ent.display_field);
   fill(d().querySelector('form'),{[editField.id]:String(second[editField.id])+' unsaved'});
   let loaded=false;frame.onload=()=>loaded=true;panel.querySelector('a.back').click();await wait(()=>loaded);
   check('cancel_edit',{unchanged:JSON.stringify(await state())===beforeCancel},'closing detail without saving preserves data',JSON.stringify(await state())===beforeCancel);
   await load(params(board.id,{id:second.id}));loaded=false;frame.onload=()=>loaded=true;d().dispatchEvent(new (w().KeyboardEvent)('keydown',{key:'Escape',bubbles:true}));await wait(()=>loaded);
   check('detail_escape',{url:frame.contentWindow.location.href,panel:!!d().querySelector('[data-detail-panel]')},'Escape closes detail panel',!d().querySelector('[data-detail-panel]'));
   const original=JSON.stringify(await state());
   const display=ent.display_field,query=String(second[display]);
   await load(params(board.id,{search:query}));
   check('search',{query,ids:collectionIds()},'display-field search returns the matching record',JSON.stringify(collectionIds())===JSON.stringify([second.id]));
   check('search_read_only',JSON.stringify(await state()),original,JSON.stringify(await state())===original);
   const reference=references[0];
   if(reference){
    const key='filter_'+reference.id;
    await load(params(board.id,{[key]:String(second[reference.id])}));
    check('filter_reference',{field:reference.id,ids:collectionIds()},'reference filter returns matching records',collectionIds().includes(second.id)&&!collectionIds().includes(main.id));
   }
   const statusField=ent.fields.find(item=>item.id===flow.field);
   await load(params(board.id,{['filter_'+flow.field]:String(second[flow.field])}));
   check('filter_status',{field:flow.field,ids:collectionIds()},'workflow status filter returns matching stage',collectionIds().includes(second.id)&&!collectionIds().includes(main.id));
   const combined={search:query,['filter_'+flow.field]:String(second[flow.field])};
   if(reference)combined['filter_'+reference.id]=String(second[reference.id]);
   await load(params(board.id,combined));
   check('combined_filters',{query:combined,ids:collectionIds()},'search and all active filters intersect',JSON.stringify(collectionIds())===JSON.stringify([second.id]));
   // Summaries render on the primary view and dashboards only (runtime presentation contract).
   if(spec.metrics.length&&board.id===spec.navigation[0]){const displayed=shownMetrics(),expected=Object.fromEntries(spec.metrics.map(metric=>[metric.id,metricExpected(metric,metric.entity===ent.id?[second]:data.records[metric.entity])]));
   check('filtered_metrics',{displayed,expected},'nonempty filtered summaries use the selected records',Object.keys(expected).every(key=>displayed[key]===expected[key]));}
   await load(params(board.id,{search:String(main[display]),['filter_'+flow.field]:String(second[flow.field])}));
   check('combined_filters',{ids:collectionIds()},'conflicting search and filter has no records',collectionIds().length===0&&!!d().querySelector('.empty'));
   await load(params(board.id));
   check('clear_filters',{ids:collectionIds()},'clearing restores both records',collectionIds().includes(main.id)&&collectionIds().includes(second.id));
   browserJourneySteps+=8;
  }
  for(const parent of spec.entities.filter(item=>spec.entities.some(child=>child.fields.some(field=>field.type==='reference'&&field.references===item.id)))){
   await load(params(viewFor(parent.id),{id:extra[parent.id]}));const form=d().querySelector('form[action$="/delete"]');form.closest('details').open=true;
   const before=JSON.stringify(await state());let offered='';w().confirm=text=>{offered=text;return true;};await submit(form,true);
   const alert=d().querySelector('[role=alert]')?.textContent||'';
   check('relationship_delete_block',{entity:parent.id,offered,alert,unchanged:JSON.stringify(await state())===before},'linked parent deletion blocked with clear guidance',!!offered&&/linked|reassign|move|delete/i.test(alert)&&JSON.stringify(await state())===before);
  }
  for(const ent of [...spec.entities].reverse()){
   await load('/?view='+viewFor(ent.id)+'&id='+extra[ent.id]);const form=d().querySelector('form[action$="/delete"]');
   const banned=spec.business_rules.find(r=>r.kind==='restrict_delete'&&r.entity===ent.id);
   if(!form){check(banned?'delete_policy:'+ent.id:'delete:'+ent.id,'absent',banned?'prohibited by ProductSpec':'delete control required',!!banned);continue;}
   form.closest('details').open=true;let message='';const before=JSON.stringify(await state());
   w().confirm=text=>{message=text;return false;};form.querySelector('button[type=submit]').click();await pause();
   const canceled=JSON.stringify(await state())===before;
   w().confirm=text=>{message=text;return true;};await submit(form,!!banned);
   deleteConfirmations.push({entity:ent.id,offered:!!message,canceled});check('state:destructive_confirmation',{message,canceled},'explicit confirmation can cancel without mutation',!!message&&canceled);
   const exists=(await state()).records[ent.id].some(r=>r.id===extra[ent.id]);check(banned?'delete_policy:'+ent.id:'delete:'+ent.id,{exists},banned?'record retained':'disposable record deleted',banned?exists:!exists);
  }
  if(criterion(/all .*deletions?.*confirm|deletions? require confirmation/))check('all_delete_confirmations',deleteConfirmations,'each declared deletable entity presents cancellable confirmation',deleteConfirmations.length===spec.entities.filter(e=>!spec.business_rules.some(r=>r.kind==='restrict_delete'&&r.entity===e.id)).length&&deleteConfirmations.every(r=>r.offered&&r.canceled));
  // Naturally applicable network failure: fail only this frame's transport for
  // one save, then restore it. Product bytes and database remain unchanged.
  const ent=spec.entities[0];
  if(ent){await load('/?view='+viewFor(ent.id)+'&id='+ids[ent.id]);const form=d().querySelector('form'),before=await state(),original=w().fetch;
   w().fetch=()=>Promise.reject(new TypeError('QA offline transport fault'));form.querySelector('button').click();await wait(()=>!!d().querySelector('[role=alert]'));w().fetch=original;
   check('state:backend_error',{message:d().querySelector('[role=alert]')?.textContent,fault:'isolated transport unavailable'},'friendly error; preserved data',!!d().querySelector('[role=alert]')&&JSON.stringify(before)===JSON.stringify(await state()));
  }
  if(spec.calculator){await load('/?view='+spec.navigation[0]);const form=d().querySelector('form');fill(form,Object.fromEntries(spec.calculator.fields.map(f=>[f.id,f.sample])));await submit(form);check('calculator',d().querySelector('[role=status]')?.textContent,spec.calculator.result_label,d().querySelector('[role=status]')?.textContent.includes(spec.calculator.result_label));}
  if(board){const declared=spec.entities.find(item=>item.id===board.entity),field=declared.fields.find(item=>item.id===declared.display_field);
   if(['text','long_text'].includes(field.type)){
    const title='W'.repeat(Math.min(field.max||180,180));await load(params(board.id,{id:ids[declared.id]}));
    fill(d().querySelector('form'),{[field.id]:title});await submit(d().querySelector('form'));
    check('long_title_saved',(await state()).records[declared.id].find(item=>item.id===ids[declared.id])?.[field.id],title,(await state()).records[declared.id].find(item=>item.id===ids[declared.id])?.[field.id]===title);
   }
  }
 }
 const snapshot=await state();const paths=spec.navigation.map(v=>'/?view='+v);
 for(const view of spec.views){const row=snapshot.records[view.entity]?.[0];if(row)paths.push('/?view='+view.id+'&id='+row.id);}
  for(const width of [1440,1280,768,390,360])for(const path of paths){
  frame.style.width=width+'px';await load(path);const doc=d(),win=w(),text=doc.body.innerText;
  check('navigation',{path,heading:doc.querySelector('h1')?.textContent},'semantic main heading and named navigation',!!doc.querySelector('main h1')&&!!doc.querySelector('nav[aria-label]'));
  check('responsive',{width,actual:win.innerWidth},width,win.innerWidth===width&&!!doc.querySelector('meta[name=viewport]'));
  check('overflow',{path,width,scroll:doc.documentElement.scrollWidth},'no page overflow',doc.documentElement.scrollWidth<=win.innerWidth+2);
  if(verifyVisuals&&width===1440&&!new URLSearchParams(path.split('?')[1]||'').has('id'))for(const req of spec.visual_requirements||[]){
   if(!path.includes('view='+encodeURIComponent(req.view_id)))continue;
   const image=doc.querySelector('[data-asset-id="'+req.id+'"] img');
   if(!req.blocking&&!image)continue;
   check('visual_asset:'+req.id,{src:image?.getAttribute('src'),complete:image?.complete,naturalWidth:image?.naturalWidth,naturalHeight:image?.naturalHeight},
    'retained asset visibly loaded at declared placement',!!image&&image.complete&&image.naturalWidth>=req.min_width&&image.naturalHeight>=req.min_height&&image.getAttribute('src')==='/assets/'+req.id+'.png');
  }
  if(spec.views.some(view=>view.kind==='board'&&path===params(view.id))&&width<=390){
   const columns=[...doc.querySelectorAll('.board-column')],boardBox=doc.querySelector('.board')?.getBoundingClientRect();
   check('board_mobile',{width,columns:columns.length,board_width:boardBox?.width},'all workflow columns remain readable and reachable on mobile',columns.length>0&&columns.every(el=>el.getBoundingClientRect().width<=win.innerWidth&&el.getBoundingClientRect().width>180)&&boardBox?.right<=win.innerWidth+2);
  }
  check('no_raw_errors',text,'no raw errors or placeholders',!/Traceback \(most recent|lorem ipsum|coming soon|\bundefined\b|\bNaN\b|TODO|KeyError:/.test(text));
  check('language',doc.documentElement.lang,'declared document language',!!doc.documentElement.lang);
  const controls=[...doc.querySelectorAll('input:not([type=hidden]),select,textarea')];
  check('labels',controls.map(el=>({name:el.name,labels:el.labels?.length})), 'associated labels on every field',controls.every(el=>el.labels?.length));
  const interactive=[...doc.querySelectorAll('a,button,input:not([type=hidden]),select,textarea,summary')].filter(visible);
  check('semantic_controls',interactive.map(el=>el.tagName),'native meaningful controls',interactive.every(el=>!!(el.textContent?.trim()||el.labels?.length)));
  const primary=[...doc.querySelectorAll('button[type=submit],summary.action')].find(visible);
  if(primary){const style=win.getComputedStyle(primary);const lum=s=>{const c=(s.match(/[\d.]+/g)||[]).slice(0,3).map(Number).map(v=>{v/=255;return v<=.04045?v/12.92:Math.pow((v+.055)/1.055,2.4)});return .2126*c[0]+.7152*c[1]+.0722*c[2]};const a=lum(style.color),b=lum(style.backgroundColor),ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05);check('semantic_controls',{check:'primary_action_contrast',path,width,ratio},'contrast >= 4.5',ratio>=4.5);}
  check('keyboard',interactive.map(el=>({tag:el.tagName,tabIndex:el.tabIndex})), 'keyboard eligible controls',interactive.every(el=>el.tabIndex>=0));
  const focused=[];for(const el of interactive){el.focus();focused.push({tag:el.tagName,name:el.name||el.textContent.trim(),active:doc.activeElement===el,focus_visible:el.matches(':focus-visible'),outline:win.getComputedStyle(el).outlineStyle});}
  check('focus',focused,'visible focus on focusable controls',focused.every(x=>x.active&&(!x.focus_visible||x.outline!=='none')));
  check('mobile_controls',controls.filter(visible).map(el=>el.getBoundingClientRect().height),'controls at least 40 CSS px',controls.filter(visible).every(el=>el.getBoundingClientRect().height>=40));
  check('links_buttons',{links:[...doc.querySelectorAll('a[href]')].map(el=>el.getAttribute('href')),buttons:[...doc.querySelectorAll('button')].map(el=>el.type)},'same-origin links and form submit buttons',[...doc.querySelectorAll('a[href]')].every(el=>new URL(el.href).origin===location.origin)&&[...doc.querySelectorAll('button')].every(el=>el.type==='submit'&&el.closest('form')));
  check('transport',[...doc.forms].map(f=>({action:f.action,method:f.method})),'local GET collection filters or POST record/calculation routes',[...doc.forms].every(f=>{const path=new URL(f.action).pathname;return f.method==='get'&&f.classList.contains('filters')&&path==='/'||f.method==='post'&&/^\/(records\/|calculate)/.test(path)}));
  report.checks.push({path,width,actual_width:win.innerWidth,scroll_width:doc.documentElement.scrollWidth,forms:doc.forms.length,heading:doc.querySelector('h1')?.textContent});
 }
  if(journey&&criterion(/real.browser verification covers|browser.*acceptance flow/))check('browser_owner_journey',{steps:browserJourneySteps,paths:paths.length,widths:[1440,1280,768,390,360],failures:report.failures.map(r=>r.assertion_id)},'complete real-browser owner journey with no failed observations',browserJourneySteps>=spec.entities.length&&paths.length>=spec.navigation.length&&report.failures.length===0);
}catch(error){check('journey_exception',String(error),'completed browser journey',false);}
report.status=report.failures.length?'FAIL':'PASS';const result=document.createElement('pre');result.id='redi-browser-evidence';result.textContent=JSON.stringify(report);document.body.appendChild(result);frame.remove();
})();
