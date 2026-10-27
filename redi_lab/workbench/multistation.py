"""REDI Workbench — EXPERIMENTAL multi-station execution path.

Separate from the qualified openai-live single-call v6 path (which remains
untouched).  Deterministic DAG for the qualified inventory/replenishment
family:

    Zojz (run) -> Ora (plan/decompose)
    parallel builders:  Agron -> normalize.py | Bindus -> reorder.py
    then:               Gentius -> service.py (integration assembly)
    then frozen gates:  Monunius (scope) -> Bato (public/private)
                        -> Redon (integration probe) -> Medaurus (reviewer)
    final:              Zojz engine verdict (APP READY / APP FAILED)

Inject fake providers only in this task.  Every station emits persisted
lifecycle events (task_started / task_completed / task_failed) so VIRTUTES
shows real simultaneous activity.  Artifact ownership is enforced: Agron
owns normalize.py, Bindus owns reorder.py, Gentius owns service.py; no task
may emit another's file.  No automatic retries.  Zero provider/network here.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..mcw_pricing import cell_cost_bound
from ..qualification_evidence import QUALIFICATION_OUTPUT_CONTRACT_V3
from ..qualification_executor import evaluate_arm, materialize

BACKEND_ID = "openai-live-multistation-experimental"
V6_MODEL = "gpt-5.6-terra"
V6_WORKER_PROFILE = {
    "worker_id": "worker_openai_gpt56_terra_holdout_v0",
    "worker_profile_version": 3,
    "model": V6_MODEL,
    "api": "chat_completions",
    "max_completion_tokens": 2048,
    "reasoning_effort": "low",
}

TASK_OWNERSHIP: Mapping[str, str] = {
    "implement_normalize": "normalize.py",
    "implement_reorder": "reorder.py",
    "integration_assembly": "service.py",
}
TASK_PERSONA: Mapping[str, str] = {
    "implement_normalize": "agron",
    "implement_reorder": "bindus",
    "integration_assembly": "gentius",
}


def build_execution_manifest(product: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministic multi-station execution manifest for one product."""
    pack = product["case_pack"]
    unit = product["pack_unit"]
    token = product["key"].split("_")[0]
    terra_bound = Decimal(cell_cost_bound(model=V6_MODEL)["hard_upper_bound"]["cost_amount"])
    terra_expected = Decimal(cell_cost_bound(model=V6_MODEL)["expected_estimate"]["cost_amount"])
    tasks = [
        {
            "task_id": "implement_normalize",
            "persona": "agron", "station": "builder.normalize",
            "dependencies": [],
            "owned_artifact": "normalize.py",
            "allowed_paths": ["normalize.py"],
            "required_output": f"def normalize_{token}_code(plant, code) -> str",
            "worker_profile": dict(V6_WORKER_PROFILE),
            "parallel_group": "builders",
        },
        {
            "task_id": "implement_reorder",
            "persona": "bindus", "station": "builder.reorder",
            "dependencies": [],
            "owned_artifact": "reorder.py",
            "allowed_paths": ["reorder.py"],
            "required_output": f"def {token}_reorder_quantity(on_hand, target, safety) -> int",
            "worker_profile": dict(V6_WORKER_PROFILE),
            "parallel_group": "builders",
        },
        {
            "task_id": "integration_assembly",
            "persona": "gentius", "station": "builder.integration_assembly",
            "dependencies": ["implement_normalize", "implement_reorder"],
            "owned_artifact": "service.py",
            "allowed_paths": ["service.py"],
            "required_output": f"def build_{product['package']}_replenishment(plant, code, on_hand, target, safety) -> dict",
            "worker_profile": dict(V6_WORKER_PROFILE),
            "parallel_group": "assembly",
        },
    ]
    return {
        "manifest_id": f"multistation_{product['key']}_v1",
        "backend_id": BACKEND_ID,
        "experimental": True,
        "product_key": product["key"],
        "package": product["package"],
        "tasks": tasks,
        "gate_sequence": [
            "scope_security", "public_verification", "private_verification",
            "integration", "reviewer",
        ],
        "persona_gate_binding": {
            "scope_security": "monunius", "public_verification": "bato",
            "private_verification": "bato", "integration": "redon",
            "reviewer": "medaurus",
        },
        "worker_profile_shared": dict(V6_WORKER_PROFILE),
        "call_budget": {"planned_provider_calls": 3, "per_task_max": 1, "automatic_retry": False},
        "cost_ceiling_usd": str(terra_bound * 3),
        "expected_cost_usd": str(terra_expected * 3),
    }


class MultistationError(RuntimeError):
    pass


class MultiStationRunner:
    """Experimental governed multi-station execution with injected fake
    providers (per-task content callables).  Real parallel builders."""

    def __init__(
        self,
        *,
        product: Mapping[str, Any],
        fake_providers: Mapping[str, Callable[[str], str]],
        progress: Any = None,
        ws_root: Path,
    ) -> None:
        self.product = product
        self.manifest = build_execution_manifest(product)
        self.fake_providers = fake_providers
        self._external_progress = progress
        self.progress = self._record_event
        self.ws_root = Path(ws_root)
        self.events: list[dict[str, Any]] = []
        self.task_states: dict[str, str] = {t["task_id"]: "PENDING" for t in self.manifest["tasks"]}
        self.artifacts: dict[str, str] = {}
        self.failures: dict[str, str] = {}

    def _record_event(self, event: str, task_id: str | None = None, detail: str = "") -> None:
        row = {"event": event, "ts": time.time()}
        if task_id:
            row["task_id"] = task_id
        if detail:
            row["detail"] = detail
        self.events.append(row)
        if self._external_progress:
            self._external_progress(event)

    def _emit(self, event: str, task_id: str | None = None, detail: str = "") -> None:
        self._record_event(event, task_id, detail)

    def _run_task(self, task_id: str) -> None:
        ownership = TASK_OWNERSHIP[task_id]
        self.task_states[task_id] = "RUNNING"
        self._emit("task_started", task_id)
        self._emit(f"component_task_started:{ownership.split('.')[0]}")
        try:
            provider = self.fake_providers.get(task_id)
            if provider is None:
                raise MultistationError(f"no injected provider for task {task_id}")
            content = provider(self.product["requirement_text"])
            patches = {p["rel_path"]: p["content"] for p in json.loads(content)["patches"]}
            if set(patches) != {ownership}:
                raise MultistationError(
                    f"artifact ownership violation in {task_id}: emitted {sorted(patches)}, "
                    f"owned exactly [{ownership}]"
                )
            self.artifacts[ownership] = patches[ownership]
            retained = materialize(product_key=self.product["key"], arm=task_id,
                                   model=V6_MODEL, patches=patches)
            self.task_states[task_id] = "COMPLETED"
            self._emit("task_completed", task_id, f"sha256={retained.files[0].sha256}")
            self._emit(f"component_task_completed:{ownership.split('.')[0]}")
        except Exception as exc:  # noqa: BLE001 — fail-closed, no retry
            self.task_states[task_id] = "FAILED"
            self.failures[task_id] = f"{type(exc).__name__}: {exc}"
            self._emit("task_failed", task_id, self.failures[task_id])
            self._emit(f"component_task_failed:{ownership.split('.')[0]}")

    def execute(self) -> dict[str, Any]:
        """Deterministic dependency-respecting execution with real parallel builders."""
        self._emit("run_started", detail=self.manifest["manifest_id"])
        builders = [t for t in self.manifest["tasks"] if t["parallel_group"] == "builders"]
        threads = [threading.Thread(target=self._run_task, args=(t["task_id"],)) for t in builders]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if any(self.task_states[t["task_id"]] == "FAILED" for t in builders):
            self._emit("integration_blocked", detail="one or more builder tasks failed")
            return self._final(verified=False,
                               detail="builder failure blocked downstream integration")
        missing = [t["owned_artifact"] for t in builders if t["owned_artifact"] not in self.artifacts]
        if missing:
            raise MultistationError(f"missing builder artifacts: {missing}")
        self._run_task("integration_assembly")
        if self.task_states["integration_assembly"] != "COMPLETED":
            return self._final(verified=False, detail="integration assembly failed")
        return self._run_gates()

    def _run_gates(self) -> dict[str, Any]:
        package = self.manifest["package"]
        retained = materialize(
            product_key=self.product["key"], arm="multistation_gates", model=V6_MODEL,
            patches={name: self.artifacts[name]
                     for name in ("normalize.py", "reorder.py", "service.py")},
        )
        evaluation = evaluate_arm(
            retained, dict(self.product), dict(QUALIFICATION_OUTPUT_CONTRACT_V3),
            ws_root=self.ws_root, progress=self.progress)
        return self._final(verified=evaluation["all_mandatory_gates_pass"],
                           evaluation=evaluation)

    def _final(self, *, verified: bool, detail: str = "", evaluation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._emit("run_finished", detail=detail or ("verified" if verified else "failed"))
        result = {
            "backend_id": BACKEND_ID,
            "experimental": True,
            "status": "VERIFIED" if verified else "FAILED",
            "verified": verified,
            "task_states": dict(self.task_states),
            "failures": dict(self.failures),
            "events": list(self.events),
            "manifest": self.manifest,
            "detail": detail,
        }
        if evaluation is not None:
            result["gates"] = evaluation["gates"]
            result["gate_reasons"] = evaluation["reasons"]
            result["retained"] = evaluation["retained"]
        return result


def canonical_fake_providers(product: Mapping[str, Any]) -> dict[str, Callable[[str], str]]:
    """Deterministic injected fake providers, one per station-owned artifact."""
    pack = product["case_pack"]
    unit = product["pack_unit"]
    token = product["key"].split("_")[0]
    package = product["package"]

    def normalize(_req: str) -> str:
        return json.dumps({"patches": [{"rel_path": "normalize.py", "action": "write", "content":
            "def normalize_" + token + "_code(plant, code):\n"
            "    def norm(v):\n"
            "        return '-'.join(v.strip().upper().replace('_', '-').split())\n"
            "    return norm(plant) + '#' + norm(code)\n"}]})

    def reorder(_req: str) -> str:
        return json.dumps({"patches": [{"rel_path": "reorder.py", "action": "write", "content":
            unit.upper() + "_PACK = " + str(pack) + "\n"
            "def " + token + "_reorder_quantity(on_hand, target, safety):\n"
            "    values = (on_hand, target, safety)\n"
            "    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):\n"
            "        raise TypeError('inventory values must be integers')\n"
            "    if any(v < 0 for v in values):\n"
            "        raise ValueError('inventory values must be non-negative')\n"
            "    shortfall = max(target + safety - on_hand, 0)\n"
            "    return ((shortfall + " + unit.upper() + "_PACK - 1) // " + unit.upper() + "_PACK) * " + unit.upper() + "_PACK\n"}]})

    def service(_req: str) -> str:
        return json.dumps({"patches": [{"rel_path": "service.py", "action": "write", "content":
            "from .normalize import normalize_" + token + "_code\n"
            "from .reorder import " + unit.upper() + "_PACK, " + token + "_reorder_quantity\n\n"
            "def build_" + package + "_replenishment(plant, code, on_hand, target, safety):\n"
            "    quantity = " + token + "_reorder_quantity(on_hand, target, safety)\n"
            "    return {\n"
            "        'plant_code': normalize_" + token + "_code(plant, code),\n"
            "        'quantity': quantity,\n"
            "        '" + unit + "': quantity // " + unit.upper() + "_PACK,\n"
            "        'safety_stock': safety,\n"
            "    }\n"}]})

    return {"implement_normalize": normalize, "implement_reorder": reorder,
            "integration_assembly": service}
