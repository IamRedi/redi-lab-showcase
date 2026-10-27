"""Forward-only retained qualification. Historical builds are never promoted."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import time
from ...qualification_executor import materialize
from ..normal_app_gates import evaluate_normal_app
from ..product_packaging import verify_package
from .acceptance import collect
from .review import evaluate, REVIEWER_VERSION, VERSION

VERSION_QUALIFICATION='professional_v1_retained_candidate_qualification_v2'


def write_new(path,value):
    with Path(path).open('x',encoding='utf-8') as stream:json.dump(value,stream,indent=2,ensure_ascii=False)


def prepare(source,destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    original=json.loads((source/'live_run_evidence.json').read_text(encoding='utf-8'))
    if original['backend']!='openai-live-professional-product-v1' or set(original['gate_states'].values())!={'PASS'}:
        raise ValueError('Retained source must have passed all frozen gates')
    identity=verify_package(source/'app');manifest=json.loads((source/'app/package.json').read_text(encoding='utf-8'))
    if identity!=original['packaging']['package_sha256']:raise ValueError('Retained package changed')
    destination.mkdir(parents=True,exist_ok=False);app=destination/'app';app.mkdir()
    for name in [*manifest['files'],'package.json']:
        src=(source/'app'/name).resolve();target=(app/name).resolve()
        if not src.is_relative_to(source/'app') or not target.is_relative_to(app):raise ValueError('Invalid artifact path')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(src.read_bytes())
    assert verify_package(app)==identity
    record={'schema_version':VERSION_QUALIFICATION,'qualification_id':destination.name,
        'source_build_id':source.name,'source_path':str(source),'original_result':original['status'],
        'original_historical_verdict':'NOT ACCEPTED' if original['status']!='VERIFIED' else 'ACCEPTED',
        'original_reviewer_version':'medaurus_prose_product_review_v1',
        'reviewer_version':REVIEWER_VERSION,'evidence_schema_version':VERSION,
        'source_evidence_sha256':hashlib.sha256((source/'live_run_evidence.json').read_bytes()).hexdigest(),
        'artifact_identity':{'package_sha256':identity,'files':manifest['files']},'provider_calls':0}
    write_new(destination/'qualification_origin.json',record)
    spec=original['product_spec'];sources={p.name:p.read_text(encoding='utf-8') for p in (app/'product_app').glob('*.py')}
    contract=json.loads((source/'frozen_gate_contract.json').read_text(encoding='utf-8'))
    retained=materialize(product_key='professional_product',arm='retained_qualification_offline',model='retained',patches=sources)
    gates=evaluate_normal_app(retained,contract['product'],{'required_files':list(sources)},ws_root=destination/'gates')
    write_new(destination/'frozen_gate_result.json',gates)
    if not gates['all_mandatory_gates_pass']:raise ValueError('Retained qualification frozen gates failed')
    result=collect(app,spec,source.name,destination/'acceptance')
    return {'origin':record,'review':result['review'],'path':str(destination)}


def finalize(root,manual_evidence,*,acceptance_directory='acceptance'):
    """Release only the new qualification after both evidence paths pass."""
    root=Path(root).resolve();origin=json.loads((root/'qualification_origin.json').read_text(encoding='utf-8'))
    source=Path(origin['source_path']);original=json.loads((source/'live_run_evidence.json').read_text(encoding='utf-8'))
    if hashlib.sha256((source/'live_run_evidence.json').read_bytes()).hexdigest()!=origin['source_evidence_sha256']:
        raise ValueError('Historical evidence changed')
    if verify_package(source/'app')!=verify_package(root/'app') or verify_package(root/'app')!=origin['artifact_identity']['package_sha256']:
        raise ValueError('Candidate artifacts changed')
    acceptance=(root/acceptance_directory).resolve()
    if not acceptance.is_relative_to(root):raise ValueError('Acceptance must be inside qualification')
    ev=json.loads((acceptance/'review_evidence.json').read_text(encoding='utf-8'));review=evaluate(ev,acceptance)
    if review['status']!='VERIFIED':raise ValueError('Medaurus has unresolved required evidence')
    required={'browser_e2e','edit_delete_policy','restart_persistence','keyboard','mobile','validation','quality'}
    if set(manual_evidence.get('checks',{}))!=required or any(v!='PASS' for v in manual_evidence['checks'].values()):
        raise ValueError('Manual candidate acceptance incomplete')
    if manual_evidence.get('package_sha256')!=origin['artifact_identity']['package_sha256'] or not manual_evidence.get('evidence_references'):
        raise ValueError('Manual acceptance identity or references missing')
    for name,sha in manual_evidence['evidence_references'].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise ValueError('Manual evidence changed')
    gates=json.loads((root/'frozen_gate_result.json').read_text(encoding='utf-8'))
    if not gates['all_mandatory_gates_pass']:raise ValueError('Qualification gates failed')
    write_new(root/'manual_acceptance.json',manual_evidence)
    write_new(root/'qualification.json',{**origin,'verdict':'ACCEPTED','medaurus':review,'acceptance_directory':acceptance_directory,'accepted_at':time.time()})
    evidence={'schema_version':VERSION_QUALIFICATION,'build_id':root.name.removeprefix('wb-'),
        'backend':'openai-live-professional-product-v1','execution_mode':'retained_qualification_offline',
        'status':'VERIFIED','verified':True,'product_name':original['product_name'],'product_spec':original['product_spec'],
        'manifest':None,'task_states':{},'events':[{'event':'product_review_started'},{'event':'product_review_finished:pass'}],
        'gates':gates['gates'],'gate_states':gates['gate_states'],'review':review,'provider_calls':0,
        'call_facts':{'calls':0},'total_cost_usd':'0','elapsed_seconds':0,'source_build_id':origin['source_build_id'],
        'detail':'Retained candidate qualified offline; original build remains NOT ACCEPTED.'}
    write_new(root/'live_run_evidence.json',evidence)
    receipt={'status':'PASS','project_path':str(root/'app'),'package_sha256':verify_package(root/'app'),
        'evidence_sha256':hashlib.sha256((root/'live_run_evidence.json').read_bytes()).hexdigest(),
        'qualification_sha256':hashlib.sha256((root/'qualification.json').read_bytes()).hexdigest(),
        'manual_sha256':hashlib.sha256((root/'manual_acceptance.json').read_bytes()).hexdigest(),
        'acceptance_directory':acceptance_directory}
    write_new(root/'packaging_evidence.json',receipt)
    return evidence
