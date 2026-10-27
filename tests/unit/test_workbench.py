"""REDI Workbench tests — real engine, real persisted state; zero provider/network."""

import json
import time
import urllib.request
from pathlib import Path

import pytest

from redi_lab.workbench.adapter import WorkbenchAdapter
from redi_lab.workbench.engine import WorkbenchBuild
from redi_lab.workbench.server import make_handler, serve


def test_end_to_end_prompt_to_result_with_real_engine():
    adapter = WorkbenchAdapter()
    started = adapter.start_build("Build me a pantry inventory tracker app")
    assert "build_id" in started
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=180)
    snap = adapter.snapshot(started["build_id"])
    assert snap["prompt"] == "Build me a pantry inventory tracker app"
    assert snap["status"] in ("VERIFIED", "FAILED")  # real verdict from the factory
    assert snap["verdict"] in ("APP READY", "APP FAILED")
    assert snap["tasks"], "task graph must come from the real DAG plan"
    assert {t["status"] for t in snap["tasks"]} <= {"PENDING", "RUNNING", "VERIFIED", "FAILED"}
    assert snap["provider_calls"] == 0 and snap["network_calls"] == 0  # dry, honest spend
    assert snap["elapsed_seconds"] > 0
    assert snap["project_path"] and Path(snap["project_path"]).exists()
    assert snap["tests"]["failed_attempts"] is not None
    assert snap["files"]


def test_engine_failure_shown_honestly(monkeypatch):
    import redi_lab.workbench.engine as engine

    def boom(**_kw):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(engine, "run_controlled_cold_start", boom)
    b = WorkbenchBuild("fail1", "prompt", "dry-fake")
    b._thread.join(timeout=10)
    snap = b.snapshot()
    assert snap["status"] == "FAILED"
    assert "engine exploded" in snap["error"]


def test_adapter_requires_prompt_and_rejects_inactive_backend():
    adapter = WorkbenchAdapter()
    assert "error" in adapter.start_build("   ")
    with pytest.raises(ValueError, match="not active"):
        WorkbenchAdapter(active_backend="openai")


def test_server_http_end_to_end():
    server, url = serve(port=0)
    try:
        with urllib.request.urlopen(url) as resp:
            html = resp.read().decode()
        assert "VIRTUTES" in html
        assert 'label for="prompt">What should the product do?' in html
        assert "BUILD" in html
        req = urllib.request.Request(
            url + "api/builds",
            data=json.dumps({"prompt": "Build the workbench demo app"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        started = json.loads(urllib.request.urlopen(req).read())
        build_id = started["build_id"]
        from redi_lab.workbench.server import _LAST_ADAPTER
        adapter = _LAST_ADAPTER
        build = adapter._builds[build_id]
        build._thread.join(timeout=180)
        snap = json.loads(urllib.request.urlopen(url + "api/builds/" + build_id).read())
        assert snap["status"] in ("VERIFIED", "FAILED")
        assert snap["verdict"] in ("APP READY", "APP FAILED")
        backends = json.loads(urllib.request.urlopen(url + "api/backends").read())["backends"]
        assert backends["dry-fake"]["active"] == "true"
        assert backends["openai"]["active"] == "false"
    finally:
        server.shutdown()


def test_rendered_selector_lists_all_backends_and_ceiling_input():
    server, url = serve(port=0)
    try:
        backends = json.loads(urllib.request.urlopen(url + "api/backends").read())["backends"]
        assert set(backends) == {"dry-fake", "openai", "openai-live",
                                 "openai-live-professional-product-v1",
                                 "openai-live-multistation-experimental",
                                 "openai-live-multistation-normal-app-experimental",
                                 "claude", "gemini", "ollama"}
        assert backends["openai-live-multistation-normal-app-experimental"]["active"] == "true"
        with urllib.request.urlopen(url) as resp:
            html = resp.read().decode()
        assert 'id="ceiling"' in html and "ceiling-row" in html
        assert "syncBackendUI" in html and "cost_ceiling_usd" in html  # selector toggles the ceiling input
        assert 'id="backend"' in html
    finally:
        server.shutdown()


def test_env_fallback_allows_crm_live_without_ui_key(tmp_path, monkeypatch):
    """Server-env key/flag drive the governed live path; UI sends only the ceiling."""
    import sys
    from redi_lab.workbench import engine

    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)

    monkeypatch.setenv("REDI_OPENAI_API_KEY", "k" * 30)
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    from redi_lab.workbench.engine import WorkbenchBuild
    from redi_lab.workbench.normal_app import canonical_fake_providers
    import json as _json

    def fake_client_factory():
        def _create(**kwargs):
            user = kwargs["messages"][1]["content"]
            for task, provider in canonical_fake_providers().items():
                artifact = {"build_database": "database.py", "build_dataflow": "dataflow.py",
                            "build_core": "core.py", "build_ui": "ui.py",
                            "integration_assembly": "service.py"}[task]
                if f"Implement ONLY {artifact}" in user:
                    envelope = provider("")  # the model returns the JSON patch envelope
                    return type("R", (), {"choices": [type("C", (), {
                        "finish_reason": "stop",
                        "message": type("M", (), {"content": envelope})()})()],
                        "usage": type("U", (), {"prompt_tokens": 300, "completion_tokens": 250,
                                                "prompt_tokens_details": None})()})()
            raise AssertionError("no canned response")

        class _Client:
            pass

        _Client.chat = type("Chat", (), {"completions": type(
            "Comp", (), {"create": staticmethod(_create)})()})()
        return _Client()

    build = WorkbenchBuild(
        "envfb1", "Build a Mini CRM / Job Manager app with clients and jobs",
        "openai-live-multistation-normal-app-experimental",
        ceiling="1.00", client_factory=fake_client_factory)
    build._thread.join(timeout=60)
    snap = build.snapshot()
    assert snap["status"] == "VERIFIED" and snap["verdict"] == "APP READY"
    assert snap["provider_calls"] == 5  # governed live path with env-only key
