"""Recovery proofs use injected workers, real integrity and frozen final gates."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from redi_lab.workbench.professional import pipeline
from redi_lab.workbench.professional.recovery import (
    RecoveryBudget, RecoveryPolicy, ContractMismatch, PlanningDefect, classify, diagnose,
    descendants, validate_plan, VERSION)

loader=importlib.util.spec_from_file_location('recovery_fixtures',Path(__file__).parents[1]/'fixtures/professional_products.py')
fixtures=importlib.util.module_from_spec(loader);loader.loader.exec_module(fixtures)
HIGH={'id':'RECOVERY_HIGH_CAPABILITY','authorized':True,'model':'offline-high-capability',
      'capability_rank':10,'reasoning_effort':'high','output_cap':16384,'context_budget':180000}


class RecoveryProvider(fixtures.FixtureProvider):
    recovery_profiles=[HIGH]
    def __init__(self,spec):super().__init__(spec);self.requests=[]
    def execute(self,task,prompt,*,attempt,profile=None):
        if profile and hasattr(self,'observe'):assert self.observe()['task_states'][task]=='ESCALATED'
        self.requests.append({'task':task,'attempt':attempt,'profile':profile,'prompt':prompt})
        output=self(task,prompt)
        data=json.loads(output)
        if task=='data_model' and attempt==1:
            source=data['patches'][0]['content']
            source=source.replace('def connect(path): return open_database(path, SCHEMA)',
                "def connect(path):\n    conn = open_database(path, SCHEMA)\n    int(conn.execute('SELECT SUM(CASE WHEN id=1 THEN 1 ELSE 0 END) FROM loan').fetchone()[0])\n    return conn")
            data['patches'][0]['content']=source
        if task=='experience' and attempt<=2:
            data['patches'][0]['content']=data['patches'][0]['content'].replace('return render_app(spec,DESIGN,state,**context)',"return {'html': 'wrong return shape'}" if attempt==2 else "return '{} body{font-family:x}'.format(spec['product_name'])")
        return json.dumps(data)


def test_database_template_correction_escalation_and_independent_retention(tmp_path):
    provider=RecoveryProvider(fixtures.business());snapshots=[]
    runner=pipeline.ProfessionalRunner(tmp_path/'wb-012345abcdee','A local lending desk called PulseBudget.',provider,execution_mode='injected_offline')
    provider.observe=runner.snapshot
    runner.progress=lambda event:snapshots.append(copy.deepcopy(runner.snapshot()))
    result=runner.run()
    assert result['verified'],result['detail']
    (tmp_path/'progress-snapshots.json').write_text(json.dumps(snapshots),encoding='utf-8')
    assert result['product_name']=='PulseBudget'
    assert set(result['gate_states'].values())=={'PASS'}
    episodes=result['recovery_episodes']
    assert [e['recovery_action'] for e in episodes]==['SAME_STATION_CORRECTION','SAME_STATION_CORRECTION','CAPABILITY_ESCALATION']
    assert 'NoneType' in episodes[0]['bato_diagnosis']['exact_cause']
    assert provider.calls.count('workflow')==1 and provider.calls.count('data_model')==2
    assert provider.calls.count('experience')==3
    stronger=[r for r in provider.requests if r['profile']]
    assert len(stronger)==1 and stronger[0]['task']=='experience' and stronger[0]['profile']==HIGH
    assert 'TARGETED CORRECTION' in stronger[0]['prompt'] and 'previous_attempts' in stronger[0]['prompt']
    assert any(s['app_state']=='RECOVERING' for s in snapshots)
    assert any(s['task_states'].get('experience')=='ESCALATED' for s in snapshots)
    completed=next(e for e in result['events'] if e['event']=='component_task_completed:ui')
    assert completed['checks']['browser']['populated']['status']=='PASS'
    for episode in episodes:
        saved=json.loads((runner.root/'recovery'/episode['episode_id']/'episode.json').read_text())
        assert saved==episode
    assert len(list((runner.root/'recovery_lessons').glob('*.json')))==2
    assert 'format' in json.loads((runner.root/'artifacts/ui.json').read_text())['content']
    assert (runner.root/'attempts/experience/003/artifact.json').is_file()
    assert result['packaging']['smoke']['status']=='PASS'


@pytest.mark.parametrize('fault',['contract','plan'])
def test_ora_repairs_only_invalid_branch_and_preserves_spec(tmp_path,monkeypatch,fault):
    original=pipeline.build_plan;calls=0
    def injected(spec):
        nonlocal calls
        calls+=1;plan=original(spec)
        if calls==1:
            if fault=='contract':plan['tasks'][0]['input_contract']['database_contract_sha256']='invalid'
            else:plan['tasks'][0]['dependencies']=['domain']
        return plan
    monkeypatch.setattr(pipeline,'build_plan',injected)
    provider=fixtures.FixtureProvider(fixtures.business())
    runner=pipeline.ProfessionalRunner(tmp_path/fault,'Local lending desk',provider,execution_mode='injected_offline')
    result=runner.run()
    assert result['verified'],result['detail']
    episode=result['recovery_episodes'][0]
    assert episode['recovery_action']==('CONTRACT_REPAIR_AND_REPLAY' if fault=='contract' else 'PARTIAL_REPLAN')
    plan=json.loads((runner.root/'recovery/episode-0001/replan.json').read_text())
    assert plan['retained_tasks']==['workflow'] and 'workflow' not in plan['invalidated_tasks']
    assert provider.calls.count('workflow')==1
    assert plan['product_spec_sha256']==result['manifest']['project_spec_sha256']
    validate_plan(result['manifest'])


def test_provider_credits_and_product_ambiguity_require_owner_without_retry(tmp_path):
    class QuotaError(Exception):status_code=429;code='insufficient_quota'
    calls=[]
    def denied(task,prompt):calls.append(task);raise QuotaError('Account credits exhausted')
    result=pipeline.ProfessionalRunner(tmp_path/'credits','Local records',denied,execution_mode='injected_offline').run()
    assert calls==['specification'] and result['status']=='OWNER_ACTION_REQUIRED'
    assert result['owner_action']['reason']=='CREDITS_REQUIRED'
    spec=fixtures.business();spec['uncertainties']=['Who may approve irreversible data deletion?']
    provider=fixtures.FixtureProvider(spec)
    result=pipeline.ProfessionalRunner(tmp_path/'ambiguity','Local records',provider,execution_mode='injected_offline').run()
    assert provider.calls==['specification']
    assert result['owner_action']['reason']=='SPEC_DECISION_REQUIRED'
    assert not result['verified'] and set(result['gate_states'].values())=={'NOT_RUN'}


def test_budget_exhaustion_and_transient_retry_are_bounded(tmp_path):
    class Temporary(Exception):status_code=503
    calls=[]
    def transient(task,prompt):calls.append(task);raise Temporary('Temporary service failure')
    result=pipeline.ProfessionalRunner(tmp_path/'transient','Records',transient,execution_mode='injected_offline',recovery_budget=RecoveryBudget(backoff_seconds=0)).run()
    assert len(calls)==2
    assert result['owner_action']['reason']=='RECOVERY_EXHAUSTED'
    assert result['recovery_episodes'][0]['recovery_action']=='PROVIDER_RETRY'
    result=pipeline.ProfessionalRunner(tmp_path/'call-limit','Records',transient,execution_mode='injected_offline',recovery_budget=RecoveryBudget(max_provider_calls=0)).run()
    assert len(calls)==2 and result['provider_calls']==0


def test_governed_recovery_profile_and_budget_reservation_use_attempt_identity():
    from redi_lab.workbench.professional.provider import GovernedProvider
    from redi_lab.workbench.professional.spec import MODEL
    profile={**HIGH,'model':MODEL};client=fixtures.sdk_client(fixtures.utility());seen=[]
    create=client.chat.completions.create
    def capture(**kwargs):seen.append(kwargs);return create(**kwargs)
    client.chat.completions.create=capture
    provider=GovernedProvider('offline-key','2',lambda:client,recovery_profiles=[profile])
    prompt='Turn the owner request into a spec'
    provider.execute('specification',prompt,attempt=1)
    provider.execute('specification',prompt,attempt=2,profile=profile)
    assert seen[1]['reasoning_effort']=='high' and seen[1]['max_completion_tokens']==16384
    assert provider.facts['per_task']['specification@2']['profile_id']=='RECOVERY_HIGH_CAPABILITY'
    with pytest.raises(ValueError,match='repeated'):provider.execute('specification',prompt,attempt=2,profile=profile)
    with pytest.raises(ValueError,match='Unauthorized'):provider.execute('domain',prompt,attempt=1,profile={**profile,'model':'unconfigured'})
    denied=GovernedProvider('offline-key','0.000001',lambda:client,recovery_profiles=[profile])
    with pytest.raises(ValueError,match='ceiling'):denied.execute('specification',prompt,attempt=1,profile=profile)
    assert denied.facts['calls']==0


def test_versioned_local_repair_only_changes_json_envelope(tmp_path):
    spec=fixtures.utility();provider=fixtures.FixtureProvider(spec)
    runner=pipeline.ProfessionalRunner(tmp_path/'bom','Utility',provider,execution_mode='injected_offline')
    task={'task_id':'specification','persona':'ora','owned_paths':[]}
    raw='\ufeff'+json.dumps(fixtures.wire_spec(spec))
    runner.contexts['specification']={'phase':'json_decode','worker_output':raw,'references':['retained/output.json']}
    got=[]
    runner.recover(task,json.JSONDecodeError('Unexpected BOM',raw,0),lambda:got.append(runner.local_outputs.pop('specification')))
    assert got==[fixtures.wire_spec(spec)] and provider.calls==[]
    episode=runner.episodes[0]
    assert episode['recovery_action']=='LOCAL_REPAIR'
    assert episode['deterministic_repair']['rule']=='json_envelope_bom_v1'
    assert episode['deterministic_repair']['original_hash']!=episode['deterministic_repair']['new_hash']


def test_retained_pulsebudget_bytes_drive_same_recovery_policy(tmp_path):
    from types import ModuleType
    fixture=json.loads((Path(__file__).parents[1]/'fixtures/pulsebudget_retained_failure.json').read_text())
    source=fixture['artifacts'][0]['content'];module=ModuleType('retained');exec(compile(source,'retained/database.py','exec'),module.__dict__)
    connection=module.connect(':memory:')
    try:
        with pytest.raises(TypeError) as original:module.dashboard_counts(connection)
    finally:connection.close()
    provider=fixtures.FixtureProvider(fixtures.business())
    runner=pipeline.ProfessionalRunner(tmp_path/'retained-recovery','PulseBudget regression',provider,execution_mode='retained_offline')
    runner.spec=provider.spec;runner.plan=pipeline.build_plan(runner.spec);runner.tasks={t['task_id']:'PENDING' for t in runner.plan['tasks']}
    task=runner.plan['tasks'][0]
    runner.contexts['data_model']={'phase':'artifact_integrity','worker_output':source,'references':['tests/fixtures/pulsebudget_retained_failure.json']}
    runner.recover(task,original.value,lambda:runner.build_task(task))
    assert runner.tasks['data_model']=='COMPLETED' and runner.episodes[0]['result']=='PASS'
    assert runner.episodes[0]['failure_class']=='DETERMINISTIC_LOCAL_DEFECT'
    assert runner.integrity['data_model']['status']=='PASS'


def test_crew_recovery_is_active_and_historical_failures_stay_failed():
    from redi_lab.workbench.crew import crew_snapshot
    from redi_lab.workbench.professional.spec import BACKEND
    plan=pipeline.build_plan(fixtures.business())
    snap={'backend':BACKEND,'status':'RUNNING','recovery_version':VERSION,'app_state':'RECOVERING','manifest':plan,
          'task_states':{'data_model':'ESCALATED','workflow':'COMPLETED'},'live_events':['planning_started','scope_resolved','component_task_failed:database','recovery_triage_started'],
          'recovery_profiles':{'data_model':HIGH}}
    cards={c['persona']['id']:c for c in crew_snapshot(snap)['crew']}
    assert cards['andinus']['state']=='ESCALATED' and cards['andinus']['worker_model']==HIGH['model']
    assert cards['bato']['state']=='ANALYZING' and cards['bindus']['state']=='VERIFIED'
    old={'backend':BACKEND,'status':'FAILED','live_events':['component_task_started:database','component_task_failed:database']}
    assert next(c for c in crew_snapshot(old)['crew'] if c['persona']['id']=='andinus')['state']=='FAILED'


def test_policy_exhaustion_status_codes_and_elapsed_are_fail_closed(tmp_path):
    from redi_lab.workbench.professional.recovery import RecoveryFault
    class ProviderError(Exception):
        def __init__(self,status,code):self.status_code=status;self.code=code
    for status,code,expected in [(401,None,'CREDENTIAL_REQUIRED'),(403,None,'SAFETY_OR_POLICY_BLOCKER'),(429,None,'MANUAL_APPROVAL_REQUIRED'),(429,'insufficient_quota','CREDITS_REQUIRED')]:
        assert classify(ProviderError(status,code),'provider')==('PROVIDER_HARD_BLOCKER',expected)
    assert classify(TimeoutError(),'provider')[0]=='PROVIDER_TRANSIENT'
    assert classify(ValueError('gate disagrees'),'frozen_gates')[0]=='HARNESS_OR_GATE_DEFECT'
    runner=pipeline.ProfessionalRunner(tmp_path/'exhausted','Records',fixtures.FixtureProvider(fixtures.business()),recovery_profiles=[HIGH])
    runner.spec=fixtures.business();runner.plan=pipeline.build_plan(runner.spec)
    task=runner.plan['tasks'][0];runner.contexts['data_model']={'phase':'artifact_integrity','references':[]}
    def fail():raise TypeError('same null defect')
    from redi_lab.workbench.professional.recovery import OwnerBlock
    with pytest.raises(OwnerBlock):runner.recover(task,TypeError('null defect'),fail)
    assert [e['recovery_action'] for e in runner.episodes]==['SAME_STATION_CORRECTION','CAPABILITY_ESCALATION','OWNER_ACTION_REQUIRED']
    assert runner.owner_action['reason']=='RECOVERY_EXHAUSTED' and runner.owner_action['stronger_profile_used']
    policy=RecoveryPolicy(RecoveryBudget(max_elapsed_seconds=1),[HIGH]);policy.started-=2
    diagnosis=diagnose(TypeError('invalid'),task=task,phase='integrity',evidence={'references':[]},plan=runner.plan,previous=[])
    assert policy.choose(diagnosis,task['worker_profile'])['action']=='OWNER_ACTION_REQUIRED'


def test_new_owner_preflight_and_browser_view_state_contract(tmp_path,monkeypatch):
    from redi_lab.workbench import engine
    from redi_lab.workbench.professional.spec import BACKEND
    import subprocess,shutil
    monkeypatch.setattr(engine,'WORKBENCH_ROOT',tmp_path)
    monkeypatch.delenv('REDI_OPENAI_API_KEY',raising=False);monkeypatch.delenv('REDI_LIVE_PROVIDER',raising=False)
    adapter=engine.WorkbenchAdapter()
    missing_budget=adapter.start_build('Local records',backend=BACKEND,ceiling='2')
    assert 'visual ceiling' in missing_budget['error']
    result=adapter.start_build('Local records',backend=BACKEND,ceiling='2',
                               visual_ceiling='1',max_visual_calls=1,max_visual_recovery=0)
    adapter._builds[result['build_id']]._thread.join(5)
    snap=adapter.snapshot(result['build_id'])
    assert snap['verdict']=='OWNER ACTION REQUIRED' and snap['owner_action']['reason']=='CREDENTIAL_REQUIRED'
    assert snap['provider_calls']==0
    node=shutil.which('node')
    assert node
    module=Path('redi_lab/workbench/static/view-model.js').resolve()
    code='const assert=require("node:assert/strict");const V=require('+json.dumps(str(module))+');'+"""
for(const state of ['RECOVERY','ESCALATED','REPLANNING','ANALYZING','VERIFYING'])assert.equal(V.active(state),true);
assert.equal(V.verdict({status:'OWNER_ACTION_REQUIRED'}),'OWNER ACTION REQUIRED');
assert.equal(V.verdict({status:'RUNNING',app_state:'RECOVERING'}),'RECOVERING');
assert.equal(V.verdict({status:'FAILED',verdict:'APP FAILED'}),'APP FAILED');
assert.equal(V.tone('FAILED_INTERNAL'),'blocked');
"""
    assert subprocess.run([node,'-e',code],capture_output=True,text=True).returncode==0


def test_truncation_is_capability_failure_with_retained_partial_output_and_unknown_billing(tmp_path):
    from redi_lab.workbench.professional.provider import GovernedProvider
    from redi_lab.workbench.professional.spec import MODEL
    profile={**HIGH,'model':MODEL};client=fixtures.sdk_client(fixtures.utility());original=client.chat.completions.create
    calls=[]
    def truncate(**kwargs):
        response=original(**kwargs);calls.append(kwargs)
        response.choices[0].finish_reason='length'
        response.choices[0].message.content='{"schema_version":'
        return response
    client.chat.completions.create=truncate
    provider=GovernedProvider('offline-key','2',lambda:client,recovery_profiles=[profile])
    runner=pipeline.ProfessionalRunner(tmp_path/'truncated','Utility',provider,execution_mode='injected_offline')
    result=runner.run()
    assert len(calls)==2 and result['owner_action']['reason']=='RECOVERY_EXHAUSTED'
    assert result['recovery_episodes'][0]['failure_class']=='CAPABILITY_LIMIT'
    assert result['recovery_episodes'][0]['recovery_action']=='CAPABILITY_ESCALATION'
    failure=json.loads((runner.root/'calls/specification/failure.json').read_text())
    assert failure['partial_output']=='{"schema_version":'
    assert result['recovery_episodes'][0]['cost_delta_is_complete']
    def timeout(**kwargs):raise TimeoutError('Transport timeout; billing unknown')
    client.chat.completions.create=timeout
    provider=GovernedProvider('offline-key','2',lambda:client,recovery_profiles=[profile])
    runner=pipeline.ProfessionalRunner(tmp_path/'unknown-billing','Utility',provider,execution_mode='injected_offline',recovery_budget=RecoveryBudget(backoff_seconds=0))
    result=runner.run()
    episode=result['recovery_episodes'][0]
    assert not episode['cost_delta_is_complete'] and not episode['token_delta_complete']
    from decimal import Decimal
    assert Decimal(episode['unresolved_cost_reservation_usd'])>0 and not result['cost_complete']
