"""Historical Workbench decisions use copied bytes; the real runs are read-only."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import shutil
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

from redi_lab.workbench.professional import continuation
from redi_lab.workbench.professional.provider import GovernedProvider
from redi_lab.workbench.professional.review import requirements


HISTORY = Path(__file__).parents[2] / 'build' / 'workbench-builds'


def copied_run(tmp_path, build_id):
    source = HISTORY / ('wb-' + build_id)
    root = tmp_path / source.name
    root.mkdir()
    for path in source.iterdir():
        if path.is_file(): shutil.copy2(path, root / path.name)
    if (source / 'app').exists(): shutil.copytree(source / 'app', root / 'app')
    for relative in ('calls/specification/output.json', 'acceptance/review_evidence.json',
                     'acceptance/medaurus.json', 'acceptance/launch.json',
                     'acceptance/browser/report.json', 'acceptance/browser/audit.js'):
        path = source / relative
        if path.is_file():
            target = root / relative;target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return root


def decision_payload(root, build_id, decision_type, resolutions=None):
    context = continuation.decision_context(root, build_id)
    return {'decision_type': decision_type, 'blocker_id': context['blocker_id'],
            'evidence_sha256': context['evidence_sha256'], 'resolutions': resolutions or {},
            'owner_text': 'Owner decision for this blocker.'}


def test_evaluator_decision_is_typed_append_only_and_resume_is_single_use(tmp_path):
    build_id = '6f4a8ab91ec6';root = copied_run(tmp_path, build_id)
    original = hashlib.sha256((root / 'live_run_evidence.json').read_bytes()).hexdigest()
    context = continuation.decision_context(root, build_id)
    assert context['choices'] == ['AUTHORIZE_EVALUATOR_REPAIR']
    payload = decision_payload(root, build_id, 'AUTHORIZE_EVALUATOR_REPAIR')
    with pytest.raises(ValueError, match='stale'):
        continuation.submit_decision(root, build_id, {**payload, 'evidence_sha256': '0' * 64})
    with pytest.raises(ValueError, match='Incompatible'):
        continuation.submit_decision(root, build_id, {**payload, 'decision_type': 'RESOLVE_SPEC_DECISION'})
    decision = continuation.submit_decision(root, build_id, payload)
    assert decision['logical_run_id'] == build_id and decision['budget_before_resume']['calls'] == 6
    assert Decimal(decision['budget_before_resume']['spent_usd']) == Decimal('0.1461210')
    assert decision['previous_segment_id'] == '0000' and decision['continuation_segment_id'] == '0001'
    assert hashlib.sha256((root / 'live_run_evidence.json').read_bytes()).hexdigest() == original
    with pytest.raises(ValueError, match='already'):
        continuation.submit_decision(root, build_id, payload)
    prepared = continuation.prepare_resume(root, build_id)
    assert prepared[3] == root / 'segments' / '0001'
    with pytest.raises(ValueError, match='duplicate'):
        continuation.prepare_resume(root, build_id)
    assert hashlib.sha256((root / 'live_run_evidence.json').read_bytes()).hexdigest() == original


def test_package_and_history_tampering_block_resume(tmp_path):
    build_id = '6f4a8ab91ec6';root = copied_run(tmp_path, build_id)
    continuation.submit_decision(root, build_id, decision_payload(root, build_id, 'AUTHORIZE_EVALUATOR_REPAIR'))
    with (root / 'app' / 'product_app' / 'ui.py').open('a', encoding='utf-8') as stream: stream.write('\n# changed')
    with pytest.raises(ValueError, match='packaged artifact changed'):
        continuation.prepare_resume(root, build_id)
    shutil.copy2(HISTORY / ('wb-' + build_id) / 'app' / 'product_app' / 'ui.py', root / 'app' / 'product_app' / 'ui.py')
    with (root / 'events.jsonl').open('a', encoding='utf-8') as stream: stream.write('\n')
    with pytest.raises(ValueError, match='Historical segment changed'):
        continuation.prepare_resume(root, build_id)


def test_spec_decision_requires_all_structured_answers_and_keeps_prior_budget(tmp_path, monkeypatch):
    build_id = '564d3dd42383';root = copied_run(tmp_path, build_id)
    context = continuation.decision_context(root, build_id)
    assert context['choices'] == ['RESOLVE_SPEC_DECISION']
    partial = {context['uncertainties'][0]: 'Use an 80-character limit.'}
    with pytest.raises(ValueError, match='Every recorded'):
        continuation.submit_decision(root, build_id,
            decision_payload(root, build_id, 'RESOLVE_SPEC_DECISION', partial))
    answers = {question: 'Owner chooses the bounded local default for this item.' for question in context['uncertainties']}
    record = continuation.submit_decision(root, build_id,
        decision_payload(root, build_id, 'RESOLVE_SPEC_DECISION', answers))
    assert record['budget_before_resume']['spent_usd'] == '0.0540020'
    prepared = continuation.prepare_resume(root, build_id)
    captured = {}
    class StubRunner:
        def __init__(self, path, prompt, provider, **kwargs):
            captured.update(path=path, prompt=prompt, calls=provider.facts['calls'],
                            spent=str(provider.spent), kwargs=kwargs)
        def run(self): return {'status': 'OWNER_ACTION_REQUIRED'}
    monkeypatch.setattr(continuation, 'ProfessionalRunner', StubRunner)
    result = continuation.execute_resume(root, build_id, prepared,
                                           client_factory=lambda: object(), key='offline-key')
    assert result['status'] == 'OWNER_ACTION_REQUIRED'
    assert captured['path'] == root / 'segments' / '0001'
    assert captured['kwargs']['logical_run_id'] == build_id
    assert captured['calls'] == 1 and Decimal(captured['spent']) == Decimal('0.0540020')
    assert 'OWNER RESOLUTIONS' in captured['prompt']


def test_prior_provider_state_reconciles_without_reset(tmp_path):
    evidence = json.loads((HISTORY / 'wb-6f4a8ab91ec6' / 'live_run_evidence.json').read_text(encoding='utf-8'))
    provider = GovernedProvider('offline-key', evidence['cost_ceiling_usd'], lambda: object(), prior_state=evidence)
    assert provider.facts['calls'] == 6 and provider.spent == Decimal('0.1461210')
    assert provider.attempted == set(evidence['call_facts']['per_task'])
    tampered = {**evidence, 'total_cost_usd': '0'}
    with pytest.raises(ValueError, match='reconcile'):
        GovernedProvider('offline-key', evidence['cost_ceiling_usd'], lambda: object(), prior_state=tampered)


def test_zana_pre_spec_recovery_exhaustion_has_only_typed_resume_choice(tmp_path):
    build_id='958effb89bb7'
    if not (HISTORY / ('wb-'+build_id) / 'live_run_evidence.json').is_file():
        pytest.skip('Historical ZANA smoke evidence is not part of this checkout')
    root=tmp_path/('wb-'+build_id)
    shutil.copytree(HISTORY/('wb-'+build_id),root)
    context=continuation.decision_context(root,build_id)
    assert context['choices']==['AUTHORIZE_RECOVERY_CONTINUATION']
    assert context['visual_authorization']==continuation.SMOKE_AUTHORIZATION
    assert context['owner_reason']=='RECOVERY_EXHAUSTED'
    assert context['budget_before_resume']['calls']==3
    assert Decimal(context['budget_before_resume']['spent_usd'])==Decimal('0.1154598')


def test_ilira_obligations_remain_required_with_real_assertion_ids():
    evidence = json.loads((HISTORY / 'wb-6f4a8ab91ec6' / 'live_run_evidence.json').read_text(encoding='utf-8'))
    reqs = {row['requirement_id']: row for row in requirements(evidence['product_spec'])}
    for key in ('dashboard', 'acceptance:10', 'acceptance:11', 'acceptance:12',
                'acceptance:13', 'acceptance:15', 'acceptance:20'):
        assert reqs[key]['applicability'] == 'REQUIRED'
        assert not any(name.startswith('unmapped_') for name in reqs[key]['assertion_ids'])
    assert 'filtered_metrics' in reqs['acceptance:10']['assertion_ids']
    assert {'required_text_validation', 'text_length_validation'} <= set(reqs['acceptance:12']['assertion_ids'])


def test_retained_ilira_replay_requalifies_only_shared_runtime_and_preserves_history(tmp_path):
    build_id = '6f4a8ab91ec6';root = copied_run(tmp_path, build_id)
    original_sha = hashlib.sha256((root / 'live_run_evidence.json').read_bytes()).hexdigest()
    original_package = continuation.verify_package(root / 'app')
    from redi_lab.workbench.professional.acceptance import collect
    spec = json.loads((root / 'product_spec.json').read_text(encoding='utf-8'))['spec']
    old_review = collect(root / 'app', spec, 'wb-' + build_id, tmp_path / 'original_acceptance')['review']
    assert old_review['verdict'] == 'FAIL' and old_review['counts'].get('FAIL', 0) > 0
    continuation.submit_decision(root, build_id, decision_payload(root, build_id, 'AUTHORIZE_EVALUATOR_REPAIR'))
    prepared = continuation.prepare_resume(root, build_id)
    result = continuation.execute_resume(root, build_id, prepared, client_factory=lambda: object())
    assert result['status'] == 'VERIFIED' and result['verified'] and result['app_state'] == 'APP_READY'
    assert result['review']['status'] == 'VERIFIED' and result['review']['verdict'] == 'PASS'
    assert result['review']['counts'].get('FAIL', 0) == 0 and result['review']['counts'].get('NOT_RUN', 0) == 0
    assert all(state == 'PASS' for state in result['gate_states'].values())
    assert all(state == 'PASS' for state in json.loads((prepared[3] / 'frozen_gate_result.json').read_text())['gate_states'].values())
    assert result['provider_calls'] == 6 and result['shared_renderer_requalification']['original_package_sha256'] == original_package
    assert result['packaging']['package_sha256'] != original_package
    assert continuation.verify_package(root / 'app') == original_package
    assert Decimal(result['total_cost_usd']) == Decimal('0.1461210')
    assert result['previous_segment_id'] == '0000'
    assert result['runtime_identity']['fingerprint'] != prepared[0]['runtime_identity']['fingerprint']
    assert hashlib.sha256((root / 'live_run_evidence.json').read_bytes()).hexdigest() == original_sha
    assert not result['review']['unresolved']


def test_shared_renderer_gate_failure_is_retained_as_precise_owner_block(tmp_path, monkeypatch):
    build_id = '6f4a8ab91ec6';root = copied_run(tmp_path, build_id)
    original = hashlib.sha256((root / 'live_run_evidence.json').read_bytes()).hexdigest()
    continuation.submit_decision(root, build_id, decision_payload(root, build_id, 'AUTHORIZE_EVALUATOR_REPAIR'))
    prepared = continuation.prepare_resume(root, build_id)
    def failed_rebuild(*args, **kwargs):
        raise ValueError('frozen gate rejected shared runtime bytes')
    monkeypatch.setattr(continuation, 'rebuild_shared_renderer', failed_rebuild)
    result = continuation.execute_resume(root, build_id, prepared, client_factory=lambda: object())
    assert result['status'] == 'OWNER_ACTION_REQUIRED' and not result['verified']
    assert result['failure_class'] == 'SHARED_RENDERER_OR_PACKAGE_CONTRACT_DEFECT'
    assert result['failure_stage'] == 'shared_renderer_requalification'
    assert 'frozen gate rejected' in result['detail']
    assert result['provider_calls'] == 6 and Decimal(result['total_cost_usd']) == Decimal('0.1461210')
    assert hashlib.sha256((root / 'live_run_evidence.json').read_bytes()).hexdigest() == original
    assert (prepared[3] / 'live_run_evidence.json').is_file()


def test_http_decision_endpoint_persists_before_resume_and_exposes_history(tmp_path, monkeypatch):
    from redi_lab.workbench import engine, server
    build_id = '6f4a8ab91ec6';root = copied_run(tmp_path, build_id)
    monkeypatch.setattr(engine, 'WORKBENCH_ROOT', tmp_path)
    monkeypatch.setattr(server, 'WORKBENCH_ROOT', tmp_path)
    adapter = engine.WorkbenchAdapter()
    http, base = server.serve(port=0, adapter=adapter)
    try:
        payload = decision_payload(root, build_id, 'AUTHORIZE_EVALUATOR_REPAIR')
        url = base + 'api/builds/' + build_id + '/decisions'
        def post(value):
            data = json.dumps(value).encode()
            request = Request(url, data=data, headers={'Content-Type':'application/json','Origin':base.rstrip('/')}, method='POST')
            return json.load(urlopen(request))
        result = post(payload)
        assert result['decision']['decision_type'] == 'AUTHORIZE_EVALUATOR_REPAIR'
        assert result['run']['build_id'] == build_id
        assert result['run']['owner_decision']['segments'][0]['status'] == 'OWNER_ACTION_REQUIRED'
        assert result['run']['owner_decision']['segments'][1]['status'] == 'DECISION_RECORDED'
        with pytest.raises(HTTPError) as error: post(payload)
        assert error.value.code == 409
        segments = json.load(urlopen(base + 'api/builds/' + build_id + '/segments'))
        assert [row['segment_id'] for row in segments['segments']] == ['0000','0001']
        resume_url = base + 'api/builds/' + build_id + '/resume'
        request = Request(resume_url, data=b'{}', headers={'Content-Type':'application/json','Origin':base.rstrip('/')}, method='POST')
        started = json.load(urlopen(request))
        assert started['build_id'] == build_id
        adapter._continuations[build_id]['thread'].join(30)
        current = json.load(urlopen(base + 'api/builds/' + build_id))
        assert current['continuation_segment_id'] == '0001'
        assert current['status'] == 'VERIFIED' and current['verdict'] == 'APP READY'
        assert current['can_open_app'] and Path(current['project_path']) == root / 'segments' / '0001' / 'app'
        assert current['provider_calls'] == 6 and Decimal(current['cost_usd']) == Decimal('0.1461210')
        assert current['owner_decision']['segments'][0]['evidence_sha256'] == payload['evidence_sha256']
        with pytest.raises(HTTPError) as error: urlopen(request)
        assert error.value.code == 409
    finally:
        http.shutdown();http.server_close()
