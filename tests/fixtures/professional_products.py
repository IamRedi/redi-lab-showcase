"""Three unrelated offline products. Never used by the live factory."""
import copy
import json


def base(name,kind):
    return {'schema_version':'professional_project_spec_v1','product_name':name,'product_type':kind,
        'primary_goal':'Keep daily work clear and dependable.','target_user':'A small local team',
        'journeys':['Create a record and return to it later.'],'functional_requirements':['Manage the declared records.'],
        'non_functional_requirements':['Fast local operation.'],'views':[],'navigation':[],
        'entities':[],'business_rules':[],'workflows':[],'validation_rules':['Required fields must be filled.'],
        'persistence':{'enabled':True,'engine':'sqlite','restart_required':True},
        'security_privacy':['Local data only; escaped user input.'],'responsive':['Desktop, tablet and mobile.'],
        'integrations':[],'states':{'empty':'Explain the next action.','loading':'Saving changes.',
        'success':'Changes saved.','validation_error':'Explain which input needs attention.',
        'backend_error':'Keep entered data and offer retry.','destructive_confirmation':'Confirm permanent deletion.'},
        'accessibility':['Semantic labels and visible keyboard focus.'],'acceptance_criteria':['Records survive restart.'],
        'assumptions':['One local operator.'],'uncertainties':[],'provenance':['offline fixture, not a real provider result'],
        'metrics':[]}


def business():
    s=base('Lending Desk','business_workflow')
    s['entities']=[
      {'id':'member','label':'Member','plural':'Members','display_field':'name','fields':[
       {'id':'name','label':'Name','type':'text','required':True,'unique':True,'sample':'Alex Reader'},
       {'id':'email','label':'Email','type':'email','required':True,'unique':True,'sample':'alex@example.test'}]},
      {'id':'loan','label':'Loan','plural':'Loans','display_field':'title','fields':[
       {'id':'member_id','label':'Member','type':'reference','references':'member','required':True,'sample':1},
       {'id':'title','label':'Book title','type':'text','required':True,'sample':'A practical handbook'},
       {'id':'status','label':'Status','type':'enum','choices':['RESERVED','BORROWED','RETURNED'],'required':True,'sample':'RESERVED'},
       {'id':'deposit','label':'Deposit','type':'money','required':True,'min':0,'sample':'15.50'},
       {'id':'notes','label':'Notes','type':'long_text','required':False,'append':True,'sample':'Collect from the desk'}]}]
    s['workflows']=[{'entity':'loan','field':'status','initial':'RESERVED','transitions':{'RESERVED':['BORROWED'],'BORROWED':['RETURNED'],'RETURNED':[]}}]
    s['views']=[{'id':'overview','title':'Overview','kind':'dashboard'},
        {'id':'members','title':'Members','kind':'table','entity':'member'},
        {'id':'loans','title':'Loans','kind':'table','entity':'loan'}]
    s['navigation']=['overview','members','loans']
    s['metrics']=[{'id':'open','label':'Active loans','entity':'loan','operation':'count','where':{'field':'status','values':['RESERVED','BORROWED']}},
                  {'id':'returned','label':'Returned','entity':'loan','operation':'count','where':{'field':'status','values':['RETURNED']}}]
    return s


def utility():
    s=base('Margin Studio','utility');s['persistence']['enabled']=False;s['persistence']['restart_required']=False
    s['views']=[{'id':'calculator','title':'Price calculator','kind':'calculator'}];s['navigation']=['calculator']
    s['calculator']={'fields':[{'id':'cost','label':'Base cost','type':'money','sample':'40'},
                               {'id':'margin','label':'Margin percentage','type':'integer','sample':'20'}],
                      'expression':'cost / (1 - margin / 100)','result_label':'Suggested price'}
    s['acceptance_criteria']=['Calculate a price from the supplied values.'];return s


def social():
    s=base('Field Notes','visual_social')
    s['entities']=[{'id':'post','label':'Entry','plural':'Entries','display_field':'title','fields':[
        {'id':'title','label':'Title','type':'text','required':True,'sample':'A morning observation'},
        {'id':'body','label':'Story','type':'long_text','required':True,'sample':'A short note from the trail.'},
        {'id':'category','label':'Category','type':'enum','choices':['NATURE','DESIGN','IDEAS'],'required':True,'sample':'NATURE'}]}]
    s['views']=[{'id':'journal','title':'Journal','kind':'feed','entity':'post'}];s['navigation']=['journal'];return s


def sources(spec):
    schema=repr(spec['entities']);flows=repr(spec['workflows']);rules=repr(spec['business_rules'])
    design={'version':'teuta_professional_ui_v2','accent':'#155e75','views':[
        {'id':v['id'],'eyebrow':spec['product_name'],'intro':'Keep '+v['title'].lower()+' organized and easy to find.',
         'empty_heading':'A fresh start','empty_body':'Add your first record with the form above.'} for v in spec['views']]}
    data={
        'database.py':'from .runtime import open_database\nSCHEMA = '+schema+'\ndef connect(path): return open_database(path, SCHEMA)\n',
        'workflow.py':'from .runtime import transition_allowed\nWORKFLOWS = '+flows+'\ndef can_transition(entity,field,old,new): return transition_allowed(WORKFLOWS,entity,field,old,new)\n',
        'core.py':'from .definition import SPEC\nfrom .runtime import DataService\n'+('from .database import connect\n' if spec['entities'] and spec['persistence']['enabled'] else '')+('from .workflow import WORKFLOWS\n' if spec['workflows'] else 'WORKFLOWS = []\n')+'RULES = '+rules+'\nclass Service(DataService):\n    def __init__(self,path): super().__init__(path,SPEC,connector='+('connect' if spec['entities'] and spec['persistence']['enabled'] else 'None')+',workflows=WORKFLOWS,rules=RULES)\n',
        'ui.py':'from .runtime import render_app\nDESIGN = '+repr(design)+'\ndef render(spec,state,**context): return render_app(spec,DESIGN,state,**context)\n'}
    from redi_lab.workbench.professional.quality import BRIDGE
    data['service.py']=BRIDGE
    return data


def wire_spec(spec):
    """Encode offline canonical examples into the declared strict wire shape."""
    spec=copy.deepcopy(spec)
    spec.setdefault('calculator',None)
    for entity in spec['entities']:
        for field in entity['fields']:
            field.setdefault('unique',False)
            for key in ('default','min','max','choices','references','append'):field.setdefault(key,None)
    for view in spec['views']:view.setdefault('entity',None)
    for metric in spec['metrics']:
        metric.setdefault('operation','count');metric.setdefault('where',None)
    for workflow in spec['workflows']:
        workflow['transitions']=[{'from_state':state,'to_states':targets} for state,targets in workflow['transitions'].items()]
    return spec


class FixtureProvider:
    def __init__(self,spec):
        from redi_lab.workbench.professional.spec_schema import decode_spec
        self.spec=decode_spec(wire_spec(spec));self.facts={'calls':0,'per_task':{},'failures':{}};self.spent='0';self.calls=[]
    def __call__(self,task,prompt):
        self.calls.append(task);self.facts['calls']+=1
        if task=='specification':return json.dumps(wire_spec(self.spec))
        if task=='product_review':return json.dumps({'verdict':'PASS','reasons':[],'checked_criteria':['offline injected review; not real provider success']})
        name={'data_model':'database.py','workflow':'workflow.py','domain':'core.py','experience':'ui.py','assembly':'service.py'}[task]
        return json.dumps({'patches':[{'rel_path':name,'action':'write','content':sources(self.spec)[name]}]})


def sdk_client(spec):
    from types import SimpleNamespace
    import re
    provider=FixtureProvider(spec)
    def create(**kwargs):
        prompt=kwargs['messages'][1]['content']
        task='specification' if prompt.startswith('Turn the owner') else 'product_review' if prompt.startswith('You are Medaurus') else re.search(r'bounded task ([a-z_]+)',prompt).group(1)
        content=provider(task,prompt)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=1000,completion_tokens=500,prompt_tokens_details=None),
            choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=content))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
