"""Owner budget and source freshness contracts; no external provider calls."""
import json
import os
from pathlib import Path
import time
import urllib.request

from redi_lab.workbench.build_budget import budget_telemetry, make_authorization
from redi_lab.workbench.runtime_identity import RuntimeIdentity, StaleRuntimeError


def _runtime():
    return {"fresh": True, "fingerprint": "a" * 64,
            "source_contract": "resolved_immutable_source_bytes_v2", "changed_files": []}


def test_runtime_hashes_source_bytes_and_reports_exact_change(tmp_path):
    source = tmp_path / "engine.py"
    source.write_text("a", encoding="utf-8")
    identity = RuntimeIdentity({"engine.py": source})
    startup = identity.require_current()
    (tmp_path / "events.jsonl").write_text("runtime evidence", encoding="utf-8")
    os.utime(source, None)
    assert identity.require_current()["fingerprint"] == startup["fingerprint"]
    assert identity.status()["changed_files"] == []
    source.write_text("b", encoding="utf-8")
    status = identity.status()
    assert status["changed_files"] == ["engine.py"]
    assert status["changed_source_details"][0]["path"] == str(source.resolve())
    assert status["changed_source_details"][0]["startup_sha256"] != status["changed_source_details"][0]["current_sha256"]
    assert status["changed_source_details"][0]["content_changed"] is True
    try:
        identity.require_current()
    except StaleRuntimeError as exc:
        assert "engine.py" in str(exc)
    else:
        raise AssertionError("source edit did not block")



def test_real_workbench_reads_and_repeated_offline_builds_keep_source_fresh(tmp_path, monkeypatch):
    from redi_lab.workbench import engine
    from redi_lab.workbench.runtime_identity import normal_app_runtime_status
    from redi_lab.workbench.server import serve
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    server, url = serve(port=0)
    try:
        startup = normal_app_runtime_status()
        assert startup["fresh"] is True
        for _ in range(2):
            with urllib.request.urlopen(url) as response:
                assert b"Build settings" in response.read()
            request = urllib.request.Request(url + "api/builds", data=json.dumps(
                {"prompt": "Build an offline pantry tracker", "backend": "dry-fake"}).encode(),
                headers={"Content-Type": "application/json"})
            build_id = json.load(urllib.request.urlopen(request))["build_id"]
            from redi_lab.workbench.server import _LAST_ADAPTER
            _LAST_ADAPTER._builds[build_id]._thread.join(30)
            with urllib.request.urlopen(url + "api/builds/" + build_id) as response:
                assert json.load(response)["status"] in ("VERIFIED", "FAILED")
            assert normal_app_runtime_status()["fingerprint"] == startup["fingerprint"]
            assert normal_app_runtime_status()["fresh"] is True
    finally:
        server.shutdown()


def test_authorization_validates_and_accounts_exact_owner_values():
    from pytest import raises
    args = dict(overall_ceiling="5.00", visual_ceiling="3.00", max_visual_calls=3,
                max_visual_recovery=1, visual_required=True, runtime=_runtime(),
                credential_present=True, live_arm=True, visual_arm=True)
    record = make_authorization("a" * 12, "openai-live-professional-product-v1", **args)
    assert record["overall_ceiling_usd"] == "5.00"
    assert record["visual_ceiling_usd"] == "3.00"
    assert record["max_visual_generation_calls"] == 3
    assert record["max_visual_recovery_generations"] == 1
    assert "key" not in json.dumps(record).lower()
    telemetry = budget_telemetry(record, total_spent="1.25", visual_spent="0.75",
                                 total_reserved="0.50", visual_reserved="0.25")
    assert telemetry["remaining_total_usd"] == "3.25"
    assert telemetry["remaining_visual_usd"] == "2.00"
    with raises(ValueError, match="Visual ceiling cannot exceed"):
        make_authorization("a" * 12, "openai-live-professional-product-v1", **{**args, "visual_ceiling": "6"})
    with raises(ValueError, match="visual ceiling"):
        make_authorization("a" * 12, "openai-live-professional-product-v1", **{**args, "visual_ceiling": None})
    with raises(ValueError, match="recovery"):
        make_authorization("a" * 12, "openai-live-professional-product-v1", **{**args, "max_visual_recovery": 3})


def test_workbench_persists_owner_budget_and_paid_adapters_consume_it(tmp_path, monkeypatch):
    from redi_lab.workbench import engine
    from redi_lab.workbench.professional import pipeline
    from redi_lab.workbench.professional.provider import GovernedProvider
    from redi_lab.workbench.professional.openai_visual import OpenAIVisualWorker

    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    monkeypatch.setenv("REDI_OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    monkeypatch.setenv("REDI_ZANA_VISUAL_AUTHORIZED", "1")
    observed = {}

    def fake_run(self):
        assert isinstance(self.provider, GovernedProvider)
        assert isinstance(self.visual_worker, OpenAIVisualWorker)
        observed["overall"] = str(self.provider.ceiling)
        observed["visual"] = str(self.visual_worker.visual_ceiling)
        observed["calls"] = self.visual_worker.max_calls
        observed["recovery"] = self.visual_worker.max_recovery_generations
        return {"status": "OWNER_ACTION_REQUIRED", "verified": False, "component_map": {},
                "detail": "offline inspection only", "provider_calls": 0, "total_cost_usd": "0"}

    monkeypatch.setattr(pipeline.ProfessionalRunner, "run", fake_run)
    adapter = engine.WorkbenchAdapter()
    assert "error" in adapter.start_build("AUREA COFFEE $999", backend="openai-live-professional-product-v1",
                                          ceiling="5.00")
    result = adapter.start_build("AUREA COFFEE $999", backend="openai-live-professional-product-v1",
                                 ceiling="5.00", visual_ceiling="3.00", max_visual_calls=3,
                                 max_visual_recovery=1)
    build_id = result["build_id"]
    adapter._builds[build_id]._thread.join(10)
    assert observed == {"overall": "5.00", "visual": "3.00", "calls": 3, "recovery": 1}
    root = tmp_path / ("wb-" + build_id)
    auth = json.loads((root / "build_authorization.json").read_text(encoding="utf-8"))
    assert auth["logical_run_id"] == build_id
    assert auth["runtime_identity"]["fresh_at_authorization"] is True
    assert auth["authorization_state"] == {"credential_present": True, "live_arm": True, "visual_arm": True}
    assert "offline-test-key" not in json.dumps(auth)
    snap = engine.WorkbenchAdapter().snapshot(build_id)
    assert snap["budget_telemetry"]["authorized_visual_usd"] == "3.00"
    assert snap["budget_telemetry"]["authorized_overall_usd"] == "5.00"
    assert snap["budget_telemetry"]["remaining_total_usd"] == "5.00"
    assert snap["provider_calls"] == 0


def test_paid_preflight_blocks_without_visual_arm_or_stale_source(tmp_path, monkeypatch):
    from redi_lab.workbench import engine
    from redi_lab.workbench import runtime_identity
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    monkeypatch.setenv("REDI_OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    monkeypatch.delenv("REDI_ZANA_VISUAL_AUTHORIZED", raising=False)
    adapter = engine.WorkbenchAdapter()
    def build():
        result = adapter.start_build("AUREA", backend="openai-live-professional-product-v1",
                                     ceiling="5", visual_ceiling="3", max_visual_calls=3,
                                     max_visual_recovery=1)
        adapter._builds[result["build_id"]]._thread.join(10)
        return adapter.snapshot(result["build_id"])
    blocked = build()
    assert blocked["status"] == "OWNER_ACTION_REQUIRED"
    assert "REDI_ZANA_VISUAL_AUTHORIZED" in blocked["detail"]
    assert blocked["provider_calls"] == 0
    source = tmp_path / "source.py"
    source.write_text("before", encoding="utf-8")
    monkeypatch.setattr(runtime_identity, "NORMAL_APP_RUNTIME", RuntimeIdentity({"source.py": source}))
    source.write_text("after", encoding="utf-8")
    stale = build()
    assert stale["status"] == "OWNER_ACTION_REQUIRED"
    assert "source.py" in stale["detail"]
    assert stale["provider_calls"] == 0
    assert stale["runtime_identity"]["changed_source_details"][0]["content_changed"] is True


def test_exception_after_attempt_retains_actual_calls_and_reservation(tmp_path, monkeypatch):
    from decimal import Decimal
    from redi_lab.workbench import engine
    from redi_lab.workbench.professional import pipeline
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    def attempted_then_failed(self):
        self.provider.facts['calls'] = 1
        self.provider.reserved['specification'] = Decimal('0.50')
        raise RuntimeError('offline injected failure after reservation')
    monkeypatch.setattr(pipeline.ProfessionalRunner, 'run', attempted_then_failed)
    adapter = engine.WorkbenchAdapter()
    result = adapter.start_build('Fixture failure', backend='openai-live-professional-product-v1',
                                 ceiling='5', key='offline-key', live_flag='1', client_factory=lambda: object())
    build_id = result['build_id']
    adapter._builds[build_id]._thread.join(10)
    snap = engine.WorkbenchAdapter().snapshot(build_id)
    assert snap['provider_calls'] == 1
    assert snap['budget_telemetry']['spent_total_usd'] == '0'
    assert snap['budget_telemetry']['reserved_total_usd'] == '0.50'
    assert snap['budget_telemetry']['remaining_total_usd'] == '4.50'


def test_budget_fields_are_owner_facing_and_never_in_prompt_parsing():
    root = Path(__file__).parents[2] / "redi_lab" / "workbench" / "static"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "workbench.js").read_text(encoding="utf-8")
    assert all(f'id="{field}"' in html for field in
               ("ceiling", "visual-ceiling", "max-visual-calls", "max-visual-recovery"))
    assert "body.visual_ceiling_usd=visual" in js
    assert "body.max_visual_generation_calls=calls" in js
    assert "body.max_visual_recovery_generations=recovery" in js


def test_zero_visual_ceiling_is_an_explicit_visuals_off_authorization():
    from pytest import raises
    from redi_lab.workbench.build_budget import visuals_disabled
    args = dict(overall_ceiling="0.95", visual_ceiling="0", max_visual_calls=3,
                max_visual_recovery=1, visual_required=True, runtime=_runtime(),
                credential_present=True, live_arm=True, visual_arm=False)
    record = make_authorization("a" * 12, "openai-live-professional-product-v1", **args)
    assert record["visual_ceiling_usd"] == "0.00" and record["visuals_disabled"] is True
    assert record["max_visual_generation_calls"] == 0 and record["max_visual_recovery_generations"] == 0
    assert visuals_disabled(record)
    telemetry = budget_telemetry(record, total_spent="0.40")
    assert telemetry["remaining_visual_usd"] == "0.00" and telemetry["remaining_total_usd"] == "0.55"
    for value in ("0.0", "0.00"):
        assert make_authorization("a" * 12, "openai-live-professional-product-v1",
                                  **{**args, "visual_ceiling": value})["visuals_disabled"] is True
    # Any enabled visual budget still reserves at least USD 1.00 for the next visual call.
    with raises(ValueError, match="at least USD 1.00"):
        make_authorization("a" * 12, "openai-live-professional-product-v1",
                           **{**args, "overall_ceiling": "2.00", "visual_ceiling": "0.50"})
    enabled = make_authorization("a" * 12, "openai-live-professional-product-v1",
                                 **{**args, "overall_ceiling": "2.00", "visual_ceiling": "1.00"})
    assert not visuals_disabled(enabled) and enabled["visuals_disabled"] is False


def test_visuals_off_build_runs_without_zana_arm_or_visual_worker(tmp_path, monkeypatch):
    from redi_lab.workbench import engine
    from redi_lab.workbench.professional import pipeline
    monkeypatch.setattr(engine, "WORKBENCH_ROOT", tmp_path)
    monkeypatch.setenv("REDI_OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("REDI_LIVE_PROVIDER", "1")
    monkeypatch.delenv("REDI_ZANA_VISUAL_AUTHORIZED", raising=False)
    observed = {}

    def fake_run(self):
        observed.update(visual_worker=self.visual_worker, reviewer=self.visual_reviewer,
                        overall=str(self.provider.ceiling))
        return {"status": "OWNER_ACTION_REQUIRED", "verified": False, "component_map": {},
                "detail": "offline inspection only", "provider_calls": 0, "total_cost_usd": "0"}

    monkeypatch.setattr(pipeline.ProfessionalRunner, "run", fake_run)
    adapter = engine.WorkbenchAdapter()
    result = adapter.start_build("Workshop job tracker", backend="openai-live-professional-product-v1",
                                 ceiling="0.95", visual_ceiling="0", max_visual_calls=3, max_visual_recovery=1)
    adapter._builds[result["build_id"]]._thread.join(10)
    assert observed == {"visual_worker": None, "reviewer": None, "overall": "0.95"}
    snap = adapter.snapshot(result["build_id"])
    assert "REDI_ZANA_VISUAL_AUTHORIZED" not in (snap.get("detail") or "")
    assert snap["budget_telemetry"]["authorized_visual_usd"] == "0.00"

