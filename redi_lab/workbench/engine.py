"""REDI Workbench engine — real REDI factory runs behind the UI.

No fake progress: every phase, task, retry, and verdict in the UI comes from
the real persisted REDI cold-start factory (`run_controlled_cold_start` +
`replay_cold_start` + the hash-chained evidence ledger).  The dry engine uses
the factory's own deterministic fake workers (zero provider/network).
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..cold_start import replay_cold_start, run_controlled_cold_start
from .crew import crew_snapshot
from .runtime_identity import (
    StaleRuntimeError, normal_app_runtime_status, require_current_normal_app_runtime,
)
from .build_budget import make_authorization, write_authorization, read_authorization, budget_telemetry

AGENT_LAYER_LABEL = "VIRTUTES"

WORKBENCH_ROOT = Path(__file__).resolve().parents[2] / "build" / "workbench-builds"

# Provider backends: only the dry factory backend is active in v0.  The
# registry keeps names stable so the UI never changes when live backends
# (OpenAI / Claude / Gemini / Ollama) are registered later.
GOVERNED_GATE_ORDER: tuple[str, ...] = (
    "structured_output_schema", "scope_security", "public_verification",
    "private_verification", "integration", "reviewer",
)

BACKENDS: dict[str, dict[str, str]] = {
    "openai-live-professional-product-v1": {"label": "Professional product factory v1 (local web products)", "active": "true"},
    "dry-fake": {"label": "Dry factory (fake workers, zero spend)", "active": "true"},
    "openai-live": {"label": "OpenAI live (governed; needs key + ceiling)", "active": "true"},
    "openai-live-multistation-experimental": {"label": "OpenAI live multi-station (EXPERIMENTAL)", "active": "true"},
    "openai-live-multistation-normal-app-experimental": {"label": "OpenAI live multi-station normal-app (EXPERIMENTAL: Mini CRM)", "active": "true"},
    "openai": {"label": "OpenAI workers (raw; superseded by openai-live)", "active": "false"},
    "claude": {"label": "Claude workers (not registered yet)", "active": "false"},
    "gemini": {"label": "Gemini workers (not registered yet)", "active": "false"},
    "ollama": {"label": "Local Ollama workers (not registered yet)", "active": "false"},
}


def _ledger_feed(output_root: Path) -> list[dict[str, Any]]:
    """Live activity feed from the real persisted evidence ledger (read-only)."""
    events: list[dict[str, Any]] = []
    for db in sorted(output_root.glob("*.sqlite")):
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=0.5)
            rows = conn.execute(
                "SELECT entity_type, COUNT(1) FROM evidence_entries GROUP BY entity_type"
            ).fetchall()
            conn.close()
        except Exception:  # noqa: BLE001 — ledger may be mid-write; skip this poll
            continue
        events = [{"entity_type": t, "count": c} for t, c in rows]
    return events


class WorkbenchBuild:
    """One owner prompt -> one real REDI factory run (background thread)."""

    def __init__(self, build_id: str, prompt: str, backend: str, *, ceiling: str | None = None, key: str | None = None, live_flag: str | None = None, client_factory: Callable[[], Any] | None = None) -> None:
        self.build_id = build_id
        self.prompt = prompt
        self.backend = backend
        self.ceiling = ceiling
        self.key = key
        self.live_flag = live_flag
        self.client_factory = client_factory
        if backend.startswith("openai-live"):
            authorization_path = WORKBENCH_ROOT / f"wb-{build_id}" / "build_authorization.json"
            if authorization_path.is_file():
                self.authorization = read_authorization(authorization_path.parent, build_id)
                self.ceiling = self.authorization["overall_ceiling_usd"]
            elif client_factory is None:
                raise ValueError("Paid Workbench build requires a persisted owner budget before execution")
        self.status = "RUNNING"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.error: str | None = None
        self.live: dict[str, Any] | None = None
        self.live_events: list[str] = []
        self._outcome: Any = None
        self._replay: Mapping[str, Any] | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # server-environment fallback for governed live backends (key never
        # comes from the UI; the UI supplies only the owner cost ceiling)
        if self.backend.startswith("openai-live"):
            if not self.key:
                self.key = os.environ.get("REDI_OPENAI_API_KEY") or None
            if not self.live_flag:
                self.live_flag = os.environ.get("REDI_LIVE_PROVIDER") or None
            # This immutable owner record was written before the worker thread
            # started. The provider never derives a budget from prompt or env.
        if self.backend == "openai-live-professional-product-v1":
            from .professional.provider import GovernedProvider
            from .professional.pipeline import ProfessionalRunner
            from .live_backend import check_live_gates_env
            try:
                require_current_normal_app_runtime()
                missing = check_live_gates_env(key=self.key, live_flag=self.live_flag, ceiling=self.ceiling)
                if missing:
                    self.live = {"status": "OWNER_ACTION_REQUIRED", "verified": False, "missing": missing,
                                 "recovery_version":"autonomous_recovery_v1",
                                 "owner_action":{"reason":"CREDENTIAL_REQUIRED" if not self.key else "MANUAL_APPROVAL_REQUIRED",
                                     "cause":"Professional build requires credential, live flag and owner ceiling",
                                     "decision_needed":"Configure the missing preflight requirements: "+", ".join(missing), "attempted":[],"stronger_profile_used":False},
                                 "detail": "Professional build requires credential, live flag and owner ceiling", "provider_calls": 0}
                else:
                    if not self.client_factory and (not self.authorization["visual_ceiling_usd"] or
                            os.environ.get('REDI_ZANA_VISUAL_AUTHORIZED') != '1'):
                        raise ValueError('Professional visual build requires an owner visual ceiling and REDI_ZANA_VISUAL_AUTHORIZED=1 before provider use')
                    provider = GovernedProvider(self.key, self.ceiling, self.client_factory)
                    self._professional_provider = provider
                    run_root = WORKBENCH_ROOT / f"wb-{self.build_id}"
                    visual_worker = visual_reviewer = None
                    if not self.client_factory:
                        from .professional.openai_visual import OpenAIVisualWorker, OpenAIVisualReviewer
                        visual_worker = OpenAIVisualWorker(provider, run_root,
                            visual_ceiling=self.authorization['visual_ceiling_usd'],
                            max_calls=self.authorization['max_visual_generation_calls'],
                            max_recovery_generations=self.authorization['max_visual_recovery_generations'])
                        visual_reviewer = OpenAIVisualReviewer(provider, visual_worker)
                    self._professional_visual_worker = visual_worker
                    runner = ProfessionalRunner(run_root, self.prompt, provider,
                        progress=self.live_events.append, ceiling=self.ceiling,
                        execution_mode="injected_offline" if self.client_factory else "real_provider",
                        visual_worker=visual_worker, visual_reviewer=visual_reviewer)
                    self._professional_runner = runner
                    self.live = runner.run()
                    self.component_map = self.live["component_map"]
                self.status = self.live["status"]
            except Exception as exc:
                self.error = self._sanitize_key_material(type(exc).__name__ + ": " + str(exc))
                provider = getattr(self, '_professional_provider', None)
                worker = getattr(self, '_professional_visual_worker', None)
                from decimal import Decimal
                known = (provider.spent + provider.visual_spent) if provider else Decimal(0)
                reserved = (sum(provider.reserved.values(), Decimal(0)) +
                            sum(provider.visual_reserved.values(), Decimal(0))) if provider else Decimal(0)
                facts = provider.facts if provider else {'calls': 0, 'per_task': {}, 'failures': {}}
                visual_calls = worker.calls if worker else 0
                self.live = {"status": "OWNER_ACTION_REQUIRED", "verified": False, "detail": self.error,
                             "provider_calls": facts['calls'], "total_provider_calls": facts['calls'] + visual_calls,
                             "call_facts": facts, "total_cost_usd": str(known),
                             "total_cost_upper_bound_usd": str(known + reserved),
                             "visual_facts": {'provider_calls': visual_calls,
                                              'cost_usd': str(provider.visual_spent) if provider else '0',
                                              'reserved_cost_usd': str(sum(provider.visual_reserved.values(), Decimal(0))) if provider else '0'},
                             "runtime_identity": normal_app_runtime_status(),
                             "recovery_version":"autonomous_recovery_v1",
                             "owner_action":{"reason":"MANUAL_APPROVAL_REQUIRED","cause":self.error,
                                 "decision_needed":"Resolve the runtime or provider configuration blocker before a new build.","attempted":[],"stronger_profile_used":False}}
                self.status = "OWNER_ACTION_REQUIRED"
            # A pre-specification failure must survive a Workbench restart.
            # Otherwise the owner cannot inspect the exact startup/preflight
            # source comparison that blocked this run.
            retained = WORKBENCH_ROOT / f"wb-{self.build_id}" / "live_run_evidence.json"
            if not retained.exists():
                try:
                    with retained.open("x", encoding="utf-8") as stream:
                        json.dump({"build_id": self.build_id, "backend": self.backend,
                                   "prompt": self._sanitize_key_material(self.prompt),
                                   "status": self.status, "verified": False,
                                   "detail": self.live.get("detail"), "owner_action": self.live.get("owner_action"),
                                   "runtime_identity": normal_app_runtime_status(),
                                   "cost_ceiling_usd": self.ceiling,
                                   "total_cost_usd": self.live.get('total_cost_usd', '0'),
                                   "total_cost_upper_bound_usd": self.live.get('total_cost_upper_bound_usd', '0'),
                                   "visual_facts": self.live.get('visual_facts'),
                                   "total_provider_calls": self.live.get('total_provider_calls', 0),
                                   "provider_calls": self.live.get('provider_calls', 0),
                                   "call_facts": self.live.get('call_facts', {"calls": 0, "per_task": {}, "failures": {}}),
                                   "execution_mode": "injected_offline" if self.client_factory else "real_provider",
                                   "elapsed_seconds": round(time.time() - self.started_at, 3)}, stream, sort_keys=True)
                except OSError as exc:
                    self.error = "preflight evidence persistence failed: " + self._sanitize_key_material(str(exc))
                    self.status = "FAILED"
            self.finished_at = time.time()
            return
        if self.backend == "openai-live-multistation-normal-app-experimental":
            from .normal_app import resolve_scope
            scope = resolve_scope(self.prompt)
            if not scope["supported"]:
                self.live = {"status": "BLOCKED", "verified": False, "provider_calls": 0,
                             "detail": scope["reason"] + "; select Professional product factory for other products"}
                self.status = "BLOCKED"
                self.finished_at = time.time()
                self._persist_live_run_evidence(None, None)
                return
            try:
                require_current_normal_app_runtime()
            except StaleRuntimeError as exc:
                self.live = {"status": "BLOCKED", "verified": False, "provider_calls": 0,
                             "detail": str(exc), "runtime_identity": normal_app_runtime_status()}
                self.finished_at = time.time()
                self._persist_live_run_evidence(None, None)
                self.status = self.live["status"]
                return
            from .live_backend import check_live_gates_env

            missing = check_live_gates_env(key=self.key, live_flag=self.live_flag,
                                           ceiling=self.ceiling)
            if missing:
                self.live = {"status": "BLOCKED", "missing": missing,
                             "detail": (
                                 "fail-before-provider-call: missing "
                                 + ", ".join(missing)
                                 + f" [answering server pid {os.getpid()}; in THIS process "
                                 + "REDI_OPENAI_API_KEY "
                                 + ("present" if os.environ.get("REDI_OPENAI_API_KEY") else "absent")
                                 + ", REDI_LIVE_PROVIDER "
                                 + ("=1" if os.environ.get("REDI_LIVE_PROVIDER") == "1" else "not '1'")
                                 + "]")}
                self.status = "BLOCKED"
                self.finished_at = time.time()
                return
            runner = None
            call_facts: Mapping[str, Any] | None = None
            try:
                from .normal_app import (NormalAppMultiStationRunner,
                                         build_live_call, canonical_fake_providers)

                live_requested = True
                if live_requested:
                    real_call, call_facts = build_live_call(
                        self.key, self.ceiling,
                        client_factory=getattr(self, 'client_factory', None))
                    runner = NormalAppMultiStationRunner(
                        fake_providers=canonical_fake_providers(),
                        ws_root=WORKBENCH_ROOT / f"wb-{self.build_id}" / "gates",
                        progress=self.live_events.append,
                        real_call=real_call, owner_ceiling=self.ceiling)
                else:
                    runner = NormalAppMultiStationRunner(
                        fake_providers=canonical_fake_providers(),
                        ws_root=WORKBENCH_ROOT / f"wb-{self.build_id}" / "gates",
                        progress=self.live_events.append)
                self.live = runner.execute()
                self.live["product_key"] = "mini_crm_job_manager"
                self.live["product_name"] = "Mini CRM"
                # andinus/gentius have dedicated crew_snapshot cards (retention
                # count / assembly events) — listing them here would duplicate
                # their cards in the grid
                self.component_map = {"agron": "core", "teuta": "ui",
                                      "bindus": "dataflow"}
                if live_requested:
                    self.live["model"] = "gpt-5.6-terra (governed live, v6 worker profile v3)"
                    self.live["provider_calls"] = call_facts["calls"]
                    self.live["call_facts"] = call_facts
                    from decimal import Decimal as _D

                    self.live["total_cost_usd"] = str(sum(
                        _D(f["cost_usd"]) for f in call_facts["per_task"].values()))
                    self.live["pricing_evidence_state"] = "SNAPSHOT_PROVEN"
                else:
                    self.live["model"] = ("fake (preparation/testing; pass key + ceiling "
                                          "for the governed live path)")
                    self.live["provider_calls"] = 0
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                self.status = "FAILED"
                self.finished_at = time.time()
                return
            finally:
                if runner is not None:
                    self._persist_live_run_evidence(runner, call_facts)
            if self.live.get("verified"):
                from .product_packaging import package_verified_build
                try:
                    self.live["packaging"] = package_verified_build(WORKBENCH_ROOT / f"wb-{self.build_id}")
                    self.live["project_path"] = self.live["packaging"]["project_path"]
                except Exception as exc:
                    self.error = "packaging: " + self._sanitize_key_material(str(exc))
                    self.live.update(status="FAILED", verified=False, detail=self.error)
            self.status = self.live["status"]
            self.finished_at = time.time()
            return
        if self.backend == "openai-live-multistation-experimental":
            try:
                from .live_backend import governed_products
                from .multistation import MultiStationRunner, canonical_fake_providers

                products = governed_products()
                scope_key = next((k for k, kws in
                                  {"stationery_supply": ("stationery",),
                                   "pharmacy_supply": ("pharmacy",)}.items()
                                  if any(w in self.prompt.lower() for w in kws)), None)
                if scope_key is None:
                    self.error = "unsupported product scope for the experimental multi-station path"
                    self.status = "FAILED"
                    self.finished_at = time.time()
                    return
                runner = MultiStationRunner(
                    product=products[scope_key] | {"key": scope_key},
                    fake_providers=canonical_fake_providers(products[scope_key] | {"key": scope_key}),
                    progress=self.live_events.append,
                    ws_root=WORKBENCH_ROOT / f"wb-{self.build_id}" / "gates")
                self.live = runner.execute()
                self.live["product_key"] = scope_key
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                self.status = "FAILED"
                self.finished_at = time.time()
                return
            self.status = self.live["status"]
            self.finished_at = time.time()
            return
        if self.backend == "openai-live":
            try:
                from .live_backend import OpenAILiveBackend, governed_products

                self.live = OpenAILiveBackend(client_factory=self.client_factory).run_build(
                    prompt=self.prompt, key=self.key, live_flag=self.live_flag,
                    ceiling=self.ceiling, output_root=WORKBENCH_ROOT / f"wb-{self.build_id}",
                    products=governed_products(), ws_root=WORKBENCH_ROOT / f"wb-{self.build_id}",
                    progress=self.live_events.append)
            except Exception as exc:  # noqa: BLE001
                self.error = f"{type(exc).__name__}: {exc}"
                self.status = "FAILED"
                self.finished_at = time.time()
                return
            self.status = self.live["status"]
            self.finished_at = time.time()
            return
        try:
            run_root = WORKBENCH_ROOT / f"wb-{self.build_id}"
            self._outcome = run_controlled_cold_start(output_root=run_root, known_recipe=None)
            self._replay = replay_cold_start(
                db_path=self._outcome.db_path, factory_run_id=self._outcome.factory_run_id
            )
        except Exception as exc:  # noqa: BLE001 — engine failures are shown honestly
            self.error = f"{type(exc).__name__}: {exc}"
            self.status = "FAILED"
            self.finished_at = time.time()
            return
        self.status = "VERIFIED" if self._outcome.verified else "FAILED"
        self.finished_at = time.time()

    def _sanitize_key_material(self, text: str) -> str:
        """Scrub the API key value (and any sk- token) out of evidence text."""
        if self.key:
            text = text.replace(self.key, "***redacted***")
        return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "***redacted***", text)

    def _persist_live_run_evidence(self, runner: Any, call_facts: Mapping[str, Any] | None) -> None:
        """Forward-only evidence for the governed live CRM path, written after
        EVERY run of that branch (success or failure) so provider exceptions
        survive the process. Never contains API key material."""
        try:
            root = WORKBENCH_ROOT / f"wb-{self.build_id}"
            root.mkdir(parents=True, exist_ok=True)
            payload = {
                "build_id": self.build_id,
                "prompt": self._sanitize_key_material(self.prompt),
                "product_name": (self.live or {}).get("product_name"),
                "backend": self.backend,
                "provider": "openai",
                "model": "gpt-5.6-terra",
                "execution_mode": "injected_offline" if self.client_factory is not None else "real_provider",
                "written_at": time.time(),
                "evidence_schema_version": 2,
                "status": (self.live or {}).get("status", "FAILED"),
                "error": self.error,
                "cost_ceiling_usd": self.ceiling,
                "total_cost_usd": (self.live or {}).get("total_cost_usd"),
                "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at, 3),
                "detail": (self.live or {}).get("detail", ""),
                "runtime_identity": normal_app_runtime_status(),
                "manifest": getattr(runner, "manifest", {}),
                "task_inputs": getattr(runner, "task_inputs", {}),
                "task_outputs": getattr(runner, "task_outputs", {}),
                "source_hashes": normal_app_runtime_status()["loaded_source_hashes"],
                "artifacts": [
                    {"rel_path": name,
                     "content": self._sanitize_key_material(source),
                     "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                     "stored_sha256": hashlib.sha256(self._sanitize_key_material(source).encode("utf-8")).hexdigest()}
                    for name, source in sorted(getattr(runner, "artifacts", {}).items())],
                "artifact_metadata": (self.live or {}).get("artifact_metadata", []),
                "integrity": getattr(runner, "integrity", {}),
                "task_states": dict(getattr(runner, "task_states", {})),
                "failures": dict(getattr(runner, "failures", {})),
                "events": list(getattr(runner, "events", [])),
                "gates": (self.live or {}).get("gates", {}),
                "gate_reasons": (self.live or {}).get("gate_reasons", {}),
                "gate_states": (self.live or {}).get("gate_states", {
                    gate: "NOT_RUN" for gate in GOVERNED_GATE_ORDER}),
                "call_facts": {
                    "calls": (call_facts or {}).get("calls", 0),
                    "per_task": (call_facts or {}).get("per_task", {}),
                    "failures": (call_facts or {}).get("failures", {}),
                },
            }
            text = self._sanitize_key_material(json.dumps(payload, indent=2))
            # Unique build IDs; refuse to overwrite any historical evidence.
            with (root / "live_run_evidence.json").open("x", encoding="utf-8") as stream:
                stream.write(text)
        except Exception as exc:  # fail closed: no success without durable evidence
            self.error = "evidence persistence failed: " + self._sanitize_key_material(str(exc))
            if self.live is not None:
                self.live.update(status="FAILED", verified=False, evidence_error=self.error)


    def snapshot(self) -> dict[str, Any]:
        """UI snapshot built ONLY from real engine state (no fabricated progress)."""
        snap: dict[str, Any] = {
            "build_id": self.build_id,
            "prompt": self.prompt,
            "backend": self.backend,
            "cost_ceiling_usd": self.ceiling,
            "backends": BACKENDS,
            "status": self.status,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at, 3),
            "verdict": None,
            "phases": [],
            "tasks": [],
            "worker_call_counts": {},
            "provider_calls": 0,
            "network_calls": 0,
            "tests": {"failed_attempts": None, "failure_categories": []},
            "project_path": None,
            "files": [],
            "error": self.error,
        }
        if hasattr(self, "authorization"):
            snap["build_authorization"] = self.authorization
            provider = getattr(self, "_professional_provider", None)
            if provider is not None:
                snap["budget_telemetry"] = budget_telemetry(self.authorization,
                    total_spent=provider.spent + provider.visual_spent,
                    visual_spent=provider.visual_spent,
                    total_reserved=sum(provider.reserved.values()) + sum(provider.visual_reserved.values()),
                    visual_reserved=sum(provider.visual_reserved.values()))
            else:
                snap["budget_telemetry"] = budget_telemetry(self.authorization)
        current_live = self.live or (self._professional_runner.snapshot() if hasattr(self,"_professional_runner") else None)
        if current_live is not None:
            snap["live_events"] = list(self.live_events)
            if getattr(self, "component_map", None):
                snap["component_map"] = self.component_map
            live = current_live
            snap["component_map"] = live.get("component_map",snap.get("component_map"))
            for key in ("recovery_version","recovery_episodes","recovery_profiles","owner_action","app_state",
                        "visual_facts","visual_assets","visual_worker_profile","visual_worker_profile_record","total_cost_upper_bound_usd","total_provider_calls","runtime_identity"):
                if key in live:snap[key]=live[key]
            snap["verdict"] = "APP READY" if live.get("verified") else ("APP FAILED" if live["status"] == "FAILED" else live["status"].replace("_", " "))
            snap["status"] = live["status"]
            if live["status"] == "RUNNING" and live.get("app_state"):snap["verdict"] = live["app_state"]
            snap["active_worker"] = live.get("model")
            snap["product_key"] = live.get("product_key")
            snap["gates"] = live.get("gates", {})
            snap["gate_reasons"] = live.get("gate_reasons", {})
            snap["gate_states"] = live.get("gate_states", {})
            snap["task_states"] = live.get("task_states", {})
            snap["integrity"] = live.get("integrity", {})
            snap["usage"] = live.get("usage", {})
            snap["cost_usd"] = live.get("total_cost_usd") or live.get("cost_usd", "0")
            if hasattr(self, "authorization") and not hasattr(self, "_professional_provider"):
                snap["budget_telemetry"] = budget_telemetry(self.authorization, total_spent=snap["cost_usd"])
            snap["pricing_evidence_state"] = live.get("pricing_evidence_state")
            if self.backend != "openai-live-multistation-normal-app-experimental":
                snap["elapsed_seconds"] = live.get("elapsed_seconds", 0)
            snap["provider_calls"] = live.get("provider_calls", 0)
            snap["failures"] = {k: self._sanitize_key_material(v)
                                for k, v in live.get("failures", {}).items()}
            snap["call_facts"] = live.get("call_facts")
            snap["finish_reason"] = live.get("finish_reason")
            snap["project_path"] = live.get("project_path")
            if self.backend == "openai-live-professional-product-v1":
                snap.update(product_name=live.get("product_name"), manifest=live.get("manifest"),
                            detail=live.get("detail"), product_review=live.get("review"), execution_mode=live.get("execution_mode"))
                snap["network_calls"] = live.get("provider_calls", 0) if live.get("execution_mode") == "real_provider" else 0
                snap["tasks"] = [{"node_id": t["task_id"], "station": t["persona"],
                                  "status": {"COMPLETED":"VERIFIED"}.get(live.get("task_states",{}).get(t["task_id"]), live.get("task_states",{}).get(t["task_id"],"PENDING"))}
                                 for t in (live.get("manifest") or {}).get("tasks",[])]
                if live.get("verified"):
                    snap = _delivery_snapshot(snap)
            if self.backend == "openai-live-multistation-normal-app-experimental":
                # Gate success is provisional until delivery and its smoke pass.
                if self.finished_at is None:
                    snap.update(status="RUNNING", verdict=None)
                elif live.get("verified"):
                    snap = _delivery_snapshot(snap)
            if live.get("status") == "BLOCKED":
                snap["missing"] = live.get("missing", [])
                snap["detail"] = live.get("detail") or live.get("reason")
            snap.update(crew_snapshot(snap))
            snap["virtutes"] = {"crew": snap.pop("crew"), "active_count": snap.pop("active_count"), "total": snap.pop("total")}
            snap["agent_layer_label"] = AGENT_LAYER_LABEL
            return snap
        if self._outcome is None:
            if self.status == "RUNNING":
                feed = _ledger_feed(WORKBENCH_ROOT / f"wb-{self.build_id}")
                if self.backend in ("openai-live-multistation-normal-app-experimental", "openai-live-professional-product-v1") \
                        and self.live_events:
                    # governed live CRM run IN FLIGHT: self.live is only set
                    # when execute() returns, but the real task events already
                    # stream into live_events — expose them so the VIRTUTES
                    # cards activate from actual progress instead of showing
                    # 0 / 11 WAITING while provider calls run.
                    snap["live_events"] = list(self.live_events)
                    snap["component_map"] = {"agron": "core", "teuta": "ui",
                                             "bindus": "workflow" if self.backend == "openai-live-professional-product-v1" else "dataflow"}
                    snap["active_worker"] = "gpt-5.6-terra (governed live, v6 worker profile v3)"
                    snap["product_key"] = "mini_crm_job_manager"
                    if self.backend == "openai-live-professional-product-v1":
                        manifest_path = WORKBENCH_ROOT / f"wb-{self.build_id}" / "execution_manifest.json"
                        if manifest_path.exists():
                            try:
                                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                                snap["manifest"] = manifest
                                snap["tasks"] = []
                                snap["task_states"] = {}
                                for task in manifest["tasks"]:
                                    fragment = ("visual:"+task["visual_requirement_id"] if task.get("persona")=="zana"
                                                else task["owned_paths"][0].removesuffix(".py"))
                                    state = "PENDING"
                                    for event in self.live_events:
                                        if event == "component_task_started:" + fragment: state = "RUNNING"
                                        if event == "component_task_verifying:" + fragment: state = "VERIFYING"
                                        if event == "component_task_completed:" + fragment: state = "VERIFIED"
                                        if event == "component_task_failed:" + fragment: state = "FAILED"
                                    snap["tasks"].append({"node_id":task["task_id"],"station":task["persona"],"status":state})
                                    snap["task_states"][task["task_id"]] = "COMPLETED" if state=="VERIFIED" else state
                            except (OSError, ValueError): pass
                        snap["product_key"] = "professional_product"
                snap["phases"] = (
                    [{"name": "orchestrator", "status": "RUNNING", "detail": "evidence ledger events persisted"}]
                    if feed
                    else [{"name": "orchestrator", "status": "RUNNING", "detail": "initializing factory run"}]
                )
                snap["ledger_feed"] = feed
            snap.update(crew_snapshot(snap))
            snap["virtutes"] = {"crew": snap.pop("crew"), "active_count": snap.pop("active_count"), "total": snap.pop("total")}
            snap["agent_layer_label"] = AGENT_LAYER_LABEL
            return snap
        o = self._outcome
        replay = self._replay or {}
        snap["verdict"] = "APP READY" if self.status == "VERIFIED" else "APP FAILED"
        snap["worker_call_counts"] = dict(o.worker_call_counts)
        snap["provider_calls"] = o.provider_calls
        snap["network_calls"] = o.network_calls
        snap["tests"] = {
            "failed_attempts": len(replay.get("failures", [])),
            "failure_categories": sorted({f["category"] for f in replay.get("failures", [])}),
            "final_verification_state": replay.get("final_verification_state"),
        }
        replay_gates: dict[str, bool] = {}
        for g in replay.get("gates") or []:
            identity, gate_status = g.get("identity"), g.get("status")
            if identity:
                replay_gates[identity] = gate_status == "pass"
        snap["replay_gates"] = replay_gates
        snap["project_path"] = str((WORKBENCH_ROOT / f"wb-{self.build_id}").resolve())
        snap["files"] = sorted(
            str(p.relative_to(WORKBENCH_ROOT / f"wb-{self.build_id}"))
            for p in (WORKBENCH_ROOT / f"wb-{self.build_id}").rglob("*")
            if p.is_file()
        )[:12]
        completion = list(o.execution.completion_order)
        launched = list(o.execution.launch_order)
        for node in o.plan.dag.nodes:
            node_id = str(node.node_id)
            if node_id in completion:
                status = "VERIFIED"
            elif node_id in launched:
                status = "FAILED"
            else:
                status = "PENDING"
            snap["tasks"].append({"node_id": node_id, "status": status, "station": str(node.station_id)})
        attempts = replay.get("attempt_trajectory", [])
        snap.update(crew_snapshot(snap))
        snap["virtutes"] = {"crew": snap.pop("crew"), "active_count": snap.pop("active_count"), "total": snap.pop("total")}
        snap["agent_layer_label"] = AGENT_LAYER_LABEL
        snap["phases"] = [
            {"name": "orchestrator+planning", "status": "VERIFIED",
             "detail": f"mode={o.selection.mode}, graph={o.execution.graph_revision}"},
            {"name": "builder tasks", "status": "VERIFIED" if self.status == "VERIFIED" else "FAILED",
             "detail": f"launch={launched}, completion={completion}"},
            {"name": "tester", "status": "VERIFIED" if self.status == "VERIFIED" else "FAILED",
             "detail": f"failed_attempts={snap['tests']['failed_attempts']}"},
            {"name": "reviewer/escalations", "status": "VERIFIED" if self.status == "VERIFIED" else "FAILED",
             "detail": f"retries={sum(1 for a in attempts if a.get('attempt_index', 0) > 1)}, categories={snap['tests']['failure_categories']}"},
        ]
        return snap


def _delivery_snapshot(snap):
    from .product_packaging import package_status, running_app
    try:
        root = Path(snap.get('continuation_root') or WORKBENCH_ROOT / ("wb-" + snap["build_id"]))
        packaging = package_status(root)
        current = running_app(root)
        snap.update(packaging=packaging, project_path=packaging["project_path"],
                    launch_command=packaging["launch_command"], can_open_app=True,
                    local_url=current["url"] if current else None)
    except Exception as exc:
        snap.update(status="FAILED", verdict="APP FAILED", can_open_app=False,
                    detail="Delivery is not ready: " + str(exc), project_path=None)
    return snap


class WorkbenchAdapter:
    """Clean API layer between any UI and the REDI engine (provider-agnostic)."""

    def __init__(self, active_backend: str = "dry-fake") -> None:
        if not BACKENDS.get(active_backend, {}).get("active") == "true":
            raise ValueError(f"backend {active_backend!r} is not active in v0")
        self.active_backend = active_backend
        self._builds: dict[str, WorkbenchBuild] = {}
        self._continuations: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def start_build(self, prompt: str, *, backend: str | None = None, ceiling: str | None = None,
                    visual_ceiling: str | None = None, max_visual_calls: int | str | None = None,
                    max_visual_recovery: int | str | None = None,
                    key: str | None = None, live_flag: str | None = None,
                    client_factory: Callable[[], Any] | None = None) -> dict[str, Any]:
        prompt = (prompt or "").strip()
        if not prompt:
            return {"error": "prompt is required"}
        chosen = backend or self.active_backend
        build_id = uuid.uuid4().hex[:12]
        if chosen.startswith("openai-live"):
            try:
                record = make_authorization(build_id, chosen, overall_ceiling=ceiling,
                    visual_ceiling=visual_ceiling, max_visual_calls=max_visual_calls,
                    max_visual_recovery=max_visual_recovery,
                    visual_required=chosen == 'openai-live-professional-product-v1' and client_factory is None,
                    runtime=normal_app_runtime_status(),
                    credential_present=bool(key or os.environ.get('REDI_OPENAI_API_KEY')),
                    live_arm=(live_flag or os.environ.get('REDI_LIVE_PROVIDER')) == '1',
                    visual_arm=os.environ.get('REDI_ZANA_VISUAL_AUTHORIZED') == '1')
                write_authorization(WORKBENCH_ROOT / f"wb-{build_id}", record)
            except (ValueError, OSError) as exc:
                return {"error": str(exc)}
        build = WorkbenchBuild(build_id, prompt, chosen, ceiling=ceiling, key=key,
                               live_flag=live_flag, client_factory=client_factory)
        with self._lock:
            self._builds[build_id] = build
        return {"build_id": build_id}

    def snapshot(self, build_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{12}", build_id):
            return {"error": "invalid build id"}
        continuation = self._continuations.get(build_id)
        build = self._builds.get(build_id)
        root = WORKBENCH_ROOT / f'wb-{build_id}'
        if continuation and continuation['thread'].is_alive():
            base = self._persisted_professional_snapshot(build_id, continuation['checkpoint'] / 'live_run_evidence.json')
            base.update(status='RUNNING', verdict='VERIFYING', app_state='VERIFYING', owner_action=None,
                        continuation_segment_id=continuation['segment'].name, continuation_root=str(continuation['segment']),
                        elapsed_seconds=round(continuation['prior_elapsed'] + time.time() - continuation['started'], 3),
                        live_events=list(continuation['events']))
            runner = continuation.get('runner')
            if runner is not None:
                base = self._with_live_runner(base, runner.snapshot())
            return base
        from .professional.continuation import latest_index, segment_id
        index = latest_index(root)
        pending = root / 'segments' / segment_id(index + 1)
        checkpoint = root if index == 0 else root / 'segments' / segment_id(index)
        if (pending / 'resume_started.json').is_file():
            base = self._persisted_professional_snapshot(build_id, checkpoint / 'live_run_evidence.json')
            base.update(owner_action={'reason':'CONTINUATION_INTERRUPTED','cause':'The continuation started but has no terminal evidence.',
                        'decision_needed':'Inspect the immutable segment and resolve the interruption before another owner decision.'},
                        detail='Continuation interrupted without terminal evidence')
            return base
        if index:
            return self._persisted_professional_snapshot(build_id, checkpoint / 'live_run_evidence.json')
        if build is not None and build.backend == 'openai-live-professional-product-v1' and build.status == 'OWNER_ACTION_REQUIRED':
            retained = WORKBENCH_ROOT / f'wb-{build_id}' / 'live_run_evidence.json'
            if retained.is_file():
                return self._persisted_professional_snapshot(build_id, retained)
        if build is None:
            path = WORKBENCH_ROOT / f"wb-{build_id}" / "live_run_evidence.json"
            if not path.is_file(): return {"error": f"unknown build {build_id}"}
            return self._persisted_professional_snapshot(build_id, path)
        return build.snapshot()

    def _persisted_professional_snapshot(self, build_id, path):
        evidence = json.loads(path.read_text(encoding="utf-8"))
        snap = {"build_id": build_id, "backend": evidence["backend"], "status": evidence["status"],
                "verdict": "APP READY" if evidence["status"] == "VERIFIED" else "OWNER ACTION REQUIRED" if evidence["status"] == "OWNER_ACTION_REQUIRED" else "APP FAILED",
                "cost_ceiling_usd": evidence.get("cost_ceiling_usd"),
                "cost_usd": evidence.get("total_cost_usd"), "elapsed_seconds": evidence.get("elapsed_seconds", 0),
                "provider_calls": evidence.get("call_facts", {}).get("calls", 0),
                "call_facts": evidence.get("call_facts", {}),
                "tests": {"failed_attempts": None, "failure_categories": []}, "files": [],
                "live_events": [e["event"] if isinstance(e, dict) else e for e in evidence.get("events", [])],
                "gates": evidence.get("gates", {}),
                "gate_states": evidence.get("gate_states", {}), "task_states": evidence.get("task_states", {}),
                "component_map": {"agron": "core", "teuta": "ui", "bindus": "dataflow"}}
        snap.update(product_name=evidence.get("product_name"), manifest=evidence.get("manifest"),
                    prompt=evidence.get("prompt"),
                    detail=evidence.get("detail"), error=evidence.get("error"), product_review=evidence.get("review"), execution_mode=evidence.get("execution_mode"))
        for key in ("recovery_version","recovery_episodes","recovery_profiles","owner_action","app_state",
                    "visual_facts","visual_assets","visual_worker_profile","visual_worker_profile_record","total_cost_upper_bound_usd","total_provider_calls","runtime_identity"):
            if key in evidence:snap[key]=evidence[key]
        if path.parent != WORKBENCH_ROOT / f'wb-{build_id}' and snap['backend'] == 'openai-live-professional-product-v1':
            from .professional.continuation import visual_attachment_status
            attachment = visual_attachment_status(WORKBENCH_ROOT / f'wb-{build_id}', build_id, evidence)
            if attachment:
                snap['visual_authorization_attachment'] = attachment
                snap['owner_action'] = {**(snap.get('owner_action') or {}), 'reason': attachment,
                    'cause': ('The visual authorization exists, but this continuation did not reattach its worker and reviewer.'
                              if attachment == 'AUTHORIZATION_PRESENT_BUT_NOT_REATTACHED' else
                              'The retained run has no complete visual authorization.'),
                    'decision_needed': ('Inspect governed same-run recovery eligibility; no new provider grant is needed.'
                                        if attachment == 'AUTHORIZATION_PRESENT_BUT_NOT_REATTACHED' else
                                        'Record an explicit owner visual authorization before visual execution.')}
        auth_path = WORKBENCH_ROOT / f'wb-{build_id}' / 'build_authorization.json'
        if auth_path.is_file():
            record = read_authorization(auth_path.parent, build_id)
            spent = evidence.get('total_cost_usd') or '0'
            upper = evidence.get('total_cost_upper_bound_usd') or spent
            from decimal import Decimal
            reserved = max(Decimal('0'), Decimal(str(upper)) - Decimal(str(spent)))
            visual = evidence.get('visual_facts') or {}
            snap['build_authorization'] = record
            snap['budget_telemetry'] = budget_telemetry(record, total_spent=spent,
                visual_spent=visual.get('cost_usd') or '0', total_reserved=reserved,
                visual_reserved=visual.get('reserved_cost_usd') or '0')
        if snap['backend'] == 'openai-live-professional-product-v1' and (WORKBENCH_ROOT / f'wb-{build_id}' / 'owner_action.json').is_file():
            from .professional.continuation import decision_context
            root = WORKBENCH_ROOT / f'wb-{build_id}'
            snap['owner_decision'] = decision_context(root, build_id)
            if path.parent != root:
                snap['continuation_segment_id'] = path.parent.name
                snap['continuation_root'] = str(path.parent)
        if snap["backend"] == "openai-live-professional-product-v1":
            snap["component_map"]["bindus"] = "workflow"
            snap["network_calls"] = snap["provider_calls"] if evidence.get("execution_mode") == "real_provider" else 0
        if snap["backend"] in ("openai-live-multistation-normal-app-experimental", "openai-live-professional-product-v1") and snap["status"] == "VERIFIED":
            snap = _delivery_snapshot(snap)
        snap.update(crew_snapshot(snap))
        snap["virtutes"] = {"crew": snap.pop("crew"), "active_count": snap.pop("active_count"), "total": snap.pop("total")}
        return snap

    @staticmethod
    def _with_live_runner(snap, live):
        """Overlay a running continuation's real runner state; nothing is inferred."""
        states = live.get("task_states", {})
        snap.update(app_state=live.get("app_state"), verdict=live.get("app_state") or snap.get("verdict"),
                    task_states=states, manifest=live.get("manifest"),
                    product_name=live.get("product_name") or snap.get("product_name"),
                    gate_states=live.get("gate_states", {}), gates=live.get("gates", {}),
                    call_facts=live.get("call_facts"), provider_calls=live.get("provider_calls", 0),
                    total_provider_calls=live.get("total_provider_calls"),
                    cost_usd=live.get("total_cost_usd"), total_cost_upper_bound_usd=live.get("total_cost_upper_bound_usd"),
                    visual_facts=live.get("visual_facts"), recovery_episodes=live.get("recovery_episodes"),
                    tasks=[{"node_id": t["task_id"], "station": t["persona"],
                            "status": {"COMPLETED": "VERIFIED"}.get(states.get(t["task_id"]), states.get(t["task_id"], "PENDING"))}
                           for t in (live.get("manifest") or {}).get("tasks", [])])
        if snap.get("network_calls") is not None:
            snap["network_calls"] = snap["provider_calls"] if snap.get("execution_mode") == "real_provider" else 0
        snap.pop("virtutes", None)
        snap.update(crew_snapshot(snap))
        snap["virtutes"] = {"crew": snap.pop("crew"), "active_count": snap.pop("active_count"), "total": snap.pop("total")}
        return snap

    def submit_owner_decision(self, build_id, payload):
        from .professional.continuation import submit_decision
        with self._lock:
            record = submit_decision(WORKBENCH_ROOT / f'wb-{build_id}', build_id, payload)
        return {'decision': record, 'run': self.snapshot(build_id)}

    def resume_build(self, build_id, *, client_factory=None, key=None):
        from .professional.continuation import prepare_resume, execute_resume
        root = WORKBENCH_ROOT / f'wb-{build_id}'
        with self._lock:
            prepared = prepare_resume(root, build_id)
            decision = prepared[2]
            previous = decision['previous_segment_id']
            state = {'started':time.time(), 'prior_elapsed':prepared[0]['elapsed_seconds'], 'events':['continuation_started'],
                     'segment':prepared[3], 'checkpoint':root if previous == '0000' else root / 'segments' / previous}
            def work():
                try:
                    execute_resume(root, build_id, prepared, client_factory=client_factory,
                                   key=key or os.environ.get('REDI_OPENAI_API_KEY'), progress=state['events'].append,
                                   on_runner=lambda runner: state.__setitem__('runner', runner))
                except Exception as exc:
                    from .professional.pipeline import write_new
                    from .runtime_identity import normal_app_runtime_status
                    prior=prepared[0];segment=prepared[3]
                    reason=self._sanitize_resume_error(exc, key)
                    if not (segment/'live_run_evidence.json').exists():
                        write_new(segment/'live_run_evidence.json', {**prior, 'schema_version':'professional_continuation_segment_v1',
                            'status':'OWNER_ACTION_REQUIRED','verified':False,'app_state':'OWNER_ACTION_REQUIRED',
                            'cost_complete':False,'post_resume_provider_usage_unresolved':True,
                            'owner_action':{'reason':'CONTINUATION_FAILED','cause':reason,'decision_needed':'Inspect the continuation evidence and resolve this blocker.',
                                            'attempted':[],'stronger_profile_used':False,'evidence':['resume_started.json']},
                            'detail':reason,'failure_stage':'continuation','runtime_identity':normal_app_runtime_status(),
                            'elapsed_seconds':round(prior['elapsed_seconds']+time.time()-state['started'],3),
                            'events':[{'event':'continuation_started','ts':state['started']},{'event':'owner_action_required','ts':time.time()}],
                            'previous_segment_id':previous,'owner_decision_sha256':hashlib.sha256((segment/'owner_decision.json').read_bytes()).hexdigest()})
            thread=threading.Thread(target=work,daemon=True)
            state['thread']=thread;self._continuations[build_id]=state;thread.start()
        return self.snapshot(build_id)

    @staticmethod
    def _sanitize_resume_error(exc, key):
        message=type(exc).__name__+': '+str(exc)
        return message.replace(key,'[REDACTED]') if key else message

    def build_ids(self):
        persisted = {p.parent.name[3:] for p in WORKBENCH_ROOT.glob("wb-*/live_run_evidence.json")}
        return sorted(set(self._builds) | persisted)

    def app_action(self, build_id, action):
        if not re.fullmatch(r"[a-f0-9]{12}", build_id):
            raise ValueError("invalid build id")
        snapshot = self.snapshot(build_id)
        if not snapshot.get("can_open_app"):
            raise ValueError("APP READY delivery is required before launching")
        from .product_packaging import open_app, stop_app
        root = Path(snapshot.get('continuation_root') or WORKBENCH_ROOT / f"wb-{build_id}")
        return {"open": open_app, "stop": stop_app}[action](root)
