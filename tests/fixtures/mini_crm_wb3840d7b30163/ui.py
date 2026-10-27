"""HTML rendering for the local Mini CRM dashboard."""

from __future__ import annotations

from html import escape
from typing import Any, Iterable, Mapping


def _text(value: Any) -> str:
    """Return a safely escaped display value."""
    if value is None:
        return ""
    return escape(str(value), quote=True)


def _count(dashboard: Mapping[str, Any], *names: str) -> int:
    """Read a dashboard count while tolerating common key spellings."""
    for name in names:
        if name in dashboard:
            try:
                return int(dashboard[name])
            except (TypeError, ValueError):
                return 0
    return 0


def render_dashboard(
    dashboard: Mapping[str, Any], jobs: Iterable[Mapping[str, Any]]
) -> str:
    """Render a complete, self-contained HTML Mini CRM dashboard.

    The returned document has no external dependencies and can be written to an
    ``.html`` file and opened directly in a browser.  Job dictionaries are the
    records returned by :meth:`mini_crm.core.CRMService.jobs`.
    """
    dashboard = dashboard or {}
    job_list = list(jobs or [])

    cards = (
        ("Clients", _count(dashboard, "clients", "client_count"), "clients"),
        ("All jobs", _count(dashboard, "jobs", "job_count"), "jobs"),
        ("New", _count(dashboard, "new", "NEW", "new_jobs"), "new"),
        ("Active", _count(dashboard, "active", "ACTIVE", "active_jobs"), "active"),
        ("Done", _count(dashboard, "done", "DONE", "done_jobs"), "done"),
        ("Notes", _count(dashboard, "notes", "note_count"), "notes"),
    )
    card_html = "\n".join(
        '      <section class="card {kind}"><span>{label}</span><strong>{count}</strong></section>'.format(
            kind=kind, label=escape(label), count=count
        )
        for label, count, kind in cards
    )

    rows: list[str] = []
    for job in job_list:
        status = str(job.get("status", "NEW")).upper()
        status_class = "status-" + "".join(
            character.lower() if character.isalnum() else "-" for character in status
        )
        notes = job.get("notes") or []
        note_items: list[str] = []
        for note in notes:
            content = note.get("content", "") if isinstance(note, Mapping) else note
            note_items.append("<li>{}</li>".format(_text(content)))
        notes_html = "<ul class=\"notes\">{}</ul>".format("".join(note_items)) if note_items else '<span class="muted">No notes</span>'
        description = _text(job.get("description")) or '<span class="muted">No description</span>'
        rows.append(
            """      <article class=\"job\">
        <header><div><h2>{title}</h2><p class=\"client\">Client: {client}</p></div><span class=\"status {status_class}\">{status}</span></header>
        <p class=\"description\">{description}</p>
        <section class=\"job-notes\"><h3>Notes</h3>{notes}</section>
      </article>""".format(
                title=_text(job.get("title", "Untitled job")),
                client=_text(job.get("client_name", job.get("client_id", "Unknown"))),
                status=_text(status),
                status_class=_text(status_class),
                description=description,
                notes=notes_html,
            )
        )

    jobs_html = "\n".join(rows) if rows else '      <p class="empty">No jobs have been created yet.</p>'
    return """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Mini CRM · Job Manager</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif; color: #1e293b; background: #f8fafc; }
    * { box-sizing: border-box; } body { margin: 0; } main { max-width: 1100px; margin: auto; padding: 2rem 1rem 4rem; }
    h1 { margin: 0; font-size: 2rem; } .subtitle, .muted { color: #64748b; } .summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 1rem; margin: 1.5rem 0 2rem; }
    .card, .job { background: white; border: 1px solid #e2e8f0; border-radius: .75rem; box-shadow: 0 1px 2px rgb(15 23 42 / .04); } .card { padding: 1rem; border-top: 4px solid #64748b; } .card strong { display: block; font-size: 2rem; margin-top: .2rem; } .card.new { border-top-color: #2563eb; } .card.active { border-top-color: #d97706; } .card.done { border-top-color: #16a34a; }
    .jobs { display: grid; gap: 1rem; } .job { padding: 1.25rem; } .job header { display: flex; align-items: start; justify-content: space-between; gap: 1rem; } h2 { margin: 0; font-size: 1.2rem; } .client { margin: .35rem 0 0; color: #475569; } .description { line-height: 1.5; } .status { border-radius: 999px; font-size: .75rem; font-weight: 700; padding: .3rem .6rem; background: #e2e8f0; } .status-new { color: #1d4ed8; background: #dbeafe; } .status-active { color: #92400e; background: #fef3c7; } .status-done { color: #166534; background: #dcfce7; } .job-notes { border-top: 1px solid #e2e8f0; margin-top: 1rem; padding-top: .75rem; } h3 { font-size: .9rem; margin: 0 0 .5rem; } .notes { margin: 0; padding-left: 1.25rem; } .notes li { margin: .35rem 0; } .empty { padding: 2rem; text-align: center; background: white; border: 1px dashed #cbd5e1; border-radius: .75rem; }
  </style>
</head>
<body>
  <main>
    <header><h1>Mini CRM</h1><p class=\"subtitle\">Client jobs and notes at a glance</p></header>
    <section class=\"summary\" aria-label=\"Dashboard counts\">
{cards}
    </section>
    <section aria-labelledby=\"jobs-heading\"><h2 id=\"jobs-heading\">Jobs</h2><div class=\"jobs\">
{jobs}
    </div></section>
  </main>
</body>
</html>
""".format(cards=card_html, jobs=jobs_html)
