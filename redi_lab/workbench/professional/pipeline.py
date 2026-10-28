"""Evidence-first professional factory with bounded, attributable recovery."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time

from ...qualification_executor import GATE_ORDER, materialize
from ..normal_app_gates import evaluate_normal_app
from ..product_packaging import python_executable, verify_package
from ..runtime_identity import require_current_normal_app_runtime, normal_app_runtime_status
from .spec import BACKEND, MODEL, build_plan, source_record, spec_prompt, validate_spec, sha, visual_request_intent
from .contracts import task_prompt, validate_artifact
from .quality import BRIDGE, trusted_sources, prepare_project, inspect_product, http_smoke
from .browser import audit
from .spec_schema import decode_spec, WIRE_VERSION
from .spec_schema import VISUAL_WIRE_VERSION, VISUAL_VERSION
from .visual import retain_visual, verify_manifest, VisualFailure, placement_key
from .acceptance import collect
from ..product_identity import requested_name
from .runtime import database_contract
from .recovery import (VERSION as RECOVERY_VERSION, LOCAL_REPAIR_RULES, RecoveryBudget, RecoveryPolicy, OwnerBlock, ContractMismatch, PlanningDefect,
    diagnose, bounded_evidence, correction_prompt, owner_action, descendants, validate_plan, lesson, digest)
from dataclasses import asdict
from decimal import Decimal


def write_new(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,indent=2,ensure_ascii=False)


class ProfessionalRunner:
    def __init__(self,root,prompt,provider,*,progress=None,execution_mode='real_provider',sources=None,ceiling='2.00',recovery_budget=None,recovery_profiles=None,logical_run_id=None,prior=None,visual_worker=None,visual_reviewer=None):
        self.root=Path(root).resolve()
        authorized_root = (self.root.is_dir() and
                           {p.name for p in self.root.iterdir()} == {'build_authorization.json'})
        self.root.mkdir(parents=True,exist_ok=logical_run_id is not None or authorized_root)
        self.prompt=prompt;self.provider=provider;self.progress=progress;self.mode=execution_mode;self.ceiling=ceiling
        self.logical_run_id=logical_run_id or self.root.name.removeprefix('wb-')
        self.is_continuation=prior is not None
        self.sources=[source_record(prompt),*(sources or [])];self.artifacts={};self.events=[];self.tasks={}
        self.inputs={};self.outputs={};self.integrity={};self.lock=threading.RLock();self.started=time.time()
        self.spec=None;self.plan=None;self.stage='specification';self.previous_hash='0'*64
        self.visual_worker=visual_worker;self.visual_reviewer=visual_reviewer;self.visual_assets={};self.visual_variants={};self.visual_attempts={};self.visual_failures=[];self.visual_cost=Decimal('0');self.visual_invocations=0;self.visual_corrections={};self.visual_duration=0.0
        self.gates=dict.fromkeys(GATE_ORDER,False);self.gate_states=dict.fromkeys(GATE_ORDER,'NOT_RUN');self.reasons={}
        self.recovery=RecoveryPolicy(recovery_budget, recovery_profiles if recovery_profiles is not None else getattr(provider,'recovery_profiles',()))
        self.episodes=[];self.owner_action=None;self.attempts={};self.call_attempts={};self.contexts={};self.corrections={};self.profiles={};self.local_outputs={}
        if prior is not None:
            self.previous_hash=hashlib.sha256((self.root/'owner_decision.json').read_bytes()).hexdigest()
            self.started=time.time()-float(prior['elapsed_seconds'])
            for key in prior['call_facts']['per_task']|prior['call_facts']['failures']:
                task=key.split('@')[0];attempt=int(key.split('@')[1]) if '@' in key else 1
                self.call_attempts[task]=max(attempt,self.call_attempts.get(task,0))
            self.episodes=list(prior['recovery_episodes'])
            for episode in self.episodes:
                action=episode.get('recovery_action');task=episode.get('task')
                mapping={'LOCAL_REPAIR':'local_repair','SAME_STATION_CORRECTION':'same_station','CONTRACT_REPAIR_AND_REPLAY':'contract_repair','PARTIAL_REPLAN':'replan','CAPABILITY_ESCALATION':'escalation','PROVIDER_RETRY':'provider_retry'}
                if action in mapping and task:
                    key=mapping[action];used=self.recovery.used.setdefault(task,{})
                    used[key]=used.get(key,0)+1

    def emit(self,event,**details):
        with self.lock:
            row={'event':event,'ts':time.time(),**details,'previous_hash':self.previous_hash}
            row['sha256']=sha(row);self.previous_hash=row['sha256'];self.events.append(row)
            with (self.root/'events.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(row)+'\n')
            if self.progress:self.progress(event)

    @staticmethod
    def artifact_hash(output):
        try:
            payload=json.loads(output);return digest(payload['patches'][0]['content'])
        except (ValueError,TypeError,KeyError,IndexError):return digest(output or '')

    def plan_for_run(self, spec):
        # Preserve the one-argument Ora planner contract used by historical
        # recovery tests; bind only ZANA's logical run identity afterward.
        plan=build_plan(spec)
        for task in plan['tasks']:
            if task.get('persona')=='zana':
                task['input_contract']['visual_task']['product_run_id']=self.logical_run_id
        return plan

    def snapshot(self):
        with self.lock:
            recovering=any(v in ('RECOVERY','REPLANNING','ESCALATED','FAILED_INTERNAL') for v in self.tasks.values())
            facts=getattr(self.provider,'facts',{'calls':sum(self.call_attempts.values()),'per_task':{},'failures':{}})
            return {'status':'RUNNING','verified':False,'app_state':'RECOVERING' if recovering else 'VERIFYING' if self.stage in ('frozen_gates','packaging','product_review') else 'BUILDING' if self.spec else 'PLANNING',
                'recovery_version':RECOVERY_VERSION,'recovery_episodes':list(self.episodes),'owner_action':self.owner_action,
                'task_states':dict(self.tasks),'manifest':self.plan,'product_name':(self.spec or {}).get('product_name'),
                'product_spec':self.spec,'events':list(self.events),'component_map':{'agron':'core','teuta':'ui','bindus':'workflow'},
                'gate_states':dict(self.gate_states),'gates':dict(self.gates),'integrity':dict(self.integrity),
                'call_facts':facts,'provider_calls':facts['calls'],'total_provider_calls':facts['calls']+self.visual_facts()['provider_calls'],
                'total_cost_usd':str(Decimal(str(getattr(self.provider,'spent','0')))+self.visual_known_cost()),
                'total_cost_upper_bound_usd':str(Decimal(str(getattr(self.provider,'spent','0')))+self.visual_known_cost()+self.outstanding_reservations()),
                'visual_facts':self.visual_facts(),'visual_assets':list(self.visual_assets.values()),
                'visual_worker_profile':getattr(self.visual_worker,'profile',None),
                'visual_worker_profile_record':self.visual_worker.profile_record() if hasattr(self.visual_worker,'profile_record') else None,
                'elapsed_seconds':round(time.time()-self.started,3),'execution_mode':self.mode,'model':MODEL,
                'recovery_profiles':dict(self.profiles)}

    def call(self,task,prompt):
        with self.lock:
            count=sum(self.call_attempts.values())
            if count>=self.recovery.budget.max_provider_calls:
                raise OwnerBlock('Global provider-call ceiling reached','RECOVERY_EXHAUSTED')
            limit=self.recovery.budget.max_elapsed_seconds
            if limit is not None and time.time()-self.started>=limit:
                raise OwnerBlock('Run elapsed-time ceiling reached','RECOVERY_EXHAUSTED')
            attempt=self.call_attempts.get(task,0)+1;self.call_attempts[task]=attempt
        directory=self.root/'calls'/task
        if attempt>1:directory=directory/f'attempt-{attempt:03}'
        context=self.contexts.setdefault(task,{})
        context.update(phase='provider',references=[str(directory.relative_to(self.root))],worker_output=None)
        prompt=self.corrections.get(task,prompt)
        profile=self.profiles.get(task)
        write_new(directory/'input.json',{'task_id':task,'attempt':attempt,'profile':profile,'prompt':prompt,'sha256':sha(prompt)})
        self.inputs[task]=prompt
        try:
            if hasattr(self.provider,'execute'):
                output=self.provider.execute(task,prompt,attempt=attempt,profile=profile)
            else:output=self.provider(task,prompt)
        except Exception as exc:
            partial=getattr(exc,'partial_output',None)
            if partial is not None:context['worker_output']=self.sanitize(partial)
            write_new(directory/'failure.json',{'exception_class':type(exc).__name__,'reason':self.sanitize(exc),'partial_output':context.get('worker_output')})
            raise
        self.outputs[task]=output;context.update(phase='json_decode',worker_output=output)
        write_new(directory/'output.json',{'content':output,'sha256':sha(output)})
        return json.loads(output)

    def visual_known_cost(self):
        return Decimal(str(getattr(self.provider,'visual_spent',0))) if getattr(self.visual_worker,'billing_managed',False) else self.visual_cost

    def outstanding_reservations(self):
        return sum(getattr(self.provider,'reserved',{}).values(),Decimal(0))+sum(getattr(self.provider,'visual_reserved',{}).values(),Decimal(0))

    def visual_facts(self):
        return {'worker_invocations':self.visual_invocations,'cost_usd':str(self.visual_known_cost()),
                'reserved_cost_usd':str(sum(getattr(self.provider,'visual_reserved',{}).values(),Decimal(0))),
                'duration_seconds':round(self.visual_duration,3),'retained_assets':sum(len(v) for v in self.visual_variants.values()),
                'accepted_variants':sum(len(v) for v in self.visual_variants.values()),
                'rejected_variants':len(self.visual_failures),'failures':list(self.visual_failures),
                'retries':sum(max(0,n-next((t['input_contract']['visual_task']['variants'] for t in (self.plan or {}).get('tasks',[]) if t['task_id']==tid),1)) for tid,n in self.visual_attempts.items()),
                'provider_calls':0 if self.mode=='injected_offline' else getattr(self.visual_worker,'calls',0),
                'independent_review_calls':sum(key.startswith('visual_review:') for key in getattr(self.provider,'facts',{}).get('per_task',{}))}

    def visual_handoffs(self):
        requirements=(self.spec or {}).get('visual_requirements',[])
        return [{'asset_id':r['id'],'view_id':r['view_id'],'slot_id':r['slot_id'],
                 'intended_placement':placement_key(r),'semantic_purpose':r['purpose'],
                 'min_width':r['min_width'],'min_height':r['min_height'],
                 'downstream_consumers':['teuta','gentius'],
                 'local_retained_path':self.visual_assets[r['id']]['generated_file_path'] if r['id'] in self.visual_assets else None,
                 'width':self.visual_assets[r['id']]['width'] if r['id'] in self.visual_assets else None,
                 'height':self.visual_assets[r['id']]['height'] if r['id'] in self.visual_assets else None,
                 'aspect_ratio':r['aspect_ratio'],'sha256':self.visual_assets[r['id']]['content_sha256'] if r['id'] in self.visual_assets else None}
                for r in requirements]

    def build_visual_task(self,task):
        tid=task['task_id'];contract=task['input_contract']['visual_task'];rid=task['visual_requirement_id']
        self.tasks[tid]='RUNNING'
        self.contexts[tid]={'phase':'visual_provider','upstream':{},'references':['execution_manifest.json'],'worker_output':None}
        try:
            if self.visual_worker is None or (self.mode!='injected_offline' and not getattr(self.visual_worker,'live_authorized',False)):
                raise OwnerBlock('No owner-authorized visual provider is configured','EXTERNAL_SERVICE_REQUIRED')
            expected=next(t for t in self.plan_for_run(self.spec)['tasks'] if t['task_id']==tid)
            if task['input_contract']!=expected['input_contract']:
                raise ContractMismatch('Visual task differs from immutable ProductSpec')
            rows=[]
            for variant_index in range(1,contract['variants']+1):
                attempt=self.visual_attempts.get(tid,0)+1;self.visual_attempts[tid]=attempt
                self.tasks[tid]='RUNNING';self.emit('component_task_started:visual:'+rid,task_id=tid,persona='zana',variant_index=variant_index)
                self.visual_invocations+=1;started=time.time()
                correction=self.visual_corrections.get(tid)
                try:result=self.visual_worker.generate(contract,variant_index=variant_index,failure_evidence=correction)
                finally:self.visual_duration+=time.time()-started
                billed=Decimal(str(result.get('cost_usd','0')))
                if not billed.is_finite() or billed<0:raise VisualFailure('budget_credit','Invalid visual worker cost')
                self.visual_cost+=billed
                if Decimal(str(getattr(self.provider,'spent','0')))+self.visual_known_cost()+self.outstanding_reservations()>Decimal(self.ceiling):
                    raise VisualFailure('budget_credit','Visual cost exceeded owner ceiling')
                self.tasks[tid]='VERIFYING';self.emit('component_task_verifying:visual:'+rid,task_id=tid,persona='zana',variant_index=variant_index)
                self.contexts[tid]['phase']='visual_validation'
                row=retain_visual(contract,result,self.root,attempt=attempt,variant_index=variant_index,independent_review=self.visual_reviewer)
                verify_manifest(row,self.root)
                rows.append(row)
                self.emit('artifact_retained:visual:'+rid,task_id=tid,sha256=row['content_sha256'],variant_index=variant_index)
            with self.lock:
                self.visual_assets[rid]=rows[0];self.visual_variants[tid]=rows;self.tasks[tid]='COMPLETED'
            self.emit('component_task_completed:visual:'+rid,task_id=tid,manifests=rows)
        except Exception as exc:
            attempt=self.visual_attempts.get(tid,0)
            failed_file=self.root/'visual'/tid/f'attempt-{attempt:03d}'/'generated.png'
            failed_hash=hashlib.sha256(failed_file.read_bytes()).hexdigest() if failed_file.is_file() else None
            if failed_hash:
                self.contexts[tid]['visual_content_sha256']=failed_hash
                self.contexts[tid]['references'].append(str(failed_file.relative_to(self.root)))
            self.visual_failures.append({'task_id':tid,'attempt':attempt,'kind':getattr(exc,'kind','provider_unavailable'),
                                         'detail':self.sanitize(exc),'content_sha256':failed_hash})
            self.tasks[tid]='FAILED_INTERNAL';self.emit('component_task_failed:visual:'+rid,task_id=tid,reason=self.sanitize(exc))
            if not contract['blocking']:
                self.tasks[tid]='COMPLETED'
                self.emit('visual_optional_skipped',task_id=tid,persona='zana',reason=self.sanitize(exc))
                return
            raise

    def recover(self, task, exc, execute):
        tid=task['task_id'];initial=task.get('worker_profile',{'id':'professional_worker_v1','model':MODEL,'capability_rank':0})
        while True:
            context=self.contexts.get(tid,{'phase':self.stage,'worker_output':None,'references':[]})
            phase=context.get('phase',self.stage)
            self.tasks[tid]='RECOVERY';self.emit('recovery_triage_started',task_id=tid,persona='bato',state='ANALYZING')
            safe_exc=exc
            # Retain exception attributes for typed provider classification, sanitize every persisted string below.
            evidence=bounded_evidence(self.spec,task,context.get('upstream',{}),context.get('worker_output'),context.get('references',[]))
            prior=[{'episode_id':e['episode_id'],'action':e['recovery_action'],'result':e['result'],
                    'failure_signature':e.get('bato_diagnosis',{}).get('failure_signature')}
                   for e in self.episodes if e['task']==tid]
            diagnosis=diagnose(safe_exc,task=task,phase=phase,evidence=evidence,plan=self.plan,previous=prior)
            diagnosis['exact_cause']=self.sanitize(diagnosis['exact_cause'])
            episode_id=f'episode-{len(self.episodes)+1:04}'
            directory=self.root/'recovery'/episode_id
            write_new(directory/'evidence.json',json.loads(self.sanitize(json.dumps(evidence))))
            write_new(directory/'diagnosis.json',diagnosis)
            self.emit('recovery_diagnosed',task_id=tid,persona='bato',episode_id=episode_id,diagnosis=diagnosis)
            self.emit('recovery_decision_started',task_id=tid,persona='zojz')
            decision=self.recovery.choose(diagnosis,initial)
            if task.get('persona')=='zana' and decision['action']=='CAPABILITY_ESCALATION':
                decision={**decision,'action':'OWNER_ACTION_REQUIRED','owner_reason':'MANUAL_APPROVAL_REQUIRED',
                          'profile':None,'reason':'No stronger visual worker is owner-authorized'}
            write_new(directory/'decision.json',decision)
            self.emit('recovery_decided',task_id=tid,persona='zojz',episode_id=episode_id,decision=decision)
            action=decision['action'];started=time.time();cost=Decimal(str(getattr(self.provider,'spent','0')))+self.visual_known_cost()
            before_facts=dict(getattr(self.provider,'facts',{}).get('per_task',{}))
            before_failures=set(getattr(self.provider,'facts',{}).get('failures',{}))
            episode={'schema_version':'recovery_episode_v1','episode_id':episode_id,'run_id':self.root.name,
                'station':task['persona'],'task':tid,'product_task_class':{'product_type':(self.spec or {}).get('product_type'),'task':tid,'storage':(self.spec or {}).get('persistence'), 'field_types':sorted({f['type'] for e in (self.spec or {}).get('entities',[]) for f in e['fields']})},
                'initial_worker_profile':initial,'initial_artifact_hash':context.get('visual_content_sha256') if task.get('persona')=='zana' else self.artifact_hash(context.get('worker_output')),'initial_output_hash':digest(context.get('worker_output') or ''),
                'failure_class':diagnosis['failure_class'],'exact_failing_evidence':diagnosis['evidence_references'],
                'bato_diagnosis':diagnosis,'zojz_decision':decision,'recovery_action':action,'recovery_profile':decision['profile'] or initial,
                'downstream_invalidations':[],'checks_rerun':[],'result':'OWNER_ACTION_REQUIRED' if action=='OWNER_ACTION_REQUIRED' else 'FAILED'}
            self.episodes.append(episode)
            write_new(directory/'episode_started.json',episode)
            if action=='OWNER_ACTION_REQUIRED':
                self.tasks[tid]='FAILED_INTERNAL';self.owner_action=owner_action(diagnosis,decision,self.episodes)
                episode.update(corrected_artifact_hash=None,cost_delta='0',token_delta={'input':0,'output':0},elapsed_delta=0,final_status='OWNER_ACTION_REQUIRED')
                write_new(directory/'episode.json',episode)
                raise OwnerBlock(diagnosis['exact_cause'],self.owner_action['reason'])
            state='ESCALATED' if action=='CAPABILITY_ESCALATION' else 'REPLANNING' if action in ('PARTIAL_REPLAN','CONTRACT_REPAIR_AND_REPLAY') else 'RECOVERY'
            if decision['profile']:self.profiles[tid]=decision['profile']
            self.tasks[tid]=state;self.emit('recovery_action_started',task_id=tid,persona=task['persona'],state=state,action=action,profile=decision['profile'])
            try:
                if action=='LOCAL_REPAIR':
                    raw=context['worker_output'];fixed=raw.removeprefix('\ufeff')
                    json.loads(fixed)
                    self.local_outputs[tid]=json.loads(fixed)
                    episode['deterministic_repair']={'rule':'json_envelope_bom_v1','original_hash':digest(raw),'new_hash':digest(fixed),'affected_checks':['json_decode']}
                elif action in ('PARTIAL_REPLAN','CONTRACT_REPAIR_AND_REPLAY'):
                    episode['downstream_invalidations']=self.repair_branch(tid,directory,action)
                    task=next(t for t in self.plan['tasks'] if t['task_id']==tid)
                else:
                    if action=='PROVIDER_RETRY':time.sleep(self.recovery.budget.backoff_seconds)
                    elif task.get('persona')=='zana':self.visual_corrections[tid]={'failure_class':diagnosis['failure_class'],'failed_check':diagnosis['failed_check'],'exact_cause':diagnosis['exact_cause'],'previous_attempts':prior}
                    else:self.corrections[tid]=correction_prompt(task_prompt(task,self.spec,context.get('upstream',{})) if self.spec else spec_prompt(self.prompt,self.sources),diagnosis,evidence,prior)
                    if decision['profile']:self.profiles[tid]=decision['profile']
                execute()
                episode['result']='PASS';episode['checks_rerun']=self.integrity.get(tid,{}).get('checks',[])
                episode['corrected_artifact_hash']=(self.visual_assets[task['visual_requirement_id']]['content_sha256'] if task.get('persona')=='zana' else digest(self.artifacts.get(next(iter(task.get('owned_paths',[])),''),self.outputs.get(tid,''))))
            except Exception as followup:
                exc=followup;episode['remaining_failure']=self.sanitize(type(exc).__name__+': '+str(exc))
            finally:
                newfacts={k:v for k,v in getattr(self.provider,'facts',{}).get('per_task',{}).items() if k not in before_facts}
                unknown=[v for k,v in getattr(self.provider,'facts',{}).get('failures',{}).items() if k not in before_failures and not v.get('billing_known')]
                visual_unknown=sum(getattr(self.provider,'visual_reserved',{}).values(),Decimal(0))
                episode['cost_delta_is_complete']=not unknown and not visual_unknown
                episode['token_delta_complete']=not unknown
                episode['unresolved_cost_reservation_usd']=str(sum((Decimal(v.get('reserved_cost_usd','0')) for v in unknown),Decimal(0))+visual_unknown)
                episode.update(cost_delta=str(Decimal(str(getattr(self.provider,'spent','0')))+self.visual_known_cost()-cost),
                    token_delta={key:sum(f.get(key,0) for f in newfacts.values()) for key in ('input','output')},
                    elapsed_delta=round(time.time()-started,3),final_status='VERIFIED' if episode['result']=='PASS' else 'FAILED_INTERNAL')
                write_new(directory/'episode.json',episode)
                self.emit('recovery_episode_finished',task_id=tid,episode_id=episode_id,result=episode['result'])
            if episode['result']=='PASS':return

    def repair_branch(self,tid,directory,action):
        original=self.plan;canonical=self.plan_for_run(self.spec);validate_plan(canonical)
        old={t['task_id']:t for t in original['tasks']};new={t['task_id']:t for t in canonical['tasks']}
        changed={k for k in old.keys()|new.keys() if old.get(k)!=new.get(k)}|{tid}
        affected=changed|set(descendants(original,changed))|set(descendants(canonical,changed))
        retained=sorted(k for k in self.tasks if k not in affected and self.tasks[k]=='COMPLETED')
        self.plan=canonical;self.corrections.pop(tid,None)
        for node in affected:
            if node in new:self.tasks[node]='PENDING'
            else:self.tasks.pop(node,None)
            for path in old.get(node,{}).get('owned_paths',[]):self.artifacts.pop(path,None)
            if old.get(node,{}).get('persona')=='zana':self.visual_assets.pop(old[node]['visual_requirement_id'],None)
            self.integrity.pop(node,None)
        write_new(directory/'replan.json',{'authority':'ora','action':action,'product_spec_sha256':sha(self.spec),
            'original_plan':original,'replacement_plan':canonical,'replaced_tasks':sorted(changed),'retained_tasks':retained,
            'invalidated_tasks':sorted(affected),'acceptance_criteria':self.spec['acceptance_criteria']})
        self.emit('partial_replan_completed',persona='ora',invalidated_tasks=sorted(affected),retained_tasks=retained,manifest=canonical)
        return sorted(affected)

    def build_task(self,task):
        if task['persona']=='zana':return self.build_visual_task(task)
        tid=task['task_id'];name=task['owned_paths'][0];fragment=name.removesuffix('.py')
        attempt=self.attempts.get(tid,0)+1;self.attempts[tid]=attempt
        base=self.root/'integrity'/tid if attempt==1 else self.root/'integrity'/tid/f'attempt-{attempt:03}'
        with self.lock:
            by_id={t['task_id']:t for t in self.plan['tasks']}
            def ancestors(node):
                deps=by_id[node]['dependencies']
                return set(deps).union(*(ancestors(d) for d in deps))
            owned={path for dep in ancestors(tid) for path in by_id[dep]['owned_paths']}
            self.tasks[tid]='ESCALATED' if tid in self.profiles else 'RECOVERY' if tid in self.corrections else 'RUNNING';upstream={name:source for name,source in self.artifacts.items() if name in owned}
        self.emit('component_task_started:'+fragment,task_id=tid,persona=task['persona'])
        try:
            self.contexts[tid]={'phase':'contract_handoff','upstream':upstream,'references':['execution_manifest.json']}
            expected=next(t for t in self.plan_for_run(self.spec)['tasks'] if t['task_id']==tid)
            if task['input_contract']!=expected['input_contract'] or task['dependencies']!=expected['dependencies']:
                raise ContractMismatch('Task handoff differs from the immutable ProductSpec-derived contract')
            payload=self.local_outputs.pop(tid) if tid in self.local_outputs else self.call(tid,task_prompt(task,self.spec,upstream,self.visual_handoffs()))
            self.contexts[tid]['phase']='artifact_integrity'
            patches=payload.get('patches',[])
            if len(patches)!=1 or patches[0].get('rel_path')!=name or patches[0].get('action')!='write':
                raise ValueError('Artifact ownership violation')
            source=patches[0]['content']
            # Retain bytes before validation; a failed producer remains inspectable.
            write_new((self.root/'artifacts'/name.replace('.py','.json')) if attempt==1 else (self.root/'attempts'/tid/f'{attempt:03}'/'artifact.json'),{'path':name,'content':source,'sha256':hashlib.sha256(source.encode()).hexdigest(),'owner':task['persona']})
            validate_artifact(name,source,self.spec)
            sources={**trusted_sources(self.spec),**upstream,name:source}
            if name=='ui.py':sources['service.py']=BRIDGE
            self.tasks[tid]='VERIFYING';self.emit('component_task_verifying:'+fragment,task_id=tid)
            candidate=prepare_project(base/'candidate',sources,self.spec,self.packaged_visual_assets())
            report=inspect_product(candidate,self.spec,base/'checks',stage=tid)
            if name=='ui.py':
                self.stage='teuta_browser';self.contexts[tid]['phase']='render_browser_integrity'
                empty=audit(candidate,self.spec,base/'empty.sqlite3',base/'browser-empty',verify_visuals=False)
                populated=audit(candidate,self.spec,report['database'],base/'browser-populated',verify_visuals=False)
                journey=audit(candidate,self.spec,base/'journey.sqlite3',base/'browser-journey',journey=True,verify_visuals=False)
                if journey['status'] != 'PASS':
                    raise ValueError('Teuta capability journey failed before completion: '+json.dumps(journey['failures']))
                report['browser']={'empty':empty,'populated':populated,'journey':journey}
            with self.lock:
                self.artifacts[name]=source;self.integrity[tid]=report;self.tasks[tid]='COMPLETED'
            write_new(base/'lineage.json',{'task_id':tid,'attempt':attempt,'owner':task['persona'],'artifact_sha256':digest(source), 'spec_sha256':sha(self.spec),'contract_sha256':sha(task['input_contract']), 'upstream':{path:digest(value) for path,value in upstream.items()}})
            self.emit('artifact_retained:'+name,task_id=tid)
            self.emit('component_task_completed:'+fragment,task_id=tid,checks=report)
        except Exception as exc:
            with self.lock:self.tasks[tid]='FAILED_INTERNAL'
            self.emit('component_task_failed:'+fragment,task_id=tid,reason=self.sanitize(exc))
            raise

    def sanitize(self,value):
        return self.provider.sanitize(value) if hasattr(self.provider,'sanitize') else str(value)

    def packaged_visual_assets(self):
        with self.lock:
            rows=list(self.visual_assets.values())
        return [{**row,'source_path':str(verify_manifest(row,self.root))} for row in rows]

    def run(self):
        status='FAILED';detail='';failure_class=None;packaging=None;review=None
        write_new(self.root/'product_order.json',{'sources':self.sources,'backend':BACKEND,'ceiling_usd':self.ceiling,
            'policy':RECOVERY_VERSION,'local_repair_rules':LOCAL_REPAIR_RULES,'recovery_budget':asdict(self.recovery.budget),'recovery_profiles':self.recovery.profiles,'execution_mode':self.mode,
            'runtime_identity':normal_app_runtime_status()})
        try:
            require_current_normal_app_runtime();self.emit('planning_started',persona='ora')
            spec_result={}
            def specification():
                wire=self.local_outputs.pop('specification') if 'specification' in self.local_outputs else self.call('specification',spec_prompt(self.prompt,self.sources,visual=visual_request_intent(self.prompt)))
                spec=decode_spec(wire)
                self.contexts.setdefault('specification',{})['phase']='spec_validation'
                if spec.get('integrations'):raise OwnerBlock('Requested integration requires an explicitly provisioned adapter','EXTERNAL_SERVICE_REQUIRED')
                validate_spec(spec)
                spec_result.update(wire=wire,spec=spec)
            try:specification()
            except Exception as exc:
                self.recover({'task_id':'specification','persona':'ora','owned_paths':[], 'required_checks':['ProductSpec schema and semantics']},exc,specification)
            wire=spec_result['wire'];spec=spec_result['spec']
            owner_name = requested_name(self.prompt)
            if owner_name:
                spec['product_name'] = owner_name
            validate_spec(spec)
            self.spec=spec
            if spec.get('uncertainties'):raise OwnerBlock('Unresolved ProductSpec decisions: '+json.dumps(spec['uncertainties']))
            write_new(self.root/'database_contract.json', database_contract(spec))
            write_new(self.root/'product_spec.json',{'spec':spec,'sha256':sha(spec),'source_provenance':self.sources,
                'product_identity':{'authority':'explicit_owner_name' if owner_name else 'generated_product_spec',
                    'canonical_name':spec['product_name'],'provider_name':wire.get('product_name')},
                'wire_contract':VISUAL_WIRE_VERSION if spec['schema_version']==VISUAL_VERSION else WIRE_VERSION,'wire_sha256':sha(wire),'normalization':'omit explicit optional nulls; index transition rows by from_state'})
            self.plan=self.plan_for_run(spec);self.plan['retry_policy']=RECOVERY_VERSION;write_new(self.root/'execution_manifest.json',self.plan)
            self.tasks={t['task_id']:'PENDING' for t in self.plan['tasks']}
            self.emit('scope_resolved',persona='ora',spec_sha256=sha(spec),manifest_sha256=sha(self.plan))
            while any(v=='PENDING' for v in self.tasks.values()):
                ready=[t for t in self.plan['tasks'] if self.tasks[t['task_id']]=='PENDING' and all(self.tasks[d]=='COMPLETED' for d in t['dependencies'])]
                if not ready:
                    task=next(t for t in self.plan['tasks'] if self.tasks[t['task_id']]=='PENDING')
                    self.contexts[task['task_id']]={'phase':'planning','references':['execution_manifest.json']}
                    self.recover(task,PlanningDefect('Blocked DAG dependencies'),lambda:self.build_task(next(t for t in self.plan['tasks'] if t['task_id']==task['task_id'])))
                    continue
                self.stage='tasks:'+','.join(t['task_id'] for t in ready)
                with ThreadPoolExecutor(max_workers=len(ready)) as pool:
                    futures=[(t,pool.submit(self.build_task,t)) for t in ready]
                    failures=[]
                    for task,future in futures:
                        try:future.result()
                        except Exception as exc:failures.append((task,exc))
                for task,exc in failures:
                    self.recover(task,exc,lambda tid=task['task_id']:self.build_task(next(t for t in self.plan['tasks'] if t['task_id']==tid)))
            self.stage='frozen_gates'
            sources={**trusted_sources(spec),**self.artifacts}
            retained=materialize(product_key='professional_product',arm=self.mode,model=MODEL,patches=sources)
            expected={'entities':len(spec['entities']),'views':len(spec['views']),'verified':True}
            def check(target):return [{'kind':'call_equals','target':target,'args':[],'expected':expected}]
            product={'package':'product_app','public_checks':check('run_acceptance'),
                     'private_checks':check('run_private'),'integration_probe':check('integration_probe')}
            write_new(self.root/'frozen_gate_contract.json',{'product':product,'required_files':list(sources)})
            result=evaluate_normal_app(retained,product,{'required_files':list(sources)},ws_root=self.root/'gates',progress=self.emit)
            self.gates=result['gates'];self.gate_states=result['gate_states'];self.reasons=result['reasons']
            write_new(self.root/'frozen_gate_result.json',result)
            if not result['all_mandatory_gates_pass']:raise ValueError('Frozen gates failed: '+json.dumps(self.reasons))
            self.stage='packaging';self.emit('packaging_started')
            # Copy the actual gate bytes, including the pinned trusted runtime.
            verified_sources={name:(self.root/'gates'/'product_app'/name).read_text(encoding='utf-8') for name in sources}
            if verified_sources!=sources:raise ValueError('Gate artifact bytes changed before delivery')
            app=prepare_project(self.root/'app',verified_sources,spec,self.packaged_visual_assets())
            smoke=http_smoke(app,spec,self.root/'delivery-smoke')
            final_browser=audit(app,spec,smoke['database'],self.root/'delivery-browser')
            visual_integration=[]
            for req in spec.get('visual_requirements',[]):
                row=self.visual_assets.get(req['id'])
                if not row:
                    if req['blocking']:raise VisualFailure('integration','Required visual asset was not retained')
                    continue
                path=app/'assets'/(req['id']+'.png')
                observed=next((a for a in final_browser.get('assertions',[]) if a['assertion_id']=='visual_asset:'+req['id']),None)
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=row['content_sha256'] or not observed or observed['status']!='PASS':
                    raise VisualFailure('integration','Visual asset is missing, changed or unused in final browser')
                row['final_integration_path']='assets/'+req['id']+'.png';row['actually_used']=True
                write_new(self.root/'visual'/row['visual_task_id']/'final_manifest.json',row)
                visual_integration.append({'asset_id':req['id'],'view_id':req['view_id'],
                                           'slot_id':req['slot_id'],'sha256':row['content_sha256'],
                                           'browser_assertion':observed})
            packaging={'status':'PASS','project_path':str(app),'package_sha256':verify_package(app),
                       'smoke':smoke,'browser':final_browser,'visual_integration':visual_integration}
            self.emit('packaging_finished:pass',package_sha256=packaging['package_sha256'])
            self.stage='product_review';self.emit('product_review_started',persona='medaurus')
            acceptance=collect(app,spec,'wb-'+self.logical_run_id,self.root/'acceptance')
            write_new(self.root/'reviewer_inputs.json',acceptance['evidence'])
            review=acceptance['review']
            if review.get('verdict')!='PASS' or not review.get('checked_criteria'):raise ValueError('Product reviewer rejected: '+json.dumps(review))
            self.emit('product_review_finished:pass',persona='medaurus',review=review)
            if self.recovery.budget.max_elapsed_seconds is not None and time.time()-self.started>self.recovery.budget.max_elapsed_seconds:
                raise OwnerBlock('Run elapsed-time ceiling reached before release','RECOVERY_EXHAUSTED')
            status='VERIFIED';self.stage='complete'
        except Exception as exc:
            detail=self.sanitize(type(exc).__name__+': '+str(exc))
            status='OWNER_ACTION_REQUIRED'
            context=self.contexts.get('specification',{}) if self.spec is None else {}
            terminal_task={'task_id':self.stage,'persona':'ora' if self.stage=='specification' else 'zojz','owned_paths':[]}
            diagnosis=diagnose(exc,task=terminal_task,phase=context.get('phase',self.stage),evidence={'references':['events.jsonl']},plan=None,previous=[])
            diagnosis['exact_cause']=detail;failure_class=diagnosis['failure_class']
            if not self.owner_action:
                decision={'schema_version':'recovery_decision_v1','authority':'zojz','action':'OWNER_ACTION_REQUIRED','owner_reason':diagnosis['owner_reason'] or 'RECOVERY_EXHAUSTED'}
                write_new(self.root/'terminal_diagnosis.json',diagnosis)
                write_new(self.root/'terminal_decision.json',decision)
                self.owner_action=owner_action(diagnosis,decision,self.episodes)
            write_new(self.root/'owner_action.json',self.owner_action)
            self.emit('owner_action_required',stage=self.stage,reason=self.owner_action,failure_class=failure_class)
        if status=='VERIFIED':
            for episode in self.episodes:
                if episode['result']=='PASS':write_new(self.root/'recovery_lessons'/(episode['episode_id']+'.json'),lesson(episode,episode['product_task_class']))
        self.emit('run_finished',status=status)
        facts=getattr(self.provider,'facts',{'calls':sum(self.call_attempts.values()),'per_task':{},'failures':{}})
        cost=str(Decimal(str(getattr(self.provider,'spent','0')))+self.visual_known_cost())
        evidence={'schema_version':'professional_build_evidence_v1','build_id':self.logical_run_id,
            'recovery_version':RECOVERY_VERSION,'recovery_episodes':self.episodes,'owner_action':self.owner_action,
            'app_state':'APP_READY' if status=='VERIFIED' else 'OWNER_ACTION_REQUIRED','recovery_profiles':self.profiles,
            'backend':BACKEND,'execution_mode':self.mode,'status':status,'verified':status=='VERIFIED',
            'product_name':(self.spec or {}).get('product_name'),'product_spec':self.spec,'manifest':self.plan,
            'task_states':self.tasks,'events':self.events,'integrity':self.integrity,'gates':self.gates,
            'gate_states':self.gate_states,'gate_reasons':self.reasons,'call_facts':facts,'provider_calls':facts['calls'],
            'total_provider_calls':facts['calls']+self.visual_facts()['provider_calls'],
            'visual_facts':self.visual_facts(),'visual_assets':list(self.visual_assets.values()),
            'visual_worker_profile':getattr(self.visual_worker,'profile',None),
            'visual_worker_profile_record':self.visual_worker.profile_record() if hasattr(self.visual_worker,'profile_record') else None,
            'visual_variants':[row for rows in self.visual_variants.values() for row in rows],
            'total_cost_usd':cost,'total_cost_upper_bound_usd':str(Decimal(cost)+self.outstanding_reservations()),
            'cost_complete':not bool(self.outstanding_reservations()),
            'outstanding_reservations_usd':str(self.outstanding_reservations()),'cost_ceiling_usd':self.ceiling,'elapsed_seconds':round(time.time()-self.started,3),
            'failure_stage':None if status=='VERIFIED' else self.stage,'failure_class':failure_class,'detail':detail,
            'review':review,'packaging':packaging,'runtime_identity':normal_app_runtime_status(),
            'previous_segment_id':json.loads((self.root/'owner_decision.json').read_text(encoding='utf-8'))['previous_segment_id'] if self.is_continuation else None,
            'owner_decision_sha256':hashlib.sha256((self.root/'owner_decision.json').read_bytes()).hexdigest() if self.is_continuation else None,
            'artifacts':[{'rel_path':name,'content':source,'sha256':hashlib.sha256(source.encode()).hexdigest()} for name,source in self.artifacts.items()]}
        write_new(self.root/'live_run_evidence.json',evidence)
        if status=='VERIFIED':
            packaging['evidence_sha256']=hashlib.sha256((self.root/'live_run_evidence.json').read_bytes()).hexdigest()
            write_new(self.root/'packaging_evidence.json',packaging)
        return {**evidence,'project_path':str(self.root/'app') if status=='VERIFIED' else None,
            'component_map':{'agron':'core','teuta':'ui','bindus':'workflow'},'model':MODEL}
