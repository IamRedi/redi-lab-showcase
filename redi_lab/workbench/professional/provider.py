"""Bounded provider adapter using the existing REDI pricing and SDK boundary."""
from decimal import Decimal
import json
import os
import re
import threading
import time
from ...mcw_pricing import calculate_cost, PRICING_SCHEDULE_ID
from ..runtime_identity import require_current_normal_app_runtime
from .spec import MODEL
from .recovery import RecoveryFault, OwnerBlock
from .spec_schema import response_format

TASKS={'specification','data_model','workflow','domain','experience','assembly','product_review'}


class GovernedProvider:
    def __init__(self,key,ceiling,client_factory=None,*,recovery_profiles=None,prior_state=None):
        require_current_normal_app_runtime()
        self.key=key;self.ceiling=Decimal(ceiling)
        self.recovery_profiles=recovery_profiles if recovery_profiles is not None else (json.loads(os.environ['REDI_RECOVERY_PROFILES']) if 'REDI_RECOVERY_PROFILES' in os.environ else [{'id':'RECOVERY_HIGH_CAPABILITY','authorized':True,'model':MODEL,'capability_rank':1,'reasoning_effort':'high','output_cap':16384,'context_budget':120000}])
        if not self.ceiling.is_finite() or self.ceiling<=0: raise ValueError('Invalid owner ceiling')
        self.client_injected=bool(client_factory)
        if client_factory: self.client=client_factory()
        else:
            from openai import OpenAI
            self.client=OpenAI(api_key=key,max_retries=0,timeout=180)
        self.facts={'calls':0,'per_task':{},'failures':{},'pricing_schedule':PRICING_SCHEDULE_ID}
        self.lock=threading.Lock();self.attempted=set();self.reserved={};self.spent=Decimal(0)
        # Shared product ceiling for text and visual calls, including uncertain billing.
        self.visual_spent=Decimal(0);self.visual_reserved={};self.visual_attempted=set()
        if prior_state is not None:
            facts=prior_state['call_facts'];spent=Decimal(str(prior_state['total_cost_usd']))
            if facts.get('pricing_schedule')!=PRICING_SCHEDULE_ID or type(facts.get('calls')) is not int:
                raise ValueError('Prior provider accounting contract is incomplete')
            attempts=set(facts.get('per_task',{}))|set(facts.get('failures',{}))
            if len(attempts)!=facts['calls'] or sum((Decimal(str(f['cost_usd'])) for f in facts['per_task'].values()),Decimal(0))!=spent:
                raise ValueError('Prior provider calls or cost do not reconcile')
            reserved={key:Decimal(str(row['reserved_cost_usd'])) for key,row in facts['failures'].items()
                      if key not in facts['per_task'] and not row.get('billing_known')}
            if sum(reserved.values(),Decimal(0))!=Decimal(str(prior_state['outstanding_reservations_usd'])):
                raise ValueError('Prior provider reservations do not reconcile')
            if spent+sum(reserved.values(),Decimal(0))>self.ceiling:
                raise ValueError('Prior usage exceeds owner ceiling')
            self.facts=json.loads(json.dumps(facts));self.spent=spent;self.reserved=reserved;self.attempted=attempts

    def sanitize(self,value):
        text=str(value)
        if self.key:text=text.replace(self.key,'[REDACTED]')
        return re.sub(r'sk-[A-Za-z0-9_-]{8,}','[REDACTED]',text)

    def __call__(self,task,prompt):
        return self.execute(task,prompt,attempt=1)

    def execute(self,task,prompt,*,attempt,profile=None):
        require_current_normal_app_runtime()
        if type(attempt) is not int or not 1<=attempt<=20:raise ValueError('Invalid attempt identity')
        if profile is not None and (profile not in self.recovery_profiles or profile.get('authorized') is not True):raise ValueError('Unauthorized recovery profile')
        profile=profile or {'id':'professional_worker_v1','model':MODEL,'reasoning_effort':'low'}
        model=profile['model']
        attempt_id=task if attempt==1 else task+'@'+str(attempt)
        system='You are a professional product engineer. Follow every versioned contract. Return only the requested JSON object.'
        output_format=response_format(visual='professional_project_spec_v2' in prompt) if task=='specification' else {'type':'json_object'}
        bound_input=len((system+prompt+json.dumps(output_format)).encode())+1024
        cap=profile.get('output_cap',12288 if task=='specification' else 8192 if task!='product_review' else 4096)
        if type(cap) is not int or not 1<=cap<=65536:raise ValueError('Invalid profile output budget')
        if bound_input>min(profile.get('context_budget',90000),240000): raise ValueError('Context bound exceeded before provider call')
        bound=Decimal(calculate_cost(model=model,usage={'input':bound_input,'cached_input':0,'output':cap})['cost_amount'])
        with self.lock:
            if task not in TASKS or attempt_id in self.attempted: raise ValueError('Unplanned or repeated provider call')
            if self.spent+self.visual_spent+sum(self.reserved.values(),Decimal(0))+sum(self.visual_reserved.values(),Decimal(0))+bound>self.ceiling: raise ValueError('Owner ceiling would be exceeded')
            if attempt>1 and (task if attempt==2 else task+'@'+str(attempt-1)) not in self.attempted:raise ValueError('Nonsequential recovery call')
            self.attempted.add(attempt_id);self.reserved[attempt_id]=bound;self.facts['calls']+=1
        started=time.time()
        try:
            response=self.client.chat.completions.create(model=model,reasoning_effort=profile.get('reasoning_effort','low'),
                max_completion_tokens=cap,response_format=output_format,
                messages=[{'role':'system','content':system},{'role':'user','content':prompt}])
            u=response.usage
            usage={'input':u.prompt_tokens,'cached_input':getattr(u.prompt_tokens_details,'cached_tokens',0) or 0,'output':u.completion_tokens}
            cost=calculate_cost(model=model,usage=usage)
            fact={**usage,'provider':'openai','model':model,'profile_id':profile['id'],'attempt':attempt,'cost_usd':cost['cost_amount'],
                  'finish_reason':response.choices[0].finish_reason,'input_bound':bound_input,'output_cap':cap,
                  'reserved_cost_usd':str(bound),'elapsed_seconds':round(time.time()-started,3)}
            with self.lock:
                self.facts['per_task'][attempt_id]=fact
                amount=Decimal(cost['cost_amount'])
                if amount>bound: raise ValueError('Provider usage exceeded its reserved bound')
                self.spent+=amount;self.reserved.pop(attempt_id)
            text=response.choices[0].message.content or ''
            if self.sanitize(text)!=text: raise OwnerBlock('Secret scanner rejected provider output','SAFETY_OR_POLICY_BLOCKER')
            if response.choices[0].finish_reason!='stop':
                reason=response.choices[0].finish_reason
                failure=RecoveryFault('Incomplete provider output: '+str(reason),'CAPABILITY_LIMIT' if reason=='length' else 'PROVIDER_HARD_BLOCKER',owner_reason=None if reason=='length' else 'SAFETY_OR_POLICY_BLOCKER')
                failure.partial_output=text
                raise failure
            return text
        except Exception as exc:
            self.facts['failures'][attempt_id]={'exception_class':type(exc).__name__,'reason':self.sanitize(exc),'reserved_cost_usd':str(self.reserved.get(attempt_id,0)),'billing_known':attempt_id in self.facts['per_task'],'ts':time.time()}
            raise

    def reserve_visual(self, attempt_id, bound, visual_ceiling):
        bound=Decimal(str(bound));visual_ceiling=Decimal(str(visual_ceiling))
        if not bound.is_finite() or bound<=0 or not visual_ceiling.is_finite() or visual_ceiling<=0:
            raise ValueError('Invalid visual reservation')
        with self.lock:
            if attempt_id in self.visual_attempted:
                raise ValueError('Repeated visual provider call')
            if self.visual_spent+sum(self.visual_reserved.values(),Decimal(0))+bound>visual_ceiling:
                raise ValueError('Visual owner ceiling would be exceeded')
            if self.spent+self.visual_spent+sum(self.reserved.values(),Decimal(0))+sum(self.visual_reserved.values(),Decimal(0))+bound>self.ceiling:
                raise ValueError('Product owner ceiling would be exceeded')
            self.visual_attempted.add(attempt_id);self.visual_reserved[attempt_id]=bound

    def reconcile_visual(self, attempt_id, amount):
        amount=Decimal(str(amount))
        with self.lock:
            bound=self.visual_reserved[attempt_id]
            if not amount.is_finite() or amount<0 or amount>bound:
                raise ValueError('Visual usage exceeded reserved bound')
            self.visual_spent+=amount;self.visual_reserved.pop(attempt_id)

    def release_visual_before_call(self, attempt_id):
        with self.lock:
            self.visual_reserved.pop(attempt_id,None);self.visual_attempted.discard(attempt_id)
