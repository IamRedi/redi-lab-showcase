"""One governed ZANA smoke run; never starts paid work without explicit arming."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import uuid

from redi_lab.workbench.professional.openai_visual import OpenAIVisualReviewer, OpenAIVisualWorker
from redi_lab.workbench.professional.pipeline import ProfessionalRunner
from redi_lab.workbench.professional.provider import GovernedProvider
from redi_lab.workbench.professional.recovery import OwnerBlock
from redi_lab.workbench.runtime_identity import require_current_normal_app_runtime

ROOT = Path(__file__).resolve().parents[1]
PROMPT = (
    'Create ZANA Visual Smoke: a deliberately small, polished one-page local landing page '
    'for an independent design journal. Use one journal view and minimal local content. '
    'Require exactly two generated PNG visuals: hero_art, a 16:9 opaque hero image '
    'of calm abstract blue geometric forms; and section_art, a 4:3 opaque secondary '
    'section illustration of complementary pale-gold geometric forms. Both have no '
    'letters, logos, or people, minimum 320x180, one variant each, blocking, and '
    'visual-first layout dependency. Teuta must bind both asset IDs to the journal '
    'hero and section slots. Gentius must package them; browser acceptance must '
    'observe both exact retained images, visible and responsive. Keep all other '
    'functionality and data deliberately minimal. No external integrations.'
)


class SmokeRunner(ProfessionalRunner):
    def plan_for_run(self, spec):
        requirements=spec.get('visual_requirements',[])
        if (len(spec.get('views',[]))!=1 or
            {r['id'] for r in requirements}!={'hero_art','section_art'} or
            {r['view_id'] for r in requirements}!={spec['views'][0]['id']} or
            {r['slot_id'] for r in requirements}!={'hero','section'} or
            {slot['id']:slot['region'] for slot in spec['views'][0].get('slots',[])}.get('hero')!='header' or
            {slot['id']:slot['region'] for slot in spec['views'][0].get('slots',[])}.get('section')!='content' or
            {r['id']:r['aspect_ratio'] for r in requirements}!={'hero_art':[16,9],'section_art':[4,3]} or
            any(r['variants']!=1 or not r['blocking'] or r['transparent'] or
                r['format']!='png' or r['layout_dependency']!='visual_first' for r in requirements)):
            raise OwnerBlock('Smoke ProductSpec must have one view and exactly two required single-variant visuals',
                             'SPEC_DECISION_REQUIRED')
        return super().plan_for_run(spec)


def positive(value):
    try: amount=Decimal(value)
    except (InvalidOperation,TypeError):raise ValueError('A numeric owner ceiling is required')
    if not amount.is_finite() or amount<=0:raise ValueError('A positive owner ceiling is required')
    return amount


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--live',action='store_true',help='explicit paid run arming')
    parser.add_argument('--check-access',action='store_true',help='read-only model metadata check')
    parser.add_argument('--product-ceiling-usd',help='owner-authorized overall product ceiling')
    args=parser.parse_args()
    if not args.live and not args.check_access:
        print('ZANA live smoke prepared but unarmed: key access, explicit visual authorization and both ceilings are required; no calls made.')
        return 0
    key=os.environ.get('REDI_OPENAI_API_KEY')
    if not key:
        print('OWNER ACTION REQUIRED: governed OpenAI key absent; no provider call made.')
        return 2
    require_current_normal_app_runtime()
    if args.check_access and not args.live:
        from openai import OpenAI
        try: model=OpenAI(api_key=key,max_retries=0,timeout=20).models.retrieve('gpt-image-2')
        except Exception:
            print('ACCESS UNCONFIRMED: gpt-image-2 model metadata request failed; no image generation attempted.')
            return 2
        print('MODEL METADATA CONFIRMED' if getattr(model,'id',None)=='gpt-image-2' else 'ACCESS UNCONFIRMED: model identity mismatch')
        return 0 if getattr(model,'id',None)=='gpt-image-2' else 2
    if os.environ.get('REDI_LIVE_PROVIDER')!='1' or os.environ.get('REDI_ZANA_VISUAL_AUTHORIZED')!='1':
        print('OWNER ACTION REQUIRED: live provider and visual authorization must both be explicitly armed; no paid call made.')
        return 2
    visual=positive(os.environ.get('REDI_ZANA_VISUAL_CEILING_USD'))
    product=positive(args.product_ceiling_usd)
    if visual<Decimal('3.00') or product<Decimal('5.00') or visual>product:
        print('OWNER ACTION REQUIRED: smoke ceiling must be at least USD 3.00 visual and USD 5.00 overall; no paid call made.')
        return 2
    run_root=ROOT/'build'/'workbench-builds'/('wb-'+uuid.uuid4().hex[:12])
    provider=GovernedProvider(key,str(product))
    worker=OpenAIVisualWorker(provider,run_root,visual_ceiling=str(visual),max_calls=3)
    worker._preflight()  # read-only model access check before text or image spending
    reviewer=OpenAIVisualReviewer(provider,worker)
    print('Starting one governed ZANA smoke run:',run_root.name,flush=True)
    result=SmokeRunner(run_root,PROMPT,provider,ceiling=str(product),execution_mode='real_provider',
                       visual_worker=worker,visual_reviewer=reviewer).run()
    print('status=',result['status'],'visual_calls=',result['visual_facts']['provider_calls'],
          'known_total_usd=',result['total_cost_usd'],
          'outstanding_reservations_usd=',result['outstanding_reservations_usd'],flush=True)
    return 0 if result['status']=='VERIFIED' else 1


if __name__=='__main__':raise SystemExit(main())
