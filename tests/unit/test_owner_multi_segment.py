"""Second and later owner decisions continue the latest retained segment.

Uses a copied retained run in which Ora raised a second spec decision after the
first resume; the real run directory stays read-only.
"""
from __future__ import annotations

from decimal import Decimal
import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import threading

import pytest

from redi_lab.workbench.professional import continuation


HISTORY = Path(__file__).parents[2] / 'build' / 'workbench-builds'
TWO_BLOCKERS = '45730951cece'
EVALUATOR_REPAIRED = '6f4a8ab91ec6'

pytestmark = pytest.mark.skipif(not (HISTORY / ('wb-' + TWO_BLOCKERS)).is_dir(),
                                reason='retained two-blocker run is not available')


def copied_tree(tmp_path, build_id):
    root = tmp_path / ('wb-' + build_id)
    shutil.copytree(HISTORY / root.name, root, ignore=shutil.ignore_patterns('browser-profile'))
    return root


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def payload(root, build_id, resolutions):
    context = continuation.decision_context(root, build_id)
    return {'decision_type': 'RESOLVE_SPEC_DECISION', 'blocker_id': context['blocker_id'],
            'evidence_sha256': context['evidence_sha256'], 'resolutions': resolutions,
            'owner_text': 'Owner answers the second blocker.'}


def answers(root, build_id):
    context = continuation.decision_context(root, build_id)
    return {question: 'Owner chooses the simplest bounded default.' for question in context['uncertainties']}


def decide(root):
    return continuation.submit_decision(root, TWO_BLOCKERS, payload(root, TWO_BLOCKERS, answers(root, TWO_BLOCKERS)))


def test_second_spec_decision_continues_from_latest_segment(tmp_path, monkeypatch):
    root = copied_tree(tmp_path, TWO_BLOCKERS)
    first = sha(root / 'segments' / '0001' / 'live_run_evidence.json')
    context = continuation.decision_context(root, TWO_BLOCKERS)
    assert context['choices'] == ['RESOLVE_SPEC_DECISION'] and context['decision'] is None
    assert context['checkpoint_segment_id'] == '0001' and context['continuation_segment_id'] == '0002'
    assert context['evidence_sha256'] == first and len(context['uncertainties']) == 4
    assert [row['segment_id'] for row in context['segments']] == ['0000', '0001']
    with pytest.raises(ValueError, match='stale'):
        continuation.submit_decision(root, TWO_BLOCKERS,
            {**payload(root, TWO_BLOCKERS, answers(root, TWO_BLOCKERS)), 'evidence_sha256': sha(root / 'live_run_evidence.json')})
    record = decide(root)
    assert record['previous_segment_id'] == '0001' and record['continuation_segment_id'] == '0002'
    assert record['budget_before_resume']['calls'] == 2 and record['budget_before_resume']['spent_usd'] == '0.0685000'
    assert (root / 'segments' / '0002' / 'owner_decision.json').is_file()
    assert (root / 'segments' / '0002' / 'origin.json').is_file()
    with pytest.raises(ValueError, match='already'):
        decide(root)
    after = continuation.decision_context(root, TWO_BLOCKERS)
    assert after['choices'] == [] and after['decision']['continuation_segment_id'] == '0002'
    assert [row['status'] for row in after['segments']] == ['OWNER_ACTION_REQUIRED', 'OWNER_ACTION_REQUIRED', 'DECISION_RECORDED']
    prepared = continuation.prepare_resume(root, TWO_BLOCKERS)
    assert prepared[3] == root / 'segments' / '0002'
    with pytest.raises(ValueError, match='duplicate'):
        continuation.prepare_resume(root, TWO_BLOCKERS)
    captured = {}

    class StubRunner:
        def __init__(self, path, prompt, provider, **kwargs):
            captured.update(path=path, prompt=prompt, calls=provider.facts['calls'],
                            spent=str(provider.spent), kwargs=kwargs)

        def run(self):
            return {'status': 'OWNER_ACTION_REQUIRED'}

    monkeypatch.setattr(continuation, 'ProfessionalRunner', StubRunner)
    seen = []
    continuation.execute_resume(root, TWO_BLOCKERS, prepared, client_factory=lambda: object(),
                                key='offline-key', on_runner=seen.append)
    assert captured['path'] == root / 'segments' / '0002' and len(seen) == 1
    assert captured['kwargs']['logical_run_id'] == TWO_BLOCKERS
    assert captured['calls'] == 2 and Decimal(captured['spent']) == Decimal('0.0685000')
    assert captured['prompt'].count('OWNER RESOLUTIONS') == 2  # both owner answers reach Ora
    assert [s['kind'] for s in captured['kwargs']['sources']].count('owner_decision') == 2
    assert sha(root / 'segments' / '0001' / 'live_run_evidence.json') == first


def test_tampered_earlier_segment_blocks_later_resume(tmp_path):
    root = copied_tree(tmp_path, TWO_BLOCKERS)
    decide(root)
    with (root / 'events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write('\n')
    with pytest.raises(ValueError, match='Historical segment changed'):
        continuation.prepare_resume(root, TWO_BLOCKERS)


def test_tampered_latest_checkpoint_blocks_resume(tmp_path):
    root = copied_tree(tmp_path, TWO_BLOCKERS)
    decide(root)
    with (root / 'segments' / '0001' / 'events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write('\n')
    with pytest.raises(ValueError, match='Historical segment changed'):
        continuation.prepare_resume(root, TWO_BLOCKERS)


def test_verified_segment_is_displayed_but_not_reopened(tmp_path):
    if not (HISTORY / ('wb-' + EVALUATOR_REPAIRED) / 'segments' / '0001').is_dir():
        pytest.skip('retained evaluator-repaired run is not available')
    root = copied_tree(tmp_path, EVALUATOR_REPAIRED)
    assert continuation.latest_index(root) == 1 and continuation.checkpoint_index(root) == 0
    context = continuation.decision_context(root, EVALUATOR_REPAIRED)
    assert context['choices'] == [] and context['decision']['continuation_segment_id'] == '0001'
    assert [row['status'] for row in context['segments']] == ['OWNER_ACTION_REQUIRED', 'VERIFIED']


def test_running_continuation_exposes_live_runner_task_states(tmp_path, monkeypatch):
    from redi_lab.workbench import engine
    root = copied_tree(tmp_path, TWO_BLOCKERS)
    monkeypatch.setattr(engine, 'WORKBENCH_ROOT', tmp_path)
    decide(root)
    manifest = {'tasks': [{'task_id': 'data_model', 'persona': 'andinus', 'purpose': 'Records', 'dependencies': []},
                          {'task_id': 'experience', 'persona': 'teuta', 'purpose': 'UI', 'dependencies': ['data_model']}]}
    release = threading.Event()

    class LiveRunner:
        def snapshot(self):
            return {'status': 'RUNNING', 'app_state': 'BUILDING', 'manifest': manifest,
                    'task_states': {'data_model': 'COMPLETED', 'experience': 'RUNNING'},
                    'product_name': 'Workshop Job Tracker', 'gate_states': {}, 'gates': {},
                    'call_facts': {'calls': 5, 'per_task': {}, 'failures': {}}, 'provider_calls': 5,
                    'total_provider_calls': 5, 'total_cost_usd': '0.1200000',
                    'total_cost_upper_bound_usd': '0.1300000', 'visual_facts': {'provider_calls': 0},
                    'recovery_episodes': []}

    def fake_execute(root_, build_id, prepared, *, on_runner=None, **kwargs):
        on_runner(LiveRunner())
        release.wait(10)

    monkeypatch.setattr(continuation, 'execute_resume', fake_execute)
    adapter = engine.WorkbenchAdapter()
    try:
        adapter.resume_build(TWO_BLOCKERS, client_factory=lambda: object(), key='offline-key')
        state = adapter._continuations[TWO_BLOCKERS]
        for _ in range(200):
            if 'runner' in state:
                break
            threading.Event().wait(0.02)
        snap = adapter.snapshot(TWO_BLOCKERS)
        assert snap['status'] == 'RUNNING' and snap['continuation_segment_id'] == '0002'
        assert snap['task_states'] == {'data_model': 'COMPLETED', 'experience': 'RUNNING'}
        assert [(t['node_id'], t['status']) for t in snap['tasks']] == [('data_model', 'VERIFIED'), ('experience', 'RUNNING')]
        assert snap['provider_calls'] == 5 and snap['cost_usd'] == '0.1200000' and snap['verdict'] == 'BUILDING'
        crew = {c['persona']['id']: c['state'] for c in snap['virtutes']['crew']}
        assert crew['teuta'] == 'WORKING' and crew['andinus'] == 'VERIFIED'
    finally:
        release.set()
        adapter._continuations[TWO_BLOCKERS]['thread'].join(10)
    # Without a live runner (e.g. before it starts) nothing is fabricated.
    assert continuation.decision_context(root, TWO_BLOCKERS)['segments'][-1]['status'] == 'RUNNING'


def test_second_decision_form_renders_in_chromium(tmp_path, monkeypatch):
    from redi_lab.workbench import engine, server
    from redi_lab.workbench.professional.browser import chrome_path
    root = copied_tree(tmp_path, TWO_BLOCKERS)
    monkeypatch.setattr(engine, 'WORKBENCH_ROOT', tmp_path)
    monkeypatch.setattr(server, 'WORKBENCH_ROOT', tmp_path)
    http, base = server.serve(port=0, adapter=engine.WorkbenchAdapter())

    def dom(label):
        command = [chrome_path(), '--headless=new', '--no-sandbox', '--disable-gpu', '--disable-gpu-sandbox',
                   '--disable-software-rasterizer', '--disable-background-networking', '--disable-extensions',
                   '--proxy-server=http://127.0.0.1:9', '--proxy-bypass-list=127.0.0.1;localhost',
                   '--user-data-dir=' + str(tmp_path / ('chromium-' + label)), '--virtual-time-budget=5000',
                   '--dump-dom', base + '?build=' + TWO_BLOCKERS]
        result = subprocess.run(command, capture_output=True, timeout=45)
        assert result.returncode == 0, result.stderr.decode(errors='replace')[-500:]
        return result.stdout.decode(errors='replace')

    try:
        initial = dom('second')
        assert 'RESOLVE SPEC DECISION' in initial
        assert len(re.findall(r'<textarea id="owner-resolution-\d+"', initial)) == 4
        assert re.search(r'<button[^>]*id="resume-run"[^>]*hidden', initial)
        decide(root)
        after = dom('second-recorded')
        assert 'Recorded decision: RESOLVE_SPEC_DECISION' in after and 'Segment 0002' in after
        assert re.search(r'<button[^>]*id="resume-run"(?![^>]*hidden)[^>]*>', after)
    finally:
        http.shutdown()
        http.server_close()
