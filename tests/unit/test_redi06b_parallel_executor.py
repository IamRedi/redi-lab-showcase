"""REDI-06B — isolated parallel DAG execution tests.

Deterministic where required; the concurrency proof uses a Barrier so it
cannot pass sequentially.  Zero network, zero live providers.
"""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from redi_lab.dag_scheduler import NodeRunState, product_dag_hash, replay_dag_run
from redi_lab.domain import (
    DagDependency,
    DagNode,
    ProductDAG,
    ProductSpec,
    Requirement,
    RiskTier,
    StationContract,
    TaskContract,
)
from redi_lab.parallel_dag_executor import (
    DuplicateNodeResultError,
    ParallelDagExecutor,
    UnknownNodeResultError,
    WorkerExecutionResult,
    recover_orphaned_running,
)
from redi_lab.persistence import EvidenceLedger, bootstrap_schema
from redi_lab.runtime.contracts import WorkerRunner

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ALPHA_IMPL = (
    "def alpha(text):\n"
    "    if not isinstance(text, str):\n        raise TypeError\n"
    "    return text.upper()\n"
)
_BETA_IMPL = (
    "def beta(text):\n"
    "    if not isinstance(text, str):\n        raise TypeError\n"
    "    return text[::-1]\n"
)


@dataclass
class _GraphBundle:
    product: ProductSpec
    task: TaskContract
    builder: StationContract
    beta_station: StationContract
    integration: StationContract
    dag: ProductDAG


def _bundle() -> _GraphBundle:
    product = ProductSpec(
        requested_outcome="Implement alpha (uppercase) and beta (reverse) modules.",
        requirements=[Requirement(text="alpha uppercases", kind="functional"),
                      Requirement(text="beta reverses", kind="functional")],
        constraints=["Stdlib only."],
        risk_tier=RiskTier.LOW, unresolved_requirements=[],
    )
    task = TaskContract(goal="implement alpha and beta modules")
    builder = StationContract(role="builder", required_output_kinds=["patch_edit"], allowed_tool_classes=[])
    beta_station = StationContract(role="builder_beta", required_output_kinds=["patch_edit"], allowed_tool_classes=[])
    integration = StationContract(role="integration", required_output_kinds=["verdict"], allowed_tool_classes=[])
    dag = ProductDAG(
        nodes=[
            DagNode(node_id="node_a", task_id=str(task.id), station_id=str(builder.id),
                    metadata={"selected_paths": ["alpha/__init__.py"],
                              "produces_handoff_ids": ["handoff_alpha"],
                              "produces_handoff_types": ["alpha.impl.v1"]}),
            DagNode(node_id="node_b", task_id=str(task.id), station_id=str(beta_station.id),
                    metadata={"selected_paths": ["beta/__init__.py"],
                              "produces_handoff_ids": ["handoff_beta"],
                              "produces_handoff_types": ["beta.impl.v1"]}),
            DagNode(node_id="node_c", task_id=str(task.id), station_id=str(integration.id),
                    metadata={"requires_handoff_ids": ["handoff_alpha", "handoff_beta"],
                              "requires_handoff_types": ["alpha.impl.v1", "beta.impl.v1"]}),
        ],
        dependencies=[
            DagDependency(upstream="node_a", downstream="node_c", kind="hard"),
            DagDependency(upstream="node_b", downstream="node_c", kind="hard"),
        ],
    )
    return _GraphBundle(product, task, builder, beta_station, integration, dag)


def _stations(bundle: _GraphBundle) -> dict:
    return {"node_a": bundle.builder, "node_b": bundle.beta_station, "node_c": bundle.integration}


def _decls():
    return {
        "required": {"node_c": ["handoff_alpha", "handoff_beta"]},
        "produced_ids": {"node_a": ["handoff_alpha"], "node_b": ["handoff_beta"]},
        "produced_types": {"handoff_alpha": ("alpha.impl.v1",),
                           "handoff_beta": ("beta.impl.v1",)},
    }


@dataclass
class _PatchWorker(WorkerRunner):
    content: str = ""
    success: bool = True
    handoff_ids: tuple[str, ...] = ()
    handoff_types_by_id: dict = field(default_factory=dict)
    error: Exception | None = None
    calls: int = 0
    barrier: threading.Barrier | None = None
    descriptor_overrides: dict = field(default_factory=dict)

    def run_node(self, *, handle, **_):
        import time as _time

        self.calls += 1
        if self.barrier is not None:
            self.barrier.wait(timeout=10)
        start = _time.perf_counter()
        if self.error is not None:
            raise self.error
        sub = {"node_a": "alpha", "node_b": "beta", "node_c": "integration"}.get(
            handle.node_id, "integration")
        target = handle.root / sub / "__init__.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.content, encoding="utf-8")
        types_by_id = {
            hid: tuple(self.handoff_types_by_id.get(hid, (f"{hid}.v1",)))
            for hid in (self.handoff_ids if self.success else ())
        }
        artifacts = []
        for hid in (self.handoff_ids if self.success else ()):
            contract = dict(handle.handoff_output_contracts.get(hid, {}))
            descriptor = {
                "handoff_id": hid,
                "artifact_id": f"artifact::{handle.node_id}::{hid}",
                "path": target.relative_to(handle.root).as_posix(),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                "kind": "file",
                "size_bytes": target.stat().st_size,
                "producer_node_id": contract.get("producer_node_id", handle.node_id),
                "producer_station_id": contract.get(
                    "producer_station_id", handle.context_package.station_id
                ),
                "consumer_scope": contract.get("consumer_scope", ""),
                "metadata": {"purpose": "test handoff"},
            }
            descriptor.update(self.descriptor_overrides)
            artifacts.append(descriptor)
        return WorkerExecutionResult(
            node_id=handle.node_id,
            workspace_id=handle.workspace_id,
            success=self.success,
            compute_seconds=round(_time.perf_counter() - start, 6),
            produced_handoff_ids=self.handoff_ids if self.success else (),
            produced_handoff_types_by_id=types_by_id,
            artifacts=tuple(artifacts),
        )


def _executor(tmp_path: Path, bundle: _GraphBundle, workers: dict, *,
              max_parallelism=2, ledger: EvidenceLedger | None = None,
              fixture_root: Path | None = None,
              consumer_scope_by_handoff_id: dict | None = None):
    product, task = bundle.product, bundle.task
    from redi_lab.context_compiler import DEFAULT_BUDGET_BYTES

    return ParallelDagExecutor(
        dag=bundle.dag,
        station_by_node={"node_a": bundle.builder, "node_b": bundle.beta_station,
                         "node_c": bundle.integration},
        task_by_node={"node_a": task, "node_b": task, "node_c": task},
        node_workers=workers,
        required_handoff_ids_by_node=_decls()["required"],
        produced_handoff_ids_by_node=_decls()["produced_ids"],
        produced_handoff_types_by_id=_decls()["produced_types"],
        consumer_scope_by_handoff_id=consumer_scope_by_handoff_id,
        fixture_root=(fixture_root or _REPO_ROOT / "experiments" / "fixtures" / "dag_pair_pkg"),
        product=product,
        max_parallelism=max_parallelism,
        context_budget_bytes=DEFAULT_BUDGET_BYTES,
        ledger=ledger,
    )


# ---------------------------------------------------------------------------
# Real concurrency proof (Barrier) — sequential execution cannot pass this
# ---------------------------------------------------------------------------


def test_06b_a_and_b_in_flight_simultaneously_barrier(tmp_path: Path):
    bundle = _bundle()
    barrier = threading.Barrier(2, timeout=10)
    entered = {"node_a": False, "node_b": False}
    lock = threading.Lock()

    class _BarrierWorker(_PatchWorker):
        def run_node(self, *, handle, **kw):
            with lock:
                entered[handle.node_id] = True
            barrier.wait(timeout=10)
            return super().run_node(handle=handle, **kw)

    workers = {
        "node_a": _BarrierWorker(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                                 handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _BarrierWorker(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                                 handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
        "node_c": _PatchWorker(content="integration PASS\n", handoff_ids=()),
    }
    ex = _executor(tmp_path, bundle, workers)
    result = ex.run()
    assert result.final_states["node_a"] == NodeRunState.COMPLETED
    assert result.final_states["node_b"] == NodeRunState.COMPLETED
    assert result.final_states["node_c"] == NodeRunState.COMPLETED
    assert entered == {"node_a": True, "node_b": True}
    assert result.launch_order == ("node_a", "node_b", "node_c")


def test_06b_max_parallelism_one_forces_sequential(tmp_path: Path):
    in_flight = {"cur": 0, "max": 0}
    lock = threading.Lock()

    class _TrackingWorker(_PatchWorker):
        def run_node(self, *, handle, **kw):
            with lock:
                in_flight["cur"] += 1
                in_flight["max"] = max(in_flight["max"], in_flight["cur"])
            try:
                return super().run_node(handle=handle, **kw)
            finally:
                with lock:
                    in_flight["cur"] -= 1

    bundle = _bundle()
    workers = {
        "node_a": _TrackingWorker(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                                  handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _TrackingWorker(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                                  handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
        "node_c": _TrackingWorker(content="integration PASS\n"),
    }
    ex = _executor(tmp_path, bundle, workers, max_parallelism=1)
    result = ex.run()
    assert result.final_states["node_c"] == NodeRunState.COMPLETED
    assert in_flight["max"] == 1  # bound respected — never two nodes in flight


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_06b_a_exception_b_completes_c_blocked(tmp_path: Path):
    bundle = _bundle()
    worker_a = _PatchWorker(content=_ALPHA_IMPL, error=RuntimeError("worker exploded"))
    worker_b = _PatchWorker(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                            handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)})
    workers = {
        "node_a": worker_a,
        "node_b": worker_b,
        "node_c": _PatchWorker(content="integration PASS\n", handoff_ids=()),
    }
    ex = _executor(tmp_path, bundle, workers)
    result = ex.run()
    assert result.final_states["node_a"] == NodeRunState.FAILED   # typed failure
    assert result.final_states["node_b"] == NodeRunState.COMPLETED  # sibling unaffected
    assert result.final_states["node_c"] == NodeRunState.BLOCKED
    assert worker_a.calls == 1 and worker_b.calls == 1


def test_06b_a_graceful_failure_b_completes_c_blocked(tmp_path: Path):
    bundle = _bundle()
    worker_a = _PatchWorker(content="def alpha(text): return text  # wrong\n", success=False)
    worker_b = _PatchWorker(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                            handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)})
    workers = {
        "node_a": worker_a,
        "node_b": worker_b,
        "node_c": _PatchWorker(content="integration PASS\n", handoff_ids=()),
    }
    ex = _executor(tmp_path, bundle, workers)
    result = ex.run()
    assert result.final_states["node_a"] == NodeRunState.FAILED
    assert result.final_states["node_b"] == NodeRunState.COMPLETED
    assert result.final_states["node_c"] == NodeRunState.BLOCKED


def test_06b_slow_b_does_not_block_a_completion_evidence(tmp_path: Path):
    import time as _time

    bundle = _bundle()

    class _SlowWorker(_PatchWorker):
        def run_node(self, *, handle, **kw):
            result = super().run_node(handle=handle, **kw)
            _time.sleep(0.2)  # B slower; A's completion evidence still lands first
            return result

    workers = {
        "node_a": _PatchWorker(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                               handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _SlowWorker(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                              handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
        "node_c": _PatchWorker(content="integration PASS\n", handoff_ids=()),
    }
    ex = _executor(tmp_path, bundle, workers)
    result = ex.run()
    assert result.final_states["node_a"] == NodeRunState.COMPLETED
    assert result.final_states["node_b"] == NodeRunState.COMPLETED
    assert result.completion_order[0] == "node_a"


# ---------------------------------------------------------------------------
# Workspace isolation (adversarial)
# ---------------------------------------------------------------------------


def test_06b_workspace_isolation_roots_distinct_and_private(tmp_path: Path):
    bundle = _bundle()
    roots = {}
    observed = {}

    class _IsolationProbe(_PatchWorker):
        def run_node(self, *, handle, **kw):
            roots[handle.node_id] = handle.root
            if handle.node_id == "node_a":
                marker = handle.root / "alpha" / "marker_private.txt"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("A-private-runtime", encoding="utf-8")
            return super().run_node(handle=handle, **kw)

    class _VerifyC(_PatchWorker):
        def run_node(self, *, handle, **kw):
            # C runs only after A and B COMPLETED — workspaces still alive,
            # so this is the only place cross-root state is observable.
            a_root, b_root = Path(roots["node_a"]), Path(roots["node_b"])
            observed["distinct"] = a_root != b_root
            observed["marker_in_a"] = len(list(a_root.rglob("marker_private.txt")))
            observed["marker_in_b"] = len(list(b_root.rglob("marker_private.txt")))
            return super().run_node(handle=handle, **kw)

    workers = {
        "node_a": _IsolationProbe(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                                  handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _IsolationProbe(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                                  handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
        "node_c": _VerifyC(content="integration PASS\n", handoff_ids=()),
    }
    ex = _executor(tmp_path, bundle, workers)
    result = ex.run()
    assert result.final_states["node_c"] == NodeRunState.COMPLETED  # probe ran
    assert observed["distinct"] is True
    assert observed["marker_in_a"] == 1   # A's private runtime file exists
    assert observed["marker_in_b"] == 0   # ...but never leaks into B's root


def test_06b_a_private_write_is_not_present_in_b_workspace(tmp_path: Path):
    bundle = _bundle()
    roots = {}
    observed = {}

    class _AdversarialA(_PatchWorker):
        def run_node(self, *, handle, **kw):
            roots["node_a"] = handle.root
            stray = handle.root / "injected_by_a.txt"
            stray.write_text("A wrote this", encoding="utf-8")
            return super().run_node(handle=handle, **kw)

    class _SentinelB(_PatchWorker):
        def run_node(self, *, handle, **kw):
            roots["node_b"] = handle.root
            return super().run_node(handle=handle, **kw)

    class _VerifyC(_PatchWorker):
        def run_node(self, *, handle, **kw):
            b_root = Path(roots["node_b"])
            observed["b_clean"] = not list(b_root.rglob("injected_by_a.txt"))
            return super().run_node(handle=handle, **kw)

    workers = {
        "node_a": _AdversarialA(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                                handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _SentinelB(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                             handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
        "node_c": _VerifyC(content="integration PASS\n", handoff_ids=()),
    }
    ex = _executor(tmp_path, bundle, workers)
    result = ex.run()
    assert result.final_states["node_b"] == NodeRunState.COMPLETED
    assert result.final_states["node_c"] == NodeRunState.COMPLETED  # probe ran
    assert observed["b_clean"] is True


# ---------------------------------------------------------------------------
# Per-node context firewall
# ---------------------------------------------------------------------------


def test_06b_context_isolation_between_nodes(tmp_path: Path):
    bundle = _bundle()
    captured: dict = {}

    class _ContextCapture(_PatchWorker):
        def run_node(self, *, handle, **kw):
            captured[handle.node_id] = handle.context_package
            return super().run_node(handle=handle, **kw)

    workers = {
        "node_a": _ContextCapture(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                                  handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _ContextCapture(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                                  handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
        "node_c": _PatchWorker(content="integration PASS\n", handoff_ids=()),
    }
    ex = _executor(tmp_path, bundle, workers)
    result = ex.run()
    assert result.final_states["node_a"] == NodeRunState.COMPLETED
    assert result.final_states["node_b"] == NodeRunState.COMPLETED

    a_ev = captured["node_a"].to_evidence()
    b_ev = captured["node_b"].to_evidence()
    assert a_ev["strategy_id"] == "selected_v0" == b_ev["strategy_id"]
    a_paths = [f["path"] for f in a_ev["included_files"]]
    b_paths = [f["path"] for f in b_ev["included_files"]]
    assert "alpha/__init__.py" in a_paths and "alpha/__init__.py" not in b_paths
    assert "beta/__init__.py" in b_paths and "beta/__init__.py" not in a_paths
    assert a_ev["package_hash"] != b_ev["package_hash"]
    # no hidden-test material in either context
    for ev in (a_ev, b_ev):
        assert "hidden" not in json.dumps(ev).lower()


def test_06b_adversarial_context_sentinels_and_downstream_handoffs(tmp_path: Path):
    """Exercise context compilation through ParallelDagExecutor, not directly."""
    fixture = tmp_path / "sentinel_fixture"
    for sub in ("alpha", "beta", "integration", "tests"):
        (fixture / sub).mkdir(parents=True, exist_ok=True)
    (fixture / "alpha" / "__init__.py").write_text(
        "A_PRIVATE_CONTEXT_SENTINEL\n", encoding="utf-8"
    )
    (fixture / "beta" / "__init__.py").write_text(
        "B_PRIVATE_CONTEXT_SENTINEL\n", encoding="utf-8"
    )
    (fixture / "integration" / "__init__.py").write_text("clean\n", encoding="utf-8")
    (fixture / "tests" / "test_hidden_secret.py").write_text(
        "HIDDEN_TEST_CONTEXT_SENTINEL\n", encoding="utf-8"
    )
    bundle = _bundle()
    captured: dict[str, object] = {}

    class _Capture(_PatchWorker):
        def run_node(self, *, handle, **kw):
            captured[handle.node_id] = handle
            if handle.node_id == "node_a":
                (handle.root / "alpha" / "A_RUNTIME_HISTORY_SENTINEL.txt").write_text(
                    "private runtime history", encoding="utf-8"
                )
            return super().run_node(handle=handle, **kw)

    workers = {
        "node_a": _Capture(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                           handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _Capture(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                           handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
        "node_c": _Capture(content="integration PASS\n"),
    }
    result = _executor(tmp_path, bundle, workers, fixture_root=fixture).run()
    assert result.final_states["node_c"] == NodeRunState.COMPLETED

    a_handle = captured["node_a"]
    b_handle = captured["node_b"]
    c_handle = captured["node_c"]
    a_text = a_handle.context_package.instructions + "\n" + a_handle.context_package.context_body
    b_text = b_handle.context_package.instructions + "\n" + b_handle.context_package.context_body
    c_text = c_handle.context_package.instructions + "\n" + c_handle.context_package.context_body
    assert "A_PRIVATE_CONTEXT_SENTINEL" in a_text
    assert "B_PRIVATE_CONTEXT_SENTINEL" not in a_text
    assert "B_PRIVATE_CONTEXT_SENTINEL" in b_text
    assert "A_PRIVATE_CONTEXT_SENTINEL" not in b_text
    assert "A_RUNTIME_HISTORY_SENTINEL" not in b_text
    assert "HIDDEN_TEST_CONTEXT_SENTINEL" not in a_text + b_text + c_text
    assert {h["handoff_id"] for h in c_handle.context_package.upstream_handoffs} == {
        "handoff_alpha", "handoff_beta"
    }
    assert "A_PRIVATE_CONTEXT_SENTINEL" not in c_text
    assert "B_PRIVATE_CONTEXT_SENTINEL" not in c_text

    # Inspect the complete governed capabilities passed to siblings. Neither
    # contains the other's private workspace identity or path.
    assert str(b_handle.root) not in repr(a_handle)
    assert b_handle.workspace_id not in repr(a_handle)
    assert str(a_handle.root) not in repr(b_handle)
    assert a_handle.workspace_id not in repr(b_handle)


# ---------------------------------------------------------------------------
# Handoff material boundary (typed)
# ---------------------------------------------------------------------------


def test_06b_handoff_manifests_validated_and_bound(tmp_path: Path):
    bundle = _bundle()
    worker_a = _PatchWorker(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                            handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)})
    worker_b = _PatchWorker(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                            handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)})
    workers = {"node_a": worker_a, "node_b": worker_b,
               "node_c": _PatchWorker(content="integration PASS\n")}
    ex = _executor(tmp_path, bundle, workers)
    result = ex.run()
    assert result.final_states["node_c"] == NodeRunState.COMPLETED
    assert set(result.handoff_manifests) == {"handoff_alpha", "handoff_beta"}
    manifest = result.handoff_manifests["handoff_alpha"]
    artifact = manifest.produced_artifacts[0]
    assert str(manifest.id) == "handoff_alpha"
    assert manifest.node_id == "node_a"
    assert str(manifest.producer_station_id) == str(bundle.builder.id)
    assert manifest.public_interfaces == ["alpha.impl.v1"]
    assert manifest.consumer_scope == str(bundle.integration.id)
    assert artifact.ref_id == "artifact::node_a::handoff_alpha"
    assert len(artifact.sha256 or "") == 64
    assert 0 < artifact.metadata["size_bytes"] < 1024 * 1024
    assert manifest.metadata["artifact_identity_sha256"] == artifact.sha256


def test_06b_wrong_producer_handoff_rejected(tmp_path: Path):
    """B attempts to deliver the handoff that only A may produce → fail closed."""
    bundle = _bundle()
    worker_a = _PatchWorker(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                            handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)})
    worker_b = _PatchWorker(content=_BETA_IMPL, handoff_ids=("handoff_alpha",),
                            handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)})
    workers = {"node_a": worker_a, "node_b": worker_b}
    ex = _executor(tmp_path, bundle, workers)
    with pytest.raises(Exception, match="handoff_alpha"):
        ex.run()


def test_06b_wrong_type_handoff_rejected(tmp_path: Path):
    """B delivers handoff_beta with a type contradicting the declaration → fail."""
    bundle = _bundle()
    worker_a = _PatchWorker(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                            handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)})
    worker_b = _PatchWorker(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                            handoff_types_by_id={"handoff_beta": ("incompatible.type.v1",)})
    workers = {"node_a": worker_a, "node_b": worker_b}
    ex = _executor(tmp_path, bundle, workers)
    with pytest.raises(Exception, match="contradict"):
        ex.run()


@pytest.mark.parametrize(
    ("case", "match"),
    [
        ("wrong_station", "producer_station_id mismatch"),
        ("wrong_hash", "artifact hash mismatch"),
        ("wrong_size", "artifact size mismatch"),
        ("outside_consumer_scope", "consumer_scope mismatch"),
        ("traversal", "FORBIDDEN_TRAVERSAL"),
        ("absolute_path", "ABSOLUTE_PATH_FORBIDDEN"),
        ("raw_reasoning", "forbidden handoff field"),
        ("hidden_test_material", "forbidden handoff material"),
        ("oversized_metadata", "metadata exceeds"),
    ],
)
def test_06b_adversarial_handoff_descriptor_rejected(
    tmp_path: Path, case: str, match: str,
):
    bundle = _bundle()
    overrides = {
        "wrong_station": {"producer_station_id": str(bundle.beta_station.id)},
        "wrong_hash": {"sha256": "0" * 64},
        "wrong_size": {"size_bytes": 0},
        "outside_consumer_scope": {"consumer_scope": str(bundle.beta_station.id)},
        "traversal": {"path": "../outside.py"},
        "absolute_path": {"path": "C:/outside.py"},
        "raw_reasoning": {"metadata": {"raw_reasoning": "private chain"}},
        "hidden_test_material": {"metadata": {"note": "hidden_test_content"}},
        "oversized_metadata": {"metadata": {"note": "x" * 5000}},
    }
    workers = {
        "node_a": _PatchWorker(
            content=_ALPHA_IMPL,
            handoff_ids=("handoff_alpha",),
            handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)},
            descriptor_overrides=overrides[case],
        ),
        "node_b": _PatchWorker(
            content=_BETA_IMPL,
            handoff_ids=("handoff_beta",),
            handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)},
        ),
    }
    with pytest.raises(Exception, match=match):
        _executor(tmp_path, bundle, workers).run()


def test_06b_duplicate_conflicting_handoff_id_rejected(tmp_path: Path):
    bundle = _bundle()
    workers = {
        "node_a": _PatchWorker(
            content=_ALPHA_IMPL,
            handoff_ids=("handoff_alpha", "handoff_alpha"),
            handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)},
        ),
        "node_b": _PatchWorker(
            content=_BETA_IMPL,
            handoff_ids=("handoff_beta",),
            handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)},
        ),
    }
    with pytest.raises(Exception, match="duplicate conflicting handoff id"):
        _executor(tmp_path, bundle, workers).run()


def test_06b_consumer_outside_persisted_scope_rejected_before_worker_launch(tmp_path: Path):
    bundle = _bundle()
    consumer = _PatchWorker(content="integration PASS\n")
    workers = {
        "node_a": _PatchWorker(
            content=_ALPHA_IMPL,
            handoff_ids=("handoff_alpha",),
            handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)},
        ),
        "node_b": _PatchWorker(
            content=_BETA_IMPL,
            handoff_ids=("handoff_beta",),
            handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)},
        ),
        "node_c": consumer,
    }
    scopes = {
        "handoff_alpha": [str(bundle.beta_station.id)],
        "handoff_beta": [str(bundle.integration.id)],
    }
    with pytest.raises(Exception, match="outside consumer_scope"):
        _executor(
            tmp_path, bundle, workers,
            consumer_scope_by_handoff_id=scopes,
        ).run()
    assert consumer.calls == 0


# ---------------------------------------------------------------------------
# Duplicate / unknown results fail closed
# ---------------------------------------------------------------------------


def test_06b_duplicate_and_unknown_results_fail_closed(tmp_path: Path):
    bundle = _bundle()
    ex = _executor(tmp_path, bundle, {
        "node_a": _PatchWorker(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                               handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _PatchWorker(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                               handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
    })
    ex._states["node_a"] = NodeRunState.RUNNING
    ex._workspace_ids["node_a"] = "ws"
    good = WorkerExecutionResult(node_id="node_a", workspace_id="ws", success=True)
    ex._commit_result("node_a", good)  # first commit OK
    with pytest.raises(DuplicateNodeResultError):
        ex._commit_result("node_a", good)  # duplicate fails closed
    with pytest.raises(UnknownNodeResultError):
        ex._commit_result("node_ghost", good)  # unknown node fails closed
    # completed node can never become launchable again
    assert ex._states["node_a"] == NodeRunState.COMPLETED


# ---------------------------------------------------------------------------
# Orphaned RUNNING recovery (06B crash rule)
# ---------------------------------------------------------------------------


def test_06b_orphaned_running_becomes_failed_interrupted():
    recovered = recover_orphaned_running(
        {"node_x": NodeRunState.RUNNING.value, "node_y": NodeRunState.COMPLETED.value})
    assert recovered["node_x"] == {"state": "FAILED", "reason": "interrupted"}
    assert "node_y" not in recovered
    assert recover_orphaned_running(
        {"node_x": NodeRunState.RUNNING.value}) == recovered  # deterministic


# ---------------------------------------------------------------------------
# Evidence / replay of a parallel run
# ---------------------------------------------------------------------------


def test_06b_pair_replay_and_metrics(tmp_path: Path):
    barrier = threading.Barrier(2, timeout=10)

    class _BarrierA(_PatchWorker):
        def run_node(self, *, handle, **kw):
            barrier.wait(timeout=10)
            return super().run_node(handle=handle, **kw)

    class _BarrierB(_PatchWorker):
        def run_node(self, *, handle, **kw):
            barrier.wait(timeout=10)
            return super().run_node(handle=handle, **kw)

    bundle = _bundle()
    workers = {
        "node_a": _BarrierA(content=_ALPHA_IMPL, handoff_ids=("handoff_alpha",),
                            handoff_types_by_id={"handoff_alpha": ("alpha.impl.v1",)}),
        "node_b": _BarrierB(content=_BETA_IMPL, handoff_ids=("handoff_beta",),
                            handoff_types_by_id={"handoff_beta": ("beta.impl.v1",)}),
        "node_c": _PatchWorker(content="integration PASS\n", handoff_ids=()),
    }
    ledger_db = tmp_path / "pilot.db"
    conn = sqlite3.connect(ledger_db)
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)

    ex = _executor(tmp_path, bundle, workers, ledger=ledger)
    result = ex.run()
    conn.close()

    assert result.final_states["node_c"] == NodeRunState.COMPLETED
    t = result.timing
    # separate metrics; with real overlap aggregate compute exceeds wall-clock
    assert t["aggregate_node_compute_seconds"] > 0
    assert t["graph_wall_clock_seconds"] > 0

    conn = sqlite3.connect(ledger_db)
    try:
        stored = [json.loads(r[0]).get("metadata") for r in conn.execute(
            "SELECT payload_json FROM evidence_entries ORDER BY global_sequence")]
        stored = [m for m in stored if m and "row_kind" in m]
    finally:
        conn.close()

    db_before = ledger_db.read_bytes()
    calls_before = {node_id: worker.calls for node_id, worker in workers.items()}
    r1 = replay_dag_run(stored)
    r2 = replay_dag_run(stored)
    assert r1["graph_hash"] == r2["graph_hash"] == product_dag_hash(bundle.dag)
    assert r1["graph_revision"] == r2["graph_revision"] == 1
    assert r1["launch_order"] == r2["launch_order"] == ["node_a", "node_b", "node_c"]
    assert r1["completion_order"] == r2["completion_order"]
    assert r1["final_states"] == r2["final_states"]
    assert r1["available_handoff_types"] == r2["available_handoff_types"]
    assert r1["handoff_descriptors"] == r2["handoff_descriptors"]
    assert set(r1["handoff_descriptors"]) == {"handoff_alpha", "handoff_beta"}
    alpha = r1["handoff_descriptors"]["handoff_alpha"]
    assert alpha["producer_node_id"] == "node_a"
    assert alpha["producer_station_id"] == str(bundle.builder.id)
    assert alpha["artifact_id"] == "artifact::node_a::handoff_alpha"
    assert len(alpha["artifact_hash"]) == 64
    assert alpha["handoff_types"] == ["alpha.impl.v1"]
    assert alpha["consumer_scope"] == str(bundle.integration.id)
    assert r1["ready_decisions"] == r2["ready_decisions"]
    assert any(d["ready_nodes"] == ["node_a", "node_b"] for d in r1["ready_decisions"])
    assert ledger_db.read_bytes() == db_before  # replay performs zero writes
    assert {node_id: worker.calls for node_id, worker in workers.items()} == calls_before
