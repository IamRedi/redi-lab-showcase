"""Offline contract regressions grounded in the immutable failed run."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from redi_lab.workbench.professional import runtime
from redi_lab.workbench.professional.spec import validate_spec, build_plan
from redi_lab.workbench.professional.quality import prepare_project, trusted_sources, inspect_product
from redi_lab.workbench.professional.contracts import task_prompt

FIXTURES=Path(__file__).parents[1]/'fixtures'
loader=importlib.util.spec_from_file_location('pulsebudget_fixtures',FIXTURES/'professional_products.py')
fixtures=importlib.util.module_from_spec(loader);loader.loader.exec_module(fixtures)


def finance():
    """Owner-described shape, manually authored offline; not a generated ProductSpec."""
    s=fixtures.base('PulseBudget','business_workflow')
    def f(name,kind,sample,required=True,**extra):
        return dict(id=name,label=name.replace('_',' ').title(),type=kind,sample=sample,required=required,**extra)
    s['entities']=[
        dict(id='category',label='Category',plural='Categories',display_field='name',fields=[f('name','text','General',unique=True)]),
        dict(id='transaction',label='Transaction',plural='Transactions',display_field='title',fields=[
            f('title','text','Coffee'),f('date','date','2026-09-01'),f('amount','money','0.10'),
            f('direction','enum','EXPENSE',choices=['INCOME','EXPENSE']),f('category_id','reference',1,references='category'),
            f('note','long_text','',False,default=''),f('sequence','integer',0,False),
            f('cleared','boolean',False,default=False)]),
        dict(id='budget',label='Budget',plural='Budgets',display_field='month',fields=[
            f('month','date','2026-09-01'),f('category_id','reference',1,references='category'),f('amount','money','100.00',default='0.00')])]
    s['views']=[dict(id='dashboard',title='Dashboard',kind='dashboard')]+[
        dict(id=e['plural'].lower(),title=e['plural'],kind='table',entity=e['id']) for e in s['entities']]
    s['navigation']=[v['id'] for v in s['views']]
    s['metrics']=[dict(id='expense',label='Expenses',entity='transaction',operation='sum',field='amount',where={'field':'direction','values':['EXPENSE']}),
                  dict(id='income',label='Income',entity='transaction',operation='sum',field='amount',where={'field':'direction','values':['INCOME']})]
    return s


def crm():
    s=fixtures.business();s['product_name']='Mini CRM'
    return s


def utility():
    s=fixtures.social();s['product_name']='Simple Notes';s['product_type']='utility'
    return s


def retained(): return json.loads((FIXTURES/'pulsebudget_retained_failure.json').read_text())


def test_exact_retained_null_aggregate_and_clear_precompletion_rejection(tmp_path):
    from redi_lab.workbench.product_integrity import _exercise, IntegrityError
    from types import ModuleType
    source=retained()['artifacts'][0]
    assert hashlib.sha256(source['content'].encode()).hexdigest()==source['stored_sha256']
    module=ModuleType('retained_database');exec(compile(source['content'],'retained/database.py','exec'),module.__dict__)
    conn=module.connect(':memory:')
    try:
        assert conn.execute("SELECT SUM(CASE WHEN status='NEW' THEN 1 ELSE 0 END) FROM jobs").fetchone()[0] is None
        with pytest.raises(TypeError,match="not 'NoneType'"):module.dashboard_counts(conn)
    finally:conn.close()
    with pytest.raises(IntegrityError,match=r'database.dashboard_counts\(empty database\).*SQL NULL'):
        _exercise({'database':module},'build_database')


def test_wrong_legacy_scope_blocks_before_any_provider_and_retains_request(tmp_path,monkeypatch):
    from redi_lab.workbench import engine,normal_app
    monkeypatch.setattr(engine,'WORKBENCH_ROOT',tmp_path)
    def forbidden(*a,**k):raise AssertionError('provider dispatch must not happen')
    monkeypatch.setattr(normal_app,'build_live_call',forbidden)
    adapter=engine.WorkbenchAdapter()
    bid=adapter.start_build(retained()['prompt'],backend=normal_app.BACKEND_ID,key='offline-key',live_flag='1',ceiling='1')['build_id']
    adapter._builds[bid]._thread.join(5)
    evidence=json.loads((tmp_path/('wb-'+bid)/'live_run_evidence.json').read_text())
    assert evidence['status']=='BLOCKED' and evidence['call_facts']['calls']==0
    assert evidence['prompt']==retained()['prompt'] and 'Professional product factory' in evidence['detail']
    assert set(evidence['gate_states'].values())=={'NOT_RUN'}


@pytest.mark.parametrize('make',[crm,finance,utility],ids=['crm_relationships','finance','utility'])
def test_same_database_contract_and_precompletion_checks_for_unrelated_specs(tmp_path,make):
    spec=validate_spec(make());sources=fixtures.sources(spec)
    project=prepare_project(tmp_path/'candidate',{**trusted_sources(spec),'database.py':sources['database.py']},spec)
    report=inspect_product(project,spec,tmp_path/'checks',stage='data_model')
    assert report['status']=='PASS'
    assert {'empty_aggregate_zero','required_missing_null','nullable_round_trip','declared_defaults',
            'exact_money_round_trip','restart_persistence','delete_where_allowed'}<=set(report['checks'])
    contract=runtime.database_contract(spec)
    assert contract['version']=='product_fields_v2'
    for task in build_plan(spec)['tasks']:
        prompt=task_prompt(task,spec,{})
        assert 'EXECUTABLE DATABASE CONTRACT' in prompt and 'integer minor units' in prompt
    for entity in contract['entities']:
        for field in entity['fields']:
            assert {'type','required','nullable','has_default','default','validation','serialization','storage'}<=field.keys()


def test_finance_exact_money_null_defaults_dates_and_atomic_mutations(tmp_path):
    spec=finance();app=runtime.DataService(str(tmp_path/'finance.sqlite3'),spec)
    try:
        assert app.state()['metrics']=={'expense':'0.00','income':'0.00'}
        ids=runtime.sample_records(app);rid=ids['transaction'];cat=ids['category']
        assert app.record('transaction',rid)['cleared'] is False
        app.save('transaction',{'sequence':None,'note':None},rid)
        assert app.record('transaction',rid)['sequence'] is None
        app.save('transaction',{'amount':'0.20'},rid)
        values={'title':'Tea','date':'2026-09-02','amount':'0.10','direction':'EXPENSE','category_id':cat}
        second=app.save('transaction',values)
        assert app.record('transaction',second)['note']=='' and app.record('transaction',second)['cleared'] is False
        assert app.state()['metrics']['expense']=='0.30'
        assert len(app.records('transaction',filters={'category_id':cat,'direction':'EXPENSE'},month='2026-09',date_field='date'))==2
        assert app.records('transaction',month='2026-08',date_field='date')==[]
        assert [r['id'] for r in app.records('transaction',search='Tea')]==[second]
        for bad in (None,'',0.1,'NaN','1.001','92233720368547758.08'):
            before=app.state()
            with pytest.raises(runtime.ValidationError):app.save('transaction',{'amount':bad},rid)
            assert app.state()==before
        for bad in (None,0,False,'unknown'):
            with pytest.raises(runtime.ValidationError,match='positive record ID'):app.record('transaction',bad)
        with pytest.raises(runtime.ValidationError):app.save('transaction',{'date':'2026-02-30'},rid)
        app.save('transaction',{'amount':'0.00','sequence':0},rid)
        assert app.record('transaction',rid)['sequence']==0
        app.delete('transaction',second);assert app.state()['metrics']['expense']=='0.00'
        expected=app.state()
    finally:app.close()
    app=runtime.DataService(str(tmp_path/'finance.sqlite3'),spec)
    try:assert app.state()==expected
    finally:app.close()


def test_product_names_are_build_local_and_spec_authoritative(tmp_path):
    from redi_lab.workbench.presentation import present_snapshot
    cases=[('111111111111','PulseBudget'),('222222222222','PitlaneOne Lite'),('333333333333','Mini CRM')]
    for bid,name in cases:
        directory=tmp_path/('wb-'+bid);directory.mkdir()
        (directory/'product_spec.json').write_text(json.dumps({'spec':{'product_name':name}}))
    for bid,name in [*cases,*reversed(cases)]:
        snap={'build_id':bid,'product_name':'mini crm job manager','manifest':{'product_key':'mini_crm_job_manager'}}
        assert present_snapshot(snap,tmp_path)['product_name']==name
    snap={'build_id':'444444444444','prompt':retained()['prompt'],'manifest':{'product_key':'mini_crm_job_manager'}}
    assert present_snapshot(snap,tmp_path)['product_name']=='PulseBudget'
    snap['product_name']='mini crm job manager'
    assert present_snapshot(snap,tmp_path)['product_name']=='PulseBudget'
    snap.pop('product_name')
    snap.pop('prompt')
    assert present_snapshot(snap,tmp_path)['product_name'] is None


def test_invalid_schema_and_defaults_fail_before_station_generation():
    s=finance();s['entities'][1]['fields'][2]['default']=None
    with pytest.raises(runtime.ValidationError,match='required'):validate_spec(s)
    s=finance();s['entities'][1]['fields'][2]['sample']=0.1
    with pytest.raises(runtime.ValidationError,match='exact decimal'):validate_spec(s)


def test_money_metrics_through_real_browser_frozen_factory(tmp_path):
    from redi_lab.workbench.professional.pipeline import ProfessionalRunner
    spec=finance();spec['product_name']='Incorrect legacy title';provider=fixtures.FixtureProvider(spec)
    result=ProfessionalRunner(tmp_path/'finance-offline','Build a local finance app called PulseBudget.',provider,execution_mode='injected_offline').run()
    assert result['verified'],result['detail']
    assert set(result['gate_states'].values())=={'PASS'}
    assert result['integrity']['data_model']['status']=='PASS'
    assert result['product_name']=='PulseBudget'
    assert json.loads((tmp_path/'finance-offline/app/package.json').read_text())['product_name']=='PulseBudget'
    assert (tmp_path/'finance-offline/app/README.md').read_text().startswith('# PulseBudget')
    raw=json.loads((tmp_path/'finance-offline/calls/specification/output.json').read_text(encoding='utf-8'))['content']
    assert json.loads(raw)['product_name']=='Incorrect legacy title', 'Retained provider output must not be rewritten'


def test_failed_database_probe_never_verifies_andinus_or_starts_consumers(tmp_path):
    from redi_lab.workbench.professional.pipeline import ProfessionalRunner
    provider=fixtures.FixtureProvider(finance())
    def broken(task,prompt):
        output=provider(task,prompt)
        return output.replace('return open_database(path, SCHEMA)','return None') if task=='data_model' else output
    result=ProfessionalRunner(tmp_path/'bad-connector','Finance records',broken,execution_mode='injected_offline').run()
    assert result['task_states']['data_model']=='FAILED_INTERNAL'
    assert 'domain' not in provider.calls and set(result['gate_states'].values())=={'NOT_RUN'}
    assert not any(e['event']=='component_task_completed:database' for e in result['events'])
