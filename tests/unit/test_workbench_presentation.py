"""Truthful UI projection: missing metrics, qualification provenance, frozen states."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from redi_lab.workbench.presentation import present_snapshot
from redi_lab.workbench.crew import CREW
from scripts.workbench_visual_fixture import fixture_snapshot


def write(root, bid, data, name="live_run_evidence.json"):
    directory = root / ("wb-" + bid)
    directory.mkdir(exist_ok=True)
    p = directory / name
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_projection_preserves_snapshot_and_retained_bytes(tmp_path):
    bid = "0123456789ab"
    data = {"elapsed_seconds": 0, "execution_mode": "retained_qualification_offline", "call_facts": {"calls": 0}, "total_cost_usd": "0"}
    path = write(tmp_path, bid, data)
    original = path.read_bytes()
    snap = {"build_id": bid, "status": "VERIFIED", "elapsed_seconds": 0, "virtutes": {"crew": []}}
    old = deepcopy(snap)
    result = present_snapshot(snap, tmp_path)
    assert snap == old and path.read_bytes() == original
    assert result["elapsed_seconds"] == 0
    assert result["presentation"]["elapsed_seconds"] is None
    assert result["presentation"]["provider_calls"] == 0
    assert result["presentation"]["cost_usd"] == "0"
    assert result["presentation"]["input_tokens"] is None
    assert result["presentation"]["personas"] == list(CREW)


def test_metrics_come_from_retained_calls_without_default_zero(tmp_path):
    bid = "0123456789ab"
    write(tmp_path, bid, {"call_facts": {"calls": 2, "per_task": {
        "a": {"provider": "openai", "model": "recorded-model", "input": 100, "output": 20},
        "b": {"provider": "openai", "model": "recorded-model", "input": 70, "output": 10}}}})
    p = present_snapshot({"build_id": bid}, tmp_path)["presentation"]
    assert (p["input_tokens"], p["output_tokens"], p["provider_calls"]) == (170, 30, 2)
    assert p["cost_usd"] is None and p["elapsed_seconds"] is None
    assert p["model"] == "recorded-model" and p["provider"] == "openai"


def test_qualification_source_link_requires_exact_hash_and_is_not_state_transplant(tmp_path):
    source = write(tmp_path, "111111111111", {"status": "FAILED", "task_states": {"ui": "COMPLETED"}})
    origin = {"source_build_id": "wb-111111111111", "source_evidence_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "original_result": "FAILED"}
    write(tmp_path, "222222222222", {"source_build_id": origin["source_build_id"]})
    write(tmp_path, "222222222222", origin, "qualification_origin.json")
    snap = {"build_id": "222222222222", "status": "VERIFIED", "task_states": {}}
    result = present_snapshot(snap, tmp_path)
    assert result["presentation"]["source_run"]["status"] == "FAILED"
    assert result["task_states"] == {} and result["presentation"]["manifest"] is None
    source.write_text('{}', encoding="utf-8")
    assert present_snapshot(snap, tmp_path)["presentation"]["source_run"] is None


def test_inflight_manifest_uses_actual_task_state_and_keeps_unknown_metrics(tmp_path):
    snap = fixture_snapshot()
    result = present_snapshot(snap, tmp_path)
    assert result["task_states"]["core"] == "PENDING"
    assert result["presentation"]["manifest"]["tasks"][2]["dependencies"] == ["database", "workflow"]
    assert result["presentation"]["elapsed_seconds"] is None


def test_inflight_default_zero_is_not_presented_as_provider_measurement(tmp_path):
    snap = {"build_id": "0123456789ab", "backend": "openai-live-professional-product-v1",
            "status": "RUNNING", "provider_calls": 0, "elapsed_seconds": 9.2}
    write(tmp_path, snap["build_id"], {"tasks": []}, "execution_manifest.json")
    p = tmp_path / ("wb-" + snap["build_id"]) / "events.jsonl"
    p.write_text('{"event":"planning_started","ts":123}\n{"event":', encoding="utf-8")
    display = present_snapshot(snap, tmp_path)["presentation"]
    assert display["provider_calls"] is None
    assert display["events"] == [{"event": "planning_started", "ts": 123}]
    assert display["elapsed_seconds"] == 9.2


def test_browser_view_model_parallelism_missing_metrics_and_failure(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for shared browser view model")
    module = Path("redi_lab/workbench/static/view-model.js").resolve()
    data = present_snapshot(fixture_snapshot(), tmp_path)
    script = "const assert=require('node:assert/strict');const V=require("+json.dumps(str(module))+");const s="+json.dumps(data)+";"+r'''
const g=V.graph(s), nodes=Object.fromEntries(g.nodes.map(n=>[n.id,n]));
assert.equal(nodes.database.y,nodes.workflow.y);
assert.notEqual(nodes.database.x,nodes.workflow.x);
assert.equal(nodes.core.state,'WAITING');
assert.ok(nodes.core.y>nodes.database.y);
assert.deepEqual(g.edges.filter(e=>e.to.id==='core').map(e=>e.from.id),['database','workflow']);
assert.equal(g.edges.filter(e=>e.to.id==='core').some(e=>e.active),false);
// Density may change geometry, never execution facts or graph dependencies.
for(const height of [360,400,620]) {
 const fitted=V.graph(s,{desktop:true,height});
 assert.ok(fitted.height<=height);
 assert.ok(fitted.nodeHeight>=48);
 assert.deepEqual(fitted.nodes.map(n=>[n.id,n.state,n.dependencies]),g.nodes.map(n=>[n.id,n.state,n.dependencies]));
 assert.deepEqual(fitted.edges.map(e=>[e.from.id,e.to.id,e.active]),g.edges.map(e=>[e.from.id,e.to.id,e.active]));
 for(const n of fitted.nodes) assert.ok(n.y+fitted.nodeHeight<=height);
}
assert.equal(V.state('NOT_RUN'),'NOT_RUN');
assert.equal(V.tone('BLOCKED'),'blocked');
assert.equal(V.verdict({status:'BLOCKED',verdict:'APP FAILED'}),'BLOCKED');
assert.equal(V.metrics({execution_mode:'retained_qualification_offline',elapsed_seconds:0}).at(-1)[1],'—');
assert.equal(V.metrics({}).find(m=>m[0]==='Calls')[1],'—');
assert.equal(V.metrics({provider_calls:0}).find(m=>m[0]==='Calls')[1],'0');
assert.equal(V.metrics({cost_usd:'0'}).find(m=>m[0]==='Cost')[1],'$0.00');
assert.equal(V.failure({status:'FAILED',task_states:{ui:'FAILED'},detail:'KeyError: font-family'}).stage,'ui');
assert.equal(V.failure({status:'BLOCKED',detail:'Ceiling required'}).reason,'Ceiling required');
'''
    p = tmp_path / "view-test.js"
    p.write_text(script, encoding="utf-8")
    result = subprocess.run([node, str(p)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
