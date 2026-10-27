"""HTML renderer for the Mini CRM / Job Manager.

The returned document is self-contained: save it as an ``.html`` file and open
it in a browser to browse, search, and filter the supplied dashboard data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from html import escape
from typing import Any


_STATUSES = ("NEW", "ACTIVE", "DONE")


def _text(value: Any) -> str:
    """Return an HTML-safe human-readable value."""
    if value is None or value == "":
        return "—"
    return escape(str(value))


def _count(dashboard: Mapping[str, Any], key: str) -> str:
    """Format a count without making rendering fail on incomplete data."""
    value = dashboard.get(key, 0)
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return _text(value)


def render_dashboard(
    dashboard: Mapping[str, Any], jobs: Iterable[Mapping[str, Any]]
) -> str:
    """Render a standalone Mini CRM dashboard.

    ``dashboard`` is normally the result of :meth:`CRMService.dashboard` and
    ``jobs`` is normally the result of :meth:`CRMService.jobs`.  The renderer
    deliberately has no server dependency, so the resulting HTML can be opened
    directly from disk.  Its search and status controls filter the rendered
    jobs locally; persistence remains the responsibility of ``CRMService``.
    """
    if not isinstance(dashboard, Mapping):
        raise TypeError("dashboard must be a mapping")

    job_cards: list[str] = []
    for raw_job in jobs:
        if not isinstance(raw_job, Mapping):
            raise TypeError("each job must be a mapping")
        status = str(raw_job.get("status", "NEW")).upper()
        if status not in _STATUSES:
            status = "NEW"
        title = _text(raw_job.get("title", "Untitled job"))
        client_name = _text(raw_job.get("client_name", "Unknown client"))
        client_email = _text(raw_job.get("client_email"))
        client_phone = _text(raw_job.get("client_phone"))
        description = _text(raw_job.get("description"))
        job_id = _text(raw_job.get("id", ""))
        created = _text(raw_job.get("created_at"))
        updated = _text(raw_job.get("updated_at"))
        notes = raw_job.get("notes") or []
        if not isinstance(notes, Iterable) or isinstance(notes, (str, bytes)):
            notes = []
        note_items: list[str] = []
        for note in notes:
            if isinstance(note, Mapping):
                content = _text(note.get("content"))
                note_created = _text(note.get("created_at"))
            else:
                content = _text(note)
                note_created = "—"
            note_items.append(
                f'<li><span class="note-content">{content}</span>'
                f'<time>{note_created}</time></li>'
            )
        notes_html = "".join(note_items) or '<li class="empty">No notes yet.</li>'
        searchable = escape(
            " ".join(
                str(raw_job.get(key, ""))
                for key in ("title", "client_name", "client_email", "client_phone", "description")
            ),
            quote=True,
        )
        job_cards.append(
            f'<article class="job" data-status="{status}" data-search="{searchable}">'
            f'<header><div><p class="eyebrow">Job #{job_id}</p><h2>{title}</h2></div>'
            f'<span class="badge {status.lower()}">{status}</span></header>'
            f'<p class="description">{description}</p>'
            f'<section class="client"><h3>Client</h3><strong>{client_name}</strong>'
            f'<span>{client_email}</span><span>{client_phone}</span></section>'
            f'<section class="notes"><h3>Notes <small>({len(note_items)})</small></h3>'
            f'<ul>{notes_html}</ul></section>'
            f'<footer>Created: {created}<br>Updated: {updated}</footer></article>'
        )

    cards = "".join(job_cards) or '<p class="no-jobs">No jobs have been created yet.</p>'
    return f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mini CRM — Job Manager</title>
<style>
:root {{ color-scheme: light; --ink:#172033; --muted:#697386; --line:#dce2ea; --panel:#fff; --bg:#f5f7fb; --blue:#276ef1; --new:#6b7280; --active:#b45309; --done:#15803d; }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif }}
main {{ max-width:1280px; margin:auto; padding:32px 20px 48px }} h1,h2,h3,p {{ margin-top:0 }} .top {{ display:flex; gap:20px; justify-content:space-between; align-items:end; margin-bottom:24px }} h1 {{ margin-bottom:4px }} .sub,.job footer,time,.client span {{ color:var(--muted) }}
.metrics {{ display:grid; grid-template-columns:repeat(5,minmax(105px,1fr)); gap:12px; margin-bottom:24px }} .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:16px }} .metric b {{ display:block; font-size:28px }} .metric span {{ color:var(--muted); font-size:13px }}
.controls {{ display:flex; flex-wrap:wrap; gap:10px; margin-bottom:20px }} input {{ min-width:230px; flex:1; border:1px solid var(--line); border-radius:8px; padding:10px 12px; font:inherit }} button {{ cursor:pointer; background:#fff; color:var(--ink); border:1px solid var(--line); border-radius:8px; padding:9px 13px; font:inherit }} button.selected {{ color:#fff; background:var(--blue); border-color:var(--blue) }}
.jobs {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(285px,1fr)); gap:16px }} .job {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:18px; box-shadow:0 2px 5px #17203308 }} .job header {{ display:flex; justify-content:space-between; gap:10px }} .job h2 {{ margin-bottom:0; font-size:19px }} .eyebrow {{ color:var(--muted); font-size:12px; margin-bottom:3px }} .badge {{ height:max-content; border-radius:99px; padding:3px 9px; color:#fff; font-size:12px; font-weight:700 }} .new {{ background:var(--new) }} .active {{ background:var(--active) }} .done {{ background:var(--done) }} .description {{ min-height:22px; white-space:pre-wrap }} .client,.notes {{ border-top:1px solid var(--line); padding-top:12px; margin-top:12px }} .client h3,.notes h3 {{ margin-bottom:5px; font-size:13px; text-transform:uppercase; letter-spacing:.04em }} .client span {{ display:block }} ul {{ list-style:none; padding:0; margin:0 }} li {{ padding:7px 0; border-bottom:1px solid #edf0f4 }} li:last-child {{ border:0 }} .note-content {{ display:block; white-space:pre-wrap }} time {{ display:block; font-size:12px; margin-top:2px }} small {{ color:var(--muted); font-weight:400 }} .empty,.no-jobs {{ color:var(--muted) }} .job footer {{ border-top:1px solid var(--line); margin-top:12px; padding-top:10px; font-size:12px }}
@media (max-width:700px) {{ main {{ padding:22px 14px }} .top {{ display:block }} .metrics {{ grid-template-columns:repeat(2,1fr) }} .metric:first-child {{ grid-column:span 2 }} }}
</style>
</head>
<body><main>
<section class="top"><div><h1>Mini CRM</h1><p class="sub">Client jobs and notes</p></div><p class="sub" id="visible-count"></p></section>
<section class="metrics" aria-label="Dashboard counts">
<div class="metric"><b>{_count(dashboard, "clients_total")}</b><span>Clients</span></div>
<div class="metric"><b>{_count(dashboard, "jobs_total")}</b><span>All jobs</span></div>
<div class="metric"><b>{_count(dashboard, "NEW")}</b><span>New</span></div>
<div class="metric"><b>{_count(dashboard, "ACTIVE")}</b><span>Active</span></div>
<div class="metric"><b>{_count(dashboard, "DONE")}</b><span>Done</span></div>
</section>
<section class="controls" aria-label="Filter jobs"><input id="search" type="search" placeholder="Search clients or jobs" aria-label="Search clients or jobs"><button class="selected" data-filter="ALL">All</button><button data-filter="NEW">New</button><button data-filter="ACTIVE">Active</button><button data-filter="DONE">Done</button></section>
<section class="jobs" id="jobs">{cards}</section>
</main><script>
(() => {{ const cards=[...document.querySelectorAll('.job')], input=document.querySelector('#search'), count=document.querySelector('#visible-count'); let filter='ALL'; function apply() {{ const query=input.value.toLowerCase(); let shown=0; cards.forEach(card=>{{ const show=(filter==='ALL'||card.dataset.status===filter)&&card.dataset.search.toLowerCase().includes(query); card.hidden=!show; if(show) shown++; }}); count.textContent=shown+' job'+(shown===1?'':'s')+' shown'; }} document.querySelectorAll('[data-filter]').forEach(button=>button.addEventListener('click',()=>{{filter=button.dataset.filter; document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('selected',b===button)); apply();}})); input.addEventListener('input',apply); apply(); }})();
</script></body></html>'''
