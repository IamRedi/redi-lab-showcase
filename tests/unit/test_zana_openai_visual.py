"""Offline OpenAI Images API adapter proofs; no provider network calls."""
import base64
from decimal import Decimal
import importlib.util
import json
from pathlib import Path
import struct
from types import SimpleNamespace as NS
import zlib

import pytest

from redi_lab.workbench.crew import crew_snapshot
from redi_lab.workbench.professional.openai_visual import (
    OpenAIVisualReviewer, OpenAIVisualWorker, PROFILE, accounted_cost, compile_request,
)
from redi_lab.workbench.professional.provider import GovernedProvider
from redi_lab.workbench.professional.pipeline import ProfessionalRunner
from redi_lab.workbench.professional.spec import build_plan
from redi_lab.workbench.professional.visual import VisualFailure, retain_visual, verify_manifest, visual_task


def task(ratio=(16, 9)):
    req={'id':'hero_art','purpose':'A calm hero visual','asset_type':'decorative_image',
         'aspect_ratio':list(ratio),'min_width':320,'min_height':180,'view_id':'journal','slot_id':'hero',
         'style':'restrained geometric composition','subject':'abstract blue shapes',
         'content_constraints':['no people'],'forbidden_content':['letters','logos'],
         'text_in_image':False,'transparent':False,'variants':1,'format':'png',
         'blocking':True,'layout_dependency':'visual_first'}
    return visual_task({'schema_version':'professional_project_spec_v2',
                        'views':[{'id':'journal','slots':[{'id':'hero','purpose':'Lead visual','region':'header','accepts_visual':True}]}],
                        'visual_requirements':[req]},req,'smoke123')


def png(width,height):
    def chunk(name,data):
        return struct.pack('>I',len(data))+name+data+struct.pack('>I',zlib.crc32(name+data)&0xffffffff)
    raw=b''.join(b'\0'+b'\x23\x56\x88'*width for _ in range(height))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')


class FakeClient:
    def __init__(self, *, usage=True, error=None):
        self.calls=[];self.usage=usage;self.error=error
        self.models=NS(retrieve=self.retrieve)
        self.images=NS(generate=self.generate)
        self.chat=NS(completions=NS(create=self.review))

    def retrieve(self, model):
        self.calls.append(('model',model));return NS(id=model)

    def generate(self, **kwargs):
        self.calls.append(('image',kwargs))
        if self.error:raise self.error
        width,height=map(int,kwargs['size'].split('x'))
        usage=NS(input_tokens=100,input_tokens_details=NS(text_tokens=100,image_tokens=0),output_tokens=1000) if self.usage else None
        return NS(data=[NS(b64_json=base64.b64encode(png(width,height)).decode())],usage=usage,_request_id='req_visual_1')

    def review(self, **kwargs):
        self.calls.append(('review',kwargs))
        return NS(usage=NS(prompt_tokens=1000,prompt_tokens_details=NS(cached_tokens=0),completion_tokens=100),
                  choices=[NS(finish_reason='stop',message=NS(content=json.dumps({'verdict':'PASS','observed_reason':'Blue geometric shapes; no letters or people.'})))])


def setup(tmp_path, **kwargs):
    client=FakeClient(**kwargs)
    provider=GovernedProvider('test-only-key','5.00',client_factory=lambda:client)
    worker=OpenAIVisualWorker(provider,tmp_path,visual_ceiling='3.00',offline=True)
    return client,provider,worker


def test_profile_and_bounded_request():
    request=compile_request(task())
    assert PROFILE['model']=='gpt-image-2' and PROFILE['provider']=='openai'
    assert request['size']=='1536x864' and request['quality']=='medium' and request['n']==1
    assert 'abstract blue shapes' in request['prompt'] and 'smoke123' not in request['prompt']
    with pytest.raises(VisualFailure,match='unsupported'):
        compile_request(task((5,4)))
    transparent=task();transparent['transparent']=True
    with pytest.raises(VisualFailure,match='opaque PNG'):
        compile_request(transparent)


def test_mocked_generation_reservation_manifest_and_review(tmp_path):
    client,provider,worker=setup(tmp_path)
    t=task();result=worker.generate(t)
    assert [c[0] for c in client.calls]==['model','image']
    assert client.calls[1][1]['model']=='gpt-image-2'
    assert result['provider_prompt_sha256'] and result['cost_evidence_state']=='USAGE_RECONCILED'
    assert provider.visual_spent==Decimal('0.01525000') and not provider.visual_reserved
    reviewer=OpenAIVisualReviewer(provider,worker)
    row=retain_visual(t,result,tmp_path,independent_review=reviewer)
    assert verify_manifest(row,tmp_path).is_file()
    assert row['provider']=='openai' and row['model']=='gpt-image-2'
    assert row['width']==1536 and row['height']==864 and row['provider_call_id']=='req_visual_1'
    assert row['provider_prompt_sha256']==result['provider_prompt_sha256']
    assert row['generation_duration_seconds'] is not None
    assert row['verification']['independent_review']['verdict']=='PASS'
    assert any(c[0]=='review' for c in client.calls)
    assert provider.spent>0 and provider.visual_spent>0
    events=[json.loads(s) for s in (tmp_path/'visual_cost_ledger.jsonl').read_text().splitlines()]
    assert [e['event'] for e in events]==['reserved','reconciled','review_reserved','review_reconciled']


def test_unknown_usage_keeps_conservative_reservation(tmp_path):
    _,provider,worker=setup(tmp_path,usage=False)
    result=worker.generate(task())
    assert result['cost_evidence_state']=='RESERVATION_PENDING'
    assert result['reserved_cost_usd']=='1.00' and result['cost_usd']=='0'
    assert sum(provider.visual_reserved.values())==Decimal('1.00')
    row=retain_visual(task(),result,tmp_path,independent_review=lambda t,p:{'reviewer':'independent_fixture','verdict':'PASS'})
    assert row['cost_evidence_state']=='RESERVATION_PENDING' and row['reserved_cost_usd']=='1.00'


def test_preflight_and_budget_fail_before_paid_request(tmp_path):
    client,provider,worker=setup(tmp_path)
    provider.key=None
    with pytest.raises(Exception,match='credential'):
        worker.generate(task())
    assert not client.calls
    provider.key='test-only-key';worker.visual_ceiling=Decimal('0.50')
    with pytest.raises(Exception,match='ceiling'):
        worker.generate(task())
    assert [c[0] for c in client.calls]==['model']
    assert not (tmp_path/'visual_cost_ledger.jsonl').exists()


def test_provider_failure_is_classified_and_reservation_retained(tmp_path):
    error=RuntimeError('rate limited');error.status_code=429
    client,provider,worker=setup(tmp_path,error=error)
    with pytest.raises(VisualFailure) as raised:worker.generate(task())
    assert raised.value.kind=='provider_unavailable'
    assert len([c for c in client.calls if c[0]=='image'])==1
    assert sum(provider.visual_reserved.values())==Decimal('1.00')


def test_text_calls_share_visual_reservation_ceiling(tmp_path):
    client,provider,worker=setup(tmp_path)
    provider.ceiling=Decimal('1.00')
    worker.generate(task())
    # A second visual call is blocked while its reservation is outstanding.
    provider.visual_reserved['unsettled']=Decimal('0.99')
    with pytest.raises(ValueError,match='ceiling'):
        provider.execute('data_model','tiny prompt',attempt=1)
    assert not [c for c in client.calls if c[0]=='review']


def test_owner_visual_recovery_limit_blocks_second_generation(tmp_path):
    client, provider, worker = setup(tmp_path)
    worker.max_recovery_generations = 0
    worker.generate(task())
    with pytest.raises(Exception, match='Visual recovery generation limit reached'):
        worker.generate(task())
    assert worker.calls == 1
    assert len([call for call in client.calls if call[0] == 'image']) == 1


def test_planned_second_visual_variant_is_not_a_recovery(tmp_path):
    client, provider, worker = setup(tmp_path)
    worker.max_recovery_generations = 0
    worker.generate(task(), variant_index=1)
    worker.generate(task(), variant_index=2)
    assert worker.calls == 2
    assert worker.recovery_calls == 0
    assert len([call for call in client.calls if call[0] == 'image']) == 2


def test_workbench_profile_before_first_asset():
    snap={'status':'RUNNING','manifest':{'tasks':[{'task_id':'visual_hero_art','persona':'zana','purpose':'Hero'}]},
          'task_states':{'visual_hero_art':'RUNNING'},'active_worker':'text-model',
          'visual_worker_profile':'openai_gpt_image_2_visual_v1',
          'visual_worker_profile_record':{**PROFILE,'availability':'model_metadata_confirmed'},
          'visual_facts':{'provider_calls':1,'cost_usd':'0','reserved_cost_usd':'1.00','duration_seconds':2.0}}
    zana=next(c for c in crew_snapshot(snap)['crew'] if c['persona']['id']=='zana')
    assert zana['state']=='WORKING' and zana['worker_model']=='gpt-image-2'
    assert zana['technical']['provider']=='openai' and zana['technical']['reserved_cost_usd']=='1.00'


def test_incomplete_usage_never_invents_cost():
    assert accounted_cost(None) is None
    assert accounted_cost(NS(input_tokens=1,input_tokens_details=NS(text_tokens=None,image_tokens=0),output_tokens=1)) is None


def test_mocked_openai_worker_runs_through_zana_pipeline(tmp_path):
    fixture_path=Path(__file__).parents[1]/'fixtures'/'professional_products.py'
    loader=importlib.util.spec_from_file_location('zana_openai_products',fixture_path)
    fixtures=importlib.util.module_from_spec(loader);loader.loader.exec_module(fixtures)
    spec=fixtures.social();spec['schema_version']='professional_project_spec_v2'
    spec['views'][0]['slots']=[{'id':'hero','purpose':'Lead visual','region':'header','accepts_visual':True}]
    req={'id':'hero_art','purpose':'A calm hero visual','asset_type':'decorative_image',
         'aspect_ratio':[16,9],'min_width':320,'min_height':180,'view_id':'journal','slot_id':'hero',
         'style':'restrained geometric composition','subject':'abstract blue shapes',
         'content_constraints':['no people'],'forbidden_content':['letters','logos'],
         'text_in_image':False,'transparent':False,'variants':1,'format':'png',
         'blocking':True,'layout_dependency':'visual_first'}
    spec['visual_requirements']=[req]
    root=tmp_path/'wb-openai-mocked'
    client,provider,worker=setup(root)
    runner=ProfessionalRunner(root,'Create a hero image',provider,execution_mode='real_provider',
                              visual_worker=worker,visual_reviewer=OpenAIVisualReviewer(provider,worker),ceiling='5.00')
    runner.spec=spec;runner.plan=build_plan(spec,runner.logical_run_id)
    runner.tasks={t['task_id']:'PENDING' for t in runner.plan['tasks']}
    visual=next(t for t in runner.plan['tasks'] if t['persona']=='zana')
    runner.build_visual_task(visual)
    assert runner.tasks[visual['task_id']]=='COMPLETED'
    assert runner.visual_facts()['provider_calls']==1
    assert runner.visual_facts()['cost_usd']=='0.01525000'
    assert runner.snapshot()['total_cost_usd']==str(provider.spent+provider.visual_spent)
    assert runner.visual_assets['hero_art']['verification']['independent_review']['verdict']=='PASS'
    zana=next(c for c in crew_snapshot(runner.snapshot())['crew'] if c['persona']['id']=='zana')
    assert zana['state']=='VERIFIED' and zana['worker_model']=='gpt-image-2'


def test_real_mode_requires_distinct_visual_authorization(tmp_path,monkeypatch):
    client=FakeClient()
    provider=GovernedProvider('test-only-key','5.00',client_factory=lambda:client)
    monkeypatch.delenv('REDI_ZANA_VISUAL_AUTHORIZED',raising=False)
    worker=OpenAIVisualWorker(provider,tmp_path,visual_ceiling='3.00')
    with pytest.raises(Exception,match='arming'):
        worker.generate(task())
    assert not client.calls and not (tmp_path/'visual_cost_ledger.jsonl').exists()


def test_offline_mode_refuses_uninjected_sdk_client(tmp_path):
    provider=GovernedProvider('test-only-key','5.00')
    with pytest.raises(ValueError,match='injected test client'):
        OpenAIVisualWorker(provider,tmp_path,visual_ceiling='3.00',offline=True)


def test_visual_prompt_secret_is_rejected_before_network_or_reservation(tmp_path):
    client,provider,worker=setup(tmp_path)
    t=task();t['subject']='abstract shapes with test-only-key'
    with pytest.raises(Exception,match='credential-like'):
        worker.generate(t)
    assert not client.calls and not (tmp_path/'visual_cost_ledger.jsonl').exists()
