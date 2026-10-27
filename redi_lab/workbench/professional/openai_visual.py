"""OpenAI Images API adapter behind ZANA's provider-neutral VisualWorker."""
from __future__ import annotations

import base64
import binascii
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import time

from ...mcw_pricing import calculate_cost
from ..runtime_identity import require_current_normal_app_runtime
from .recovery import OwnerBlock
from .spec import MODEL as REVIEW_MODEL
from .visual import VisualFailure, validate_task

PROFILE_ID = 'openai_gpt_image_2_visual_v1'
MODEL = 'gpt-image-2'
RESERVATION_USD = Decimal('1.00')
PRICING_POLICY = 'openai_gpt_image_2_standard_2026_09_v1'
# The documented API accepts flexible multiples-of-16 sizes. This adapter
# deliberately accepts only these verified, bounded request profiles.
SIZES = {(16, 9): (1536, 864), (4, 3): (1536, 1152),
         (1, 1): (1024, 1024), (3, 2): (1536, 1024), (2, 3): (1024, 1536)}
PROFILE = {
    'schema_version': 'visual_worker_profile_v1', 'id': PROFILE_ID,
    'provider': 'openai', 'model': MODEL, 'adapter_version': 'openai_visual_worker_v1',
    'capabilities': ['text_to_image', 'opaque_background'],
    'aspect_ratios': [f'{a}:{b}' for a, b in SIZES],
    'sizes': [f'{w}x{h}' for w, h in SIZES.values()], 'output_formats': ['png'],
    'quality': 'medium', 'estimated_cost_policy': PRICING_POLICY,
    'reservation_per_call_usd': str(RESERVATION_USD),
    'availability': 'not_checked',
}


def compile_request(task, *, failure_evidence=None):
    """Reject unsupported visual tasks before a paid request is possible."""
    validate_task(task)
    if task['expected_format'] != 'png' or task['transparent']:
        raise VisualFailure('requirement_mismatch', 'Visual adapter supports opaque PNG only')
    ratio = task['aspect_ratio'];factor = math.gcd(*ratio)
    size = SIZES.get((ratio[0] // factor, ratio[1] // factor))
    if size is None or task['min_width'] > size[0] or task['min_height'] > size[1]:
        raise VisualFailure('dimensions', 'Visual dimensions or aspect ratio unsupported by authorized profile')
    parts = [
        'Create one original visual asset for a local product page.',
        'Subject: ' + task['subject'], 'Purpose: ' + task['purpose'],
        'Style direction: ' + task['style'],
        'Placement: ' + task['target_placement'].split(':', 1)[1],
        'Asset type: ' + task['asset_type'],
        'Content constraints: ' + '; '.join(task['content_constraints']),
        'Forbidden content: ' + '; '.join(task['forbidden_content']),
        'Text in image: ' + ('allowed' if task['text_in_image'] else 'forbidden'),
    ]
    if failure_evidence:
        allowed = {key: str(failure_evidence[key])[:300] for key in ('failed_check', 'exact_cause') if key in failure_evidence}
        if allowed: parts.append('Correct the previous check: ' + json.dumps(allowed, sort_keys=True))
    prompt = '\n'.join(parts)
    if len(prompt.encode('utf-8')) > 4000:
        raise VisualFailure('requirement_mismatch', 'Visual prompt exceeds adapter bound')
    return {'model': MODEL, 'prompt': prompt, 'size': f'{size[0]}x{size[1]}',
            'quality': 'medium', 'background': 'opaque', 'output_format': 'png', 'n': 1}


def accounted_cost(usage):
    """Use current standard GPT Image 2 token rates only with complete usage."""
    if usage is None:return None
    details = getattr(usage, 'input_tokens_details', None)
    text = getattr(details, 'text_tokens', None)
    image = getattr(details, 'image_tokens', None)
    output = getattr(usage, 'output_tokens', None)
    if any(type(value) is not int or value < 0 for value in (text, image, output)):
        return None
    if getattr(usage, 'input_tokens', None) != text + image:
        return None
    return ((Decimal(text) * Decimal('2.50') + Decimal(image) * Decimal('4.00') +
             Decimal(output) * Decimal('15.00')) / Decimal(1_000_000)).quantize(Decimal('0.00000001'))


class OpenAIVisualWorker:
    """No key store: shares REDI's governed OpenAI client and product ceiling."""
    profile = PROFILE_ID
    billing_managed = True

    def __init__(self, provider, run_root, *, visual_ceiling, max_calls=3, max_recovery_generations=1, offline=False):
        self.provider = provider
        self.root = Path(run_root).resolve()
        self.visual_ceiling = Decimal(str(visual_ceiling))
        self.max_calls = max_calls
        self.max_recovery_generations = max_recovery_generations
        if offline and not getattr(provider, "client_injected", False):
            raise ValueError("Offline visual mode requires an injected test client")
        self.offline = offline
        self.live_authorized = offline or os.environ.get('REDI_ZANA_VISUAL_AUTHORIZED') == '1'
        self.calls = 0
        self.recovery_calls = 0
        self.attempts = {}
        self.availability = 'not_checked'
        if (not self.visual_ceiling.is_finite() or self.visual_ceiling <= 0 or
                type(max_calls) is not int or not 1 <= max_calls <= 3 or
                type(max_recovery_generations) is not int or not 0 <= max_recovery_generations < max_calls):
            raise ValueError('Invalid visual ceiling or call limit')

    def profile_record(self):
        return {**PROFILE, 'availability': self.availability}

    def _preflight(self):
        require_current_normal_app_runtime()
        if not self.provider.key:
            raise OwnerBlock('Governed OpenAI credential is missing', 'CREDENTIAL_REQUIRED')
        if not self.live_authorized or (not self.offline and os.environ.get('REDI_LIVE_PROVIDER') != '1'):
            raise OwnerBlock('Explicit visual and live provider arming required', 'MANUAL_APPROVAL_REQUIRED')
        if self.availability != 'model_metadata_confirmed':
            try:
                model = self.provider.client.models.retrieve(MODEL)
            except Exception as exc:
                self.availability = 'access_unconfirmed'
                status = getattr(exc, 'status_code', None)
                reason = 'CREDENTIAL_REQUIRED' if status == 401 else 'SAFETY_OR_POLICY_BLOCKER' if status in (403, 404) else 'MANUAL_APPROVAL_REQUIRED'
                raise OwnerBlock('GPT Image 2 model access could not be confirmed', reason) from exc
            if getattr(model, 'id', None) != MODEL:
                raise OwnerBlock('GPT Image 2 model identity did not match', 'MANUAL_APPROVAL_REQUIRED')
            self.availability = 'model_metadata_confirmed'

    def _record(self, row):
        path = self.root / 'visual_cost_ledger.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, sort_keys=True) + '\n')
            stream.flush();os.fsync(stream.fileno())

    def generate(self, task, *, variant_index=1, failure_evidence=None):
        request = compile_request(task, failure_evidence=failure_evidence)
        if self.provider.sanitize(request['prompt']) != request['prompt']:
            raise OwnerBlock('Visual task contains credential-like material', 'SAFETY_OR_POLICY_BLOCKER')
        self._preflight()
        if self.calls >= self.max_calls:
            raise OwnerBlock('Visual call limit reached', 'RECOVERY_EXHAUSTED')
        number = self.attempts.get(task['task_id'], 0) + 1
        recovery_generation = number > variant_index
        if recovery_generation and self.recovery_calls >= self.max_recovery_generations:
            raise OwnerBlock('Visual recovery generation limit reached', 'RECOVERY_EXHAUSTED')
        attempt_id = task['task_id'] + '@' + str(number)
        prompt_hash = hashlib.sha256(request['prompt'].encode()).hexdigest()
        try:
            self.provider.reserve_visual(attempt_id, RESERVATION_USD, self.visual_ceiling)
        except ValueError as exc:
            raise OwnerBlock('Visual or product ceiling is insufficient for the next reserved call', 'CREDITS_REQUIRED') from exc
        try:
            self._record({'event': 'reserved', 'attempt_id': attempt_id, 'reserved_usd': str(RESERVATION_USD),
                          'prompt_sha256': prompt_hash, 'model': MODEL, 'size': request['size'], 'pricing_policy': PRICING_POLICY})
        except OSError:
            self.provider.release_visual_before_call(attempt_id)
            raise VisualFailure('retention', 'Visual cost reservation could not be persisted')
        self.calls += 1;self.attempts[task['task_id']] = number
        if recovery_generation:
            self.recovery_calls += 1
        started = time.monotonic()
        try:
            response = self.provider.client.images.generate(**request)
        except Exception as exc:
            status = getattr(exc, 'status_code', None)
            code = getattr(exc, 'code', None)
            kind = ('budget_credit' if code in ('insufficient_quota', 'billing_hard_limit_reached', 'credits_exhausted') else
                    'provider_unavailable' if isinstance(exc, TimeoutError) or type(exc).__name__ in ('APITimeoutError', 'APIConnectionError') or status == 429 or status is not None and 500 <= status <= 599 else
                    'provider_restriction')
            self._record({'event': 'provider_failure', 'attempt_id': attempt_id, 'kind': kind,
                          'status_code': status, 'billing_state': 'uncertain', 'reserved_usd': str(RESERVATION_USD)})
            raise VisualFailure(kind, 'OpenAI visual request failed; reservation retained pending billing evidence') from exc
        duration = round(time.monotonic() - started, 3)
        cost = accounted_cost(getattr(response, 'usage', None))
        if cost is not None:
            try:self.provider.reconcile_visual(attempt_id, cost)
            except ValueError as exc:
                raise VisualFailure('budget_credit', 'OpenAI visual usage exceeded reservation') from exc
            self._record({'event': 'reconciled', 'attempt_id': attempt_id, 'cost_usd': str(cost)})
        else:
            self._record({'event': 'billing_uncertain', 'attempt_id': attempt_id,
                          'reserved_usd': str(RESERVATION_USD)})
        data = getattr(response, 'data', None)
        if not isinstance(data, list) or len(data) != 1 or not getattr(data[0], 'b64_json', None):
            raise VisualFailure('invalid_output', 'OpenAI visual response contained no image bytes')
        try: image = base64.b64decode(data[0].b64_json, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise VisualFailure('invalid_output', 'OpenAI visual response contained malformed image data') from exc
        call_id = getattr(response, '_request_id', None) or getattr(response, 'id', None)
        if not isinstance(call_id, str) or len(call_id) > 128 or any(ch in call_id for ch in '\r\n\\/'):
            call_id = None
        width, height = map(int, request['size'].split('x'))
        return {'bytes': image, 'worker_profile': PROFILE_ID, 'provider': 'openai', 'model': MODEL,
                'provider_call_id': call_id, 'provider_prompt_sha256': prompt_hash,
                'duration_seconds': duration, 'cost_usd': str(cost or Decimal(0)),
                'cost_evidence_state': 'USAGE_RECONCILED' if cost is not None else 'RESERVATION_PENDING',
                'reserved_cost_usd': '0' if cost is not None else str(RESERVATION_USD),
                'safe_parameters': {'width': width, 'height': height, 'quality': 'medium'}}


class OpenAIVisualReviewer:
    """Independent vision review; the generation worker never supplies a verdict."""
    profile = 'openai_independent_visual_review_v1'
    reservation = Decimal('0.50')

    def __init__(self, provider, worker):
        self.provider=provider;self.worker=worker

    def __call__(self, task, retained_path):
        self.worker._preflight()
        path=Path(retained_path)
        image=path.read_bytes()
        if not image or len(image)>12_000_000:
            raise VisualFailure('review_rejection','Retained visual is outside reviewer bounds')
        attempt_id='visual_review:'+task['task_id']+'@'+str(self.worker.attempts.get(task['task_id'],0))
        criteria={key:task[key] for key in ('purpose','subject','style','content_constraints','forbidden_content','text_in_image')}
        prompt='Independently inspect the attached image against these visual criteria. Return a JSON object with verdict PASS or FAIL and a concise observed_reason. Do not infer compliance without visual evidence. Criteria: '+json.dumps(criteria,ensure_ascii=False)
        if self.provider.sanitize(prompt) != prompt:
            raise OwnerBlock('Visual review criteria contain credential-like material', 'SAFETY_OR_POLICY_BLOCKER')
        with self.provider.lock:
            if attempt_id in self.provider.attempted:
                raise OwnerBlock('Visual review call already attempted','RECOVERY_EXHAUSTED')
            if self.provider.spent+self.provider.visual_spent+sum(self.provider.reserved.values(),Decimal(0))+sum(self.provider.visual_reserved.values(),Decimal(0))+self.reservation>self.provider.ceiling:
                raise OwnerBlock('Product ceiling cannot reserve independent visual review','CREDITS_REQUIRED')
            self.provider.attempted.add(attempt_id);self.provider.reserved[attempt_id]=self.reservation;self.provider.facts['calls']+=1
        try:self.worker._record({'event':'review_reserved','attempt_id':attempt_id,'reserved_usd':str(self.reservation),'model':REVIEW_MODEL,'image_sha256':hashlib.sha256(image).hexdigest()})
        except OSError:
            with self.provider.lock:
                self.provider.attempted.discard(attempt_id);self.provider.reserved.pop(attempt_id,None);self.provider.facts['calls']-=1
            raise VisualFailure('retention','Visual review reservation could not be persisted')
        started=time.monotonic()
        try:
            response=self.provider.client.chat.completions.create(model=REVIEW_MODEL,reasoning_effort='low',max_completion_tokens=512,
                response_format={'type':'json_object'},messages=[{'role':'user','content':[
                    {'type':'text','text':prompt},
                    {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(image).decode(),'detail':'high'}}]}])
            usage=response.usage
            if usage is None:raise VisualFailure('review_rejection','Independent review usage unavailable')
            amount=Decimal(calculate_cost(model=REVIEW_MODEL,usage={'input':usage.prompt_tokens,
                'cached_input':getattr(getattr(usage,'prompt_tokens_details',None),'cached_tokens',0) or 0,
                'output':usage.completion_tokens})['cost_amount'])
            if amount>self.reservation:raise VisualFailure('budget_credit','Independent review exceeded reservation')
            with self.provider.lock:
                self.provider.spent+=amount;self.provider.reserved.pop(attempt_id)
                self.provider.facts['per_task'][attempt_id]={'provider':'openai','model':REVIEW_MODEL,'profile_id':self.profile,
                    'cost_usd':str(amount),'input':usage.prompt_tokens,'cached_input':getattr(getattr(usage,'prompt_tokens_details',None),'cached_tokens',0) or 0,
                    'output':usage.completion_tokens,'reserved_cost_usd':str(self.reservation),'elapsed_seconds':round(time.monotonic()-started,3)}
            self.worker._record({'event':'review_reconciled','attempt_id':attempt_id,'cost_usd':str(amount)})
            if response.choices[0].finish_reason!='stop':raise VisualFailure('review_rejection','Independent visual review did not finish')
            verdict=json.loads(response.choices[0].message.content or '')
            if verdict.get('verdict') not in ('PASS','FAIL') or not isinstance(verdict.get('observed_reason'),str):
                raise VisualFailure('review_rejection','Independent visual review returned invalid evidence')
            return {'reviewer':self.profile,'model':REVIEW_MODEL,'verdict':verdict['verdict'],
                    'observed_reason':verdict['observed_reason'][:500],'cost_usd':str(amount)}
        except Exception as exc:
            with self.provider.lock:
                self.provider.facts['failures'][attempt_id]={'exception_class':type(exc).__name__,
                    'reason':self.provider.sanitize(exc),'reserved_cost_usd':str(self.provider.reserved.get(attempt_id,0)),
                    'billing_known':attempt_id in self.provider.facts['per_task'],'ts':time.time()}
            raise
