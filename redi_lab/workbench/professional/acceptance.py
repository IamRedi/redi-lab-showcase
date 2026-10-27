"""Internal acceptance launch and full evidence collection, without providers."""
from __future__ import annotations
import hashlib
import ast
import json
from pathlib import Path
import uuid
from ..product_packaging import start_process, stop_process, local_request, verify_package
from .review import assertion, envelope, evaluate
from .browser import audit


class AcceptanceLaunch:
    """Launch exact package bytes with a separate QA DB; never grants OPEN APP."""
    def __init__(self, app, workspace):
        self.app=Path(app).resolve();self.workspace=Path(workspace).resolve()
        self.workspace.mkdir(parents=True,exist_ok=True)
        self.database=self.workspace/'acceptance.sqlite3';self.proc=None;self.launches=[]

    def start(self):
        if self.proc is not None:raise ValueError('Acceptance process already started')
        self.proc,self.info=start_process(self.app,self.database,self.workspace/(uuid.uuid4().hex+'.json'))
        self.health=json.loads(local_request(self.info['url']+'api/health'))
        self.launches.append({**self.info,'health':self.health,'mode':'internal_acceptance'})
        return self.info

    def stop(self):
        if self.proc is None:raise ValueError('No owned acceptance process')
        stop_process(self.proc)
        self.launches[-1]['shutdown_exit_code']=self.proc.returncode
        self.proc=None


def collect(app,spec,source_id,output):
    """A new acceptance execution. Does not write to the candidate directory."""
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    app=Path(app).resolve();identity=verify_package(app)
    definition=ast.parse((app/'product_app/definition.py').read_text(encoding='utf-8'))
    assigned=[node.value for node in definition.body if isinstance(node,ast.Assign)
              and any(isinstance(target,ast.Name) and target.id=='SPEC' for target in node.targets)]
    if len(assigned)!=1 or ast.literal_eval(assigned[0])!=spec:
        raise ValueError('Acceptance ProductSpec differs from immutable package definition')
    session=AcceptanceLaunch(app,output/'launch')
    rows=[]
    def add(key,observed,expected,passed):
        rows.append(assertion(key,observed,expected,passed=passed,location='launch.json',reason='Observed result differs from required result'))
    session.start()
    try:
        initial=json.loads(local_request(session.info['url']+'api/state'))
        add('startup',session.health,{'project_path':str(app),'package_sha256':identity},session.health['package_sha256']==identity)
        add('package_identity',identity,identity,verify_package(app)==identity)
    finally:session.stop()
    add('shutdown',session.launches[-1]['shutdown_exit_code'],'process exited',session.launches[-1]['shutdown_exit_code'] is not None)
    # Full browser journey operates a separate QA database, using native form
    # submission and the generated application's own frontend handlers.
    browser=audit(app,spec,session.database,output/'browser',journey=True)
    session.start()
    try:before=json.loads(local_request(session.info['url']+'api/state'))
    finally:session.stop()
    session.start()
    try:after=json.loads(local_request(session.info['url']+'api/state'))
    finally:session.stop()
    add('restart',session.launches,'new process; same database; exact package',len({x['pid'] for x in session.launches})==3)
    add('persistence',after,before,after==before)
    (output/'launch.json').write_text(json.dumps({'launches':session.launches,'initial':initial,'before':before,'after':after,'assertions':rows},indent=2),encoding='utf-8')
    browser_rows=[{**x,'evidence_location':'browser/report.json'} for x in browser['assertions']]
    refs={name:hashlib.sha256((output/name).read_bytes()).hexdigest() for name in ('launch.json','browser/report.json','browser/audit.js')}
    evidence=envelope(spec,source_id,{'package_sha256':identity,'files':json.loads((app/'package.json').read_text(encoding='utf-8'))['files']},
        output.name,rows+browser_rows,browser_identity={'engine':browser['browser'],'test_sha256':refs['browser/audit.js'],'source':'real_chromium_dom'},references=refs)
    (output/'review_evidence.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
    review=evaluate(evidence,output)
    (output/'medaurus.json').write_text(json.dumps(review,indent=2),encoding='utf-8')
    return {'evidence':evidence,'review':review,'directory':str(output)}
