import importlib.util
import json
from pathlib import Path

import pytest

from redi_lab.workbench.professional.spec import validate_spec,build_plan
from redi_lab.workbench.professional.pipeline import ProfessionalRunner
from redi_lab.workbench.professional.contracts import validate_artifact
from redi_lab.workbench.professional.quality import prepare_project,trusted_sources,inspect_product

loader=importlib.util.spec_from_file_location('professional_fixtures',Path(__file__).parents[1]/'fixtures'/'professional_products.py')
fixtures=importlib.util.module_from_spec(loader);loader.loader.exec_module(fixtures)


def test_three_structurally_different_specs_drive_different_dags():
    business,utility,social=(build_plan(validate_spec(f())) for f in (fixtures.business,fixtures.utility,fixtures.social))
    assert [t['task_id'] for t in business['tasks']]==['data_model','workflow','domain','experience','assembly']
    assert [t['task_id'] for t in utility['tasks']]==['domain','experience','assembly']
    assert [t['task_id'] for t in social['tasks']]==['data_model','domain','experience','assembly']
    assert business['tasks'][0]['dependencies']==business['tasks'][1]['dependencies']==[]
    assert business['tasks'][2]['dependencies']==['data_model','workflow']


def test_spec_and_producer_mismatches_fail_before_completion():
    spec=fixtures.business();spec['navigation'].pop()
    with pytest.raises(ValueError,match='reachable'):validate_spec(spec)
    spec=fixtures.business();code=fixtures.sources(spec)['database.py'].replace("'required': True","'required': False",1)
    with pytest.raises(ValueError,match='disagrees'):validate_artifact('database.py',code,spec)
    with pytest.raises(ValueError,match='Unsafe'):validate_artifact('ui.py','def render(): return "body{font-family:x}".format()',spec)


def test_domain_integrity_relationships_money_transitions_and_restart(tmp_path):
    spec=fixtures.business();project=prepare_project(tmp_path/'candidate',{**trusted_sources(spec),**fixtures.sources(spec)},spec)
    report=inspect_product(project,spec,tmp_path/'checks')
    assert report['status']=='PASS'
    assert {'state_transitions','duplicates','deletion_constraints','restart_persistence'}<=set(report['checks'])


def test_full_offline_factory_includes_real_chromium_before_teuta_and_delivery(tmp_path):
    provider=fixtures.FixtureProvider(fixtures.business())
    result=ProfessionalRunner(tmp_path/'wb-abcdef123456','A local lending desk',provider,execution_mode='injected_offline').run()
    assert result['verified'],result['detail']
    assert set(result['gate_states'].values())=={'PASS'}
    ui=next(e for e in result['events'] if e['event']=='component_task_completed:ui')
    assert ui['checks']['browser']['empty']['source']=='real_chromium_dom'
    assert {row['width'] for row in ui['checks']['browser']['populated']['checks']}=={360,390,768,1280,1440}
    assert result['packaging']['smoke']['status']=='PASS'
    assert not (Path(result['project_path'])/'data'/'app.sqlite3').exists()
    assert provider.calls.count('product_review')==0
    assert (tmp_path/'wb-abcdef123456'/'reviewer_inputs.json').is_file()


@pytest.mark.parametrize('factory',[fixtures.utility,fixtures.social])
def test_distinct_product_renderers_use_the_same_quality_infrastructure(tmp_path,factory):
    from redi_lab.workbench.professional.browser import audit
    spec=factory();project=prepare_project(tmp_path/'app',{**trusted_sources(spec),**fixtures.sources(spec)},spec)
    report=inspect_product(project,spec,tmp_path/'integrity')
    browser=audit(project,spec,report['database'],tmp_path/'browser')
    assert browser['status']=='PASS'
    assert all(r['actual_width']==r['width'] for r in browser['checks'])


def test_browser_failure_blocks_teuta_assembly_and_ready(tmp_path,monkeypatch):
    from redi_lab.workbench.professional import pipeline
    def broken(*a,**k):raise ValueError('browser detected overflow')
    monkeypatch.setattr(pipeline,'audit',broken)
    provider=fixtures.FixtureProvider(fixtures.business())
    result=ProfessionalRunner(tmp_path/'wb-failedbrowser','Lending desk',provider,execution_mode='injected_offline').run()
    assert result['task_states']['experience']=='FAILED_INTERNAL'
    assert result['task_states']['assembly']=='PENDING'
    assert not any(e['event']=='component_task_completed:ui' for e in result['events'])
    assert set(result['gate_states'].values())=={'NOT_RUN'}
    assert 'assembly' not in provider.calls and 'product_review' not in provider.calls


def test_professional_workbench_fake_sdk_through_open_app(tmp_path,monkeypatch):
    from redi_lab.workbench import engine
    from redi_lab.workbench.professional.spec import BACKEND
    from redi_lab.workbench.product_packaging import local_request
    monkeypatch.setattr(engine,'WORKBENCH_ROOT',tmp_path)
    adapter=engine.WorkbenchAdapter()
    start=adapter.start_build('A local lending desk',backend=BACKEND,key='offline-key',live_flag='1',ceiling='2',
                              client_factory=lambda:fixtures.sdk_client(fixtures.business()))
    build=adapter._builds[start['build_id']];build._thread.join(90)
    snap=adapter.snapshot(start['build_id'])
    assert snap['verdict']=='APP READY',snap.get('detail')
    assert snap['execution_mode']=='injected_offline' and snap['network_calls']==0
    launch=adapter.app_action(start['build_id'],'open')
    try:
        assert json.loads(local_request(launch['url']+'api/state'))['records']=={'member':[],'loan':[]}
        reloaded=engine.WorkbenchAdapter().snapshot(start['build_id'])
        assert reloaded['can_open_app'] and reloaded['product_name']=='Lending Desk'
    finally:adapter.app_action(start['build_id'],'stop')


def test_real_chromium_rejects_overflowing_generated_markup(tmp_path):
    from redi_lab.workbench.professional.browser import audit
    spec=fixtures.social();sources=fixtures.sources(spec)
    sources['ui.py']=sources['ui.py'].replace('return render_app(', 'return \'<div style="width:2400px">Overflow</div>\' + render_app(')
    project=prepare_project(tmp_path/'app',{**trusted_sources(spec),**sources},spec)
    with pytest.raises(ValueError,match='overflow'):
        audit(project,spec,tmp_path/'empty.sqlite3',tmp_path/'browser')


def test_governed_adapter_reserves_budget_and_forbids_repeated_calls():
    from redi_lab.workbench.professional.provider import GovernedProvider
    client=lambda:fixtures.sdk_client(fixtures.utility())
    denied=GovernedProvider('offline-key','0.000001',client)
    with pytest.raises(ValueError,match='ceiling'):denied('specification','Turn the owner request into a spec')
    assert denied.facts['calls']==0
    provider=GovernedProvider('offline-key','2',client)
    provider('specification','Turn the owner request into a spec')
    with pytest.raises(ValueError,match='repeated'):provider('specification','Turn the owner request into a spec')
    assert provider.facts['calls']==1


def test_utility_calculation_uses_inputs_and_rejects_invalid_values():
    from redi_lab.workbench.professional.runtime import calculate,ValidationError
    contract=fixtures.utility()['calculator']
    assert calculate(contract,{'cost':'40','margin':'20'})=='50.00'
    assert calculate(contract,{'cost':'80','margin':'20'})=='100.00'
    for values in ({'cost':'invalid','margin':'20'},{'cost':'40','margin':'100'}):
        with pytest.raises(ValidationError):calculate(contract,values)


def test_professional_personas_follow_actual_tasks_and_review_events():
    from redi_lab.workbench.crew import crew_snapshot
    from redi_lab.workbench.professional.spec import BACKEND
    snapshot={'backend':BACKEND,'status':'RUNNING','component_map':{'agron':'core','teuta':'ui','bindus':'workflow'},
              'live_events':['planning_started','scope_resolved','artifact_retained:core.py','component_task_started:ui','gate_finished:reviewer:pass']}
    cards={c['persona']['id']:c for c in crew_snapshot(snapshot)['crew']}
    assert cards['andinus']['state']==cards['bindus']['state']==cards['medaurus']['state']=='WAITING'
    assert cards['teuta']['state']=='WORKING'
    snapshot['live_events'] += ['component_task_completed:ui','product_review_started']
    cards={c['persona']['id']:c for c in crew_snapshot(snapshot)['crew']}
    assert cards['teuta']['state']=='VERIFIED' and cards['medaurus']['state']=='REVIEWING'


def test_retained_missing_version_reproduces_and_strict_wire_rejects_before_planning(tmp_path):
    from redi_lab.workbench.professional.spec_schema import decode_spec
    raw=(Path(__file__).parents[1]/'fixtures'/'professional_missing_version.json').read_text(encoding='utf-8')
    with pytest.raises(ValueError,match='unsupported ProjectSpec version'):validate_spec(json.loads(raw))
    with pytest.raises(ValueError,match='missing.*schema_version'):decode_spec(json.loads(raw))
    calls=[]
    def retained(task,prompt):calls.append(task);return raw
    result=ProfessionalRunner(tmp_path/'retained-replay','Recorded specification',retained,execution_mode='retained_offline').run()
    assert calls==['specification','specification'] and not result['verified']
    assert result['status']=='OWNER_ACTION_REQUIRED'
    assert result['manifest'] is None and set(result['gate_states'].values())=={'NOT_RUN'}
    assert json.loads((tmp_path/'retained-replay/calls/specification/output.json').read_text())['content']==raw


def test_specification_sdk_boundary_uses_same_closed_schema_and_no_fallback():
    from redi_lab.workbench.professional.spec_schema import PROJECT_SPEC_SCHEMA,REQUIRED,validate_wire,decode_spec
    from redi_lab.workbench.professional.spec import spec_prompt
    from redi_lab.workbench.professional.provider import GovernedProvider
    assert 'schema_version' in REQUIRED
    def closed(schema):
        if schema.get('type')=='object':
            assert schema['additionalProperties'] is False
            assert set(schema['required'])==set(schema['properties'])
            for child in schema['properties'].values():closed(child)
        if 'items' in schema:closed(schema['items'])
        for child in schema.get('anyOf',[]):closed(child)
    closed(PROJECT_SPEC_SCHEMA)
    captured=[];client=fixtures.sdk_client(fixtures.business());original=client.chat.completions.create
    def create(**kwargs):captured.append(kwargs);return original(**kwargs)
    client.chat.completions.create=create
    provider=GovernedProvider('offline-key','2',lambda:client)
    output=provider('specification',spec_prompt('A lending desk',[]))
    assert captured[0]['response_format']['json_schema']['strict'] is True
    assert captured[0]['response_format']['json_schema']['schema']==PROJECT_SPEC_SCHEMA
    validate_spec(decode_spec(json.loads(output)))
    for make in (fixtures.business,fixtures.utility,fixtures.social):validate_wire(fixtures.wire_spec(make()))
    def unsupported(**kwargs):raise TypeError('strict format unsupported')
    client.chat.completions.create=unsupported
    denied=GovernedProvider('offline-key','2',lambda:client)
    with pytest.raises(TypeError):denied('specification',spec_prompt('A utility',[]))
    assert denied.facts['calls']==1 and 'specification' in denied.facts['failures']


def test_spec_semantics_disambiguate_money_persistence_and_unconditional_deletion():
    from redi_lab.workbench.professional.spec_schema import decode_spec
    spec=fixtures.business();wire=fixtures.wire_spec(spec)
    wire['entities'][1]['fields'][3]['sample']=1550
    with pytest.raises(ValueError,match='wire'):decode_spec(wire)
    spec['persistence']['restart_required']=False
    with pytest.raises(ValueError,match='restart persistence'):validate_spec(spec)
    spec=fixtures.business();spec['business_rules']=[{'kind':'restrict_delete','entity':'member'}]
    with pytest.raises(ValueError,match='source-grounded reason'):validate_spec(spec)
