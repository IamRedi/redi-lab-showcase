"""Versioned, fail-closed product acceptance evidence. No provider dependency.

An assertion is an observation, not a worker's claim of completion. Every
required criterion names its assertions; absent observations remain NOT_RUN.
Owner launch is a post-approval action, never a pre-approval prerequisite.
"""
from __future__ import annotations
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
from .runtime import presentation_contract

VERSION = 'professional_product_review_evidence_v2'
REVIEWER_VERSION = 'medaurus_requirement_review_v2'
STATUSES = {'PASS', 'FAIL', 'NOT_APPLICABLE', 'NOT_RUN'}
MODES = {'REQUIRED', 'OPTIONAL', 'NOT_APPLICABLE'}
ASSERTION_FIELDS = {'assertion_id','status','evidence_type','evidence_location','observed','expected',
                    'failure_reason','requirement_ids','acceptance_criterion_ids'}
ENVELOPE_FIELDS = {'schema_version','source_build_id','run_id','timestamp','product_spec_version',
                   'product_spec_hash','artifact_identity','browser_test_identity','product_spec',
                   'requirements','assertions','references'}
# Public JSON Schema describes the same closed envelope validated below.
EVIDENCE_SCHEMA = {
    '$schema':'https://json-schema.org/draft/2020-12/schema',
    'title':VERSION,'type':'object','additionalProperties':False,
    'required':sorted(ENVELOPE_FIELDS),
    'properties':{key:{'type':'string'} for key in ENVELOPE_FIELDS},
}
EVIDENCE_SCHEMA['properties'].update({
    'schema_version':{'const':VERSION},
    **{key:{'type':'object'} for key in ('artifact_identity','browser_test_identity','product_spec','references')},
    'requirements':{'type':'array','items':{'type':'object','required':['requirement_id','criterion_id','applicability','spec_reference','reason','assertion_ids'],
        'properties':{'applicability':{'enum':sorted(MODES)}}}},
    'assertions':{'type':'array','items':{'type':'object','additionalProperties':False,'required':sorted(ASSERTION_FIELDS),
        'properties':{**{key:{} for key in ASSERTION_FIELDS},'status':{'enum':sorted(STATUSES)},
                      'requirement_ids':{'type':'array','items':{'type':'string'}},
                      'acceptance_criterion_ids':{'type':'array','items':{'type':'string'}}}}},
})


# A prohibition ("cannot move backward") is not a requirement to move back.
NEGATED_REVERSE = re.compile(r"(cannot|can't|can not|must not|may not|never|not allowed to|no)\s+(be\s+)?(move[ds]?|go(es)?|return(s|ed)?)\s*(back|backward)")
NEGATED_REVERSE_SHORT = re.compile(r"\b(not|never)\s+(back|backward)\b")


def has_reverse_transition(flow):
    """True when declared transitions lead from a later stage back to an earlier one."""
    order = {stage: index for index, stage in enumerate([flow["initial"], *flow["transitions"]])}
    return any(order.get(target, -1) < order.get(source, -1)
               for source, targets in flow["transitions"].items() for target in targets)

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def assertion(check, observed, expected, *, passed, location, reason=''):
    return {'assertion_id': check, 'status': 'PASS' if passed else 'FAIL',
            'evidence_type': 'observed_execution', 'evidence_location': location,
            'observed': observed, 'expected': expected, 'failure_reason': reason if not passed else ''}


def requirements(spec):
    """Compile the bounded local-product model; unknown prose fails closed.

Model properties create concrete obligations. Original prose criteria remain
separate requirements and are mapped only for supported semantics. No prose
criterion is silently dropped. Prohibited deletion overrides general CRUD.
"""
    rows = []
    presentation = presentation_contract(spec)
    primary = next(view for view in spec['views'] if view['id'] == spec['navigation'][0])
    primary_capabilities = presentation[primary['id']]
    def add(key, checks, ref, mode='REQUIRED', reason=''):
        rows.append({'requirement_id': key, 'criterion_id': key, 'applicability': mode,
                     'spec_reference': ref, 'reason': reason, 'assertion_ids': checks})
    base = ['startup', 'shutdown', 'restart', 'package_identity', 'navigation',
            'labels', 'semantic_controls', 'keyboard', 'focus', 'links_buttons',
            'transport', 'responsive', 'overflow', 'mobile_controls', 'language', 'no_raw_errors']
    for key in base: add(key, [key], '/non_functional_requirements')
    add('persistence', ['persistence'], '/persistence',
        'REQUIRED' if spec['persistence']['enabled'] else 'NOT_APPLICABLE',
        '' if spec['persistence']['enabled'] else 'ProductSpec /persistence/enabled is false.')
    capabilities = {key: [key] for key in base}
    capabilities['persistence'] = ['persistence']
    banned = {r['entity']: r['reason'] for r in spec['business_rules'] if r['kind']=='restrict_delete'}
    for i, ent in enumerate(spec['entities']):
        name = ent['id']
        # A vehicle criterion mentioning its customer relationship must not
        # accidentally require customer deletion. Bind operation prose to its
        # first named entity; generic record requirements cover all entities.
        scoped=[]
        for text in spec['acceptance_criteria']+spec['functional_requirements']:
            lower=text.lower();mentions=[]
            for candidate in spec['entities']:
                labels={candidate['label'].lower(),candidate['plural'].lower(),candidate['id'].replace('_',' ')}
                positions=[match.start() for label in labels for match in re.finditer(r'\b'+re.escape(label)+r'\b',lower)]
                if positions:mentions.append((min(positions),candidate['id']))
            if (mentions and min(mentions)[1]==name) or (not mentions and 'record' in lower):scoped.append(lower)
        declared=' '.join(scoped)
        for op in ('create', 'read', 'edit', 'validation'):
            mode='OPTIONAL' if op=='edit' and 'edit' not in declared else 'REQUIRED'
            add(op+':'+name, [op+':'+name], '/entities/'+str(i),mode)
        add('delete:'+name, ['delete:'+name] if name not in banned else ['delete_policy:'+name],
            '/business_rules' if name in banned else '/entities/'+str(i),
            'REQUIRED' if name in banned or 'delete' in declared else 'OPTIONAL',
            reason=banned.get(name, 'Standard editable records; no deletion prohibition declared.'))
        if any(f['type']=='reference' for f in ent['fields']):
            add('relationships:'+name, ['relationships:'+name], '/entities/'+str(i)+'/fields')
    for i, flow in enumerate(spec['workflows']):
        add('workflow:'+flow['entity'], ['workflow:'+flow['entity']], '/workflows/'+str(i))
    if spec['metrics']: add('dashboard', ['dashboard'], '/metrics')
    if spec.get('calculator'): add('calculator', ['calculator'], '/calculator')
    for state in spec['states']:
        mode='REQUIRED';reason=''
        if state=='destructive_confirmation' and not spec['entities']:
            mode='NOT_APPLICABLE';reason='ProductSpec /entities is empty: no destructive record operation.'
        if state in ('empty', 'populated') and not spec['entities']:
            mode='NOT_APPLICABLE';reason='ProductSpec /entities is empty: stateless calculation.'
        add('state:'+state, ['state:'+state], '/states/'+state,mode,reason)
    if spec['entities']: add('state:populated',['state:populated'],'/entities')
    for index, visual in enumerate(spec.get('visual_requirements', [])):
        add('visual:'+visual['id'], ['visual_asset:'+visual['id']],
            '/visual_requirements/'+str(index), 'REQUIRED' if visual['blocking'] else 'OPTIONAL')
    # Prose acceptance criteria are mapped conservatively, with concrete model
    # obligations as their evidence. Unsupported prose remains a missing check.
    for i, text in enumerate(spec['acceptance_criteria']):
        lower=text.lower(); selected=[]
        if re.search(r'board.*(column|stage|count)|(column|stage).*board',lower):
            selected += ['board_columns']
        if re.search(r'cards?.*(show|display|include)',lower):
            selected += ['board_card_fields']
        if 'search' in lower and not re.search(r'empty search|no (search )?results?',lower):
            selected += ['search','search_read_only']
        if re.search(r'filter by|filtering by|combined filter|clearing filters|clear.*filters',lower):
            selected += ['filter_reference' if any(field['type']=='reference' for entity in spec['entities'] if entity['id']==primary.get('entity') for field in entity['fields']) else 'unsupported_reference_filter',
                         'filter_status' if primary_capabilities.get('workflow_field') else 'unsupported_status_filter',
                         'combined_filters','clear_filters']
        if re.search(r'drawer|detail panel|detail experience',lower):
            selected += ['detail_panel','cancel_edit','detail_escape']
        if re.search(r'mobile.*(stage|board|cards?)',lower):
            selected += ['board_mobile','long_title_saved','overflow']
        # The structured workflow wins: a reverse obligation needs declared reverse transitions.
        if (re.search(r'back to.*earlier|earlier stage|move.*back',lower) and not NEGATED_REVERSE.search(lower)
                and not NEGATED_REVERSE_SHORT.search(lower) and any(has_reverse_transition(f) for f in spec['workflows'])):
            selected += ['workflow_reverse']
        if re.search(r'delet.*(contain|depend|linked|while it still has)',lower):
            subject = re.split(r'\b(?:contain\w*|with|linked|depend\w*)\b',lower,maxsplit=1)[0]
            for parent in spec['entities']:
                if any(field.get('references')==parent['id'] for child in spec['entities'] for field in child['fields']):
                    labels=(parent['label'].lower(),parent['plural'].lower())
                    if any(re.search(r'\b'+re.escape(label)+r'\b',subject) for label in labels):
                        selected += ['relationship_delete_block'];break
        if any(w in lower for w in ('launch','open app','packaged','tested','delivery')):
            selected += ['startup','package_identity','navigation']
        if any(w in lower for w in ('persist','restart','survive','sqlite')): selected += ['persistence','restart']
        if any(w in lower for w in ('dashboard','counts')): selected += ['dashboard'] if spec['metrics'] else []
        if spec['metrics'] and 'filter' in lower and any(w in lower for w in ('summar', 'metric', 'count', 'value')):
            selected += ['filtered_metrics']
        if any(w in lower for w in ('status','workflow','transition')): selected += ['workflow:'+f['entity'] for f in spec['workflows']]
        if any(w in lower for w in ('responsive',' ui','states')):
            selected += ['responsive','mobile_controls','overflow'] + [r['requirement_id'] for r in rows if r['requirement_id'].startswith('state:') and r['applicability']=='REQUIRED']
        if 'calculat' in lower and spec.get('calculator'): selected += ['calculator']
        if re.search(r'empty|optional|without', lower) and re.search(r'date', lower):
            selected += ['optional_date']
        if re.search(r'blank|whitespace', lower) and re.search(r'title|name|text', lower):
            selected += ['required_text_validation']
        if re.search(r'over.limit|too long|maximum length|truncat', lower):
            selected += ['text_length_validation']
        if re.search(r'empty search|no (search )?results?|nothing matches', lower):
            selected += ['empty_search_state']
        if re.search(r'all .*deletions?.*confirm|deletions? require confirmation', lower):
            selected += ['all_delete_confirmations']
        if re.search(r'real.browser verification covers|browser.*acceptance flow', lower):
            selected += ['browser_owner_journey']
        if re.search(r'generated (?:visuals?|images?|imagery)|hero image|visual assets?', lower):
            selected += ['visual_asset:'+visual['id'] for visual in spec.get('visual_requirements', [])]
        subject=re.split(r'\b(?:linked to|when they|with dependent)\b',lower,maxsplit=1)[0]
        entities=[e for e in spec['entities'] if e['label'].lower() in subject or e['plural'].lower() in subject or e['id'].replace('_',' ') in subject]
        if not entities and 'record' in lower: entities=spec['entities']
        for ent in entities:
            ops=[op for op in ('create','read','edit','delete') if op in lower or (op=='read' and 'view' in lower)]
            if not ops and any(w in lower for w in ('support','field','description','notes','price')):ops=['create','read','edit']
            selected += [('delete_policy:' if op=='delete' and ent['id'] in banned else op+':')+ent['id'] for op in ops]
            if 'linked' in lower and any(f['type']=='reference' for f in ent['fields']):selected+=['relationships:'+ent['id']]
        # Resolve composite state requirement IDs to assertion IDs (same here).
        add('acceptance:'+str(i+1), sorted(set(selected)) or ['unmapped_acceptance:'+str(i+1)], '/acceptance_criteria/'+str(i),reason=text)
    return rows


def envelope(spec, source_id, artifacts, run_id, observations, *, browser_identity, references):
    reqs=requirements(spec)
    linked=[]
    for row in observations:
        matching=[r for r in reqs if row['assertion_id'] in r['assertion_ids']]
        linked.append({**row,'requirement_ids':[r['requirement_id'] for r in matching],
                       'acceptance_criterion_ids':[r['criterion_id'] for r in matching]})
    return {'schema_version':VERSION,'source_build_id':source_id,'run_id':run_id,
            'timestamp':datetime.now(timezone.utc).isoformat(),
            'product_spec_version':spec['schema_version'],'product_spec_hash':digest(spec),
            'artifact_identity':artifacts,'browser_test_identity':browser_identity,
            'product_spec':spec,'requirements':reqs,'assertions':linked,'references':references}


def evaluate(evidence, evidence_root=None):
    """Authoritative Medaurus rule. Missing/malformed/tampered evidence fails."""
    if evidence.get('schema_version')!=VERSION: raise ValueError('Unsupported review evidence version')
    if set(evidence)!=ENVELOPE_FIELDS:raise ValueError('Malformed review evidence envelope')
    spec=evidence['product_spec']
    if evidence['product_spec_hash']!=digest(spec) or evidence['product_spec_version']!=spec['schema_version']:
        raise ValueError('ProductSpec evidence identity mismatch')
    if evidence['requirements']!=requirements(spec):raise ValueError('Requirement map changed or omitted')
    for name in ('source_build_id','run_id','timestamp','artifact_identity','browser_test_identity','references'):
        if not evidence.get(name):raise ValueError('Missing evidence identity: '+name)
    if evidence_root is not None:
        root=Path(evidence_root).resolve()
        for ref,sha in evidence['references'].items():
            path=(root/ref).resolve()
            if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:
                raise ValueError('Evidence reference changed: '+ref)
    observations={}
    for row in evidence['assertions']:
        if set(row)!=ASSERTION_FIELDS:raise ValueError('Malformed assertion fields')
        if row['status'] not in STATUSES or not row.get('evidence_location') or 'observed' not in row or 'expected' not in row:
            raise ValueError('Malformed assertion')
        if row['evidence_location'] not in evidence['references']:raise ValueError('Unbound observation reference')
        expected=[r for r in evidence['requirements'] if row['assertion_id'] in r['assertion_ids']]
        if row['requirement_ids']!=[r['requirement_id'] for r in expected] or row['acceptance_criterion_ids']!=[r['criterion_id'] for r in expected]:
            raise ValueError('Assertion requirement mapping mismatch')
        if row['status'] in ('FAIL','NOT_APPLICABLE') and not row.get('failure_reason'):raise ValueError('Missing assertion reason')
        observations.setdefault(row['assertion_id'],[]).append(row)
    results=[]
    for req in evidence['requirements']:
        if req['applicability'] not in MODES:raise ValueError('Unknown applicability')
        if req['applicability']=='NOT_APPLICABLE':
            if not req['reason'] or not req['spec_reference']:raise ValueError('Unjustified N/A')
            status='NOT_APPLICABLE'
        else:
            rows=[observations.get(key,[]) for key in req['assertion_ids']]
            status=('FAIL' if any(v['status']=='FAIL' for values in rows for v in values)
                    else 'NOT_RUN' if any(not values or any(v['status']!='PASS' for v in values) for values in rows)
                    else 'PASS')
        results.append({**req,'status':status})
    blockers=[r for r in results if r['applicability']=='REQUIRED' and r['status']!='PASS']
    blockers += [{'assertion_id':r['assertion_id'],'status':'FAIL','reason':r['failure_reason']}
                 for r in evidence['assertions'] if r['status']=='FAIL' and not r['requirement_ids']]
    return {'reviewer_version':REVIEWER_VERSION,'evidence_schema_version':VERSION,
            'evidence_hash':digest(evidence),'persona':'medaurus','execution_mode':'deterministic_offline',
            'status':'VERIFIED' if not blockers else 'FAILED','verdict':'PASS' if not blockers else 'FAIL',
            'criteria':results,'counts':dict(Counter(r['status'] for r in results)),
            'unresolved':blockers,'checked_criteria':[r['criterion_id'] for r in results],
            'provider_calls':0}
