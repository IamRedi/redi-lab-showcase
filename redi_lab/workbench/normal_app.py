"""REDI Workbench — EXPERIMENTAL normal-app path: Mini CRM / Job Manager.

Multi-station decomposition for the first normal-app family.  All VIRTUTES
personas get REAL work; events drive the dashboard; gates are deterministic;
fake providers are used for preparation/testing only (live provider calls
are a separate owner-authorized step).
"""

from __future__ import annotations

import json
import re
import threading
from decimal import Decimal
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..qualification_executor import GATE_ORDER, materialize
from .normal_app_contracts import CONTRACT_ID, INTERFACES, CALL_SHAPES, WEB_CONTRACT, handoff, validate_local_imports
from .runtime_identity import require_current_normal_app_runtime

BACKEND_ID = "openai-live-multistation-normal-app-experimental"
PACKAGE = "mini_crm"
EXPECTED_ACCEPTANCE = {
    "jobs_total": 2, "NEW": 0, "ACTIVE": 1, "DONE": 1, "clients_total": 1,
}

CRM_REQUIREMENT_TEXT = (
    "Build a Mini CRM / Job Manager as the Python package `mini_crm` with: "
    "Clients; Jobs linked to a client; Job status NEW / ACTIVE / DONE; "
    "Notes per job; Dashboard counts; SQLite persistence; and a local "
    "runnable UI. Standard library only."
)

# deterministic ownership/decomposition (artifact -> persona)
TASKS: tuple[dict[str, Any], ...] = (
    {"task_id": "build_database", "persona": "andinus", "station": "persistence.sqlite",
     "artifact": "database.py", "dependencies": [], "parallel_group": "foundation"},
    {"task_id": "build_dataflow", "persona": "bindus", "station": "dataflow.status",
     "artifact": "dataflow.py", "dependencies": [], "parallel_group": "foundation"},
    {"task_id": "build_core", "persona": "agron", "station": "backend.core",
     "artifact": "core.py", "dependencies": ["build_database", "build_dataflow"],
     "parallel_group": "backend"},
    {"task_id": "build_ui", "persona": "teuta", "station": "frontend.product_ui",
     "artifact": "ui.py", "dependencies": ["build_core"], "parallel_group": "frontend"},
    {"task_id": "integration_assembly", "persona": "gentius", "station": "integration.assembly",
     "artifact": "service.py", "dependencies": ["build_ui"], "parallel_group": "assembly"},
)

PUBLIC_CHECKS = (
    {"kind": "call_equals", "target": "run_acceptance", "args": [], "expected": EXPECTED_ACCEPTANCE},
)
PRIVATE_CHECKS = (
    {"kind": "call_equals", "target": "normalize_status", "args": [" active "], "expected": "ACTIVE"},
    {"kind": "call_raises", "target": "normalize_status", "args": ["bogus"], "exception": "ValueError"},
)
INTEGRATION_PROBE = (
    {"kind": "call_equals", "target": "integration_probe", "args": [],
     "expected": {"crm": "ready", "clients": 1, "jobs": 1}},
)

CRM_PRODUCT = {
    "package": PACKAGE,
    "requirement_text": CRM_REQUIREMENT_TEXT,
    "public_checks": PUBLIC_CHECKS,
    "private_checks": PRIVATE_CHECKS,
    "integration_probe": INTEGRATION_PROBE,
}

# gpt-5.6-terra is a reasoning model: max_completion_tokens INCLUDES the
# reasoning tokens. The former 2048 cap left no room for build_core's content
# after reasoning (wb-1ac58eb0bb0d: output=2048, finish_reason="length",
# empty content -> JSONDecodeError at char 0, run failed closed). Billed cost
# stays usage-based; only the worst-case ceiling projection rises, and the
# runner/estimate below use the same ceiling so the guard stays honest.
MAX_COMPLETION_TOKENS = 8192
MAX_INPUT_TOKENS = 45000


def resolve_scope(prompt: str) -> dict[str, Any]:
    low = (prompt or "").lower()
    if re.match(r"^(?:(?:build|create) (?:me )?(?:a )?)?(?:mini )?(?:crm\b|job manager\b)", low.strip()):
        return {"supported": True, "product_key": "mini_crm_job_manager"}
    return {"supported": False,
            "reason": "experimental normal-app path v0 supports only the Mini CRM / Job Manager brief"}


def build_execution_manifest() -> dict[str, Any]:
    return {
        "manifest_id": "multistation_mini_crm_v1",
        "backend_id": BACKEND_ID,
        "experimental": True,
        "product_key": "mini_crm_job_manager",
        "package": PACKAGE,
        "tasks": [dict(t) for t in TASKS],
        "interface_contract_id": CONTRACT_ID,
        "interfaces": INTERFACES,
        "call_shapes": CALL_SHAPES,
        "web_contract": WEB_CONTRACT,
        "gate_sequence": ["scope_security", "public_verification", "private_verification",
                          "integration", "reviewer"],
        "persona_gate_binding": {"scope_security": "monunius",
                                 "public_verification": "bato", "private_verification": "bato",
                                 "integration": "redon", "reviewer": "medaurus"},
        "call_budget": {"planned_provider_calls": len(TASKS), "per_task_max": 1,
                        "automatic_retry": False},
    }


class NormalAppMultiStationRunner:
    """Dependency-respecting multi-station execution for the Mini CRM path.

    Fake providers are injected per task (preparation/testing).  Lifecycle
    events (task_started/completed/failed, artifact_retained, gate_*) are
    recorded for VIRTUTES and replay.  Ownership is enforced per artifact.
    """

    def __init__(self, *, fake_providers: Mapping[str, Callable[[str], str]],
                 ws_root: Path, progress: Any = None,
                 real_call: Callable[[str, str], str] | None = None,
                 owner_ceiling: str | None = None) -> None:
        self.manifest = build_execution_manifest()
        self.fake_providers = dict(fake_providers)
        self.real_call = real_call
        self.owner_ceiling = owner_ceiling
        self.call_facts: dict[str, Any] = {"calls": 0, "per_task": {}}
        self.task_inputs: dict[str, Any] = {}
        self.task_outputs: dict[str, str] = {}
        self._external_progress = progress
        self.ws_root = Path(ws_root)
        self.events: list[dict[str, Any]] = []
        self.task_states = {t["task_id"]: "PENDING" for t in TASKS}
        self.artifacts: dict[str, str] = {}
        self.failures: dict[str, str] = {}
        self.gate_states = {gate: "NOT_RUN" for gate in GATE_ORDER}
        self.gate_reasons: dict[str, str] = {}
        self.integrity: dict[str, Any] = {}

    def progress(self, event: str) -> None:
        self._record_event(event)

    def _record_event(self, event: str, task_id: str | None = None, detail: str = "") -> None:
        parts = event.split(":")
        if parts[0] == "gate_started":
            self.gate_states[parts[1]] = "RUNNING"
        elif parts[0] == "gate_finished":
            self.gate_states[parts[1]] = "PASS" if parts[2] == "pass" else "FAIL"
        row = {"event": event, "ts": time.time()}
        if task_id:
            row["task_id"] = task_id
        if detail:
            row["detail"] = detail
        self.events.append(row)
        if self._external_progress:
            self._external_progress(event)

    def _run_task(self, task_id: str) -> None:
        task = next(t for t in TASKS if t["task_id"] == task_id)
        artifact = task["artifact"]
        self.task_states[task_id] = "RUNNING"
        self._record_event("task_started", task_id)
        self._record_event(f"component_task_started:{artifact.split('.')[0]}")
        try:
            prompt = task_prompt(task_id, self.artifacts)
            self.task_inputs[task_id] = {"prompt": prompt, "contract_id": CONTRACT_ID,
                                        "owned_artifact": artifact}
            if self.real_call is not None:
                if (self.owner_ceiling is None or not Decimal(self.owner_ceiling).is_finite()
                        or Decimal(self.owner_ceiling) < Decimal(live_cost_estimate()["hard_cost_bound_usd"])):
                    raise RuntimeError(f"owner budget ceiling exceeded before {task_id}")
                content = self.real_call(task_id, prompt)
            else:
                provider = self.fake_providers.get(task_id)
                if provider is None:
                    raise RuntimeError(f"no injected provider for {task_id}")
                content = provider(prompt)
            self.task_outputs[task_id] = content
            patch_list = json.loads(content)["patches"]
            if (not isinstance(patch_list, list) or len(patch_list) != 1
                    or patch_list[0].get("rel_path") != artifact
                    or patch_list[0].get("action") != "write"
                    or not isinstance(patch_list[0].get("content"), str)):
                raise RuntimeError(f"artifact ownership violation in {task_id}: expected one write to {artifact}")
            patches = {artifact: patch_list[0]["content"]}
            # Retain even a syntactically or contract-invalid owned artifact.
            self.artifacts[artifact] = patches[artifact]
            validate_local_imports(patches[artifact])
            from .product_integrity import check_product
            self._record_event("integrity_started", task_id)
            checked = upstream_artifacts(task_id, self.artifacts) | patches
            self.integrity[task_id] = check_product(checked, stage=task_id, root=self.ws_root.parent / "integrity")
            self._record_event("integrity_passed", task_id)
            retained = materialize(product_key=PACKAGE, arm=task_id, model="fake", patches=patches)
            self.task_states[task_id] = "COMPLETED"
            if self.real_call is not None:
                self._record_event(f"call_completed:{task_id}")
            self._record_event("task_completed", task_id, f"sha256={retained.files[0].sha256}")
            self._record_event(f"component_task_completed:{artifact.split('.')[0]}")
            self._record_event(f"artifact_retained:{artifact}")
        except Exception as exc:  # noqa: BLE001 — fail-closed, no retry
            self.task_states[task_id] = "FAILED"
            self.failures[task_id] = f"{type(exc).__name__}: {exc}"
            self.integrity[task_id] = {"status": "FAIL", "reason": self.failures[task_id]}
            self._record_event("task_failed", task_id, self.failures[task_id])
            self._record_event(f"component_task_failed:{artifact.split('.')[0]}")

    def execute(self) -> dict[str, Any]:
        self._record_event("run_started", detail=self.manifest["manifest_id"])
        self._record_event("planning_started")
        pending = {t["task_id"]: t for t in TASKS}
        self._record_event("scope_resolved", detail=CONTRACT_ID)
        while pending:
            ready = [t for t in pending.values()
                     if all(self.task_states[d] == "COMPLETED" for d in t["dependencies"])]
            if not ready:
                break  # dependency failure blocks downstream
            threads = [threading.Thread(target=self._run_task, args=(t["task_id"],))
                       for t in ready if t["parallel_group"] in ("foundation", "backend")]
            sequential = [t for t in ready if t["parallel_group"] not in ("foundation", "backend")]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            for t in sequential:
                self._run_task(t["task_id"])
            for t in ready:
                pending.pop(t["task_id"], None)
        if any(s == "FAILED" for s in self.task_states.values()):
            return self._final(verified=False,
                               detail="task failure blocked downstream stations")
        if self.task_states["integration_assembly"] != "COMPLETED":
            return self._final(verified=False, detail="assembly incomplete")
        return self._run_gates()

    def _run_gates(self) -> dict[str, Any]:
        contract = {"required_files": [t["artifact"] for t in TASKS],
                    "extra_files": "forbidden", "missing_files": "forbidden",
                    "action": "write"}
        retained = materialize(
            product_key=PACKAGE, arm="normal_app_gates", model="fake",
            patches=dict(self.artifacts))
        from .normal_app_gates import evaluate_normal_app

        try:
            require_current_normal_app_runtime()
            from .product_integrity import check_product
            self._record_event("product_integrity_started")
            try:
                self.integrity["product"] = check_product(dict(self.artifacts), stage="pre_gate_product", root=self.ws_root.parent / "integrity")
            except Exception as exc:
                self.integrity["product"] = {"status": "FAIL", "reason": f"{type(exc).__name__}: {exc}"}
                self._record_event("product_integrity_failed", detail=self.integrity["product"]["reason"])
                raise
            self._record_event("product_integrity_passed")
            evaluation = evaluate_normal_app(retained, dict(CRM_PRODUCT),
                                             contract, ws_root=self.ws_root, progress=self._record_event)
            require_current_normal_app_runtime()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            active = next((g for g, s in self.gate_states.items() if s == "RUNNING"), None)
            self.gate_reasons[active or "gate_executor"] = reason
            if active:
                self._record_event(f"gate_finished:{active}:fail")
            return self._final(verified=False, detail=reason)
        return self._final(verified=evaluation["all_mandatory_gates_pass"],
                           evaluation=evaluation)

    def _final(self, *, verified: bool, detail: str = "",
               evaluation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._record_event("run_finished", detail=detail or ("verified" if verified else "failed"))
        result = {
            "backend_id": BACKEND_ID, "experimental": True,
            "status": "VERIFIED" if verified else "FAILED", "verified": verified,
            "task_states": dict(self.task_states), "failures": dict(self.failures),
            "events": list(self.events), "manifest": self.manifest, "detail": detail,
            "project_path": str(self.ws_root.resolve()),
            "task_inputs": dict(self.task_inputs),
            "integrity": dict(self.integrity),
            "artifact_metadata": handoff(self.artifacts),
            "gate_states": dict(self.gate_states),
            "gate_reasons": dict(self.gate_reasons),
            "gates": {gate: state == "PASS" for gate, state in self.gate_states.items()},
        }
        if evaluation is not None:
            result["gates"] = evaluation["gates"]
            result["gate_reasons"] = evaluation["reasons"]
            result["gate_states"] = evaluation["gate_states"]
            result["retained"] = evaluation["retained"]
            result["files"] = sorted(f["rel_path"] for f in evaluation["retained"]["files"])
        if self.real_call is not None:
            result["call_facts"] = self.call_facts
        return result


TASK_SCOPE_LINES = {
    "build_database": ("andinus", "database.py",
        "Implement ONLY database.py using its shared interface contract. "
        "dashboard_counts returns exactly jobs_total, NEW, ACTIVE, DONE, clients_total."),
    "build_dataflow": ("bindus", "dataflow.py",
        "Implement ONLY dataflow.py using the shared status and transition contract."),
    "build_core": ("agron", "core.py",
        "Implement ONLY core.py using the shared CRMService contract. "
        "create_client and create_job return int ids; jobs returns dictionaries with notes."),
    "build_ui": ("teuta", "ui.py",
        "Implement ONLY ui.py using the exact shared dashboard and job dictionary keys."),
    "integration_assembly": ("gentius", "service.py",
        "Implement ONLY service.py using the shared assembly contract and ALL retained modules. "
        "No shape-guessing adapters. Check the actual upstream APIs against the shared contract; "
        "do not invent modules or silently substitute success values. create methods return int ids. "
        "run_acceptance() returns {'jobs_total': 2, 'NEW': 0, 'ACTIVE': 1, 'DONE': 1, 'clients_total': 1}. "
        "integration_probe() returns {'crm': 'ready', 'clients': 1, 'jobs': 1} from actual counts. "
        "Re-export normalize_status from .dataflow for private checks."),
}


def _retained_block(artifacts: Mapping[str, str], needed: Sequence[str]) -> str:
    blocks = []
    for name in needed:
        if name in artifacts:
            blocks.append(name + ":\n```python\n" + artifacts[name] + "```")
    return "\n".join(blocks)


def upstream_artifacts(task_id: str, artifacts: Mapping[str, str]) -> dict[str, str]:
    # Scheduler dependencies encode ordering, not the complete interface context.
    # Send every transitive ancestor to consumers, including Gentius.
    by_id = {t["task_id"]: t for t in TASKS}
    def ancestors(tid: str) -> set[str]:
        deps = by_id[tid]["dependencies"]
        return set(deps).union(*(ancestors(d) for d in deps))
    return {t["artifact"]: artifacts[t["artifact"]] for t in TASKS
                if t["task_id"] in ancestors(task_id) and t["artifact"] in artifacts}


def task_prompt(task_id: str, artifacts: Mapping[str, str]) -> str:
    persona, artifact, scope = TASK_SCOPE_LINES[task_id]
    upstream = upstream_artifacts(task_id, artifacts)
    retained = _retained_block(upstream, list(upstream))
    retained_block = ("\n## RETAINED BUILDER ARTIFACTS (already implemented)\n" + retained + "\n"
                      if retained else "")
    contracts = {"contract_id": CONTRACT_ID, "package": PACKAGE,
                 "allowed_modules": list(INTERFACES), "interfaces": INTERFACES,
                 "call_shapes": CALL_SHAPES, "web_contract": WEB_CONTRACT,
                 "acceptance": {"public_checks": PUBLIC_CHECKS, "private_checks": PRIVATE_CHECKS,
                                "integration_probe": INTEGRATION_PROBE},
                 "upstream_artifacts": handoff(upstream)}
    return (CRM_REQUIREMENT_TEXT + "\n## SHARED PRODUCER / CONSUMER CONTRACT (binding JSON)\n"
            + json.dumps(contracts, sort_keys=True) + "\n" + retained_block
            + "## TASK SCOPE (binding)\nYou are " + persona.capitalize() + ", " + scope + " "
            + "Implement every declared export and keyword name exactly. Deterministic integrity checks will "
            + "execute the declared call shapes, persistence, transitions, notes, escaped rendering and POST forms "
            + "BEFORE task completion. Keep the implementation compact, standard-library only. "
            + "Return ONLY one JSON patch: {\"patches\": [{\"rel_path\": \"" + artifact
            + "\", \"action\": \"write\", \"content\": \"...\"}]}. No other files.")


def _provider_failure_fact(task_id: str, exc: BaseException, *, key: str | None) -> dict[str, Any]:
    """Structured, key-free evidence for one failed provider call: exception
    class, provider status/code, sanitized message, task id/persona,
    timestamp, provider/model. The API key value is never included."""
    message = f"{type(exc).__name__}: {exc}"
    if key:
        message = message.replace(key, "***redacted***")
    message = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "***redacted***", message)
    status_code = getattr(exc, "status_code", None)
    code = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        code = err.get("code") if isinstance(err, dict) else body.get("code")
    persona = next((t["persona"] for t in TASKS if t["task_id"] == task_id), None)
    return {
        "task_id": task_id,
        "persona": persona,
        "exception_class": type(exc).__name__,
        "provider_status": status_code,
        "provider_code": code,
        "message": message[:500],
        "provider": "openai",
        "model": "gpt-5.6-terra",
        "ts": time.time(),
    }


def build_live_call(key: str, ceiling: str, *, client_factory: Callable[[], Any] | None = None
                    ) -> tuple[Callable[[str, str], str], dict[str, Any]]:
    """Governed live provider call for each Mini CRM task (v6 worker profile).

    One call per task; no automatic retry; per-task cost recorded; the
    fail-closed ceiling projection is enforced by the runner before each task.
    `client_factory` is the single offline-test seam (defaults to the real
    OpenAI client); production never passes it.
    """
    from ..mcw_pricing import calculate_cost

    require_current_normal_app_runtime()
    limit = Decimal(ceiling)
    if not limit.is_finite() or limit <= 0:
        raise ValueError("owner ceiling must be a positive finite amount")
    if client_factory is not None:
        client = client_factory()
    else:
        from openai import OpenAI

        client = OpenAI(api_key=key, timeout=120.0, max_retries=0)
    facts: dict[str, Any] = {"calls": 0, "per_task": {}, "failures": {}}
    lock = threading.Lock()
    spent = Decimal(0)
    reserved: dict[str, Decimal] = {}
    attempted: set[str] = set()
    system = "You are a deterministic Python builder. Follow the specification exactly."

    def live_call(task_id: str, prompt: str) -> str:
        nonlocal spent
        require_current_normal_app_runtime()
        # UTF-8 bytes conservatively bound content tokens, plus ample chat envelope.
        # Full transitive source is larger than the old fixed 2K-token assumption.
        input_bound = len((system + prompt).encode("utf-8")) + 1024
        if input_bound > MAX_INPUT_TOKENS:
            raise RuntimeError(f"input budget exceeded before {task_id}: {input_bound} > {MAX_INPUT_TOKENS}")
        bound = Decimal(calculate_cost(model="gpt-5.6-terra", usage={
            "input": input_bound, "cached_input": 0,
            "output": MAX_COMPLETION_TOKENS})["cost_amount"])
        with lock:
            if task_id not in {t["task_id"] for t in TASKS} or task_id in attempted:
                raise RuntimeError(f"unplanned or repeated provider call: {task_id}")
            if spent + sum(reserved.values(), Decimal(0)) + bound > limit:
                raise RuntimeError(f"owner budget ceiling exceeded before {task_id}")
            reserved[task_id] = bound
            attempted.add(task_id)
            facts["calls"] += 1
        try:
            resp = client.chat.completions.create(
                model="gpt-5.6-terra",
                messages=[
                    {"role": "system",
                     "content": system},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                max_completion_tokens=MAX_COMPLETION_TOKENS,
                reasoning_effort="low",
            )
        except Exception as exc:
            facts["failures"][task_id] = _provider_failure_fact(task_id, exc, key=key)
            raise
        u = resp.usage
        cost = calculate_cost(model="gpt-5.6-terra", usage={
            "input": u.prompt_tokens,
            "cached_input": getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0,
            "output": u.completion_tokens})
        facts["per_task"][task_id] = {
            "max_completion_tokens": MAX_COMPLETION_TOKENS,
            "provider": "openai", "model": "gpt-5.6-terra",
            "input": u.prompt_tokens,
            "cached_input": getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0,
            "output": u.completion_tokens,
            "cost_usd": cost["cost_amount"],
            "pricing_evidence_state": cost["evidence_status"],
            "finish_reason": str(resp.choices[0].finish_reason),
            "reserved_cost_usd": str(bound),
            "input_bound_tokens": input_bound,
        }
        with lock:
            amount = Decimal(cost["cost_amount"])
            if amount > bound:
                # Keep reservation and stop: reported usage violated the input/output envelope.
                raise RuntimeError("provider usage exceeded reserved bound")
            spent += amount
            reserved.pop(task_id)
        return resp.choices[0].message.content or ""

    return live_call, facts


def live_cost_estimate() -> dict[str, str]:
    from ..mcw_pricing import cell_cost_bound, calculate_cost

    bound = cell_cost_bound(model="gpt-5.6-terra", output_ceiling=MAX_COMPLETION_TOKENS)
    hard = str(Decimal(calculate_cost(model="gpt-5.6-terra", usage={
        "input": MAX_INPUT_TOKENS, "cached_input": 0,
        "output": MAX_COMPLETION_TOKENS})["cost_amount"]) * len(TASKS))
    expected = str(Decimal(bound["expected_estimate"]["cost_amount"]) * len(TASKS))
    return {"provider_calls": len(TASKS), "model": "gpt-5.6-terra",
            "hard_cost_bound_usd": hard, "expected_cost_usd": expected,
            "input_upper_tokens_per_call": MAX_INPUT_TOKENS,
            "expected_estimate_note": "legacy 2K-input reference only; actual cost depends on retained source size",
            "recommended_owner_ceiling_usd": "1.00"}


def canonical_fake_providers() -> dict[str, Callable[[str], str]]:
    """Injected controlled artifacts using the same contracts as real workers."""
    from .normal_app_fakes import providers
    return providers(TASKS)
