"""Exact producer/consumer inputs for generated product modules."""
from __future__ import annotations
import ast
import json
from .spec import CONTRACT, sha
from .visual import placement_key
from .runtime import database_contract

TEUTA_V2 = {
    'version':'teuta_professional_ui_v2',
    'composition':'Use the trusted render_app component API. Literal Python DESIGN dictionary plus render wrapper only. No .format, f-strings, percent interpolation, Template, eval, external resources or custom scripts.',
    'design':'Product-specific page copy and information hierarchy derived from the ProjectSpec journeys. Choose a dark accessible accent color, e.g. #155e75, #166534 or #4338ca. No fake charts, metrics, users, notifications or sample records.',
    'states':'The component API provides real empty/populated states, server validation alerts, saving feedback, success notices, backend errors, and delete confirmation. Do not remove or replace these states.',
    'capabilities':'The trusted renderer composes search over each entity display field; enum/reference/boolean filters; summaries on the primary view, including boards and galleries; workflow-stage board columns; bounded text fields; and responsive detail panels. Keep the ProjectSpec display field, enum/reference fields, workflow, metrics and limits exact. Do not claim a search/filter capability outside this model.',
    'accessibility':'Semantic navigation, labeled fields, keyboard focus, minimum 44px controls, contrast >=4.5 and responsive tables/cards are mandatory.',
    'completion':'Actual Chromium must execute the full capability journey at 1440,1280,768,390,360px without overflow, missing labels, dead actions or errors BEFORE Teuta is VERIFIED.',
}


def task_prompt(task, spec, upstream, visual_handoffs=()):
    contracts = {
      'database.py': 'Export SCHEMA exactly equal to ProjectSpec.entities (including every field property). Export connect(path), returning runtime.open_database(path, SCHEMA). Import open_database from .runtime. No import-time I/O.',
      'workflow.py': 'Export WORKFLOWS exactly equal to ProjectSpec.workflows. Export can_transition(entity,field,old,new), returning runtime.transition_allowed(WORKFLOWS,entity,field,old,new). Import transition_allowed from .runtime.',
      'core.py': 'Import SPEC from .definition and DataService from .runtime. Export RULES exactly equal to ProjectSpec.business_rules. Export class Service(DataService) with __init__(self,path): super().__init__(path,SPEC,connector=connect,workflows=WORKFLOWS,rules=RULES). Import connect from .database only if data_model task exists; otherwise connector=None. Import WORKFLOWS from .workflow only if workflow task exists; otherwise WORKFLOWS=[]. Inherited methods save(entity,values,record_id=None)->int, record(entity,id)->dict, records(entity)->list, append_note(entity,id,field,note), delete(entity,id), state()->{records,metrics}, close(). Runtime enforces field validation, duplicate constraints, references, money, declared rules and transitions. Do not override these verified operations.',
      'ui.py': 'Import render_app from .runtime. Export DESIGN = {version:"teuta_professional_ui_v2",accent:"#155e75",views:[{id:spec view id,eyebrow:concise product-specific section label,intro:useful domain-specific guidance,empty_heading:meaningful empty state,empty_body:actionable next step}],visual_placements:{"view_id:slot_id":asset_id}}. Every spec view exactly once. For every visual requirement bind its exact declared view_id and slot_id pair to its asset ID; never guess filenames or placement from prose. Export render(spec,state,**context) returning render_app(spec,DESIGN,state,**context). Keep real workflows and meaningful domain labels. No fixture data.',
      'service.py': 'Import Service from .core, SPEC from .definition, render from .ui, acceptance from .runtime. Export build_app(path) returning Service(path). Export run_ui(path,**context): create app=build_app(path), return render(SPEC,app.state(),**context), finally close app. Export run_acceptance() returning acceptance(Service,SPEC). Export run_private() returning acceptance(Service,SPEC,private=True). Export integration_probe() returning acceptance(Service,SPEC). No app-specific fallback, literal success results, extra modules or import-time I/O.',
    }
    return ('You are '+task['persona']+'. Implement the bounded task '+task['task_id']+'.\n'
        'Return JSON only: {"patches":[{"rel_path":"'+task['owned_paths'][0]+'","action":"write","content":"Python source"}]}. '
        'Exactly one owned file. All modules live in product_app. The shared runtime is a verified reusable framework, not demo data. '
        'The executable source policy permits ONLY the declared relative imports, literal module-level configuration, functions and classes. '
        'Do not add absolute imports, typing imports, __future__ imports, module-level calls, computed configuration, f-strings, .format or percent interpolation. '
        'Use the exact small wrappers specified below; built-in type annotations are optional. '
        'Do not invent APIs or modify another station artifact. Preserve all source owner requirements.\n'
        'PROJECT SPEC:\n'+json.dumps(spec,ensure_ascii=False)+'\nTASK CONTRACT:\n'+json.dumps(task)+'\n'
        'BINDING INTERFACES '+CONTRACT+':\n'+json.dumps(contracts)+'\nEXECUTABLE DATABASE CONTRACT:\n'+json.dumps(database_contract(spec))+'\nTEUTA V2:\n'+json.dumps(TEUTA_V2)+'\n'
        'COMPLETE RETAINED PRODUCER HANDOFF:\n'+json.dumps({name:{'sha256':sha(source),'content':source} for name,source in upstream.items()})+
        '\nVISUAL ASSET HANDOFF (IDs and retained paths only; no image bytes):\n'+json.dumps(list(visual_handoffs)))


def validate_source(path, source):
    tree=ast.parse(source)
    allowed={'runtime','definition','database','workflow','core','ui'}
    for node in ast.walk(tree):
        if isinstance(node,ast.Import): raise ValueError('Only declared relative product imports are permitted')
        if isinstance(node,ast.ImportFrom) and (node.level!=1 or node.module not in allowed):
            raise ValueError('Undeclared producer import')
        if isinstance(node,(ast.JoinedStr,ast.AsyncFunctionDef)) or isinstance(node,ast.BinOp) and isinstance(node.op,ast.Mod):
            raise ValueError('Unsafe template or asynchronous generated code')
        if isinstance(node,ast.Call):
            name=node.func.id if isinstance(node.func,ast.Name) else node.func.attr if isinstance(node.func,ast.Attribute) else ''
            if name in {'format','format_map','eval','exec','compile','open','__import__','Template','substitute','safe_substitute'}:
                raise ValueError('Unsafe generated operation: '+name)
    # Imports, literal configuration, classes and functions only at module scope.
    for node in tree.body:
        if isinstance(node,(ast.ImportFrom,ast.FunctionDef,ast.ClassDef)): continue
        if isinstance(node,ast.Expr) and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str): continue
        if isinstance(node,ast.Assign):
            ast.literal_eval(node.value); continue
        if isinstance(node,ast.AnnAssign):
            ast.literal_eval(node.value); continue
        raise ValueError('Import-time side effect or nonliteral configuration')
    return tree


def literal(tree,name):
    for node in tree.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets): return ast.literal_eval(node.value)
        if isinstance(node,ast.AnnAssign) and isinstance(node.target,ast.Name) and node.target.id==name: return ast.literal_eval(node.value)
    raise ValueError('Missing contract configuration: '+name)


def validate_artifact(name,source,spec):
    tree=validate_source(name,source)
    expected={'database.py':('SCHEMA',spec['entities']),'workflow.py':('WORKFLOWS',spec['workflows']),
              'core.py':('RULES',spec['business_rules'])}
    if name in expected:
        key,value=expected[name]
        if literal(tree,key)!=value: raise ValueError('Producer configuration disagrees with ProjectSpec: '+key)
    if name=='ui.py':
        design=literal(tree,'DESIGN')
        if design.get('version')!=TEUTA_V2['version']: raise ValueError('Teuta contract version missing')
        required_visuals={placement_key(r):r['id'] for r in spec.get('visual_requirements',[])}
        if design.get('visual_placements',{}) != required_visuals:
            raise ValueError('Teuta visual asset handoff mismatch')
        if {v['id'] for v in design['views']}!={v['id'] for v in spec['views']}: raise ValueError('UI navigation does not cover ProductSpec')
        for v in design['views']:
            if any(not v.get(k) for k in ('eyebrow','intro','empty_heading','empty_body')): raise ValueError('UI state copy is incomplete')
    return True
