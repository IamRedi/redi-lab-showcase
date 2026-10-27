"""HTML rendering for the Mini CRM local interface."""

from html import escape


def render_dashboard(dashboard: dict, jobs: list[dict]) -> str:
    """Render the dashboard and its ordinary local POST forms."""
    def text(value: object) -> str:
        return escape(str(value), quote=True)

    rows = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            text(job["id"]), text(job["client_id"]), text(job["client_name"]),
            text(job["title"]), text(job["status"]), text(job["notes"]),
        )
        for job in jobs
    )
    if not rows:
        rows = "<tr><td colspan=\"6\">No jobs.</td></tr>"

    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Mini CRM</title>
<style>body{font-family:sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}section{margin:1.25rem 0}form{display:inline-block;vertical-align:top;margin:.4rem;padding:.6rem;border:1px solid #ccc}label{display:block;margin:.25rem 0}input,select,button{margin:.15rem 0}table{border-collapse:collapse;width:100%}th,td{border:1px solid #ccc;padding:.4rem;text-align:left;white-space:pre-wrap}</style>
</head><body><h1>Mini CRM</h1>
<section><strong>Clients:</strong> {clients_total} &nbsp; <strong>Jobs:</strong> {jobs_total} &nbsp; <strong>New:</strong> {new} &nbsp; <strong>Active:</strong> {active} &nbsp; <strong>Done:</strong> {done}</section>
<section>
<form method="post" action="/clients"><h2>New client</h2><label>Name <input name="name" required></label><button type="submit">Create client</button></form>
<form method="post" action="/jobs"><h2>New job</h2><label>Client ID <input type="number" name="client_id" required></label><label>Title <input name="title" required></label><label>Notes <input name="notes"></label><button type="submit">Create job</button></form>
<form method="post" action="/notes"><h2>Add note</h2><label>Job ID <input type="number" name="job_id" required></label><label>Note <input name="note" required></label><button type="submit">Add note</button></form>
<form method="post" action="/status"><h2>Set status</h2><label>Job ID <input type="number" name="job_id" required></label><label>Status <select name="status"><option>NEW</option><option>ACTIVE</option><option>DONE</option></select></label><button type="submit">Set status</button></form>
</section><section><h2>Jobs</h2><table><thead><tr><th>Job ID</th><th>Client ID</th><th>Client</th><th>Title</th><th>Status</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table></section>
</body></html>""".format(
        clients_total=text(dashboard["clients_total"]),
        jobs_total=text(dashboard["jobs_total"]),
        new=text(dashboard["NEW"]), active=text(dashboard["ACTIVE"]),
        done=text(dashboard["DONE"]), rows=rows,
    )
