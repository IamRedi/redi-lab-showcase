"""HTML UI renderer for the Mini CRM / Job Manager.

The returned document is self-contained: save it as an ``.html`` file and open
it in a browser.  Its controls operate on the displayed data locally, which is
also useful when the renderer is used as a small read-only service front end.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from html import escape
import json
from typing import Any


_STATUSES = ("NEW", "ACTIVE", "DONE")


def _text(value: Any) -> str:
    """Return an HTML-safe display value."""
    return escape("" if value is None else str(value))


def _status_count(dashboard: Mapping[str, Any], status: str) -> int:
    """Accept both common dashboard key styles without constraining callers."""
    for key in (status, status.lower(), "jobs_" + status.lower()):
        if key in dashboard:
            try:
                return int(dashboard[key])
            except (TypeError, ValueError):
                return 0
    return 0


def render_dashboard(dashboard: Mapping[str, Any], jobs: Iterable[Mapping[str, Any]]) -> str:
    """Render a standalone, local Mini CRM dashboard.

    ``dashboard`` normally comes from :meth:`CRMService.dashboard` and
    ``jobs`` from :meth:`CRMService.jobs`.  Job mappings may optionally contain
    a ``notes`` sequence; this permits callers that have loaded notes to show
    them in the job detail panels.
    """
    job_list = [dict(job) for job in jobs]
    try:
        client_count = int(dashboard.get("clients", dashboard.get("client_count", 0)))
    except (TypeError, ValueError):
        client_count = 0

    # JSON is data, not markup; escaping '<' prevents an accidental closing
    # script tag when a title or note contains HTML-like text.
    initial_data = json.dumps(job_list, ensure_ascii=False, default=str).replace("<", "\\u003c")
    initial_dashboard = json.dumps(
        {
            "clients": client_count,
            "NEW": _status_count(dashboard, "NEW"),
            "ACTIVE": _status_count(dashboard, "ACTIVE"),
            "DONE": _status_count(dashboard, "DONE"),
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c")

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mini CRM / Job Manager</title>
<style>
:root { color-scheme: light; --ink:#1d2939; --muted:#667085; --line:#d0d5dd; --blue:#175cd3; --bg:#f8fafc; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
main { max-width:1180px; margin:auto; padding:28px 18px 48px; } h1 { margin:0; font-size:28px; } h2 { font-size:18px; margin:0 0 14px; } .sub { color:var(--muted); margin:4px 0 24px; }
.cards { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:24px; } .card,.panel { background:#fff; border:1px solid var(--line); border-radius:10px; box-shadow:0 1px 2px #1018280d; } .card { padding:16px; } .card b { display:block; font-size:28px; } .card span { color:var(--muted); font-size:13px; }
.panel { padding:18px; } .toolbar { display:flex; justify-content:space-between; align-items:center; gap:12px; margin-bottom:14px; flex-wrap:wrap; } button { border:0; border-radius:6px; padding:8px 11px; font:inherit; cursor:pointer; background:var(--blue); color:#fff; } button.secondary { color:var(--ink); background:#eef2f6; } select,input,textarea { border:1px solid var(--line); border-radius:6px; padding:8px; font:inherit; background:#fff; } textarea { width:100%; min-height:70px; }
table { width:100%; border-collapse:collapse; } th,td { padding:11px 8px; text-align:left; border-top:1px solid #eaecf0; vertical-align:top; } th { color:var(--muted); font-size:12px; text-transform:uppercase; } .badge { display:inline-block; border-radius:999px; padding:3px 8px; font-size:12px; font-weight:650; background:#eaf2ff; color:#175cd3; } .DONE { background:#dcfae6; color:#067647; } .NEW { background:#f2f4f7; color:#475467; }
details { margin:4px 0; } summary { cursor:pointer; color:var(--blue); } .note { border-left:3px solid #d0d5dd; margin:8px 0; padding:5px 9px; white-space:pre-wrap; } .small { color:var(--muted); font-size:12px; } dialog { width:min(520px,calc(100% - 28px)); border:0; border-radius:10px; box-shadow:0 12px 40px #10182844; padding:22px; } dialog::backdrop { background:#10182866; } .form-row { margin:12px 0; } .form-row label { display:block; font-weight:600; margin-bottom:4px; } .actions { display:flex; justify-content:flex-end; gap:8px; margin-top:18px; } .empty { color:var(--muted); padding:18px 0; }
@media(max-width:700px) { .cards { grid-template-columns:repeat(2,1fr); } table thead { display:none; } table,tr,td { display:block; } td { border:0; padding:4px 8px; } tr { border-top:1px solid #eaecf0; padding:7px 0; } }
</style>
</head>
<body><main>
<h1>Mini CRM</h1><p class="sub">Clients, jobs, status, and job notes</p>
<section class="cards" aria-label="Dashboard counts">
 <div class="card"><b id="client-count"></b><span>Clients</span></div>
 <div class="card"><b id="new-count"></b><span>New jobs</span></div>
 <div class="card"><b id="active-count"></b><span>Active jobs</span></div>
 <div class="card"><b id="done-count"></b><span>Done jobs</span></div>
</section>
<section class="panel"><div class="toolbar"><h2>Jobs</h2><div><select id="filter" aria-label="Filter jobs"><option value="">All statuses</option><option>NEW</option><option>ACTIVE</option><option>DONE</option></select> <button id="add-job">Add job</button> <button class="secondary" id="add-client">Add client</button></div></div>
<div id="job-table"></div></section>
</main>
<dialog id="job-dialog"><form id="job-form"><h2>Add job</h2><div class="form-row"><label for="job-client">Client</label><select id="job-client" required></select></div><div class="form-row"><label for="job-title">Title</label><input id="job-title" required maxlength="200" style="width:100%"></div><div class="form-row"><label for="job-description">Description</label><textarea id="job-description"></textarea></div><div class="form-row"><label for="job-note">Initial note</label><textarea id="job-note"></textarea></div><div class="actions"><button class="secondary" type="button" data-close="job-dialog">Cancel</button><button type="submit">Create job</button></div></form></dialog>
<dialog id="client-dialog"><form id="client-form"><h2>Add client</h2><div class="form-row"><label for="client-name">Name</label><input id="client-name" required maxlength="200" style="width:100%"></div><div class="actions"><button class="secondary" type="button" data-close="client-dialog">Cancel</button><button type="submit">Create client</button></div></form></dialog>
<script>
const jobs = %s;
const initialDashboard = %s;
let nextJobId = Math.max(0, ...jobs.map(j => Number(j.id) || 0)) + 1;
let clients = [...new Set(jobs.map(j => j.client_name).filter(Boolean))];
let nextClientNumber = clients.length + 1;
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function counts() { return { clients: Math.max(clients.length, initialDashboard.clients || 0), NEW: jobs.filter(j=>j.status==='NEW').length, ACTIVE: jobs.filter(j=>j.status==='ACTIVE').length, DONE: jobs.filter(j=>j.status==='DONE').length }; }
function render() {
 const c=counts(); document.querySelector('#client-count').textContent=c.clients; document.querySelector('#new-count').textContent=c.NEW; document.querySelector('#active-count').textContent=c.ACTIVE; document.querySelector('#done-count').textContent=c.DONE;
 const wanted=document.querySelector('#filter').value; const visible=jobs.filter(j=>!wanted || j.status===wanted); const root=document.querySelector('#job-table');
 if (!visible.length) { root.innerHTML='<p class="empty">No jobs match this view.</p>'; return; }
 root.innerHTML='<table><thead><tr><th>Job</th><th>Client</th><th>Status</th><th>Notes</th></tr></thead><tbody>'+visible.map(j=>{ const notes=Array.isArray(j.notes)?j.notes:[]; const noteHtml=notes.map(n=>'<div class="note">'+esc(typeof n==='object' ? n.body : n)+'</div>').join(''); return '<tr><td><strong>'+esc(j.title)+'</strong><div class="small">'+esc(j.description)+'</div></td><td>'+esc(j.client_name)+'</td><td><select data-status="'+esc(j.id)+'">'+['NEW','ACTIVE','DONE'].map(s=>'<option '+(j.status===s?'selected':'')+'>'+s+'</option>').join('')+'</select></td><td><details><summary>'+notes.length+' note'+(notes.length===1?'':'s')+'</summary>'+noteHtml+'<button class="secondary" data-note="'+esc(j.id)+'">Add note</button></details></td></tr>'; }).join('')+'</tbody></table>';
 root.querySelectorAll('[data-status]').forEach(el=>el.onchange=()=>{ const job=jobs.find(j=>String(j.id)===el.dataset.status); if(job) { job.status=el.value; render(); }});
 root.querySelectorAll('[data-note]').forEach(el=>el.onclick=()=>{ const body=prompt('Note for this job:'); const job=jobs.find(j=>String(j.id)===el.dataset.note); if(job && body && body.trim()) { (job.notes ||= []).push({body:body.trim()}); render(); }});
}
function fillClients() { const select=document.querySelector('#job-client'); select.innerHTML=clients.map(c=>'<option>'+esc(c)+'</option>').join(''); }
document.querySelector('#filter').onchange=render;
document.querySelector('#add-client').onclick=()=>document.querySelector('#client-dialog').showModal();
document.querySelector('#add-job').onclick=()=>{ fillClients(); if(!clients.length) { alert('Create a client first.'); return; } document.querySelector('#job-dialog').showModal(); };
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>document.querySelector('#'+b.dataset.close).close());
document.querySelector('#client-form').onsubmit=e=>{ e.preventDefault(); const name=document.querySelector('#client-name').value.trim(); if(name && !clients.includes(name)) clients.push(name); document.querySelector('#client-form').reset(); document.querySelector('#client-dialog').close(); render(); };
document.querySelector('#job-form').onsubmit=e=>{ e.preventDefault(); const title=document.querySelector('#job-title').value.trim(); if(!title) return; const note=document.querySelector('#job-note').value.trim(); jobs.push({id:nextJobId++,client_name:document.querySelector('#job-client').value,title,description:document.querySelector('#job-description').value,status:'NEW',notes:note?[{body:note}]:[]}); e.target.reset(); document.querySelector('#job-dialog').close(); render(); };
render();
</script></body></html>""" % (initial_data, initial_dashboard)
