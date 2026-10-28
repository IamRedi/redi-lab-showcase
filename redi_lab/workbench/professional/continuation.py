"""Owner decisions and append-only continuations of professional Workbench runs."""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

from ..build_budget import read_authorization
from ..product_packaging import verify_package
from ..runtime_identity import require_current_normal_app_runtime, normal_app_runtime_status
from .acceptance import collect
from .pipeline import ProfessionalRunner, write_new
from .provider import GovernedProvider
from .recovery import RecoveryBudget
from .requalification import rebuild as rebuild_shared_renderer
from .review import digest as review_digest
from .spec import BACKEND, MODEL, sha, source_record
from .openai_visual import OpenAIVisualWorker, OpenAIVisualReviewer, PROFILE_ID, PROFILE, MODEL as IMAGE_MODEL

RECOVERY_CONTINUATION = 'AUTHORIZE_RECOVERY_CONTINUATION'
SMOKE_RUN_ID = '958effb89bb7'
SMOKE_ORIGIN_FILES = 25
SMOKE_ORIGIN_SHA256 = 'e787cb395bf6aaa82011e2225cac47493e0de0d320b50b56204784a25fd35a1b'
SMOKE_AUTHORIZATION = {
    'schema_version': 'zana_smoke_continuation_authorization_v1',
    'visual_provider': 'openai', 'visual_model': IMAGE_MODEL,
    'visual_worker_profile': PROFILE_ID,
    'visual_reviewer_profile': OpenAIVisualReviewer.profile,
    'visual_review_model': MODEL, 'required_visual_assets': 2,
    'max_total_visual_generation_calls': 3,
    'max_additional_recovery_generation': 1,
    'visual_ceiling_usd': '3.00', 'overall_ceiling_usd': '5.00',
    'requires_live_provider_arm': True, 'requires_visual_provider_arm': True,
}

VERSION = 'professional_owner_decision_v1'
SEGMENT_VERSION = 'professional_continuation_segment_v1'
HASHED_ORIGIN = ('build_authorization.json', 'live_run_evidence.json', 'events.jsonl', 'owner_action.json', 'product_order.json',
                 'product_spec.json', 'execution_manifest.json', 'frozen_gate_result.json',
                 'frozen_gate_contract.json', 'database_contract.json', 'terminal_diagnosis.json',
                 'terminal_decision.json', 'reviewer_inputs.json', 'acceptance/review_evidence.json',
                 'acceptance/medaurus.json')


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def segment_id(index):
    return f'{index:04d}'


def _segment_dir(root, index):
    return Path(root) / 'segments' / segment_id(index)


def latest_index(root):
    """Latest segment with retained terminal evidence; 0 is the original run directory."""
    index = 0
    while (_segment_dir(root, index + 1) / 'live_run_evidence.json').is_file():
        index += 1
    return index


def checkpoint_index(root):
    """Latest retained owner-action checkpoint that a new decision can continue."""
    for index in range(latest_index(root), 0, -1):
        base = _segment_dir(root, index)
        if ((base / 'owner_action.json').is_file() and (base / 'product_order.json').is_file()
                and _read(base / 'live_run_evidence.json').get('status') == 'OWNER_ACTION_REQUIRED'):
            return index
    return 0


def _checkpoint(root, index):
    return Path(root) if index == 0 else _segment_dir(root, index)


def _segment(root):
    """The next append-only segment, continuing the latest retained checkpoint."""
    return _segment_dir(root, checkpoint_index(root) + 1)


def _origin(root, index=0):
    # Checkpoint 0 keeps its historical anchor; later anchors live beside the
    # decision that continues them, so a retained segment is never rewritten.
    return Path(root) / 'segments' / '0000' / 'origin.json' if index == 0 else _segment_dir(root, index + 1) / 'origin.json'


def _decision(root):
    return _segment(root) / 'owner_decision.json'


def _context(root, build_id):
    root = Path(root).resolve()
    if root.name != 'wb-' + build_id or len(build_id) != 12 or any(c not in '0123456789abcdef' for c in build_id):
        raise ValueError('Invalid logical run ID')
    index = checkpoint_index(root)
    base = _checkpoint(root, index)
    evidence = _read(base / 'live_run_evidence.json')
    order = _read(base / 'product_order.json')
    action = _read(base / 'owner_action.json')
    if evidence.get('build_id') != build_id or evidence.get('backend') != BACKEND or order.get('backend') != BACKEND:
        raise ValueError('Logical run or backend identity mismatch')
    if evidence.get('status') != 'OWNER_ACTION_REQUIRED' or evidence.get('owner_action') != action:
        raise ValueError('Run is not at its retained owner-action checkpoint')
    if order.get('ceiling_usd') != evidence.get('cost_ceiling_usd'):
        raise ValueError('Owner ceiling differs from retained product order')
    reason = action.get('reason')
    if (build_id == SMOKE_RUN_ID and index == 0 and reason == 'RECOVERY_EXHAUSTED'
          and evidence.get('failure_stage') == 'specification' and not (root / 'product_spec.json').exists()):
        _verify_smoke_checkpoint(root, evidence, order)
        choices = [RECOVERY_CONTINUATION]
    elif reason == 'SPEC_DECISION_REQUIRED' and evidence.get('failure_stage') == 'specification' and not (base / 'product_spec.json').exists():
        choices = ['RESOLVE_SPEC_DECISION']
    elif (reason == 'MANUAL_APPROVAL_REQUIRED' and evidence.get('failure_stage') == 'product_review'
          and evidence.get('review', {}).get('reviewer_version') == 'medaurus_requirement_review_v2'
          and evidence.get('review', {}).get('counts', {}).get('NOT_RUN', 0) > 0
          and all(state == 'PASS' for state in evidence.get('gate_states', {}).values())):
        choices = ['AUTHORIZE_EVALUATOR_REPAIR']
    else:
        choices = []
    block_id = hashlib.sha256((_hash(base / 'live_run_evidence.json') + _hash(base / 'owner_action.json')).encode()).hexdigest()[:24]
    return evidence, order, action, choices, block_id


def _segment_row(root, index):
    path = _checkpoint(root, index) / 'live_run_evidence.json'
    if path.is_file():
        evidence = _read(path)
        return {'segment_id': segment_id(index), 'status': evidence['status'], 'evidence_path': str(path),
                'evidence_sha256': _hash(path), 'runtime_identity': evidence.get('runtime_identity')}
    return {'segment_id': segment_id(index),
            'status': 'RUNNING' if (path.parent / 'resume_started.json').exists() else 'DECISION_RECORDED',
            'evidence_path': None, 'evidence_sha256': None, 'runtime_identity': None}


def decision_context(root, build_id):
    """Context for the latest retained blocker; earlier decisions stay history."""
    evidence, order, action, choices, block_id = _context(root, build_id)
    root = Path(root)
    index = checkpoint_index(root)
    decision = _read(_decision(root)) if _decision(root).is_file() else None
    history = [_segment_row(root, n) for n in range(index + 1)]
    if decision:
        history.append(_segment_row(root, index + 1))
    return {'blocker_id': block_id, 'owner_reason': action['reason'], 'decision_needed': action['decision_needed'],
            'evidence_sha256': _hash(_checkpoint(root, index) / 'live_run_evidence.json'), 'choices': choices if not decision else [],
            'checkpoint_segment_id': segment_id(index), 'continuation_segment_id': segment_id(index + 1),
            'decision': decision, 'segments': history, 'uncertainties': (evidence.get('product_spec') or {}).get('uncertainties', []),
            'budget_before_resume': _budget(evidence),
            'visual_authorization': dict(SMOKE_AUTHORIZATION) if RECOVERY_CONTINUATION in choices or
                decision and decision.get('decision_type') == RECOVERY_CONTINUATION else None}


def _budget(evidence):
    return {'calls': evidence['provider_calls'], 'spent_usd': evidence['total_cost_usd'],
            'reserved_usd': evidence['outstanding_reservations_usd'],
            'ceiling_usd': evidence['cost_ceiling_usd'], 'elapsed_seconds': evidence['elapsed_seconds'],
            'recovery_episodes': len(evidence['recovery_episodes'])}


def _requires_visual_context(evidence):
    return bool(evidence.get('visual_worker_profile') or
                (evidence.get('product_spec') or {}).get('visual_requirements'))


def _retained_visual_authorization(root, build_id, evidence, order):
    """Bind an existing visual grant to this run; never manufacture a grant."""
    if not _requires_visual_context(evidence):
        return None
    path = Path(root) / 'build_authorization.json'
    if not path.is_file():
        raise ValueError('AUTHORIZATION_NOT_PRESENT: retained visual build authorization is missing')
    authorization = read_authorization(root, build_id)
    state = authorization.get('authorization_state') or {}
    profile = evidence.get('visual_worker_profile_record') or {}
    expected_profile = {key: value for key, value in PROFILE.items() if key != 'availability'}
    try:
        overall = Decimal(str(authorization.get('overall_ceiling_usd')))
        ceiling = Decimal(str(authorization.get('visual_ceiling_usd')))
    except Exception as exc:
        raise ValueError('AUTHORIZATION_NOT_PRESENT: retained visual ceilings are invalid') from exc
    calls = authorization.get('max_visual_generation_calls')
    recovery = authorization.get('max_visual_recovery_generations')
    if (authorization.get('backend') != BACKEND or
        any(state.get(name) is not True for name in ('credential_present', 'live_arm', 'visual_arm')) or
        not overall.is_finite() or not ceiling.is_finite() or
        overall <= 0 or ceiling <= 0 or ceiling > overall or
        type(calls) is not int or not 1 <= calls <= 3 or
        type(recovery) is not int or not 0 <= recovery < calls or
        evidence.get('visual_worker_profile') != PROFILE_ID or
        any(profile.get(name) != value for name, value in expected_profile.items()) or
        authorization.get('runtime_identity', {}).get('fingerprint') != evidence.get('runtime_identity', {}).get('fingerprint') or
        overall != Decimal(str(order['ceiling_usd'])) or
        overall != Decimal(str(evidence['cost_ceiling_usd']))):
        raise ValueError('AUTHORIZATION_NOT_PRESENT: retained visual authorization or worker identity is incomplete')
    visual = evidence.get('visual_facts') or {}
    if (visual.get('provider_calls') != 0 or Decimal(str(visual.get('cost_usd', '0'))) != 0 or
        Decimal(str(visual.get('reserved_cost_usd', '0'))) != 0 or
        evidence.get('visual_assets') or (Path(root) / 'visual_cost_ledger.jsonl').exists()):
        raise ValueError('Prior visual usage needs a separately reconciled continuation; it cannot be reset')
    return authorization


def _restore_visual_context(root, build_id, evidence, order, segment, provider, *, client_factory):
    authorization = _retained_visual_authorization(root, build_id, evidence, order)
    if authorization is None:
        return None, None
    if not provider.key or (client_factory is None and
                            (os.environ.get('REDI_LIVE_PROVIDER') != '1' or
                             os.environ.get('REDI_ZANA_VISUAL_AUTHORIZED') != '1')):
        raise ValueError('AUTHORIZATION_NOT_PRESENT: governed credential and live visual arms are required')
    worker = OpenAIVisualWorker(provider, segment,
                                visual_ceiling=authorization['visual_ceiling_usd'],
                                max_calls=authorization['max_visual_generation_calls'],
                                max_recovery_generations=authorization['max_visual_recovery_generations'],
                                offline=client_factory is not None)
    reviewer = OpenAIVisualReviewer(provider, worker)
    if reviewer.profile != OpenAIVisualReviewer.profile:
        raise ValueError('Retained visual reviewer profile could not be reconstructed')
    return worker, reviewer


def visual_attachment_status(root, build_id, segment_evidence):
    """Explain a retained missing-worker failure without changing its evidence."""
    action = segment_evidence.get('owner_action') or {}
    visual = segment_evidence.get('visual_facts') or {}
    if (action.get('reason') != 'EXTERNAL_SERVICE_REQUIRED' or
        action.get('cause') != 'No owner-authorized visual provider is configured' or
        segment_evidence.get('visual_worker_profile') is not None or
        visual.get('provider_calls') != 0):
        return None
    root = Path(root)
    evidence = _read(root / 'live_run_evidence.json')
    order = _read(root / 'product_order.json')
    try:
        authorization = _retained_visual_authorization(root, build_id, evidence, order)
    except (ValueError, KeyError):
        return 'AUTHORIZATION_NOT_PRESENT'
    return 'AUTHORIZATION_PRESENT_BUT_NOT_REATTACHED' if authorization else 'AUTHORIZATION_NOT_PRESENT'


def _verify_history(root, origin):
    for relative, expected in origin['file_hashes'].items():
        path = (Path(root) / relative).resolve()
        if not path.is_relative_to(Path(root).resolve()) or not path.is_file() or _hash(path) != expected:
            raise ValueError('Historical segment changed: ' + relative)


def _verify_checkpoint(root, evidence, order, choice):
    facts = evidence.get('call_facts', {})
    calls = facts.get('per_task', {})
    if not isinstance(calls, dict) or facts.get('calls') != evidence.get('provider_calls') or len(calls) + len(facts.get('failures', {})) != facts.get('calls'):
        raise ValueError('Prior provider call state is incomplete')
    if sum((Decimal(str(f['cost_usd'])) for f in calls.values()), Decimal(0)) != Decimal(str(evidence['total_cost_usd'])):
        raise ValueError('Historical cost does not reconcile')
    if Decimal(str(evidence['total_cost_usd'])) + Decimal(str(evidence['outstanding_reservations_usd'])) >= Decimal(str(evidence['cost_ceiling_usd'])):
        raise ValueError('Insufficient remaining owner budget')
    budget = RecoveryBudget(**order['recovery_budget'])
    if evidence['provider_calls'] >= budget.max_provider_calls or (budget.max_elapsed_seconds is not None and evidence['elapsed_seconds'] >= budget.max_elapsed_seconds):
        raise ValueError('Cumulative recovery ceiling reached')
    if choice == 'AUTHORIZE_EVALUATOR_REPAIR':
        spec_record = _read(Path(root) / 'product_spec.json')
        plan = _read(Path(root) / 'execution_manifest.json')
        if spec_record['sha256'] != sha(spec_record['spec']) or evidence['product_spec'] != spec_record['spec']:
            raise ValueError('ProductSpec hash mismatch')
        if sha(plan) != sha(evidence['manifest']) or plan != evidence['manifest']:
            raise ValueError('Production plan hash mismatch')
        if any(state != 'COMPLETED' for state in evidence['task_states'].values()):
            raise ValueError('Production tasks are not completed')
        if any(state != 'PASS' for state in evidence['gate_states'].values()) or evidence['packaging']['status'] != 'PASS':
            raise ValueError('Prior frozen gates or package did not pass')
        if verify_package(Path(root) / 'app') != evidence['packaging']['package_sha256']:
            raise ValueError('Retained package hash mismatch')
        old_inputs = _read(Path(root) / 'acceptance/review_evidence.json')
        old_review = _read(Path(root) / 'acceptance/medaurus.json')
        if old_inputs.get('product_spec_hash') != review_digest(spec_record['spec']) or old_review.get('evidence_hash') != review_digest(old_inputs) or old_review != evidence['review']:
            raise ValueError('Prior Medaurus evidence mismatch')
        for name, expected in old_inputs['references'].items():
            path = (Path(root) / 'acceptance' / name).resolve()
            if not path.is_relative_to((Path(root) / 'acceptance').resolve()) or _hash(path) != expected:
                raise ValueError('Prior Medaurus reference changed: ' + name)
    else:
        if (Path(root) / 'product_spec.json').exists() or evidence.get('manifest') is not None:
            raise ValueError('Specification checkpoint is not unresolved')
        outputs = Path(root) / 'calls' / 'specification'
        if not ((outputs / 'output.json').is_file() or any(outputs.glob('attempt-*/output.json'))):
            raise ValueError('Prior specification output is missing')
    if choice == RECOVERY_CONTINUATION:
        _verify_smoke_checkpoint(root, evidence, order)


def _verify_smoke_checkpoint(root, evidence, order):
    """Limit this decision to the retained pre-spec ZANA smoke checkpoint."""
    from scripts.run_zana_openai_smoke import PROMPT
    historical = sorted(p for p in Path(root).rglob('*') if p.is_file() and 'segments' not in p.relative_to(root).parts)
    digest = hashlib.sha256()
    for path in historical:
        digest.update(str(path.relative_to(root)).replace('\\', '/').encode())
        digest.update(b'\0')
        digest.update(bytes.fromhex(_hash(path)))
    if len(historical) != SMOKE_ORIGIN_FILES or digest.hexdigest() != SMOKE_ORIGIN_SHA256:
        raise ValueError('Historical ZANA smoke evidence/hash mismatch')
    if (evidence.get('build_id') != SMOKE_RUN_ID or
        evidence.get('provider_calls') != 3 or
        Decimal(str(evidence.get('total_cost_usd'))) != Decimal('0.1154598') or
        Decimal(str(evidence.get('outstanding_reservations_usd'))) != 0 or
        Decimal(str(order.get('ceiling_usd'))) != Decimal('5.00') or
        order.get('sources', [{}])[0].get('text') != PROMPT or
        evidence.get('visual_worker_profile') != PROFILE_ID or
        evidence.get('visual_worker_profile_record', {}).get('model') != IMAGE_MODEL or
        evidence.get('visual_worker_profile_record', {}).get('provider') != 'openai' or
        evidence.get('visual_facts', {}).get('provider_calls') != 0 or
        evidence.get('visual_facts', {}).get('retained_assets') != 0 or
        evidence.get('visual_facts', {}).get('independent_review_calls') != 0 or
        evidence.get('recovery_episodes', [])[-1].get('recovery_action') != 'OWNER_ACTION_REQUIRED' or
        evidence.get('recovery_episodes', [])[-1].get('task') != 'specification'):
        raise ValueError('Retained ZANA smoke checkpoint or cumulative accounting differs from the authorized scope')
    if (Path(root) / 'visual_cost_ledger.jsonl').exists() or (Path(root) / 'visual').exists():
        raise ValueError('Unexpected historical visual usage')


def submit_decision(root, build_id, payload):
    runtime = require_current_normal_app_runtime()
    evidence, order, action, choices, block_id = _context(root, build_id)
    index = checkpoint_index(root)
    base = _checkpoint(root, index)
    if _decision(root).exists():
        raise ValueError('Owner blocker already has a persisted decision')
    choice = payload.get('decision_type')
    if choice not in choices or payload.get('blocker_id') != block_id or payload.get('evidence_sha256') != _hash(base / 'live_run_evidence.json'):
        raise ValueError('Incompatible or stale owner decision')
    note = payload.get('owner_text', '')
    if not isinstance(note, str) or len(note) > 4000:
        raise ValueError('Invalid owner note')
    resolutions = payload.get('resolutions', {})
    if choice == 'RESOLVE_SPEC_DECISION':
        uncertainties = (evidence.get('product_spec') or {}).get('uncertainties', [])
        if not isinstance(resolutions, dict) or set(resolutions) != set(uncertainties) or any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in resolutions.values()):
            raise ValueError('Every recorded ProductSpec uncertainty needs a structured owner resolution')
    elif resolutions not in ({}, None):
        raise ValueError('Evaluator repair does not accept specification changes')
    authorization = payload.get('visual_authorization')
    if choice == RECOVERY_CONTINUATION:
        if authorization != SMOKE_AUTHORIZATION:
            raise ValueError('Exact visual worker, reviewer and smoke authorization is required')
    elif authorization is not None:
        raise ValueError('Visual authorization is incompatible with this decision')
    _verify_checkpoint(base, evidence, order, choice)
    for earlier in range(index):
        _verify_history(_checkpoint(root, earlier), _read(_origin(root, earlier)))
    files = {name: _hash(base / name) for name in HASHED_ORIGIN if (base / name).is_file()}
    for directory in ('calls', 'artifacts', 'recovery'):
        for path in (base / directory).rglob('*'):
            if path.is_file(): files[str(path.relative_to(base)).replace('\\', '/')] = _hash(path)
    origin = {'schema_version': SEGMENT_VERSION, 'logical_run_id': build_id, 'segment_id': segment_id(index),
              'status': evidence['status'], 'runtime_identity': evidence['runtime_identity'], 'file_hashes': files,
              'package_sha256': evidence['packaging']['package_sha256'] if evidence.get('packaging') else None}
    if _origin(root, index).exists():
        if _read(_origin(root, index)) != origin: raise ValueError('Historical segment anchor changed')
    else: write_new(_origin(root, index), origin)
    record = {'schema_version': VERSION, 'logical_run_id': build_id, 'owner_block_id': block_id,
              'owner_action_reason_code': action['reason'], 'decision_type': choice, 'owner_text': note,
              'resolutions': resolutions or {}, 'timestamp': time.time(), 'runtime_identity': runtime,
              'evidence_sha256': _hash(base / 'live_run_evidence.json'), 'previous_segment_id': segment_id(index),
              'continuation_segment_id': segment_id(index + 1), 'budget_before_resume': _budget(evidence),
              'authorization_scope': 'zana_smoke_specification_reentry_with_governed_visuals' if choice == RECOVERY_CONTINUATION else 'evaluator_coverage_and_affected_station_repair_within_existing_budget' if choice == 'AUTHORIZE_EVALUATOR_REPAIR' else 'resolve_specification_and_continue_within_existing_budget',
              'visual_authorization': dict(authorization) if choice == RECOVERY_CONTINUATION else None}
    write_new(_decision(root), record)
    return record


def prepare_resume(root, build_id):
    runtime = require_current_normal_app_runtime()
    evidence, order, action, choices, block_id = _context(root, build_id)
    index = checkpoint_index(root)
    base = _checkpoint(root, index)
    decision = _read(_decision(root))
    origin = _read(_origin(root, index))
    if (decision['logical_run_id'] != build_id or decision['owner_block_id'] != block_id or
            decision['evidence_sha256'] != _hash(base / 'live_run_evidence.json') or
            decision['previous_segment_id'] != segment_id(index)):
        raise ValueError('Owner decision or blocker identity changed')
    if decision['decision_type'] not in choices:
        raise ValueError('Decision no longer matches the retained blocker')
    if decision['decision_type'] == RECOVERY_CONTINUATION and decision.get('visual_authorization') != SMOKE_AUTHORIZATION:
        raise ValueError('Persisted visual authorization or smoke limits are incomplete')
    if decision['runtime_identity']['fingerprint'] != runtime['fingerprint']:
        raise ValueError('Runtime changed after owner decision; this decision is bound to its recorded source identity')
    if decision['budget_before_resume'] != _budget(evidence) or origin['logical_run_id'] != build_id or origin['runtime_identity'] != evidence['runtime_identity']:
        raise ValueError('Historical budget or runtime identity changed')
    if decision['decision_type'] == 'RESOLVE_SPEC_DECISION':
        _retained_visual_authorization(root, build_id, evidence, order)
    for earlier in range(index):
        _verify_history(_checkpoint(root, earlier), _read(_origin(root, earlier)))
    _verify_history(base, origin)
    if origin['package_sha256'] is not None and verify_package(base / 'app') != origin['package_sha256']:
        raise ValueError('Retained package changed')
    _verify_checkpoint(base, evidence, order, decision['decision_type'])
    segment = _segment(root)
    if (segment / 'resume_started.json').exists():
        raise ValueError('Continuation already started; duplicate resume rejected')
    write_new(segment / 'resume_started.json', {'schema_version': SEGMENT_VERSION, 'logical_run_id': build_id,
              'previous_segment_id': segment_id(index), 'segment_id': segment_id(index + 1), 'decision_sha256': _hash(_decision(root)),
              'runtime_identity': runtime, 'started_at': time.time()})
    return evidence, order, decision, segment


def _copy_package(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    manifest = _read(source / 'package.json')
    destination.mkdir(parents=True, exist_ok=False)
    for name in [*manifest['files'], 'package.json', 'run_app.py', 'README.md', 'start.cmd']:
        file = (source / name).resolve()
        target = (destination / name).resolve()
        if not file.is_relative_to(source) or not target.is_relative_to(destination):
            raise ValueError('Unsafe package file path')
        if file.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
    if verify_package(destination) != verify_package(source):
        raise ValueError('Continuation package identity mismatch')


def _segment_events(segment, rows):
    previous = _hash(segment / 'owner_decision.json')
    events = []
    for row in rows:
        event = {**row, 'previous_hash': previous}
        event['sha256'] = sha(event)
        previous = event['sha256'];events.append(event)
    with (segment / 'events.jsonl').open('x', encoding='utf-8') as stream:
        for event in events: stream.write(json.dumps(event, ensure_ascii=False) + '\n')
    return events


def review_defect_class(review, requalified):
    """Classify by observed failure and artifact ownership, never by product name."""
    if not review['counts'].get('FAIL'):
        return 'HARNESS_OR_GATE_DEFECT'
    return 'SHARED_RENDERER_OR_PACKAGE_CONTRACT_DEFECT' if requalified else 'PRODUCT_DEFECT'


def _blocked_local_resume(segment, evidence, decision, build_id, started, exc, stage, progress=None):
    """Retain a precise terminal segment if provider-free repair cannot pass."""
    reason = type(exc).__name__ + ': ' + str(exc)
    owner = {'reason': 'SHARED_RENDERER_OR_PACKAGE_CONTRACT_DEFECT',
             'station': 'trusted_runtime', 'cause': reason,
             'decision_needed': 'Repair the shared renderer or package contract and reverify its frozen gates and browser acceptance.',
             'attempted': [{'action': 'LOCAL_REPAIR', 'result': 'FAIL'}],
             'stronger_profile_used': False,
             'evidence': ['shared_renderer_requalification.json' if (segment / 'shared_renderer_requalification.json').exists()
                          else 'frozen_gate_result.json' if (segment / 'frozen_gate_result.json').exists()
                          else 'resume_started.json']}
    gates = _read(segment / 'frozen_gate_result.json') if (segment / 'frozen_gate_result.json').is_file() else None
    result = {**evidence, 'schema_version': SEGMENT_VERSION, 'build_id': build_id,
              'status': 'OWNER_ACTION_REQUIRED', 'verified': False, 'app_state': 'OWNER_ACTION_REQUIRED',
              'owner_action': owner, 'review': None, 'packaging': None,
              'failure_stage': stage, 'failure_class': owner['reason'], 'detail': reason,
              'gate_states': gates['gate_states'] if gates else {key: 'NOT_RUN' for key in evidence['gate_states']},
              'gates': gates['gates'] if gates else {key: False for key in evidence['gates']},
              'runtime_identity': normal_app_runtime_status(),
              'elapsed_seconds': round(evidence['elapsed_seconds'] + time.time() - started, 3),
              'previous_segment_id': decision['previous_segment_id'], 'owner_decision_sha256': _hash(segment / 'owner_decision.json'),
              'events': _segment_events(segment, [{'event': 'continuation_started', 'ts': started, 'segment_id': decision['continuation_segment_id']},
                          {'event': 'shared_renderer_requalification_failed', 'ts': time.time(), 'reason': reason},
                          {'event': 'owner_action_required', 'ts': time.time()}])}
    write_new(segment / 'owner_action.json', owner)
    write_new(segment / 'live_run_evidence.json', result)
    if progress: progress('owner_action_required')
    return result


def _restore_smoke_runner(root, build_id, prepared, *, client_factory, key, progress):
    """Reconstruct the exact smoke runner from the persisted decision and origin."""
    from scripts.run_zana_openai_smoke import PROMPT, SmokeRunner
    evidence, order, decision, segment = prepared
    runtime = require_current_normal_app_runtime()
    if (decision.get('runtime_identity', {}).get('fingerprint') != runtime['fingerprint'] or
        decision.get('visual_authorization') != SMOKE_AUTHORIZATION or
        decision.get('budget_before_resume') != _budget(evidence) or
        _hash(_decision(root)) != _read(segment / 'resume_started.json')['decision_sha256']):
        raise ValueError('Continuation identity, authorization or cumulative budget changed')
    _verify_history(root, _read(_origin(root, 0)))
    _verify_checkpoint(root, evidence, order, RECOVERY_CONTINUATION)
    if not key or client_factory is None and (os.environ.get('REDI_LIVE_PROVIDER') != '1' or os.environ.get('REDI_ZANA_VISUAL_AUTHORIZED') != '1'):
        raise ValueError('Governed credential and separate live visual authorization are required')
    provider = GovernedProvider(key, order['ceiling_usd'], client_factory,
                                recovery_profiles=order['recovery_profiles'], prior_state=evidence)
    if provider.spent != Decimal('0.1154598') or provider.facts['calls'] != 3 or provider.reserved:
        raise ValueError('Cumulative provider accounting could not be reconstructed')
    authorization = decision['visual_authorization']
    worker = OpenAIVisualWorker(provider, segment, visual_ceiling=authorization['visual_ceiling_usd'],
                                max_calls=authorization['max_total_visual_generation_calls'],
                                offline=client_factory is not None)
    reviewer = OpenAIVisualReviewer(provider, worker)
    if reviewer.profile != authorization['visual_reviewer_profile']:
        raise ValueError('Independent visual reviewer profile changed')
    worker._preflight()  # Read-only access gate before resumed spending.
    sources = order['sources'][1:]  # The owner note is audit evidence, not a ProductSpec change.
    return SmokeRunner(segment, PROMPT, provider, progress=progress,
                       execution_mode='injected_offline' if client_factory else 'real_provider', sources=sources,
                       ceiling=order['ceiling_usd'], recovery_budget=RecoveryBudget(**order['recovery_budget']),
                       recovery_profiles=order['recovery_profiles'], logical_run_id=build_id, prior=evidence,
                       visual_worker=worker, visual_reviewer=reviewer)


def execute_resume(root, build_id, prepared, *, client_factory=None, key=None, progress=None, on_runner=None):
    """Run only the checkpoint's remaining work. Caller supplies a test client or live key.

    ``on_runner`` receives the live runner before it starts so a UI can read
    its real task states while the continuation executes.
    """
    evidence, order, decision, segment = prepared
    base = _checkpoint(root, int(decision['previous_segment_id']))
    started = time.time()
    choice = decision['decision_type']
    result = None
    if choice == RECOVERY_CONTINUATION:
        runner = _restore_smoke_runner(root, build_id, prepared, client_factory=client_factory,
                                       key=key, progress=progress)
        if on_runner: on_runner(runner)
        result = runner.run()
    elif choice == 'RESOLVE_SPEC_DECISION':
        if not key or os.environ.get('REDI_LIVE_PROVIDER') != '1' and client_factory is None:
            raise ValueError('Provider credential and live flag required for specification continuation')
        provider = GovernedProvider(key, order['ceiling_usd'], client_factory,
                                    recovery_profiles=order['recovery_profiles'], prior_state=evidence)
        visual_worker, visual_reviewer = _restore_visual_context(
            root, build_id, evidence, order, segment, provider, client_factory=client_factory)
        original_prompt = next(s['text'] for s in order['sources'] if s['kind'] == 'owner_prompt')
        resolution = json.dumps(decision['resolutions'], ensure_ascii=False, sort_keys=True)
        sources = [*order['sources'][1:], source_record(resolution, kind='owner_decision', name='structured owner resolution')]
        runner = ProfessionalRunner(segment, original_prompt + '\nOWNER RESOLUTIONS:\n' + resolution, provider,
                                    progress=progress, execution_mode='injected_offline' if client_factory else 'real_provider',
                                    sources=sources, ceiling=order['ceiling_usd'],
                                    recovery_budget=RecoveryBudget(**order['recovery_budget']),
                                    recovery_profiles=order['recovery_profiles'], logical_run_id=build_id, prior=evidence,
                                    visual_worker=visual_worker, visual_reviewer=visual_reviewer)
        if on_runner: on_runner(runner)
        result = runner.run()
    else:
        if progress: progress('product_review_started')
        spec = evidence['product_spec']
        old_runtime = _hash(base / 'app' / 'product_app' / 'runtime.py')
        from . import runtime as shared_runtime
        new_runtime = _hash(shared_runtime.__file__)
        requalified = None
        episodes = list(evidence['recovery_episodes'])
        if old_runtime != new_runtime:
            budget = RecoveryBudget(**order['recovery_budget'])
            prior_repairs = sum(item.get('task') == 'shared_renderer' and item.get('recovery_action') == 'LOCAL_REPAIR' for item in episodes)
            if prior_repairs >= budget.local_repair:
                raise ValueError('Shared renderer local-repair budget exhausted')
            if progress: progress('shared_renderer_requalification_started')
            try:
                requalified = rebuild_shared_renderer(base / 'app', segment, spec,
                                                       _read(base / 'frozen_gate_contract.json'))
            except Exception as exc:
                return _blocked_local_resume(segment, evidence, decision, build_id, started, exc,
                                             'shared_renderer_requalification', progress)
            episodes.append({'schema_version': 'recovery_episode_v1', 'episode_id': 'shared-renderer-0001',
                             'run_id': build_id, 'task': 'shared_renderer', 'authority': 'factory_trusted_runtime',
                             'recovery_action': 'LOCAL_REPAIR', 'result': 'PASS', 'provider_calls': 0,
                             'evidence': ['shared_renderer_requalification.json', 'frozen_gate_result.json'],
                             'original_runtime_sha256': old_runtime, 'new_runtime_sha256': new_runtime})
            if progress: progress('shared_renderer_requalification_finished:pass')
        else:
            _copy_package(base / 'app', segment / 'app')
        try:
            collected = collect(segment / 'app', spec, 'wb-' + build_id, segment / 'acceptance')
        except Exception as exc:
            return _blocked_local_resume(segment, evidence, decision, build_id, started, exc,
                                         'product_review_execution', progress)
        current_gates = _read(segment / 'frozen_gate_result.json') if requalified else None
        review = collected['review']
        write_new(segment / 'reviewer_inputs.json', collected['evidence'])
        status = 'VERIFIED' if review['verdict'] == 'PASS' and all(r['status'] == 'PASS' for r in review['criteria'] if r['applicability'] == 'REQUIRED') else 'OWNER_ACTION_REQUIRED'
        reason = None if status == 'VERIFIED' else ('PRODUCT_DEFECT' if review['counts'].get('FAIL') else 'EVALUATOR_COVERAGE_INCOMPLETE')
        defect_class = review_defect_class(review, requalified)
        owner = None if status == 'VERIFIED' else {'reason': reason, 'station': 'trusted_runtime' if defect_class == 'SHARED_RENDERER_OR_PACKAGE_CONTRACT_DEFECT' else 'medaurus',
                'defect_class': defect_class,
                'cause': 'Required acceptance observations remain unresolved: ' + ', '.join(r['requirement_id'] for r in review['unresolved']),
                'decision_needed': ('Repair the trusted product renderer and package contract, then reverify the app; this immutable package cannot satisfy the frozen ProductSpec.'
                                    if reason == 'PRODUCT_DEFECT' else 'Extend real evaluator assertions and rerun the frozen reviewer.'),
                'attempted': [], 'stronger_profile_used': False, 'evidence': ['acceptance/review_evidence.json']}
        result = {**evidence, 'schema_version': SEGMENT_VERSION, 'build_id': build_id,
                  'execution_mode': 'injected_offline' if client_factory else 'real_provider',
                  'status': status, 'verified': status == 'VERIFIED', 'app_state': 'APP_READY' if status == 'VERIFIED' else 'OWNER_ACTION_REQUIRED',
                  'owner_action': owner, 'review': review, 'detail': '' if status == 'VERIFIED' else owner['cause'],
                  'failure_stage': None if status == 'VERIFIED' else 'product_review',
                  'failure_class': None if status == 'VERIFIED' else owner['defect_class'],
                  'recovery_episodes': episodes, 'shared_renderer_requalification': requalified,
                  'gates': current_gates['gates'] if current_gates else evidence['gates'],
                  'gate_states': current_gates['gate_states'] if current_gates else evidence['gate_states'],
                  'elapsed_seconds': round(evidence['elapsed_seconds'] + time.time() - started, 3),
                  'runtime_identity': normal_app_runtime_status(),
                  'events': _segment_events(segment, [{'event': 'continuation_started', 'ts': started, 'segment_id': decision['continuation_segment_id']},
                             *([{'event': 'shared_renderer_requalification_finished:pass', 'ts': time.time(),
                                 'runtime_sha256': new_runtime, 'package_sha256': requalified['new_package_sha256']}]
                               if requalified else []),
                             {'event': 'product_review_started', 'ts': started, 'persona': 'medaurus'},
                             {'event': 'product_review_finished:pass' if status == 'VERIFIED' else 'owner_action_required', 'ts': time.time(), 'persona': 'medaurus'}]),
                  'packaging': {**evidence['packaging'], 'project_path': str(segment / 'app'),
                                'package_sha256': verify_package(segment / 'app'),
                                **({'smoke': requalified['smoke'], 'browser': requalified['browser']} if requalified else {})},
                  'previous_segment_id': decision['previous_segment_id'], 'owner_decision_sha256': _hash(segment / 'owner_decision.json')}
        if owner: write_new(segment / 'owner_action.json', owner)
        write_new(segment / 'live_run_evidence.json', result)
        if status == 'VERIFIED':
            write_new(segment / 'packaging_evidence.json', {'status': 'PASS', 'project_path': str(segment / 'app'),
                      'package_sha256': verify_package(segment / 'app'), 'evidence_sha256': _hash(segment / 'live_run_evidence.json')})
        if progress: progress('product_review_finished:pass' if status == 'VERIFIED' else 'owner_action_required')
    return result
