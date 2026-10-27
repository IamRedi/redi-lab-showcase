"""Independent product checks and build-local delivery utilities."""
from __future__ import annotations
import hashlib
from html import escape
import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from decimal import Decimal
import sqlite3
from urllib.error import HTTPError

from ..product_packaging import child_environment, python_executable, local_request, start_process, stop_process
from . import runtime

BRIDGE = '''from .core import Service
from .definition import SPEC
from .ui import render
from .runtime import acceptance
def build_app(path): return Service(path)
def run_ui(path, **context):
    app=build_app(path)
    try: return render(SPEC,app.state(),**context)
    finally: app.close()
def run_acceptance(): return acceptance(Service,SPEC)
def run_private(): return acceptance(Service,SPEC,private=True)
def integration_probe(): return acceptance(Service,SPEC)
'''


def trusted_sources(spec):
    return {'runtime.py':Path(runtime.__file__).read_text(encoding='utf-8'),
            'definition.py':'# Owner-derived ProjectSpec; factory-owned immutable input\nSPEC = '+repr(spec)+'\n'}


def prepare_project(path,sources,spec,visual_assets=()):
    path=Path(path).resolve();(path/'product_app').mkdir(parents=True,exist_ok=False)
    for name,source in sources.items(): (path/'product_app'/name).write_bytes(source.encode())
    if visual_assets:
        handoff=[]
        for row in visual_assets:
            asset_id=row['asset_id'];name='assets/'+asset_id+'.png'
            if not asset_id or not all(c.islower() or c.isdigit() or c=='_' for c in asset_id):
                raise ValueError('Invalid visual asset ID')
            data=Path(row['source_path']).read_bytes()
            if hashlib.sha256(data).hexdigest()!=row['content_sha256']:
                raise ValueError('Visual asset handoff hash mismatch')
            if row['intended_placement'] != row['view_id']+':'+row['slot_id']:
                raise ValueError('Visual asset placement identity mismatch')
            destination=path/name;destination.parent.mkdir(exist_ok=True)
            destination.write_bytes(data)
            handoff.append({'asset_id':asset_id,'path':'/'+name,'placement':row['intended_placement'],
                            'view_id':row['view_id'],'slot_id':row['slot_id'],
                            'purpose':row['semantic_purpose'],'width':row['width'],'height':row['height'],
                            'sha256':row['content_sha256'],'downstream_consumers':row['downstream_consumers']})
        (path/'product_app'/'visual_asset_handoff.json').write_text(json.dumps({'schema_version':'visual_handoff_v1','assets':handoff},indent=2),encoding='utf-8')
    (path/'run_app.py').write_text('from product_app.runtime import main\nif __name__ == "__main__": main()\n',encoding='utf-8')
    (path/'README.md').write_text('# '+spec['product_name']+'\n\nPython 3.11+, standard library only. No pip install or provider key.\n\n'
        'Run `python -B run_app.py --open-browser`. The printed loopback URL uses a free port. '
        'Use `--port 8766` for a fixed port. Stop with Ctrl+C and rerun to resume.\n\n'
        'SQLite: `data/app.sqlite3`, relative to this project. Override with `--database FULL_PATH`. '
        'Back up the database while stopped. Owner data starts empty; all verification uses separate databases.\n',encoding='utf-8')
    (path/'start.cmd').write_bytes(b'@echo off\r\npy -3 -B "%~dp0run_app.py" --open-browser\r\npause\r\n')
    manifest={'version':'professional_delivery_v1','product_name':spec['product_name'],
        'files':{p.relative_to(path).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in path.rglob('*') if p.is_file()}}
    (path/'package.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return path


def inspect_product(project,spec,output,stage='domain'):
    """Fresh interpreter executes generated interfaces against independent spec."""
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    payload={'project':str(Path(project).resolve()),'spec':spec,'output':str(output.resolve()),'stage':stage}
    result=subprocess.run([sys.executable,'-B','-m','redi_lab.workbench.professional.quality'],
        input=json.dumps(payload),text=True,encoding='utf-8',capture_output=True,timeout=40,
        env=child_environment(),cwd=Path(__file__).resolve().parents[3])
    (output/'worker.log').write_text(result.stderr,encoding='utf-8')
    if result.returncode: raise ValueError('Product integrity: '+result.stderr[-3000:])
    report=json.loads(result.stdout)
    (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


def _worker(payload):
    import socket
    def deny(*a,**k): raise RuntimeError('External networking forbidden in product integrity')
    socket.socket.connect=deny;socket.create_connection=deny
    project=Path(payload['project']);spec=payload['spec'];sys.path.insert(0,str(project))
    if payload['stage']=='data_model':
        module=importlib.import_module('product_app.database')
        conn=module.connect(str(Path(payload['output'])/'schema.sqlite3'))
        try:
            assert module.SCHEMA==spec['entities']
            assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
            for ent in spec['entities']:
                assert {r['name'] for r in conn.execute('PRAGMA table_info('+runtime.ident(ent['id'])+')')}=={'id','created_at','updated_at'}|{f['id'] for f in ent['fields']}
        finally:conn.close()
        return database_integrity(module, spec, Path(payload['output']))
    if payload['stage']=='workflow':
        module=importlib.import_module('product_app.workflow')
        for flow in spec['workflows']:
            for old in flow['transitions']:
                for new in flow['transitions']:
                    assert module.can_transition(flow['entity'],flow['field'],old,new)==(old==new or new in flow['transitions'][old])
        return {'status':'PASS','checks':['exhaustive_transition_matrix']}
    module=importlib.import_module('product_app.core')
    Service=module.Service
    output=Path(payload['output']);db=output/'records.sqlite3'
    app=Service(str(db));checks=[]
    try:
        if app.spec!=spec: raise AssertionError('service specification mismatch')
        assert app.conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        for ent in spec['entities']:
            columns={r['name'] for r in app.conn.execute('PRAGMA table_info('+runtime.ident(ent['id'])+')')}
            assert columns=={'id','created_at','updated_at'}|{f['id'] for f in ent['fields']}
        checks.extend(['schema','foreign_keys'])
        ids=runtime.sample_records(app)
        for ent in spec['entities']:
            name=ent['id'];rid=ids[name]
            for f in ent['fields']:
                def rejects(value):
                    before=app.state()
                    try: app.save(name,{f['id']:value},rid)
                    except ValueError: pass
                    else: raise AssertionError('invalid input accepted: '+name+'.'+f['id'])
                    assert app.state()==before,'failed mutation changed data'
                if f.get('required'):
                    rejects('')
                    if f['type'] in {'text','long_text','email'}: rejects('   ')
                if f['type'] in {'text','long_text','email'} and f.get('max') is not None:
                    rejects('x' * (f['max'] + 1))
                if f['type'] in {'money','integer'}: rejects(str(float(f.get('min',0))-1));rejects('not a number')
                if f['type']=='reference': rejects(999999)
                if f['type']=='enum': rejects('INVALID_STATE')
                if f.get('append'):
                    before=app.record(name,rid)[f['id']] or ''
                    app.append_note(name,rid,f['id'],'Browser-ready note {literal} <safe>')
                    assert app.record(name,rid)[f['id']]==(before+'\n' if before else '')+'Browser-ready note {literal} <safe>'
            unique=next((f for f in ent['fields'] if f.get('unique')),None)
            if unique:
                data={f['id']:app.record(name,rid)[f['id']] for f in ent['fields']}
                try:app.save(name,data)
                except ValueError:pass
                else:raise AssertionError('duplicate unique record accepted')
        checks.extend(['required_data','numeric_validation','relationship_validation','duplicates','notes','atomic_failures'])
        for flow in spec['workflows']:
            name=flow['entity'];rid=ids[name];current=flow['initial'];seen=set()
            while current not in seen:
                seen.add(current);targets=flow['transitions'][current]
                if not targets:break
                target=targets[0]
                values={flow['field']:target}
                for rule in spec['business_rules']:
                    if rule['entity']==name and rule['kind']=='required_when' and rule['when_value']==target:
                        field=next(f for e in spec['entities'] if e['id']==name for f in e['fields'] if f['id']==rule['field'])
                        values[rule['field']]=field['sample']
                app.save(name,values,rid);current=target
            if flow['initial'] not in flow['transitions'][current] and current!=flow['initial']:
                try:app.save(name,{flow['field']:flow['initial']},rid)
                except ValueError:pass
                else:raise AssertionError('invalid reverse workflow accepted')
        checks.append('state_transitions')
        for ent in spec['entities']:
            if any(f.get('references')==ent['id'] for x in spec['entities'] for f in x['fields']):
                try:app.delete(ent['id'],ids[ent['id']])
                except ValueError:pass
                else:raise AssertionError('parent deletion broke relationships')
        checks.append('deletion_constraints')
        state=app.state()
        for metric in spec['metrics']:
            records=state['records'][metric['entity']];where=metric.get('where')
            selected=[r for r in records if not where or r[where['field']] in where['values']]
            expected=len(selected)
            if metric.get('operation') == 'sum':
                total=sum((Decimal(str(r[metric['field']])) for r in selected if r[metric['field']] is not None), Decimal(0))
                field=next(f for e in spec['entities'] if e['id']==metric['entity'] for f in e['fields'] if f['id']==metric['field'])
                expected=format(total,'.2f') if field['type']=='money' else int(total)
            assert state['metrics'][metric['id']]==expected
        checks.append('derived_metrics')
        if (project/'product_app'/'ui.py').is_file():
            ui=importlib.import_module('product_app.ui')
            for view in spec['navigation']:
                page=ui.render(spec,state,view=view)
                assert '<main' in page and 'name="viewport"' in page and escape(spec['product_name']) in page
                assert '<safe>' not in page
            checks.extend(['render_all_views','safe_user_text','declared_navigation'])
    finally:app.close()
    app=Service(str(db))
    try:assert app.state()==state
    finally:app.close()
    checks.append('restart_persistence')
    return {'status':'PASS','stage':payload['stage'],'checks':checks,'database':str(db),'state':state}


def database_integrity(module, spec, output):
    """Exercise the generated connector before accepting the producer handoff."""
    db=Path(output)/'database-contract.sqlite3'
    def service(): return runtime.DataService(str(db),spec,connector=module.connect,rules=[])
    app=service();checks=['parse_import','schema','foreign_keys','clean_initialization'];ids={}
    try:
        initial=app.state()
        assert all(not rows for rows in initial['records'].values()), 'Database must start empty'
        for metric in spec['metrics']:
            expected='0.00' if metric.get('operation')=='sum' and any(f['id']==metric['field'] and f['type']=='money' for e in spec['entities'] if e['id']==metric['entity'] for f in e['fields']) else 0
            assert initial['metrics'][metric['id']]==expected, 'Empty aggregate must be zero, never NULL'
        checks.append('empty_aggregate_zero')
        ids=runtime.sample_records(app)
        for ent in spec['entities']:
            name=ent['id'];rid=ids[name];original=app.record(name,rid)
            for f in ent['fields']:
                key=f['id'];contract=runtime.field_contract(f)
                before=app.state()
                if contract['required'] or not contract['nullable']:
                    for invalid in (None,''):
                        try: app.save(name,{key:invalid},rid)
                        except runtime.ValidationError as exc: assert exc.field==key
                        else: raise AssertionError('Required/null contract violated: '+name+'.'+key)
                        assert app.state()==before, 'Rejected null changed records'
                    if 'default' not in f and not any(w['entity']==name and w['field']==key for w in spec['workflows']):
                        missing={x['id']:original[x['id']] for x in ent['fields'] if x['id']!=key}
                        try:app.save(name,missing)
                        except runtime.ValidationError as exc:assert exc.field==key
                        else:raise AssertionError('Missing required field accepted: '+name+'.'+key)
                else:
                    app.save(name,{key:None},rid)
                    assert app.record(name,rid)[key] is None, 'Optional NULL did not round-trip'
                    app.save(name,{key:original[key]},rid)
                if f['type']=='money':
                    actual=app.record(name,rid)[key]
                    assert actual==format(Decimal(str(f['sample'])),'.2f'), 'Money round trip changed value'
                    stored=app.conn.execute('SELECT '+runtime.ident(key)+',typeof('+runtime.ident(key)+') FROM '+runtime.ident(name)+' WHERE id=?',(rid,)).fetchone()
                    assert stored[1]=='integer' and stored[0]==int(Decimal(str(f['sample']))*100)
                if 'default' in f:
                    # Exercise SQL defaults on a separate fresh schema, avoiding unique collisions.
                    with tempfile.TemporaryDirectory(dir=output) as temporary:
                        probe=runtime.DataService(str(Path(temporary)/'default.sqlite3'),spec,connector=module.connect,rules=[])
                        try:
                            created=runtime.sample_records(probe, omitted={name:{key}})
                            new_id=created[name]
                            expected=f['default']
                            if f['type']=='money' and expected is not None:expected=format(Decimal(str(expected)),'.2f')
                            assert probe.record(name,new_id)[key]==expected, 'Declared default not applied'
                        finally:probe.close()
                if f['type']=='reference':
                    try:
                        app.conn.execute('UPDATE '+runtime.ident(name)+' SET '+runtime.ident(key)+'=999999 WHERE id=?',(rid,));app.conn.commit()
                    except sqlite3.IntegrityError:app.conn.rollback()
                    else:raise AssertionError('SQLite foreign key constraint missing: '+name+'.'+key)
            # An update preserves all omitted values, including zero/null fields.
            app.save(name,{},rid)
            assert {f['id']:app.record(name,rid)[f['id']] for f in ent['fields']}=={f['id']:original[f['id']] for f in ent['fields']}
        checks.extend(['representative_insert_read_update','required_missing_null','nullable_round_trip','declared_defaults','exact_money_round_trip','relationship_behavior'])
        state=app.state()
    finally:app.close();app.close()
    app=service()
    try:
        assert app.state()==state, 'Reopen persistence mismatch'
        for name in reversed(ids):
            if any(r['kind']=='restrict_delete' and r['entity']==name for r in spec['business_rules']):continue
            # A retained restricted child may still legitimately prevent deletion.
            referenced=any(f.get('references')==name and any(row[f['id']]==ids[name] for row in app.records(e['id'])) for e in spec['entities'] for f in e['fields'])
            if referenced:
                try:app.delete(name,ids[name])
                except runtime.ValidationError:pass
                else:raise AssertionError('Referenced parent deletion succeeded')
            else:
                app.delete(name,ids[name]);assert not app.records(name)
        assert not app.conn.execute('PRAGMA foreign_key_check').fetchall()
    finally:app.close()
    checks.extend(['restart_persistence','delete_where_allowed','idempotent_close'])
    return {'status':'PASS','stage':'data_model','checks':checks,'contract':runtime.database_contract(spec),'database':str(db)}


def http_smoke(app,spec,output):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    database=output/'http.sqlite3'
    proc,info=start_process(app,database,output/'first.json')
    ids={};operations=[]
    try:
        pending=list(spec['entities'])
        while pending:
            ready=[e for e in pending if all(f['references'] in ids for f in e['fields'] if f['type']=='reference')]
            if not ready:raise ValueError('cyclic smoke graph')
            for ent in ready:
                name=ent['id'];view=next(v['id'] for v in spec['views'] if v.get('entity')==name)
                values={f['id']:ids[f['references']] if f['type']=='reference' else f['sample'] for f in ent['fields']}
                for w in spec['workflows']:
                    if w['entity']==name:values[w['field']]=w['initial']
                values['_view']=view
                local_request(info['url']+'records/'+name,values)
                state=json.loads(local_request(info['url']+'api/state'));ids[name]=state['records'][name][0]['id']
                operations.append('create:'+name);pending.remove(ent)
                required=next((f for f in ent['fields'] if f.get('required')),None)
                if required:
                    try:local_request(info['url']+'records/'+name+'/'+str(ids[name]),{'_view':view,required['id']:''})
                    except HTTPError as exc:
                        assert exc.code==422
                        assert 'role="alert"' in exc.read().decode()
                    else:raise AssertionError('invalid HTTP form accepted')
                    operations.append('validation:'+name)
                for field in ent['fields']:
                    if field.get('append'):
                        local_request(info['url']+'records/'+name+'/'+str(ids[name])+'/notes',{'_view':view,'_field':field['id'],'note':'Persistent delivery note'})
                        operations.append('note:'+name)
        for flow in spec['workflows']:
            name=flow['entity'];current=flow['initial'];seen=set()
            view=next(v['id'] for v in spec['views'] if v.get('entity')==name)
            while current not in seen:
                seen.add(current);targets=flow['transitions'][current]
                if not targets:break
                current=targets[0];values={'_view':view,flow['field']:current}
                for rule in spec['business_rules']:
                    if rule['kind']=='required_when' and rule['entity']==name and rule['when_value']==current:
                        values[rule['field']]=next(f['sample'] for e in spec['entities'] if e['id']==name for f in e['fields'] if f['id']==rule['field'])
                local_request(info['url']+'records/'+name+'/'+str(ids[name]),values)
                operations.append('transition:'+name+':'+current)
        state=json.loads(local_request(info['url']+'api/state'))
        for view in spec['navigation']:local_request(info['url']+'?view='+view)
    finally:stop_process(proc)
    proc,info=start_process(app,database,output/'restart.json')
    try:
        assert json.loads(local_request(info['url']+'api/state'))==state
        for view in spec['navigation']:local_request(info['url']+'?view='+view)
    finally:stop_process(proc)
    result={'status':'PASS','operations':operations+['navigation','restart','persistence'], 'database':str(database),'state':state}
    (output/'report.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    print(json.dumps(_worker(json.load(sys.stdin))))
