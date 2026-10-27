"""Workbench selector hydration tests — against the REAL served HTML/API."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request

import pytest

from redi_lab.workbench.server import serve

EXPECTED_BACKENDS = {
    "openai-live-professional-product-v1": True,
    "dry-fake": True,
    "openai-live": True,
    "openai-live-multistation-experimental": True,
    "openai-live-multistation-normal-app-experimental": True,
    "openai": False,
    "claude": False,
    "gemini": False,
    "ollama": False,
}


@pytest.fixture()
def server():
    server, url = serve(port=0)
    try:
        yield url
    finally:
        server.shutdown()


def _get(url):
    with urllib.request.urlopen(url) as resp:
        return resp.read().decode("utf-8"), dict(resp.headers)


def test_api_backends_contract_string_active(server):
    body, _ = _get(server + "api/backends")
    payload = json.loads(body)
    assert set(payload["backends"]) == set(EXPECTED_BACKENDS)
    # current contract: active serialized as STRINGS "true"/"false"
    for backend_id, meta in payload["backends"].items():
        assert meta["active"] in ("true", "false"), backend_id
        assert isinstance(meta["active"], str), backend_id
    assert payload["backends"]["openai-live-multistation-normal-app-experimental"]["active"] == "true"
    assert payload["backends"]["ollama"]["active"] == "false"


def test_rendered_select_contains_all_options_server_side(server):
    html, headers = _get(server)
    assert headers.get("Cache-Control") == "no-store"
    # every backend id appears as an <option value=...> in the RENDERED select
    html_body = html.split("<script>")[0]  # server-rendered markup only (JS template lives after)
    rendered = dict(re.findall(r'<option value="([^"]+)"([^>]*)>', html_body))
    assert set(rendered) == set(EXPECTED_BACKENDS)
    for backend_id, active in EXPECTED_BACKENDS.items():
        attrs = rendered[backend_id]
        assert ("disabled" in attrs) != active  # active selectable, inactive disabled
    assert "openai-live-multistation-normal-app-experimental" in rendered
    # no leftover template placeholder
    assert "BACKEND_OPTIONS" not in html


def test_client_normalizes_string_active_values(server):
    """Hydration JS normalizes 'true'/'false' strings to booleans before use."""
    html, _ = _get(server)
    assert "toLowerCase()==='true'" in html  # 'true' literal comparison
    assert "backendOptionsHtml" in html
    # idempotent: hydration only when the select is empty (rendered wins)
    assert "if(sel.options.length){return;}" in html


def test_ceiling_input_revealed_for_live_backends(server):
    html, _ = _get(server)
    assert 'id="ceiling-row"' in html
    assert "startsWith('openai-live')" in html  # toggle bound to live backends


def test_rendered_page_owner_build_flow_dispatches_exactly_once(server):
    """Real rendered page: server-rendered options present; the owner flow
    (select CRM live backend -> ceiling visible -> ceiling 1.00 -> BUILD)
    dispatches EXACTLY one build request with the exact fields."""
    from redi_lab.workbench.adapter import WorkbenchAdapter

    recorded: list[dict] = []

    class RecordingAdapter(WorkbenchAdapter):
        def start_build(self, prompt, **kwargs):
            recorded.append({"prompt": prompt, **kwargs})
            return {"build_id": "stub-build-1"}

    stub = RecordingAdapter()
    server2, url2 = serve(port=0, adapter=stub)
    try:
        html, headers = _get(url2)
        assert headers.get("Cache-Control") == "no-store"
        # page starts with server-rendered options (CRM backend included)
        rendered = dict(re.findall(
            r'<option value="([^"]+)"([^>]*)>', html.split("<script>")[0]))
        assert "openai-live-multistation-normal-app-experimental" in rendered
        assert rendered["openai-live-multistation-normal-app-experimental"] == ""
        # the initialization must bind handlers regardless of rendered options
        assert "bindEventHandlers();" in html
        assert "document.getElementById('build').addEventListener('click',startBuild);" in html
        assert "addEventListener('change',syncBackendUI);" in html
        assert "if(rendered.length){return;}" not in html.split("<script>")[1]
        assert "cost_ceiling_usd" in html
        assert 'onclick="startBuild()"' not in html  # no inline-only binding

        # owner presses BUILD: exactly one dispatch with exact fields
        owner_prompt = ("Build a Mini CRM / Job Manager app with clients, jobs linked "
                        "to clients, statuses NEW/ACTIVE/DONE, notes, dashboard counts, "
                        "SQLite persistence and a local runnable UI.")
        req = urllib.request.Request(
            url2 + "api/builds",
            data=json.dumps({"prompt": owner_prompt,
                             "backend": "openai-live-multistation-normal-app-experimental",
                             "cost_ceiling_usd": "1.00"}).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req).read())
        assert resp["build_id"] == "stub-build-1"
        assert len(recorded) == 1
        assert recorded[0]["prompt"] == owner_prompt
        assert recorded[0]["backend"] == "openai-live-multistation-normal-app-experimental"
        assert recorded[0]["ceiling"] == "1.00"
    finally:
        server2.shutdown()


def test_inline_page_script_parses_as_javascript(server):
    """Regression: the served inline script must survive the browser's parse
    step. A single parse-time SyntaxError (broken ternary, raw newline inside
    a string literal) silently kills the ENTIRE script block — init(), event
    bindings, the ceiling-row toggle — while substring assertions still pass.
    Node's parser is the same ECMAScript grammar the browser applies; skip
    when node is not installed."""
    html, _ = _get(server)
    script = html.split("<script>")[1].split("</script>")[0]
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(script)
        proc = subprocess.run([node, "--check", path], capture_output=True)
    finally:
        os.unlink(path)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-800:]


def test_result_screen_identifies_backend_and_terminates_on_blocked(server):
    """The Result screen must name the selected backend and must show the
    dry-run label ONLY for dry-fake (a live build that fails or blocks must
    never be reported as 'dry run, fake workers'); polling must stop when a
    governed build ends BLOCKED instead of polling the factory screen forever."""
    html, _ = _get(server)
    script = html.split("<script>")[1].split("</script>")[0]
    assert "||s.status==='BLOCKED'" in script.replace("\n", "")  # poll terminates on BLOCKED
    assert "['Backend',s.backend||'—']" in script
    assert "s.backend==='dry-fake'?'Offline execution · fake workers'" in script
    assert "['Calls',number(" in script  # missing measurements are not fabricated zeroes


def test_live_crm_record_keeps_backend_and_ceiling_and_blocks(server, monkeypatch):
    """Provider disabled: the selected live CRM backend must reach the build
    record intact, be dispatched to the governed CRM branch, and end BLOCKED
    with zero provider calls — never dry-fake, never '0 USD dry run'."""
    import time

    from redi_lab.workbench.engine import WorkbenchAdapter

    monkeypatch.delenv("REDI_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REDI_LIVE_PROVIDER", raising=False)
    adapter = WorkbenchAdapter()
    resp = adapter.start_build(
        "Build a Mini CRM / job manager",
        backend="openai-live-multistation-normal-app-experimental",
        ceiling="1.00")
    build_id = resp["build_id"]
    deadline = time.time() + 5
    while time.time() < deadline:
        snap = adapter.snapshot(build_id)
        if snap["status"] != "RUNNING":
            break
        time.sleep(0.05)
    assert snap["backend"] == "openai-live-multistation-normal-app-experimental"
    assert snap["cost_ceiling_usd"] == "1.00"
    assert snap["status"] == "BLOCKED"
    assert snap["verdict"] == "BLOCKED"
    assert snap["provider_calls"] == 0
    assert snap["network_calls"] == 0
    assert "REDI_LIVE_PROVIDER=1" in snap["missing"]
    assert "REDI_OPENAI_API_KEY" in snap["missing"]


def test_server_reports_own_identity_and_live_env_booleans_only(server):
    """Diagnostic surface for the double-bind incident: /api/server identifies
    the ANSWERING process and what ITS environment sees — booleans only,
    never the key value."""
    body, _ = _get(server + "api/server")
    payload = json.loads(body)
    assert isinstance(payload["pid"], int) and payload["pid"] > 0
    assert payload["started_at"] > 0
    assert set(payload["live_provider"]) == {"key_present", "live_flag_is_1"}
    assert payload["live_provider"]["key_present"] is False      # test env is clean
    assert payload["live_provider"]["live_flag_is_1"] is False
    assert "sk-" not in body                                     # no key material, ever


def test_second_server_on_same_port_fails_loudly():
    """Root cause of the false-BLOCKED incident: SO_REUSEADDR let a re-armed
    server silently co-bind a port already served by a stale process. The
    second bind must now raise instead of shadowing."""
    import urllib.parse

    server1, url1 = serve(port=0)
    try:
        port = urllib.parse.urlparse(url1).port
        with pytest.raises(OSError):
            serve(port=port)
    finally:
        server1.shutdown()
