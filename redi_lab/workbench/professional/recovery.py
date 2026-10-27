"""Versioned, bounded recovery decisions. Evidence only; no hidden reasoning."""
from __future__ import annotations
from dataclasses import dataclass, asdict
from enum import Enum
import hashlib
import json
import math
import time

VERSION = 'autonomous_recovery_v1'
LOCAL_REPAIR_RULES = {'json_envelope_bom_v1':'Remove only a leading Unicode BOM from the JSON envelope; preserve every source byte.'}


class FailureClass(str, Enum):
    DETERMINISTIC_LOCAL_DEFECT = 'DETERMINISTIC_LOCAL_DEFECT'
    WORKER_IMPLEMENTATION_DEFECT = 'WORKER_IMPLEMENTATION_DEFECT'
    CONTRACT_MISMATCH = 'CONTRACT_MISMATCH'
    PLANNING_DEFECT = 'PLANNING_DEFECT'
    CAPABILITY_LIMIT = 'CAPABILITY_LIMIT'
    PROVIDER_TRANSIENT = 'PROVIDER_TRANSIENT'
    PROVIDER_HARD_BLOCKER = 'PROVIDER_HARD_BLOCKER'
    OWNER_DECISION_REQUIRED = 'OWNER_DECISION_REQUIRED'
    EXTERNAL_DEPENDENCY_BLOCKER = 'EXTERNAL_DEPENDENCY_BLOCKER'
    HARNESS_OR_GATE_DEFECT = 'HARNESS_OR_GATE_DEFECT'


class RecoveryFault(ValueError):
    def __init__(self, message, failure_class, *, owner_reason=None):
        super().__init__(message)
        self.failure_class = failure_class
        self.owner_reason = owner_reason


class ContractMismatch(RecoveryFault):
    def __init__(self, message): super().__init__(message, 'CONTRACT_MISMATCH')


class PlanningDefect(RecoveryFault):
    def __init__(self, message): super().__init__(message, 'PLANNING_DEFECT')


class OwnerBlock(RecoveryFault):
    def __init__(self, message, reason='SPEC_DECISION_REQUIRED'):
        super().__init__(message, 'EXTERNAL_DEPENDENCY_BLOCKER' if reason=='EXTERNAL_SERVICE_REQUIRED' else 'OWNER_DECISION_REQUIRED', owner_reason=reason)


@dataclass(frozen=True)
class RecoveryBudget:
    same_station: int = 1
    escalation: int = 1
    replan: int = 1
    contract_repair: int = 1
    local_repair: int = 1
    provider_retry: int = 1
    max_provider_calls: int = 20
    max_elapsed_seconds: float | None = 1200
    backoff_seconds: float = 1

    def __post_init__(self):
        for name in ('same_station','escalation','replan','contract_repair','local_repair','provider_retry','max_provider_calls'):
            if type(getattr(self,name)) is not int or getattr(self,name)<0: raise ValueError('Invalid recovery limit: '+name)
        if self.max_elapsed_seconds is not None and (self.max_elapsed_seconds<=0 or not math.isfinite(self.max_elapsed_seconds)): raise ValueError('Invalid elapsed ceiling')
        if not 0<=self.backoff_seconds<=60: raise ValueError('Invalid retry backoff')


def digest(value):
    return hashlib.sha256((value if isinstance(value,str) else json.dumps(value,sort_keys=True)).encode()).hexdigest()


def descendants(plan, seeds):
    result=set(seeds)
    while True:
        extended=result|{t['task_id'] for t in plan['tasks'] if set(t['dependencies'])&result}
        if extended==result:return sorted(result-set(seeds))
        result=extended


def validate_plan(plan):
    tasks=plan['tasks'];ids=[t['task_id'] for t in tasks]
    if len(ids)!=len(set(ids)):raise PlanningDefect('Duplicate task IDs')
    done=set()
    while len(done)<len(ids):
        ready={t['task_id'] for t in tasks if t['task_id'] not in done and set(t['dependencies'])<=done}
        if not ready:raise PlanningDefect('Cyclic or missing dependencies')
        done.update(ready)


def classify(exc, phase):
    """Provider status/code has precedence; generated exception prose is evidence, not policy."""
    if isinstance(exc,RecoveryFault):return exc.failure_class,exc.owner_reason
    code=getattr(exc,'code',None)
    body=getattr(exc,'body',None)
    if isinstance(body,dict):code=code or body.get('code') or (body.get('error') or {}).get('code')
    status=getattr(exc,'status_code',None)
    if code in ('insufficient_quota','billing_hard_limit_reached','credits_exhausted'):
        return 'PROVIDER_HARD_BLOCKER','CREDITS_REQUIRED'
    if status==401 or code=='invalid_api_key':return 'PROVIDER_HARD_BLOCKER','CREDENTIAL_REQUIRED'
    if status==403 or code in ('account_deactivated','permission_denied','provider_disabled'):
        return 'PROVIDER_HARD_BLOCKER','SAFETY_OR_POLICY_BLOCKER'
    if phase=='provider':
        if isinstance(exc,TimeoutError) or type(exc).__name__ in ('APITimeoutError','APIConnectionError') or (status is not None and 500<=status<=599) or (status==429 and code in ('rate_limit_exceeded','requests','tokens')):
            return 'PROVIDER_TRANSIENT',None
        # An unclassified 429 may be quota exhaustion; do not spend on a guess.
        return 'PROVIDER_HARD_BLOCKER','MANUAL_APPROVAL_REQUIRED'
    if phase in ('frozen_gates','packaging','product_review','harness'):
        return 'HARNESS_OR_GATE_DEFECT','MANUAL_APPROVAL_REQUIRED'
    if isinstance(exc,(SyntaxError,ImportError,KeyError,TypeError,json.JSONDecodeError)) or any(x in str(exc) for x in ('TypeError:', 'KeyError:', 'SyntaxError:', 'ImportError:')):
        return 'DETERMINISTIC_LOCAL_DEFECT',None
    return 'WORKER_IMPLEMENTATION_DEFECT',None


def diagnose(exc, *, task, phase, evidence, plan, previous):
    failure_class,reason=classify(exc,phase)
    tid=task['task_id']
    signature=getattr(exc,'failure_signature',None)
    repeated_structure=bool(signature and any(row.get('failure_signature')==signature for row in previous))
    if repeated_structure:reason='RECOVERY_EXHAUSTED'
    local=phase=='json_decode' and (evidence.get('worker_output') or '').startswith('\ufeff')
    action=('OWNER_ACTION_REQUIRED' if repeated_structure else 'LOCAL_REPAIR' if local else 'CONTRACT_REPAIR_AND_REPLAY' if failure_class=='CONTRACT_MISMATCH' else
            'PARTIAL_REPLAN' if failure_class=='PLANNING_DEFECT' else 'CAPABILITY_ESCALATION' if failure_class=='CAPABILITY_LIMIT' else
            'PROVIDER_RETRY' if failure_class=='PROVIDER_TRANSIENT' else 'OWNER_ACTION_REQUIRED' if reason else 'SAME_STATION_CORRECTION')
    return {'schema_version':'recovery_diagnosis_v1','failure_class':failure_class,
            'visual_failure_kind':getattr(exc,'kind',None),'failing_station':task['persona'],
            'task_id':tid,'failing_artifact':next(iter(task.get('owned_paths',[])),None),'exact_cause':str(exc),
            'confidence':1.0 if isinstance(exc,RecoveryFault) or local else .9,'deterministic_fix_possible':local,
            'same_worker_retry_reasonable':failure_class in ('DETERMINISTIC_LOCAL_DEFECT','WORKER_IMPLEMENTATION_DEFECT'),
            'contract_change_required':failure_class=='CONTRACT_MISMATCH','replan_required':failure_class=='PLANNING_DEFECT',
            'stronger_worker_recommended':failure_class=='CAPABILITY_LIMIT' or bool(previous),
            'impacted_downstream_tasks':descendants(plan,[tid]) if plan else [],'suggested_recovery_action':action,
            'owner_required':bool(reason),'owner_reason':reason,'evidence_references':evidence['references'],
            'failed_check':phase,'previous_attempts':previous,
            'failure_signature':signature,
            'structural_failure':({'code':getattr(exc,'code'),'requirement_id':getattr(exc,'requirement_id'),
                                   'view_id':getattr(exc,'view_id'),'slot_id':getattr(exc,'slot_id'),
                                   'valid_targets':getattr(exc,'valid_targets')}
                                  if signature else None)}


class RecoveryPolicy:
    def __init__(self,budget=None,profiles=()):
        self.budget=budget or RecoveryBudget();self.used={};self.started=time.monotonic()
        self.profiles=[dict(p) for p in profiles if p.get('authorized') is True and p.get('id')=='RECOVERY_HIGH_CAPABILITY']
        for p in self.profiles:
            if not p.get('model') or not isinstance(p.get('capability_rank'),int) or p.get('output_cap',0)<=0:raise ValueError('Invalid authorized recovery profile')

    def choose(self,diagnosis,initial_profile):
        tid=diagnosis['task_id'];used=self.used.setdefault(tid,{})
        action=diagnosis['suggested_recovery_action'];reason=diagnosis['owner_reason'];profile=None
        mapping={'LOCAL_REPAIR':'local_repair','SAME_STATION_CORRECTION':'same_station','CONTRACT_REPAIR_AND_REPLAY':'contract_repair','PARTIAL_REPLAN':'replan','CAPABILITY_ESCALATION':'escalation','PROVIDER_RETRY':'provider_retry'}
        if action=='SAME_STATION_CORRECTION' and used.get('same_station',0)>=self.budget.same_station:action='CAPABILITY_ESCALATION'
        if action=='CAPABILITY_ESCALATION':
            suitable=[p for p in self.profiles if p['capability_rank']>initial_profile.get('capability_rank',0)]
            if suitable:profile=max(suitable,key=lambda p:p['capability_rank'])
            else:action='OWNER_ACTION_REQUIRED';reason='MANUAL_APPROVAL_REQUIRED'
        key=mapping.get(action)
        if key and used.get(key,0)>=getattr(self.budget,key):action='OWNER_ACTION_REQUIRED';reason='RECOVERY_EXHAUSTED'
        if self.budget.max_elapsed_seconds is not None and time.monotonic()-self.started>=self.budget.max_elapsed_seconds:
            action='OWNER_ACTION_REQUIRED';reason='RECOVERY_EXHAUSTED'
        if action!='OWNER_ACTION_REQUIRED':used[key]=used.get(key,0)+1
        return {'schema_version':'recovery_decision_v1','authority':'zojz','action':action,'owner_reason':reason,
                'profile':profile,'reason':diagnosis['exact_cause'],'budget_used':dict(used),'budget':asdict(self.budget)}


def correction_prompt(original, diagnosis, evidence, previous):
    context={'diagnosis':diagnosis,'failed_check':diagnosis['failed_check'],'actual':diagnosis['exact_cause'],
             'expected':evidence['task_contract'].get('required_checks',[]),'failed_artifact':evidence.get('worker_output'),
             'previous_attempts':previous,'evidence':evidence}
    if diagnosis.get('structural_failure'):
        context['required_visual_placement_correction']={
            'code':'INVALID_VISUAL_PLACEMENT',
            'invalid_reference':{key:diagnosis['structural_failure'][key] for key in ('requirement_id','view_id','slot_id')},
            'valid_targets':diagnosis['structural_failure']['valid_targets'],
            'instruction':'Copy one exact view_id and slot_id pair from valid_targets. Preserve owner intent; if multiple consequential choices remain, record an uncertainty instead of guessing.'}
    return original+'\nTARGETED CORRECTION: repair this specific defect while preserving valid existing behavior. Keep ownership and ProductSpec unchanged.\n'+json.dumps(context,ensure_ascii=False)


def bounded_evidence(spec,task,upstream,output,refs):
    value={'product_spec':spec,'task_contract':task,'producer_inputs':upstream,'expected_outputs':task.get('output_contract'),
           'worker_output':output,'references':refs}
    # Fail closed rather than silently truncating the artifact or its binding contract.
    if len(json.dumps(value,ensure_ascii=False).encode())>240000:raise OwnerBlock('Recovery evidence exceeds bounded context; inspect retained references','MANUAL_APPROVAL_REQUIRED')
    return value


def owner_action(diagnosis,decision,episodes):
    reason=decision.get('owner_reason') or 'RECOVERY_EXHAUSTED'
    instructions={'CREDENTIAL_REQUIRED':'Configure a valid provider credential.', 'CREDITS_REQUIRED':'Restore provider credits.',
        'SPEC_DECISION_REQUIRED':'Resolve the recorded ProductSpec uncertainty.', 'EXTERNAL_SERVICE_REQUIRED':'Provide the required external service.',
        'UNSUPPORTED_CAPABILITY':'Choose a supported capability or revise the requested scope.',
        'RECOVERY_EXHAUSTED':'Review the attempted repairs and authorize a revised recovery budget or implementation approach.',
        'SAFETY_OR_POLICY_BLOCKER':'Resolve the recorded permission or policy restriction.',
        'MANUAL_APPROVAL_REQUIRED':'Review the recorded blocker and authorize the needed configuration or evaluator repair.'}
    return {'reason':reason,'station':diagnosis['failing_station'],'cause':diagnosis['exact_cause'],
            'attempted':[{'episode_id':e['episode_id'],'action':e['recovery_action'],'result':e['result']} for e in episodes],
            'stronger_profile_used':any(e['recovery_action']=='CAPABILITY_ESCALATION' for e in episodes),
            'evidence':diagnosis['evidence_references'],'decision_needed':instructions[reason]}


def lesson(episode,scope):
    return {'schema_version':'recovery_lesson_candidate_v1','episode_id':episode['episode_id'],
            'evidence':[episode['episode_id']], 'scope':scope,'confidence':.5,
            'proposed_policy_change':'Evaluate earlier integrity or tighter task-class contracts for '+episode['failure_class'],
            'expected_benefit':'Avoid the same diagnosed defect in this task class.',
            'risk':'One recovery is insufficient evidence for broad worker selection changes.',
            'rollback':'No production policy changed; discard candidate.', 'promotion_status':'CANDIDATE_ONLY',
            'repeated_evidence_required':True}
