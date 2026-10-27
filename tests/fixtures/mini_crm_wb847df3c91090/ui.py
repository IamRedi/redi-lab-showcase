"""HTML rendering for the Mini CRM / Job Manager dashboard.

The returned document is self-contained: save it to an ``.html`` file and open
it in a browser to use its local status filter and expandable job notes.
"""

from __future__ import annotations

from html import escape
from typing import Any, Iterable, Mapping


_STATUSES = ("NEW", "ACTIVE", "DONE")


def _text(value: Any) -> str:
    """Return a safely escaped display value."""
    if value is None:
        return ""
    return escape(str(value), quote=True)


def _count(dashboard: Mapping[str, Any], *names: str) -> str:
    """Read a count while accepting the common lower- and upper-case keys."""
    for name in names:
        if name in dashboard:
            return _text(dashboard[name])
    return "0"


def _notes_markup(notes: Any) -> str:
    """Render note dictionaries (or strings) as an expandable note list."""
    if not notes:
        return '<span class="muted">No notes</span>'
    if isinstance(notes, (str, bytes)):
        notes = [notes]
    try:
        values: Iterable[Any] = notes
    except TypeError:
        values = [notes]

    items = []
    for note in values:
        if isinstance(note, Mapping):
            content = note.get("content", "")
            created = note.get("created_at", "")
        else:
            content = note
            created = ""
        timestamp = (
            f'<time class="note-time">{_text(created)}</time>' if created else ""
        )
        items.append(f"<li>{_text(content)}{timestamp}</li>")
    return "<details><summary>Notes ({})</summary><ul>{}</ul></details>".format(
        len(items), "".join(items)
    )


def render_dashboard(dashboard: Mapping[str, Any], jobs: Iterable[Mapping[str, Any]]) -> str:
    """Return a complete, local-browser-ready Mini CRM dashboard document.

    ``dashboard`` normally comes from :meth:`CRMService.dashboard`, and ``jobs``
    can be the output of :meth:`CRMService.jobs` (or full job dictionaries from
    :meth:`CRMService.job`).  Missing optional fields are displayed as blanks.
    All supplied text is HTML-escaped before insertion into the document.
    """
    dashboard = dashboard or {}
    job_rows = []
    for job in jobs or []:
        status = str(job.get("status", "NEW")).upper()
        if status not in _STATUSES:
            status = "NEW"
        job_id = _text(job.get("id", ""))
        title = _text(job.get("title", "Untitled job"))
        client = _text(job.get("client_name", job.get("client", "")))
        description = _text(job.get("description", ""))
        notes = _notes_markup(job.get("notes", []))
        job_rows.append(
            "<tr data-status=\"{}\">"
            "<td class=\"id\">{}</td><td><strong>{}</strong>"
            "<span class=\"description\">{}</span></td><td>{}</td>"
            "<td><span class=\"status status-{}\">{}</span></td><td>{}</td></tr>".format(
                status, job_id, title, description, client, status.lower(), status, notes
            )
        )

    rows = "".join(job_rows) or (
        '<tr class="empty"><td colspan="5">No jobs have been created yet.</td></tr>'
    )
    clients = _count(dashboard, "clients", "client_count", "CLIENTS")
    new = _count(dashboard, "NEW", "new", "new_jobs")
    active = _count(dashboard, "ACTIVE", "active", "active_jobs")
    done = _count(dashboard, "DONE", "done", "done_jobs")

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mini CRM — Job Manager</title>
<style>
:root { color-scheme: light; --ink:#1f2937; --muted:#6b7280; --line:#e5e7eb; --card:#fff; --page:#f3f4f6; }
* { box-sizing:border-box; } body { margin:0; font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; color:var(--ink); background:var(--page); }
main { max-width:1150px; margin:auto; padding:32px 20px 48px; } h1 { margin:0; font-size:1.9rem; } .subtitle { margin:4px 0 24px; color:var(--muted); }
.cards { display:grid; grid-template-columns:repeat(4,minmax(130px,1fr)); gap:14px; margin-bottom:28px; } .card { background:var(--card); border-radius:10px; padding:17px; box-shadow:0 1px 2px #0000000b; border:1px solid var(--line); } .card b { display:block; font-size:1.65rem; } .card span { color:var(--muted); font-size:.86rem; }
.toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px; } h2 { margin:0; font-size:1.2rem; } select { padding:8px 10px; border:1px solid #cbd5e1; border-radius:6px; background:white; color:var(--ink); }
.table-wrap { overflow-x:auto; background:white; border:1px solid var(--line); border-radius:10px; } table { border-collapse:collapse; width:100%; min-width:720px; } th,td { padding:13px 14px; text-align:left; vertical-align:top; border-bottom:1px solid var(--line); } th { color:var(--muted); background:#fafafa; font-size:.78rem; letter-spacing:.04em; text-transform:uppercase; } tr:last-child td { border-bottom:0; } .id { color:var(--muted); width:55px; } .description { display:block; max-width:390px; color:var(--muted); font-size:.88rem; margin-top:3px; white-space:pre-wrap; } .status { display:inline-block; padding:3px 8px; border-radius:999px; font-size:.75rem; font-weight:700; } .status-new { background:#dbeafe; color:#1d4ed8; } .status-active { background:#fef3c7; color:#92400e; } .status-done { background:#dcfce7; color:#166534; } details { min-width:130px; } summary { cursor:pointer; color:#2563eb; } ul { padding-left:19px; margin:8px 0 0; } li { margin:4px 0; white-space:pre-wrap; } .note-time { display:block; color:var(--muted); font-size:.75rem; } .muted,.empty td { color:var(--muted); } .empty td { padding:28px; text-align:center; }
@media (max-width:650px) { main { padding:22px 12px; } .cards { grid-template-columns:repeat(2,1fr); } }
</style>
</head>
<body>
<main>
  <h1>Mini CRM</h1>
  <p class="subtitle">Client jobs and notes at a glance</p>
  <section class="cards" aria-label="Dashboard counts">
    <div class="card"><b>__CLIENTS__</b><span>Clients</span></div>
    <div class="card"><b>__NEW__</b><span>New jobs</span></div>
    <div class="card"><b>__ACTIVE__</b><span>Active jobs</span></div>
    <div class="card"><b>__DONE__</b><span>Done jobs</span></div>
  </section>
  <section aria-labelledby="jobs-heading">
    <div class="toolbar"><h2 id="jobs-heading">Jobs</h2><label>Status <select id="status-filter"><option value="ALL">All statuses</option><option value="NEW">New</option><option value="ACTIVE">Active</option><option value="DONE">Done</option></select></label></div>
    <div class="table-wrap"><table><thead><tr><th>ID</th><th>Job</th><th>Client</th><th>Status</th><th>Notes</th></tr></thead><tbody>__ROWS__</tbody></table></div>
  </section>
</main>
<script>
(function () { var filter = document.getElementById('status-filter'); filter.addEventListener('change', function () { document.querySelectorAll('tbody tr[data-status]').forEach(function (row) { row.hidden = filter.value !== 'ALL' && row.dataset.status !== filter.value; }); }); }());
</script>
</body>
</html>""".replace("__CLIENTS__", clients).replace("__NEW__", new).replace("__ACTIVE__", active).replace("__DONE__", done).replace("__ROWS__", rows)
