"""Real Chromium rendering audits, independent of generated UI code.

Same-origin frames exercise each declared screen at desktop/tablet/mobile widths.
No screenshot matching, external sites, browser downloads or provider calls.
"""
from __future__ import annotations
import html
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from ..product_packaging import child_environment, python_executable, stop_process, local_request



def chrome_path():
    candidates = [os.environ.get('REDI_CHROMIUM_PATH'),
        r'C:\Program Files\Google\Chrome\Application\chrome.exe',
        r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe']
    for candidate in candidates:
        if candidate and Path(candidate).is_file(): return str(candidate)
    raise RuntimeError('A local Chromium browser is required before Teuta can complete')


def audit(app, spec, database, output, *, journey=False, verify_visuals=True):
    app, output = Path(app).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    script = output/'audit.js'
    paths = ['/?view='+v for v in spec['navigation']]
    source=Path(__file__).with_name('acceptance_browser.js').read_text(encoding='utf-8')
    source=source.replace('const spec=SPEC, journey=JOURNEY, verifyVisuals=VERIFY_VISUALS;',
                          'const spec='+json.dumps(spec)+', journey='+json.dumps(journey)+', verifyVisuals='+json.dumps(verify_visuals)+';')
    script.write_text(source,encoding='utf-8')
    ready=output/'ready.json'
    with (output/'host.log').open('wb') as log:
        host=subprocess.Popen([python_executable(),'-B',str(app/'run_app.py'),'--database',str(database),
            '--port','0','--ready-file',str(ready),'--audit-script',str(script)],cwd=app,
            env=child_environment(),stdout=log,stderr=log,stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        deadline=time.monotonic()+15
        while not ready.exists():
            if host.poll() is not None or time.monotonic()>deadline: raise RuntimeError('Browser audit host failed to start')
            time.sleep(.05)
        info=json.loads(ready.read_text())
        local_request(info['url'])
        state=json.loads(local_request(info['url']+'api/state'))
        for view in spec['views']:
            rows=state['records'].get(view.get('entity'),[])
            if rows:paths.append('/?view='+view['id']+'&id='+str(rows[0]['id']))
        command=[chrome_path(),'--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
                 '--disable-background-networking','--disable-component-update','--disable-sync','--disable-extensions',
                 '--proxy-server=http://127.0.0.1:9','--proxy-bypass-list=127.0.0.1;localhost',
                 '--user-data-dir='+str(output/'browser-profile'),'--window-size=1440,1000',
                 '--virtual-time-budget=60000','--dump-dom',info['url']+'?_audit=1']
        if os.environ.get('REDI_CHROMIUM_SANDBOX_WORKAROUND')=='1':
            # Restricted Windows test environments can deny the GPU subprocess.
            # Opt-in only; the normal browser audit retains Chromium sandboxing.
            command[1:1]=['--no-sandbox','--disable-gpu-sandbox','--disable-software-rasterizer']
        try:
            run=subprocess.run(command,capture_output=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired as exc:
            (output/'rendered.html').write_bytes(exc.stdout or b'')
            (output/'browser.log').write_bytes(exc.stderr or b'')
            (output/'report.json').write_text(json.dumps({'status':'FAIL','reason':'Chromium audit timed out after 90 seconds'}))
            raise
        dom=run.stdout.decode('utf-8',errors='replace')
        (output/'rendered.html').write_text(dom,encoding='utf-8')
        (output/'browser.log').write_bytes(run.stderr)
        match=re.search(r'<pre id="redi-browser-evidence">(.*?)</pre>',dom,re.S)
        if not match: raise RuntimeError('Browser produced no completed quality report')
        report=json.loads(html.unescape(match.group(1)))
        report.update(browser=chrome_path(),source='real_chromium_dom',database=str(database))
        (output/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        if report['status']!='PASS' and not journey: raise ValueError('Browser quality failed: '+json.dumps(report['failures']))
        return report
    finally: stop_process(host)
