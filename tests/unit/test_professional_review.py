import copy
import hashlib
import json
from pathlib import Path
import pytest
from redi_lab.workbench.professional.review import requirements, assertion, envelope, evaluate
import importlib.util
loader=importlib.util.spec_from_file_location('review_fixtures',Path(__file__).parents[1]/'fixtures/professional_products.py')
fixtures=importlib.util.module_from_spec(loader);loader.loader.exec_module(fixtures)


def complete(spec,tmp_path):
    path=tmp_path/'observations.json';path.write_text('{}')
    refs={'observations.json':hashlib.sha256(path.read_bytes()).hexdigest()}
    rows=[assertion(key,True,True,passed=True,location='observations.json')
          for key in {k for r in requirements(spec) if r['applicability']!='NOT_APPLICABLE' for k in r['assertion_ids']}]
    return envelope(spec,'wb-source',{'sha256':'fixture'},'test',rows,browser_identity={'test':'fixture'},references=refs)


def test_required_missing_failed_and_not_run_are_not_approved(tmp_path):
    ev=complete(fixtures.business(),tmp_path);assert evaluate(ev,tmp_path)['status']=='VERIFIED'
    key=next(x for x in ev['assertions'] if x['assertion_id']=='startup')
    for status in ('FAIL','NOT_RUN','NOT_APPLICABLE'):
        other=copy.deepcopy(ev);row=next(x for x in other['assertions'] if x['assertion_id']=='startup')
        row.update(status=status,failure_reason='injected missing startup')
        assert evaluate(other,tmp_path)['status']=='FAILED'
    ev['assertions'].remove(key);assert evaluate(ev,tmp_path)['status']=='FAILED'


def test_requirement_and_reference_tampering_fail_closed(tmp_path):
    ev=complete(fixtures.business(),tmp_path);other=copy.deepcopy(ev);other['requirements'].pop()
    with pytest.raises(ValueError,match='Requirement map'):evaluate(other,tmp_path)
    other=copy.deepcopy(ev);other['assertions'][0]['requirement_ids']=[]
    with pytest.raises(ValueError,match='mapping'):evaluate(other,tmp_path)
    (tmp_path/'observations.json').write_text('changed')
    with pytest.raises(ValueError,match='reference changed'):evaluate(ev,tmp_path)


def test_prohibited_delete_is_policy_evidence_not_a_required_delete_ui(tmp_path):
    spec=fixtures.business();spec['business_rules']=[{'kind':'restrict_delete','entity':'member','reason':'Owner requires permanent lending history.'}]
    req=next(x for x in requirements(spec) if x['requirement_id']=='delete:member')
    assert req['assertion_ids']==['delete_policy:member'] and req['spec_reference']=='/business_rules'
    ev=complete(spec,tmp_path);assert evaluate(ev,tmp_path)['status']=='VERIFIED'


def test_operation_requirements_do_not_leak_to_a_referenced_entity():
    spec=fixtures.business()
    spec['acceptance_criteria']=['A user may edit and delete loan records linked to a member.']
    rows={r['requirement_id']:r for r in requirements(spec)}
    assert rows['edit:loan']['applicability']==rows['delete:loan']['applicability']=='REQUIRED'
    assert rows['edit:member']['applicability']==rows['delete:member']['applicability']=='OPTIONAL'
    assert set(rows['acceptance:1']['assertion_ids'])=={'edit:loan','delete:loan','relationships:loan'}


def test_na_is_spec_bound_and_unknown_criterion_is_not_silently_approved(tmp_path):
    spec=fixtures.utility();ev=complete(spec,tmp_path)
    result=evaluate(ev,tmp_path)
    assert next(r for r in result['criteria'] if r['requirement_id']=='persistence')['status']=='NOT_APPLICABLE'
    spec['acceptance_criteria'].append('Produce a certified interplanetary navigation solution.')
    ev=envelope(spec,'source',{'sha256':'x'},'run',[],browser_identity={'test':'fixture'},references={'observations.json':'x'})
    result=evaluate(ev);assert any('unmapped_acceptance:2' in r.get('assertion_ids',[]) for r in result['unresolved'])


def test_internal_launch_does_not_grant_owner_approval_and_keeps_db_outside_app(tmp_path):
    from redi_lab.workbench.professional.quality import prepare_project,trusted_sources
    from redi_lab.workbench.professional.acceptance import AcceptanceLaunch
    from redi_lab.workbench.product_packaging import package_status
    spec=fixtures.business();app=prepare_project(tmp_path/'app',{**trusted_sources(spec),**fixtures.sources(spec)},spec)
    session=AcceptanceLaunch(app,tmp_path/'qa');session.start()
    try:
        assert session.health['database']==str(tmp_path/'qa/acceptance.sqlite3')
        assert session.health['project_path']==str(app)
        assert not (app/'data').exists()
        with pytest.raises(FileNotFoundError):package_status(tmp_path)
    finally:session.stop()
    assert session.launches[0]['shutdown_exit_code'] is not None


def test_acceptance_rejects_misdecoded_or_different_spec_before_launch(tmp_path):
    from redi_lab.workbench.professional.quality import prepare_project,trusted_sources
    from redi_lab.workbench.professional.acceptance import collect
    spec=fixtures.business();spec['primary_goal']='Keep cafés and garages organized — locally.'
    app=prepare_project(tmp_path/'app',{**trusted_sources(spec),**fixtures.sources(spec)},spec)
    changed=copy.deepcopy(spec);changed['primary_goal']='different'
    with pytest.raises(ValueError,match='differs from immutable package'):
        collect(app,changed,'source',tmp_path/'wrong')
    assert not (tmp_path/'wrong/launch').exists()


def test_browser_assertions_reach_medaurus_without_prose_reduction(tmp_path):
    from redi_lab.workbench.professional.quality import prepare_project,trusted_sources
    from redi_lab.workbench.professional.acceptance import collect
    spec=fixtures.business();app=prepare_project(tmp_path/'app',{**trusted_sources(spec),**fixtures.sources(spec)},spec)
    result=collect(app,spec,'fixture',tmp_path/'acceptance')
    assert result['review']['status']=='VERIFIED',result['review']['unresolved']
    assertions=result['evidence']['assertions']
    assert {'labels','focus','keyboard','state:loading','delete:member','delete:loan','edit:member'}<=set(x['assertion_id'] for x in assertions)
    assert all(x['evidence_location'] in result['evidence']['references'] for x in assertions)
    assert all(x['observed'] is not None for x in assertions if x['assertion_id']=='labels')
