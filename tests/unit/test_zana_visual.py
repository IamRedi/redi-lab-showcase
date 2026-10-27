"""Zero-spend ZANA station and delivery acceptance."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from redi_lab.workbench.crew import crew_snapshot
from redi_lab.workbench.professional.spec import build_plan, validate_spec
from redi_lab.workbench.professional.spec import spec_prompt
from redi_lab.workbench.professional.spec_schema import VISUAL_VERSION, decode_spec
from redi_lab.workbench.professional.visual import (
    DeterministicFakeVisualWorker, VISUAL_MANIFEST_VERSION, VISUAL_TASK_VERSION,
    VisualFailure, VisualPlacementError, placement_targets, png_dimensions,
    retain_visual, verify_manifest, visual_task,
)
from redi_lab.workbench.professional.pipeline import ProfessionalRunner
from redi_lab.workbench.professional.recovery import diagnose, correction_prompt
from scripts.run_zana_openai_smoke import SmokeRunner, PROMPT

loader = importlib.util.spec_from_file_location('professional_fixtures', Path(__file__).parents[1]/'fixtures'/'professional_products.py')
fixtures = importlib.util.module_from_spec(loader);loader.loader.exec_module(fixtures)


def visual_spec():
    spec=fixtures.social();spec['schema_version']=VISUAL_VERSION
    spec['views'][0]['slots']=[
        {'id':'hero','purpose':'Lead image beside the page introduction','region':'header','accepts_visual':True},
        {'id':'section','purpose':'Secondary illustration below the introduction','region':'content','accepts_visual':True}]
    def req(rid, slot, ratio, dependency):
        return {'id':rid,'purpose':'Abstract decorative '+slot+' visual','asset_type':'decorative_image',
                'aspect_ratio':ratio,'min_width':320,'min_height':180,
                'view_id':'journal','slot_id':slot,'style':'calm geometric color field','subject':'abstract shapes',
                'content_constraints':['no people'],'forbidden_content':['letters','logos'],
                'text_in_image':False,'transparent':False,'variants':1,'format':'png','blocking':True,
                'layout_dependency':dependency}
    spec['visual_requirements']=[req('hero_art','hero',[16,9],'visual_first'),
                                 req('section_art','section',[4,3],'parallel')]
    spec['acceptance_criteria'].append('Both generated visuals appear in the browser.')
    return spec


class VisualFixtureProvider:
    def __init__(self,spec):
        self.spec=decode_spec(fixtures.wire_spec(spec));self.facts={'calls':0,'per_task':{},'failures':{}}
        self.spent='0';self.calls=[]

    def __call__(self,task,prompt):
        self.calls.append(task);self.facts['calls']+=1
        if task=='specification':return json.dumps(fixtures.wire_spec(self.spec))
        name={'data_model':'database.py','workflow':'workflow.py','domain':'core.py',
              'experience':'ui.py','assembly':'service.py'}[task]
        source=fixtures.sources(self.spec)[name]
        if name=='ui.py':
            from redi_lab.workbench.professional.contracts import literal, validate_source
            design=literal(validate_source(name,source),'DESIGN')
            design['visual_placements']={r['view_id']+':'+r['slot_id']:r['id'] for r in self.spec['visual_requirements']}
            source=source.replace('DESIGN = '+repr(literal(validate_source(name,source),'DESIGN')),
                                  'DESIGN = '+repr(design))
        return json.dumps({'patches':[{'rel_path':name,'action':'write','content':source}]})


def independent_review(task,path):
    width,height,_=png_dimensions(Path(path).read_bytes())
    return {'reviewer':'independent_offline_visual_gate','verdict':'PASS',
            'observed_dimensions':[width,height],'scope':'deterministic abstract fixture only'}


def test_visual_spec_dag_and_omission():
    spec=visual_spec();validate_spec(spec)
    plan=build_plan(spec,'run123');tasks={t['task_id']:t for t in plan['tasks']}
    assert tasks['visual_hero_art']['input_contract']['visual_task']['schema_version']==VISUAL_TASK_VERSION
    assert 'visual_hero_art' in tasks['experience']['dependencies']
    assert 'visual_section_art' not in tasks['experience']['dependencies']
    assert {'visual_hero_art','visual_section_art','experience'}<=set(tasks['assembly']['dependencies'])
    assert not any(t['persona']=='zana' for t in build_plan(fixtures.social())['tasks'])
    bad=copy.deepcopy(spec);bad['visual_requirements'][0]['view_id']='missing'
    with pytest.raises(ValueError,match='INVALID_VISUAL_PLACEMENT'):validate_spec(bad)


@pytest.mark.parametrize('mutation,code',[
    (lambda s:s['visual_requirements'][0].update(view_id='missing'),'VIEW_NOT_DECLARED'),
    (lambda s:s['visual_requirements'][0].update(slot_id='missing'),'SLOT_NOT_DECLARED'),
    (lambda s:s['visual_requirements'][0].update(slot_id='other_slot'),'SLOT_BELONGS_TO_OTHER_VIEW'),
    (lambda s:s['visual_requirements'][0].update(slot_id='text_only'),'SLOT_NOT_VISUAL_CAPABLE'),
])
def test_visual_placement_referential_integrity(mutation,code):
    spec=visual_spec()
    spec['views'].append({'id':'about','title':'About','kind':'dashboard',
                          'slots':[{'id':'other_slot','purpose':'About artwork','region':'content','accepts_visual':True}]})
    spec['views'][0]['slots'].append({'id':'text_only','purpose':'Text content','region':'content','accepts_visual':False})
    spec['navigation'].append('about')
    mutation(spec)
    original=copy.deepcopy(spec)
    with pytest.raises(VisualPlacementError) as caught:validate_spec(spec)
    assert caught.value.code==code
    assert {'view_id':'journal','slot_id':'hero','purpose':'Lead image beside the page introduction','region':'header'} in caught.value.valid_targets
    assert spec==original  # no placement is silently chosen


def test_prose_placement_rejected_and_ora_receives_exact_targets():
    spec=visual_spec();req=spec['visual_requirements'][0]
    req['placement']='Journal hero artwork';del req['view_id'];del req['slot_id']
    with pytest.raises(VisualPlacementError) as caught:validate_spec(spec)
    assert caught.value.code=='PROSE_ONLY'
    evidence={'references':['calls/specification/output.json'],'task_contract':{'required_checks':['ProductSpec schema and semantics']},
              'worker_output':json.dumps(spec)}
    task={'task_id':'specification','persona':'ora','owned_paths':[]}
    diagnosis=diagnose(caught.value,task=task,phase='spec_validation',evidence=evidence,plan=None,previous=[])
    prompt=correction_prompt(spec_prompt('Create two illustrations',[],visual=True),diagnosis,evidence,[])
    correction=json.loads(prompt.split('TARGETED CORRECTION:',1)[1].split('\n',1)[1])
    assert correction['required_visual_placement_correction']['valid_targets']==placement_targets(spec)
    assert correction['required_visual_placement_correction']['invalid_reference']['requirement_id']=='hero_art'
    assert 'slots' in prompt and 'view_id' in prompt and 'slot_id' in prompt


def test_repeated_identical_placement_failure_stops_after_one_correction(tmp_path):
    spec=visual_spec();spec['visual_requirements'][0]['slot_id']='missing'
    class InvalidProvider:
        def __init__(self):self.calls=0;self.facts={'calls':0,'per_task':{},'failures':{}};self.spent='0'
        def __call__(self,task,prompt):
            assert task=='specification'
            self.calls+=1;self.facts['calls']+=1
            return json.dumps(fixtures.wire_spec(spec))
    provider=InvalidProvider()
    result=ProfessionalRunner(tmp_path/'wb-invalid-placement','Create two illustrations',provider,
                              execution_mode='injected_offline').run()
    assert result['status']=='OWNER_ACTION_REQUIRED' and provider.calls==2
    assert [e['recovery_action'] for e in result['recovery_episodes']]==['SAME_STATION_CORRECTION','OWNER_ACTION_REQUIRED']
    assert result['recovery_episodes'][1]['bato_diagnosis']['failure_signature']=='INVALID_VISUAL_PLACEMENT:SLOT_NOT_DECLARED'
    correction=json.loads((tmp_path/'wb-invalid-placement/calls/specification/attempt-002/input.json').read_text())['prompt']
    assert 'required_visual_placement_correction' in correction and '"slot_id": "hero"' in correction


def test_retention_dimensions_hash_and_attempts(tmp_path):
    spec=visual_spec();task=visual_task(spec,spec['visual_requirements'][0],'run123')
    worker=DeterministicFakeVisualWorker();output=worker.generate(task)
    row=retain_visual(task,output,tmp_path,independent_review=independent_review)
    assert row['schema_version']==VISUAL_MANIFEST_VERSION and row['actually_used'] is False
    assert verify_manifest(row,tmp_path).read_bytes()==output['bytes']
    assert row['content_sha256']==hashlib.sha256(output['bytes']).hexdigest()
    bad=copy.deepcopy(task);bad['min_width']=100000
    with pytest.raises(VisualFailure,match='minimum'):retain_visual(bad,output,tmp_path,attempt=2,independent_review=independent_review)
    assert (tmp_path/'visual'/task['task_id']/'attempt-002'/'generated.png').is_file()
    with pytest.raises(VisualFailure,match='PNG'):retain_visual(task,{**output,'bytes':b'broken'},tmp_path,attempt=3,independent_review=independent_review)
    assert (tmp_path/'visual'/task['task_id']/'attempt-003'/'generated.png').is_file()
    (tmp_path/row['generated_file_path']).write_bytes(b'changed')
    with pytest.raises(VisualFailure):verify_manifest(row,tmp_path)


def test_review_is_independent_and_worker_interface_is_bounded(tmp_path):
    spec=visual_spec();task=visual_task(spec,spec['visual_requirements'][0],'run123')
    assert 'entities' not in task and 'repository' not in json.dumps(task)
    worker=DeterministicFakeVisualWorker()
    with pytest.raises(VisualFailure,match='Independent'):retain_visual(task,worker.generate(task),tmp_path)
    assert worker.calls==1


def test_workbench_visual_states_and_count():
    plan=build_plan(visual_spec(),'run123');states={'visual_hero_art':'PENDING','visual_section_art':'PENDING'}
    snap={'backend':'openai-live-professional-product-v1','status':'RUNNING','manifest':plan,
          'task_states':states,'events':[{'event':'scope_resolved'}],'live_events':['scope_resolved']}
    def zana():
        crew=crew_snapshot(snap)
        card=next(c for c in crew['crew'] if c['persona']['id']=='zana')
        assert crew['total']==12
        assert crew['active_count']==sum(c['state'] in ('WORKING','TESTING','REVIEWING','VERIFYING','RECOVERY','REPLANNING','ESCALATED','ANALYZING','RECOVERY DECISION') for c in crew['crew'])
        return card
    assert zana()['state']=='WAITING'
    states['visual_hero_art']='RUNNING';assert zana()['state']=='WORKING'
    states['visual_hero_art']='VERIFYING';assert zana()['state']=='VERIFYING'
    states['visual_hero_art']='COMPLETED';states['visual_section_art']='COMPLETED';assert zana()['state']=='VERIFIED'
    states['visual_section_art']='FAILED_INTERNAL';assert zana()['state']=='FAILED'
    snap['owner_action']={'station':'zana'};assert zana()['state']=='BLOCKED'
    assert crew_snapshot({'status':'PENDING','tasks':[],'ledger_feed':[]})['total']==12


def test_zana_card_uses_visual_worker_and_visual_cost():
    plan=build_plan(visual_spec(),'run123')
    snap={'backend':'openai-live-professional-product-v1','status':'RUNNING','manifest':plan,
          'task_states':{'visual_hero_art':'COMPLETED','visual_section_art':'COMPLETED'},
          'active_worker':'text-only-model','provider_calls':7,'cost_usd':'3.75',
          'visual_worker_profile':'visual-profile-v1',
          'visual_assets':[{'model':'image-model-v1','provider':'visual-provider',
                            'worker_profile':'visual-profile-v1'}],
          'visual_facts':{'provider_calls':2,'cost_usd':'0.50','duration_seconds':1.25,
                          'retries':1,'retained_assets':2,'accepted_variants':2,'rejected_variants':1}}
    zana=next(c for c in crew_snapshot(snap)['crew'] if c['persona']['id']=='zana')
    assert zana['worker_model']=='image-model-v1'
    assert zana['technical']['station']=='zana'
    assert zana['technical']['provider']=='visual-provider'
    assert zana['technical']['provider_calls']==2
    assert zana['technical']['cost_usd']=='0.50'
    assert zana['technical']['duration_seconds']==1.25
    assert zana['technical']['tokens'] is None


def test_offline_visual_product_integration_and_real_browser(tmp_path,monkeypatch):
    monkeypatch.setenv('REDI_CHROMIUM_SANDBOX_WORKAROUND','1')
    spec=visual_spec();provider=VisualFixtureProvider(spec);worker=DeterministicFakeVisualWorker()
    result=ProfessionalRunner(tmp_path/'wb-zanaoffline','Create a hero image and secondary illustration for Field Notes',
        provider,execution_mode='injected_offline',visual_worker=worker,visual_reviewer=independent_review).run()
    assert result['verified'],result['detail']
    assert worker.calls==2 and result['visual_facts']['provider_calls']==0
    assert result['visual_facts']['retained_assets']==2 and result['visual_facts']['cost_usd']=='0'
    assert all(row['actually_used'] for row in result['visual_assets'])
    assert {row['asset_id'] for row in result['visual_assets']}=={'hero_art','section_art'}
    assert all(a['status']=='PASS' for a in result['packaging']['browser']['assertions'] if a['assertion_id'].startswith('visual_asset:'))
    assert len(result['packaging']['visual_integration'])==2
    assert {(a['view_id'],a['slot_id']) for a in result['packaging']['visual_integration']}=={('journal','hero'),('journal','section')}
    packaged=json.loads((Path(result['project_path'])/'product_app/visual_asset_handoff.json').read_text())
    assert {(a['view_id'],a['slot_id'],a['placement']) for a in packaged['assets']}=={
        ('journal','hero','journal:hero'),('journal','section','journal:section')}
    assert {(r['view_id'],r['slot_id']) for r in result['visual_assets']}=={('journal','hero'),('journal','section')}
    from redi_lab.workbench.product_packaging import verify_package
    assert verify_package(Path(result['project_path']))==result['packaging']['package_sha256']
    events=[e['event'] for e in result['events']]
    assert 'component_task_started:visual:hero_art' in events and 'component_task_verifying:visual:hero_art' in events
    assert events.index('scope_resolved')<events.index('component_task_started:visual:hero_art')


def test_original_smoke_intent_reaches_zana_offline(tmp_path,monkeypatch):
    monkeypatch.setenv('REDI_CHROMIUM_SANDBOX_WORKAROUND','1')
    spec=visual_spec();spec['visual_requirements'][1]['layout_dependency']='visual_first'
    spec['visual_requirements'][0]['aspect_ratio']=[16,9]
    spec['visual_requirements'][1]['aspect_ratio']=[4,3]
    provider=VisualFixtureProvider(spec);worker=DeterministicFakeVisualWorker()
    result=SmokeRunner(tmp_path/'wb-smoke-offline',PROMPT,provider,execution_mode='injected_offline',
                       visual_worker=worker,visual_reviewer=independent_review).run()
    assert result['verified'],result['detail']
    assert worker.calls==2 and result['visual_facts']['provider_calls']==0
    assert [t['input_contract']['visual_task']['slot_id'] for t in result['manifest']['tasks'] if t['persona']=='zana']==['hero','section']
    assert all(a['status']=='PASS' for a in result['packaging']['browser']['assertions'] if a['assertion_id'].startswith('visual_asset:'))


def test_explicit_variants_have_distinct_retained_identities(tmp_path):
    spec=visual_spec();spec['visual_requirements'][0]['variants']=2
    task=visual_task(spec,spec['visual_requirements'][0],'run123')
    worker=DeterministicFakeVisualWorker()
    first=retain_visual(task,worker.generate(task,variant_index=1),tmp_path,attempt=1,variant_index=1,independent_review=independent_review)
    second=retain_visual(task,worker.generate(task,variant_index=2),tmp_path,attempt=2,variant_index=2,independent_review=independent_review)
    assert worker.calls==2
    assert first['asset_id']=='hero_art' and second['asset_id']=='hero_art_v002'
    assert first['content_sha256']!=second['content_sha256']


def test_visual_recovery_keeps_failed_attempt_and_hash(tmp_path):
    spec=visual_spec();provider=VisualFixtureProvider(spec);fake=DeterministicFakeVisualWorker()
    class Flaky:
        calls=0
        def generate(self,task,*,variant_index=1,failure_evidence=None):
            self.calls+=1
            return {**fake.generate(task,variant_index=variant_index),
                    'bytes':b'broken'} if self.calls==1 else fake.generate(task,variant_index=variant_index)
    worker=Flaky()
    runner=ProfessionalRunner(tmp_path/'wb-recovery','hero image',provider,execution_mode='injected_offline',
                              visual_worker=worker,visual_reviewer=independent_review)
    runner.spec=spec;runner.plan=build_plan(spec,runner.logical_run_id)
    task=next(t for t in runner.plan['tasks'] if t['task_id']=='visual_hero_art')
    runner.tasks={t['task_id']:'PENDING' for t in runner.plan['tasks']}
    with pytest.raises(VisualFailure) as caught:runner.build_visual_task(task)
    failed=tmp_path/'wb-recovery'/'visual'/'visual_hero_art'/'attempt-001'/'generated.png'
    assert failed.read_bytes()==b'broken'
    runner.recover(task,caught.value,lambda:runner.build_visual_task(task))
    assert runner.tasks['visual_hero_art']=='COMPLETED'
    assert worker.calls==2 and runner.episodes[0]['result']=='PASS'
    assert runner.episodes[0]['initial_artifact_hash']==hashlib.sha256(b'broken').hexdigest()
    assert failed.read_bytes()==b'broken'
    assert (tmp_path/'wb-recovery'/'visual'/'visual_hero_art'/'attempt-002'/'generated.png').is_file()


def test_visual_provider_selection_blocks_real_execution(tmp_path):
    spec=visual_spec();provider=VisualFixtureProvider(spec)
    runner=ProfessionalRunner(tmp_path/'wb-ownerblock','hero image',provider,execution_mode='injected_offline')
    runner.spec=spec;runner.plan=build_plan(spec,runner.logical_run_id)
    task=next(t for t in runner.plan['tasks'] if t['task_id']=='visual_hero_art')
    runner.tasks={t['task_id']:'PENDING' for t in runner.plan['tasks']}
    with pytest.raises(Exception) as caught:runner.build_visual_task(task)
    assert 'owner-authorized visual provider' in str(caught.value)
    assert runner.visual_facts()['worker_invocations']==0
    assert runner.tasks['visual_hero_art']=='FAILED_INTERNAL'


def test_unused_retained_visual_fails_browser_gate(tmp_path,monkeypatch):
    monkeypatch.setenv('REDI_CHROMIUM_SANDBOX_WORKAROUND','1')
    from redi_lab.workbench.professional.quality import prepare_project,trusted_sources
    from redi_lab.workbench.professional.browser import audit
    spec=visual_spec();task=visual_task(spec,spec['visual_requirements'][0],'run123')
    worker=DeterministicFakeVisualWorker()
    row=retain_visual(task,worker.generate(task),tmp_path/'run',independent_review=independent_review)
    app=prepare_project(tmp_path/'app',{**trusted_sources(spec),**fixtures.sources(spec)},spec,
                        [{**row,'source_path':str(verify_manifest(row,tmp_path/'run'))}])
    with pytest.raises(ValueError,match='visual_asset:'):
        audit(app,spec,tmp_path/'empty.sqlite3',tmp_path/'browser')


def test_visual_cost_and_failure_taxonomy(tmp_path):
    from redi_lab.workbench.professional.recovery import classify
    assert classify(VisualFailure('provider_unavailable','offline'),'visual_provider')==('PROVIDER_TRANSIENT',None)
    assert classify(VisualFailure('budget_credit','credits'),'visual_provider')==('PROVIDER_HARD_BLOCKER','CREDITS_REQUIRED')
    assert classify(VisualFailure('decode','bad PNG'),'visual_validation')==('WORKER_IMPLEMENTATION_DEFECT',None)
    spec=visual_spec();provider=VisualFixtureProvider(spec);fake=DeterministicFakeVisualWorker()
    class Metered:
        def generate(self,task,*,variant_index=1,failure_evidence=None):
            return {**fake.generate(task,variant_index=variant_index),'cost_usd':'0.25'}
    runner=ProfessionalRunner(tmp_path/'wb-metered','hero image',provider,execution_mode='injected_offline',
                              visual_worker=Metered(),visual_reviewer=independent_review)
    runner.spec=spec;runner.plan=build_plan(spec,runner.logical_run_id)
    runner.tasks={t['task_id']:'PENDING' for t in runner.plan['tasks']}
    task=next(t for t in runner.plan['tasks'] if t['task_id']=='visual_hero_art')
    runner.build_visual_task(task)
    assert runner.visual_facts()['cost_usd']=='0.25'
    assert runner.snapshot()['total_cost_usd']=='0.25'
    assert runner.visual_assets['hero_art']['cost_usd']=='0.25'


def test_optional_visual_failure_does_not_block_product_dag(tmp_path):
    spec=visual_spec();spec['visual_requirements'][0]['blocking']=False
    provider=VisualFixtureProvider(spec)
    runner=ProfessionalRunner(tmp_path/'wb-optional','hero image',provider,execution_mode='injected_offline')
    runner.spec=spec;runner.plan=build_plan(spec,runner.logical_run_id)
    runner.tasks={t['task_id']:'PENDING' for t in runner.plan['tasks']}
    task=next(t for t in runner.plan['tasks'] if t['task_id']=='visual_hero_art')
    runner.build_visual_task(task)
    assert runner.tasks['visual_hero_art']=='COMPLETED' and not runner.visual_assets
    assert runner.visual_facts()['rejected_variants']==1
    zana=next(c for c in crew_snapshot(runner.snapshot())['crew'] if c['persona']['id']=='zana')
    assert zana['state']=='FAILED'
