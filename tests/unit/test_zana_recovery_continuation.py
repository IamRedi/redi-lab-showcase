"""Copied historical ZANA run; all provider responses are local fixtures."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace as NS

import pytest

from redi_lab.workbench.professional import continuation
from redi_lab.workbench.professional.contracts import literal, validate_source
from redi_lab.workbench.professional.openai_visual import OpenAIVisualWorker, OpenAIVisualReviewer
from redi_lab.workbench.build_budget import make_authorization, write_authorization
from redi_lab.workbench.professional.provider import GovernedProvider
from redi_lab.workbench.professional.pipeline import ProfessionalRunner
from redi_lab.workbench.runtime_identity import normal_app_runtime_status
from redi_lab.workbench.professional.spec import BACKEND
from redi_lab.workbench.professional.spec_schema import VISUAL_VERSION, decode_spec

HISTORY = Path(__file__).parents[2] / 'build' / 'workbench-builds' / 'wb-958effb89bb7'
BUILD_ID = '958effb89bb7'


def module(name):
    path = Path(__file__).with_name(name + '.py')
    spec = importlib.util.spec_from_file_location(name + '_continuation_fixture', path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def copied(tmp_path):
    if not (HISTORY / 'live_run_evidence.json').is_file():
        pytest.skip('Historical ZANA run unavailable')
    root = tmp_path / HISTORY.name
    shutil.copytree(HISTORY, root)
    return root


def hashes(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def payload(root):
    context = continuation.decision_context(root, BUILD_ID)
    return {'decision_type': continuation.RECOVERY_CONTINUATION,
            'blocker_id': context['blocker_id'], 'evidence_sha256': context['evidence_sha256'],
            'owner_text': 'Continue the same ZANA smoke run under the recorded bounds.',
            'visual_authorization': context['visual_authorization'], 'resolutions': {}}


def fake_spec():
    visual = module('test_zana_visual')
    spec = visual.visual_spec()
    for req in spec['visual_requirements']:
        req['layout_dependency'] = 'visual_first'
    assert spec['schema_version'] == VISUAL_VERSION
    return spec, visual.fixtures


class FakeClient:
    def __init__(self):
        visual = module('test_zana_openai_visual')
        self.png = visual.png
        raw_spec, self.fixtures = fake_spec()
        self.spec = decode_spec(self.fixtures.wire_spec(raw_spec))
        self.models = NS(retrieve=self.retrieve)
        self.images = NS(generate=self.generate)
        self.chat = NS(completions=NS(create=self.create))
        self.image_calls = 0
        self.review_calls = 0

    def retrieve(self, model):
        assert model == 'gpt-image-2'
        return NS(id=model)

    def generate(self, **request):
        import base64
        assert request['model'] == 'gpt-image-2'
        self.image_calls += 1
        width, height = map(int, request['size'].split('x'))
        usage = NS(input_tokens=100, input_tokens_details=NS(text_tokens=100, image_tokens=0), output_tokens=1000)
        return NS(data=[NS(b64_json=base64.b64encode(self.png(width, height)).decode())],
                  usage=usage, _request_id='offline_visual_' + str(self.image_calls))

    def create(self, **request):
        content = request['messages'][-1]['content']
        if isinstance(content, list):
            self.review_calls += 1
            output = {'verdict': 'PASS', 'observed_reason': 'Opaque abstract color field without text or people.'}
        elif 'Implement the bounded task ' not in content:
            output = self.fixtures.wire_spec(self.spec)
        else:
            task = re.search(r'Implement the bounded task ([a-z_]+)', content).group(1)
            name = {'data_model': 'database.py', 'workflow': 'workflow.py', 'domain': 'core.py',
                    'experience': 'ui.py', 'assembly': 'service.py'}[task]
            source = self.fixtures.sources(self.spec)[name]
            if name == 'ui.py':
                design = literal(validate_source(name, source), 'DESIGN')
                fixed = {req['view_id'] + ':' + req['slot_id']: req['id'] for req in self.spec['visual_requirements']}
                updated = {**design, 'visual_placements': fixed}
                source = source.replace('DESIGN = ' + repr(design), 'DESIGN = ' + repr(updated))
            output = {'patches': [{'rel_path': name, 'action': 'write', 'content': source}]}
        usage = NS(prompt_tokens=1000, prompt_tokens_details=NS(cached_tokens=0), completion_tokens=100)
        return NS(usage=usage, choices=[NS(finish_reason='stop', message=NS(content=json.dumps(output)))])


def test_visual_spec_decision_restores_existing_authorization_and_completes_offline(tmp_path, monkeypatch):
    monkeypatch.setenv('REDI_CHROMIUM_SANDBOX_WORKAROUND', '1')
    build_id = 'aa11bb22cc33'
    root = tmp_path / ('wb-' + build_id)
    authorization = make_authorization(build_id, BACKEND, overall_ceiling='5.00',
        visual_ceiling='3.00', max_visual_calls=3, max_visual_recovery=1,
        visual_required=True, runtime=normal_app_runtime_status(),
        credential_present=True, live_arm=True, visual_arm=True)
    write_authorization(root, authorization)
    class SpecDecisionClient(FakeClient):
        def create(self, **request):
            response = super().create(**request)
            prompt = request['messages'][-1]['content']
            if isinstance(prompt, str) and 'Implement the bounded task ' not in prompt and 'OWNER RESOLUTIONS:' not in prompt:
                wire = json.loads(response.choices[0].message.content)
                wire['uncertainties'] = ['Choose the exact visual direction.']
                response.choices[0].message.content = json.dumps(wire)
            return response
    client = SpecDecisionClient()
    provider = GovernedProvider('offline-key', '5.00', lambda: client)
    worker = OpenAIVisualWorker(provider, root, visual_ceiling='3.00', max_calls=3,
                                max_recovery_generations=1, offline=True)
    reviewer = OpenAIVisualReviewer(provider, worker)
    prompt = 'Create a professional local gallery with two generated images.'
    initial = ProfessionalRunner(root, prompt, provider, execution_mode='injected_offline',
        ceiling='5.00', logical_run_id=build_id, visual_worker=worker,
        visual_reviewer=reviewer).run()
    assert initial['owner_action']['reason'] == 'SPEC_DECISION_REQUIRED'
    assert initial['visual_worker_profile'] == worker.profile
    assert initial['visual_facts']['provider_calls'] == 0
    before = hashes(root)
    context = continuation.decision_context(root, build_id)
    decision = continuation.submit_decision(root, build_id, {
        'decision_type': 'RESOLVE_SPEC_DECISION', 'blocker_id': context['blocker_id'],
        'evidence_sha256': context['evidence_sha256'],
        'resolutions': {'Choose the exact visual direction.': 'Use the retained two-image editorial direction.'},
        'owner_text': 'Resolve the recorded specification uncertainty.'})
    assert decision['visual_authorization'] is None
    assert decision['budget_before_resume']['calls'] == initial['provider_calls']
    assert decision['budget_before_resume']['spent_usd'] == initial['total_cost_usd']
    authorization_path = root / 'build_authorization.json'
    authorization_bytes = authorization_path.read_bytes()
    authorization_path.unlink()
    with pytest.raises(ValueError, match='AUTHORIZATION_NOT_PRESENT'):
        continuation.prepare_resume(root, build_id)
    assert not (root / 'segments' / '0001' / 'resume_started.json').exists()
    authorization_path.write_bytes(authorization_bytes)
    altered = {**authorization, 'max_visual_generation_calls': 2}
    authorization_path.write_text(json.dumps(altered), encoding='utf-8')
    with pytest.raises(ValueError, match='Historical segment changed'):
        continuation.prepare_resume(root, build_id)
    authorization_path.write_bytes(authorization_bytes)
    prepared = continuation.prepare_resume(root, build_id)
    import subprocess
    import sys
    check = subprocess.run([sys.executable, '-B', '-c',
        'import sys; from redi_lab.workbench.professional.continuation import decision_context, _retained_visual_authorization, _read; '
        'from pathlib import Path; r=Path(sys.argv[1]); d=_read(r/"live_run_evidence.json"); '
        'o=_read(r/"product_order.json"); c=decision_context(r,sys.argv[2]); '
        'assert c["decision"]["decision_type"]=="RESOLVE_SPEC_DECISION"; '
        'assert _retained_visual_authorization(r,sys.argv[2],d,o)["max_visual_generation_calls"]==3',
        str(root), build_id], capture_output=True, text=True, timeout=20)
    assert check.returncode == 0, check.stderr
    result = continuation.execute_resume(root, build_id, prepared,
                                          client_factory=lambda: client, key='offline-key')
    assert result['status'] == 'VERIFIED', result['detail']
    assert client.image_calls == 2 and client.review_calls == 2
    assert result['visual_worker_profile'] == worker.profile
    assert result['visual_facts']['retained_assets'] == 2
    assert result['packaging']['browser']['status'] == 'PASS'
    assert result['provider_calls'] > initial['provider_calls']
    assert Decimal(result['total_cost_usd']) >= Decimal(initial['total_cost_usd'])
    assert result['outstanding_reservations_usd'] == '0'
    assert json.loads((root / 'build_authorization.json').read_text()) == authorization
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in before.items())


def test_retained_visual_authorization_is_required_and_misattachment_is_classified(tmp_path, monkeypatch):
    build_id = 'b1df967f8bfd'
    history = Path(__file__).parents[2] / 'build' / 'workbench-builds' / ('wb-' + build_id)
    if not (history / 'segments' / '0001' / 'live_run_evidence.json').is_file():
        pytest.skip('Retained visual specification decision run unavailable')
    root = tmp_path / history.name
    shutil.copytree(history, root)
    before = hashes(root)
    segment = json.loads((root / 'segments' / '0001' / 'live_run_evidence.json').read_text())
    assert continuation.visual_attachment_status(root, build_id, segment) == 'AUTHORIZATION_PRESENT_BUT_NOT_REATTACHED'
    from redi_lab.workbench import engine
    monkeypatch.setattr(engine, 'WORKBENCH_ROOT', tmp_path)
    snapshot = engine.WorkbenchAdapter().snapshot(build_id)
    assert snapshot['visual_authorization_attachment'] == 'AUTHORIZATION_PRESENT_BUT_NOT_REATTACHED'
    assert snapshot['owner_action']['reason'] == 'AUTHORIZATION_PRESENT_BUT_NOT_REATTACHED'
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in before.items())
    (root / 'build_authorization.json').unlink()
    assert continuation.visual_attachment_status(root, build_id, segment) == 'AUTHORIZATION_NOT_PRESENT'


def test_typed_decision_rejects_wrong_scope_and_preserves_history(tmp_path):
    root = copied(tmp_path)
    before = hashes(root)
    request = payload(root)
    with pytest.raises(ValueError, match='Exact visual'):
        continuation.submit_decision(root, BUILD_ID, {**request, 'visual_authorization': None})
    with pytest.raises(ValueError, match='Incompatible'):
        continuation.submit_decision(root, BUILD_ID, {**request, 'decision_type': 'RESOLVE_SPEC_DECISION'})
    decision = continuation.submit_decision(root, BUILD_ID, request)
    assert decision['logical_run_id'] == BUILD_ID
    assert decision['previous_segment_id'] == '0000' and decision['continuation_segment_id'] == '0001'
    assert decision['budget_before_resume']['calls'] == 3
    assert decision['budget_before_resume']['spent_usd'] == '0.1154598'
    assert decision['visual_authorization'] == continuation.SMOKE_AUTHORIZATION
    with pytest.raises(ValueError, match='already'):
        continuation.submit_decision(root, BUILD_ID, request)
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in before.items())


def test_reconstruction_is_bounded_and_fail_closed(tmp_path):
    root = copied(tmp_path)
    continuation.submit_decision(root, BUILD_ID, payload(root))
    decision_path = root / 'segments' / '0001' / 'owner_decision.json'
    original = decision_path.read_bytes()
    record = json.loads(original)
    record['visual_authorization'].pop('visual_reviewer_profile')
    decision_path.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='authorization'):
        continuation.prepare_resume(root, BUILD_ID)
    decision_path.write_bytes(original)
    record = json.loads(original)
    record['runtime_identity']['fingerprint'] = '0' * 64
    decision_path.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='Runtime changed'):
        continuation.prepare_resume(root, BUILD_ID)
    decision_path.write_bytes(original)
    prepared = continuation.prepare_resume(root, BUILD_ID)
    with pytest.raises(ValueError, match='duplicate'):
        continuation.prepare_resume(root, BUILD_ID)
    client = FakeClient()
    runner = continuation._restore_smoke_runner(root, BUILD_ID, prepared,
                                                 client_factory=lambda: client, key='offline-key', progress=None)
    assert isinstance(runner.visual_worker, OpenAIVisualWorker)
    assert isinstance(runner.visual_reviewer, OpenAIVisualReviewer)
    assert runner.visual_worker.max_calls == 3
    assert str(runner.visual_worker.visual_ceiling) == '3.00'
    assert runner.provider.facts['calls'] == 3
    assert str(runner.provider.spent) == '0.1154598'
    assert runner.call_attempts['specification'] == 3
    assert len(runner.episodes) == 3
    assert client.image_calls == client.review_calls == 0


def test_historical_tamper_and_budget_mismatch_fail_before_resume(tmp_path):
    root = copied(tmp_path)
    continuation.submit_decision(root, BUILD_ID, payload(root))
    path = root / 'calls' / 'specification' / 'output.json'
    path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='Historical ZANA smoke evidence/hash mismatch'):
        continuation.prepare_resume(root, BUILD_ID)
    shutil.copy2(HISTORY / 'calls' / 'specification' / 'output.json', path)
    path = root / 'segments' / '0001' / 'owner_decision.json'
    record = json.loads(path.read_text(encoding='utf-8'))
    record['budget_before_resume']['spent_usd'] = '0'
    path.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(ValueError, match='budget'):
        continuation.prepare_resume(root, BUILD_ID)


def test_offline_same_run_reaches_zana_and_browser(tmp_path, monkeypatch):
    monkeypatch.setenv('REDI_CHROMIUM_SANDBOX_WORKAROUND', '1')
    root = copied(tmp_path)
    before = hashes(root)
    continuation.submit_decision(root, BUILD_ID, payload(root))
    client = FakeClient()
    result = continuation.execute_resume(root, BUILD_ID, continuation.prepare_resume(root, BUILD_ID),
                                         client_factory=lambda: client, key='offline-key')
    assert result['status'] == 'VERIFIED', result['detail']
    assert result['build_id'] == BUILD_ID and result['previous_segment_id'] == '0000'
    assert client.image_calls == 2 and client.review_calls == 2
    assert result['provider_calls'] >= 10 and result['visual_facts']['retained_assets'] == 2
    assert {row['asset_id'] for row in result['visual_assets']} == {'hero_art', 'section_art'}
    assert all(row['verification']['independent_review']['verdict'] == 'PASS' for row in result['visual_assets'])
    assert len(result['packaging']['visual_integration']) == 2
    assert result['packaging']['browser']['status'] == 'PASS'
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in before.items())



def test_workbench_owner_choice_and_resume_render_in_chromium(tmp_path, monkeypatch):
    import subprocess
    from redi_lab.workbench import engine, server
    from redi_lab.workbench.professional.browser import chrome_path
    root = copied(tmp_path)
    monkeypatch.setattr(engine, 'WORKBENCH_ROOT', tmp_path)
    monkeypatch.setattr(server, 'WORKBENCH_ROOT', tmp_path)
    http, base = server.serve(port=0, adapter=engine.WorkbenchAdapter())
    def dom(label):
        profile = tmp_path / ('chromium-' + label)
        command = [chrome_path(), '--headless=new', '--no-sandbox', '--disable-gpu',
                   '--disable-gpu-sandbox', '--disable-software-rasterizer',
                   '--disable-background-networking', '--disable-extensions',
                   '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost',
                   '--user-data-dir=' + str(profile), '--virtual-time-budget=5000', '--dump-dom',
                   base + '?build=' + BUILD_ID]
        result = subprocess.run(command, capture_output=True, timeout=45)
        assert result.returncode == 0, result.stderr.decode(errors='replace')[-500:]
        return result.stdout.decode(errors='replace')
    try:
        initial = dom('choice')
        assert 'AUTHORIZE RECOVERY CONTINUATION' in initial
        assert 'Previous 3 calls and $0.1154598 remain counted' in initial
        assert re.search(r'<button[^>]*id="resume-run"[^>]*hidden', initial)
        decision = continuation.submit_decision(root, BUILD_ID, payload(root))
        assert decision['visual_authorization'] == continuation.SMOKE_AUTHORIZATION
        after = dom('resume')
        assert 'Recorded decision: AUTHORIZE_RECOVERY_CONTINUATION' in after
        assert re.search(r'<button[^>]*id="resume-run"(?![^>]*hidden)[^>]*>', after)
    finally:
        http.shutdown();http.server_close()


def test_fresh_process_reconstructs_persisted_smoke_context(tmp_path):
    import subprocess
    import sys
    root = copied(tmp_path)
    continuation.submit_decision(root, BUILD_ID, payload(root))
    continuation.prepare_resume(root, BUILD_ID)
    code = """
import json, sys
from pathlib import Path
from types import SimpleNamespace as NS
from redi_lab.workbench.professional import continuation
root=Path(sys.argv[1]);segment=root/'segments'/'0001'
read=lambda path:json.loads(path.read_text(encoding='utf-8'))
prepared=(read(root/'live_run_evidence.json'),read(root/'product_order.json'),
          read(segment/'owner_decision.json'),segment)
client=NS(models=NS(retrieve=lambda model:NS(id=model)))
runner=continuation._restore_smoke_runner(root,'958effb89bb7',prepared,
    client_factory=lambda:client,key='offline-key',progress=None)
print(json.dumps({'logical_run_id':runner.logical_run_id,'worker':runner.visual_worker.profile,
    'reviewer':runner.visual_reviewer.profile,'max_calls':runner.visual_worker.max_calls,
    'visual_ceiling':str(runner.visual_worker.visual_ceiling),'prior_calls':runner.provider.facts['calls'],
    'prior_spend':str(runner.provider.spent),'spec_attempt':runner.call_attempts['specification'],
    'recovery_episodes':len(runner.episodes)}))
"""
    result = subprocess.run([sys.executable, '-c', code, str(root)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    restored = json.loads(result.stdout)
    assert restored == {'logical_run_id': BUILD_ID, 'worker': 'openai_gpt_image_2_visual_v1',
                        'reviewer': 'openai_independent_visual_review_v1', 'max_calls': 3,
                        'visual_ceiling': '3.00', 'prior_calls': 3, 'prior_spend': '0.1154598',
                        'spec_attempt': 3, 'recovery_episodes': 3}
