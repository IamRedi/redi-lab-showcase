"""One strict provider wire contract, also checked locally before planning.

The wire form uses transition rows (closed JSON objects) and explicit nulls for
optional properties. Decoding only changes representation; it never supplies a
missing version or invents domain requirements. Canonical station APIs stay v1.
"""
from __future__ import annotations
import copy
import re

VERSION = 'professional_project_spec_v1'
WIRE_VERSION = 'professional_project_spec_wire_v1'
VISUAL_VERSION = 'professional_project_spec_v2'
VISUAL_WIRE_VERSION = 'professional_project_spec_wire_v2'
TEXT = {'type':'string'}
BOOL = {'type':'boolean'}
NUMBER = {'type':'number'}


def obj(properties):
    return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}


def array(item): return {'type':'array','items':item}
def nullable(item): return {'anyOf':[item,{'type':'null'}]}
def enum(*values): return {'type':'string','enum':list(values)}


SCALAR = {'anyOf':[TEXT,NUMBER,BOOL]}
FIELD_PROPERTIES = {
    'id':TEXT,'label':TEXT,'type':enum('text','long_text','email','integer','date','enum','reference','boolean'),
    'required':BOOL,'unique':BOOL,'sample':SCALAR,'default':nullable(SCALAR),
    'min':nullable(NUMBER),'max':nullable(NUMBER),'choices':nullable(array(TEXT)),
    'references':nullable(TEXT),'append':nullable(BOOL),
}
MONEY = {**FIELD_PROPERTIES,'type':enum('money'),
    'sample':{'type':'string','pattern':r'^[0-9]+(?:\.[0-9]{1,2})?$',
              'description':'Major-unit decimal amount, e.g. 180.00. Never integer cents; the runtime converts to cents.'},
    'default':nullable({'type':'string','pattern':r'^[0-9]+(?:\.[0-9]{1,2})?$'})}
FIELD = {'anyOf':[obj(FIELD_PROPERTIES),obj(MONEY)]}
RULE = {'anyOf':[
    obj({'kind':enum('required_when'),'entity':TEXT,'field':TEXT,'when_field':TEXT,'when_value':SCALAR}),
    obj({'kind':enum('compare'),'entity':TEXT,'left':TEXT,'op':enum('lte','gte'),'right':TEXT}),
    obj({'kind':enum('unique'),'entity':TEXT,'fields':array(TEXT)}),
    obj({'kind':enum('restrict_delete'),'entity':TEXT,'reason':{
        'type':'string','description':'Explicit source-grounded reason to forbid ALL deletion. Do not use for ordinary foreign-key restrictions.'}}),
]}
PROPERTIES = {
    'schema_version':enum(VERSION),
    'product_name':TEXT,'product_type':enum('business_workflow','utility','visual_social'),
    'primary_goal':TEXT,'target_user':TEXT,
    **{key:array(TEXT) for key in ('journeys','functional_requirements','non_functional_requirements')},
    'views':array(obj({'id':TEXT,'title':TEXT,'kind':enum('dashboard','table','board','feed','gallery','calculator'),'entity':nullable(TEXT)})),
    'navigation':array(TEXT),
    'entities':array(obj({'id':TEXT,'label':TEXT,'plural':TEXT,'display_field':TEXT,'fields':array(FIELD)})),
    'business_rules':array(RULE),
    'workflows':array(obj({'entity':TEXT,'field':TEXT,'initial':TEXT,
        'transitions':array(obj({'from_state':TEXT,'to_states':array(TEXT)}))})),
    'validation_rules':array(TEXT),
    'persistence':obj({'enabled':BOOL,'engine':enum('sqlite'),'restart_required':BOOL}),
    **{key:array(TEXT) for key in ('security_privacy','responsive','integrations')},
    'states':obj({key:TEXT for key in ('empty','loading','success','validation_error','backend_error','destructive_confirmation')}),
    **{key:array(TEXT) for key in ('accessibility','acceptance_criteria','assumptions','uncertainties','provenance')},
    'metrics':array({'anyOf':[
        obj({'id':TEXT,'label':TEXT,'entity':TEXT,'operation':enum('count'),
             'where':nullable(obj({'field':TEXT,'values':array(SCALAR)}))}),
        obj({'id':TEXT,'label':TEXT,'entity':TEXT,'operation':enum('sum'),'field':TEXT,
             'where':nullable(obj({'field':TEXT,'values':array(SCALAR)}))})]}),
    'calculator':nullable(obj({'fields':array(obj({'id':TEXT,'label':TEXT,'type':enum('money','integer'),'sample':SCALAR})),
        'expression':TEXT,'result_label':TEXT})),
}
PROJECT_SPEC_SCHEMA = obj(PROPERTIES)
REQUIRED = tuple(key for key in PROPERTIES if key != 'calculator')
VISUAL_REQUIREMENT = obj({
    'id':TEXT,'purpose':TEXT,'asset_type':TEXT,'aspect_ratio':array(NUMBER),
    'min_width':NUMBER,'min_height':NUMBER,'view_id':TEXT,'slot_id':TEXT,'style':TEXT,'subject':TEXT,
    'content_constraints':array(TEXT),'forbidden_content':array(TEXT),
    'text_in_image':BOOL,'transparent':BOOL,'variants':NUMBER,'format':enum('png'),
    'blocking':BOOL,'layout_dependency':enum('parallel','visual_first'),
})
VISUAL_SLOT = obj({'id':TEXT,'purpose':TEXT,'region':enum('header','content'),'accepts_visual':BOOL})
VISUAL_VIEW = obj({'id':TEXT,'title':TEXT,'kind':enum('dashboard','table','board','feed','gallery','calculator'),
                   'entity':nullable(TEXT),'slots':array(VISUAL_SLOT)})
VISUAL_PROJECT_SPEC_SCHEMA = obj({**PROPERTIES,'schema_version':enum(VISUAL_VERSION),
                                  'views':array(VISUAL_VIEW),
                                  'visual_requirements':array(VISUAL_REQUIREMENT)})


def response_format(*, visual=False):
    return {'type':'json_schema','json_schema':{'name':VISUAL_WIRE_VERSION if visual else WIRE_VERSION,
        'strict':True,'schema':copy.deepcopy(VISUAL_PROJECT_SPEC_SCHEMA if visual else PROJECT_SPEC_SCHEMA)}}


def validate_wire(value, schema=None, path='$'):
    """Validate exactly the finite JSON Schema subset emitted above, offline."""
    schema = PROJECT_SPEC_SCHEMA if schema is None else schema
    if 'anyOf' in schema:
        for branch in schema['anyOf']:
            try: validate_wire(value,branch,path); return
            except ValueError: pass
        raise ValueError('ProjectSpec wire type/shape mismatch at '+path)
    kind=schema['type']
    valid={'object':isinstance(value,dict),'array':isinstance(value,list),
           'string':isinstance(value,str),'boolean':type(value) is bool,
           'number':type(value) in (int,float),'null':value is None}[kind]
    if not valid: raise ValueError('ProjectSpec wire type mismatch at '+path)
    if 'enum' in schema and value not in schema['enum']: raise ValueError('ProjectSpec wire enum mismatch at '+path)
    if kind=='object':
        missing=set(schema['required'])-set(value)
        if missing: raise ValueError('ProjectSpec wire missing at '+path+': '+', '.join(sorted(missing)))
        if set(value)-set(schema['properties']): raise ValueError('ProjectSpec wire unexpected property at '+path)
        for key,child in value.items():validate_wire(child,schema['properties'][key],path+'.'+key)
    elif kind=='array':
        for index,child in enumerate(value):validate_wire(child,schema['items'],path+'['+str(index)+']')
    elif kind=='string' and 'pattern' in schema and not re.fullmatch(schema['pattern'],value):
        raise ValueError('ProjectSpec wire value mismatch at '+path)


def decode_spec(value):
    validate_wire(value, VISUAL_PROJECT_SPEC_SCHEMA if value.get('schema_version') == VISUAL_VERSION else PROJECT_SPEC_SCHEMA)
    def omit_nulls(item):
        if isinstance(item,dict):return {k:omit_nulls(v) for k,v in item.items() if v is not None}
        if isinstance(item,list):return [omit_nulls(v) for v in item]
        return item
    spec=omit_nulls(value)
    for workflow in spec['workflows']:
        rows=workflow['transitions']
        if len({row['from_state'] for row in rows})!=len(rows):raise ValueError('Duplicate transition source')
        workflow['transitions']={row['from_state']:row['to_states'] for row in rows}
    return spec
