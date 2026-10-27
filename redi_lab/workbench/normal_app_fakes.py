"""Controlled artifacts implementing the shared CRM contract; never provider output."""
import json

SOURCES = {
    "database.py": '''import sqlite3

def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('CREATE TABLE IF NOT EXISTS clients (id INTEGER PRIMARY KEY, name TEXT NOT NULL)')
    conn.execute("CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id), title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('NEW','ACTIVE','DONE')), notes TEXT NOT NULL DEFAULT '')")
    conn.commit()
    return conn

def dashboard_counts(conn: sqlite3.Connection) -> dict[str, int]:
    result = dict.fromkeys(('NEW', 'ACTIVE', 'DONE'), 0)
    result.update(dict(conn.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status')))
    result['jobs_total'] = sum(result.values())
    result['clients_total'] = conn.execute('SELECT COUNT(*) FROM clients').fetchone()[0]
    return result
''',
    "dataflow.py": '''VALID_STATUSES = ('NEW', 'ACTIVE', 'DONE')
TRANSITIONS = {'NEW': ('ACTIVE',), 'ACTIVE': ('DONE',), 'DONE': ()}

def normalize_status(status: str) -> str:
    if not isinstance(status, str) or status.strip().upper() not in VALID_STATUSES:
        raise ValueError('invalid status')
    return status.strip().upper()

def can_transition(old: str, new: str) -> bool:
    old, new = normalize_status(old), normalize_status(new)
    return old == new or new in TRANSITIONS[old]
''',
    "core.py": '''from .database import connect, dashboard_counts
from .dataflow import normalize_status, can_transition

class CRMService:
    def __init__(self, path: str = ':memory:'):
        self.conn = connect(path)

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def create_client(self, name: str) -> int:
        cur = self.conn.execute('INSERT INTO clients(name) VALUES (?)', (name,))
        self.conn.commit()
        return cur.lastrowid

    def create_job(self, client_id: int, title: str, notes: str = '') -> int:
        cur = self.conn.execute("INSERT INTO jobs(client_id,title,status,notes) VALUES (?,?,'NEW',?)", (client_id,title,notes))
        self.conn.commit()
        return cur.lastrowid

    def set_status(self, job_id: int, status: str) -> str:
        status = normalize_status(status)
        row = self.conn.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None or not can_transition(row['status'], status):
            raise ValueError('invalid job or transition')
        self.conn.execute('UPDATE jobs SET status=? WHERE id=?', (status,job_id))
        self.conn.commit()
        return status

    def dashboard(self) -> dict[str, int]:
        return dashboard_counts(self.conn)

    def add_note(self, job_id: int, note: str) -> None:
        if not isinstance(note, str) or not note.strip():
            raise ValueError('note required')
        row = self.conn.execute('SELECT notes FROM jobs WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise ValueError('unknown job')
        self.conn.execute('UPDATE jobs SET notes=? WHERE id=?', (row['notes'] + '\\n' + note.strip(), job_id))
        self.conn.commit()

    def jobs(self) -> list[dict]:
        rows = self.conn.execute('SELECT j.id,j.client_id,c.name AS client_name,j.title,j.status,j.notes FROM jobs j JOIN clients c ON c.id=j.client_id ORDER BY j.id')
        return [dict(row) for row in rows]
''',
    "ui.py": '''from html import escape

def render_dashboard(dashboard: dict, jobs: list[dict]) -> str:
    counts = ' '.join(escape(key) + '=<span data-count="' + key + '">' + str(dashboard[key]) + '</span>' for key in ('clients_total','jobs_total','NEW','ACTIVE','DONE'))
    rows = ''.join('<tr>' + ''.join('<td>' + escape(str(job[key])) + '</td>' for key in ('id','client_id','client_name','title','status','notes')) + '</tr>' for job in jobs)
    forms = '<form method="post" action="/clients"><label>Name<input name="name" required></label><button>Create client</button></form>'
    forms += '<form method="post" action="/jobs"><label>Client ID<input name="client_id" type="number" required></label><label>Title<input name="title" required></label><label>Notes<textarea name="notes"></textarea></label><button>Create job</button></form>'
    forms += '<form method="post" action="/status"><label>Job ID<input name="job_id" type="number" required></label><label>Status<select name="status"><option>NEW</option><option>ACTIVE</option><option>DONE</option></select></label><button>Set status</button></form>'
    forms += '<form method="post" action="/notes"><label>Job ID<input name="job_id" type="number" required></label><label>Note<textarea name="note" required></textarea></label><button>Add note</button></form>'
    style = '<style>body{font-family:sans-serif;max-width:960px;margin:2rem auto}form{margin:1rem 0}td{padding:.4rem;border:1px solid #ccc}</style>'
    return '<html><head><title>Mini CRM</title>' + style + '</head><body><h1>Mini CRM / Job Manager</h1><p>' + counts + '</p><table>' + rows + '</table>' + forms + '</body></html>'
''',
    "service.py": '''from pathlib import Path
from tempfile import TemporaryDirectory
from .core import CRMService
from .dataflow import normalize_status
from .ui import render_dashboard

def build_crm(path: str = ':memory:') -> CRMService:
    return CRMService(path)

def run_acceptance() -> dict[str, int]:
    with TemporaryDirectory(prefix='mini-crm-') as directory:
        svc = build_crm(str(Path(directory) / 'crm.sqlite'))
        try:
            cid = svc.create_client('Acme Ltd')
            first = svc.create_job(cid, 'First job', notes='kickoff')
            second = svc.create_job(cid, 'Second job', notes='follow-up')
            svc.set_status(first, 'ACTIVE')
            svc.set_status(second, 'ACTIVE')
            svc.set_status(second, 'DONE')
            return svc.dashboard()
        finally:
            svc.close()

def integration_probe() -> dict:
    with TemporaryDirectory(prefix='mini-crm-') as directory:
        svc = build_crm(str(Path(directory) / 'crm.sqlite'))
        try:
            cid = svc.create_client('Probe Client')
            svc.create_job(cid, 'Probe job', notes='probe')
            counts = svc.dashboard()
            return {'crm': 'ready', 'clients': counts['clients_total'], 'jobs': counts['jobs_total']}
        finally:
            svc.close()

def run_ui(path: str = ':memory:') -> str:
    svc = build_crm(path)
    try:
        return render_dashboard(svc.dashboard(), svc.jobs())
    finally:
        svc.close()

if __name__ == '__main__':
    Path('mini_crm.html').write_text(run_ui('mini_crm.sqlite'), encoding='utf-8')
''',
}


def providers(tasks):
    def provider(name):
        def call(prompt):
            return json.dumps({"patches": [{"rel_path": name, "action": "write", "content": SOURCES[name]}]})
        return call
    return {task["task_id"]: provider(task["artifact"]) for task in tasks}
