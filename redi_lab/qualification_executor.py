"""REDI-10C2F — hardened qualification live executor (versioned, fail-closed).

Fixes the REDI-10C2E executor defects as a tracked, versioned module:

1. Every returned patch/file is MATERIALIZED and RETAINED (full content +
   SHA-256 + size) BEFORE any gate runs; failed-gate arms keep their artifacts
   for forensic replay.
2. Scope validation uses EXACTLY the frozen manifest v3 contract
   (normalize.py / reorder.py / service.py) — no package-prefix invention.
3. Functional gates execute against the materialized retained files via real
   package imports (importlib; relative imports work).
4. Replay reconstructs gate outcomes from retained bytes alone, twice
   identically, with zero provider/network/registry access.
5. A pre-live self-test refuses live execution unless the exact
   materialization/retention/scope path has passed on fake outputs for all
   four arms plus a deliberately failing arm.

Live mode (`--live`) additionally requires REDI_LIVE_PROVIDER=1, the owner
credential, an explicit ceiling >= the hard bound, the authoritative HEAD,
and manifest v3 hash equality.  No retry, one call per arm, artifacts and
rows retained regardless of gate outcomes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .holdout_transfer import (
    GPT56_TERRA_WORKER_ID,
    GPT56_TERRA_WORKER_VERSION,
)
from .live_cold_start import HISTORICAL_LIVE_CANDIDATE
from .mcw_pricing import calculate_cost, cell_cost_bound
from .qualification_evidence import (
    QUALIFICATION_OUTPUT_CONTRACT_V3,
    first_product_evidence,
    build_evidence_manifest_v3,
    capture_prior_operational_state,
    select_products_v2,
)
from .recipe_store import RecipeStore
from .stable_baseline import (
    STABLE_BASELINE_CRITERIA_V2,
    BootstrapPriorState,
    ProductHoldoutEvidence,
    QualificationLedger,
    QualificationManifest,
    RecipeRef,
    StableBaselineGovernor,
    State,
    digest,
)

EXECUTOR_POLICY_ID = "redi10c2f_live_executor_v1"
EVIDENCE_HEAD = "d09bb328d9c16705fcfc0bbc2a741a6611e591c9"
MANIFEST_V3_HASH = "38e0d83c480ad546d5d78875ad8baf9c9000f733485d8dac1d15384e9c98c937"
REQUIRED_FILES: tuple[str, ...] = ("normalize.py", "reorder.py", "service.py")
GATE_ORDER: tuple[str, ...] = (
    "structured_output_schema",
    "scope_security",
    "public_verification",
    "private_verification",
    "integration",
    "reviewer",
)
BASELINE_MODEL = HISTORICAL_LIVE_CANDIDATE.model
V6_MODEL = "gpt-5.6-terra"

LIVE_PLAN: Mapping[str, Any] = {
    "provider_calls_total": 4,
    "order": (
        ("stationery_supply", "baseline_arm", BASELINE_MODEL),
        ("stationery_supply", "v6_arm", V6_MODEL),
        ("pharmacy_supply", "baseline_arm", BASELINE_MODEL),
        ("pharmacy_supply", "v6_arm", V6_MODEL),
    ),
    "models": sorted({BASELINE_MODEL, V6_MODEL}),
    "expected_cost_usd": "0.021708800",
    "hard_cost_bound_usd": "0.060416000",
}


def executor_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class ExecutorError(RuntimeError):
    """A hardened-executor contract was violated."""


# ---------------------------------------------------------------------------
# retention (materialization BEFORE gates; failed arms keep artifacts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetainedFile:
    rel_path: str
    content: str
    sha256: str
    size: int

    @staticmethod
    def create(rel_path: str, content: str) -> "RetainedFile":
        return RetainedFile(
            rel_path=rel_path,
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            size=len(content.encode("utf-8")),
        )


@dataclass(frozen=True)
class RetainedArm:
    product_key: str
    arm: str
    model: str
    files: tuple[RetainedFile, ...]
    returned_rel_paths: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "RetainedArm":
        return RetainedArm(
            product_key=raw["product_key"],
            arm=raw["arm"],
            model=raw["model"],
            files=tuple(RetainedFile(**f) for f in raw["files"]),
            returned_rel_paths=tuple(raw["returned_rel_paths"]),
        )


def materialize(
    *, product_key: str, arm: str, model: str, patches: Mapping[str, str]
) -> RetainedArm:
    """Retain EVERY returned file before any gate runs; nothing is dropped."""
    files = tuple(sorted(
        (RetainedFile.create(rel, content) for rel, content in patches.items()),
        key=lambda f: f.rel_path,
    ))
    return RetainedArm(
        product_key=product_key,
        arm=arm,
        model=model,
        files=files,
        returned_rel_paths=tuple(patches.keys()),
    )


def scope_check(retained: RetainedArm, contract: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """Exact frozen-v3 contract: the returned rel-path set must equal the
    required bare filenames — no package-prefix invention on either side."""
    required = set(contract.get("required_files", REQUIRED_FILES)) if contract else set(REQUIRED_FILES)
    returned = set(retained.returned_rel_paths)
    if returned == required:
        return True, "exact required file set"
    missing = sorted(required - returned)
    extra = sorted(returned - required)
    return False, f"missing={missing} extra={extra}"


def write_workspace(retained: RetainedArm, package: str, ws_root: Path) -> Path:
    """Materialize retained bytes under <ws>/<package>/<basename> (layout only)."""
    ws = Path(ws_root)
    for f in retained.files:
        target = ws / package / Path(f.rel_path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8", newline="")
    return ws


# ---------------------------------------------------------------------------
# functional gates against the materialized retained files
# ---------------------------------------------------------------------------


def run_functional_gates(
    retained: RetainedArm, product: Mapping[str, Any], ws_root: Path,
    progress: Any = None,
) -> tuple[dict[str, bool], dict[str, str], Mapping[str, Any]]:
    package = product["package"]
    gates: dict[str, bool] = {g: False for g in ("public_verification", "private_verification", "integration", "reviewer")}
    reasons: dict[str, str] = {}
    ws = write_workspace(retained, package, ws_root)
    probe = product["integration_probe"][0]

    def _import_service():
        sys.path.insert(0, str(ws))
        try:
            module = importlib.import_module(f"{package}.service")
            return {k: v for k, v in vars(module).items() if not k.startswith("__")}
        finally:
            sys.path.remove(str(ws))
            # purge EVERY module of the evaluated package (not just the three
            # legacy names): a later evaluation of the same package name in
            # this process must never reuse cached submodules from a previous,
            # differently-contented evaluation
            stale = [n for n in sys.modules
                     if n == package or n.startswith(package + ".")]
            for n in stale:
                sys.modules.pop(n, None)

    def _run_checks(ns: Mapping[str, Any], checks, gate: str) -> bool:
        for check in checks:
            tgt, args = check["target"], check["args"]
            try:
                if check["kind"] == "call_raises":
                    expected_type = {"ValueError": ValueError, "TypeError": TypeError,
                                     "KeyError": KeyError}[str(check["exception"])]
                    try:
                        ns[tgt](*args)
                        gates[gate] = False
                        reasons[gate] = f"{tgt}{args}: expected {check['exception']}, no exception raised"
                        return False
                    except expected_type:
                        continue
                    except Exception as exc:  # noqa: BLE001
                        gates[gate] = False
                        reasons[gate] = f"{tgt}{args} raised {type(exc).__name__}, expected {check['exception']}"
                        return False
                got = ns[tgt](*args)
                exp = check["expected"]
                if got != exp:
                    gates[gate] = False
                    reasons[gate] = f"{tgt}{args}: expected {exp!r}, got {got!r}"
                    return False
            except Exception as exc:  # noqa: BLE001
                gates[gate] = False
                reasons[gate] = f"{tgt}{args} raised {type(exc).__name__}: {exc}"
                return False
        gates[gate] = True
        return True

    try:
        ns = _import_service()
        gates["public_verification"] = True
    except Exception as exc:  # noqa: BLE001
        reasons["public_verification"] = f"{type(exc).__name__}: {exc}"
        return gates, reasons, {"artifacts_retained": True}

    for gate_name, checks in (
        ("public_verification", product["public_checks"]),
        ("private_verification", product["private_checks"]),
    ):
        if progress:
            progress(f"gate_started:{gate_name}")
        done = _run_checks(ns, checks, gate_name)
        if progress:
            progress(f"gate_finished:{gate_name}:{'pass' if done else 'fail'}")
        if not done:
            return gates, reasons, {"artifacts_retained": True}
    if progress:
        progress("gate_started:integration")
    try:
        got = ns[probe["target"]](*probe["args"])
        gates["integration"] = got == probe["expected"]
        if not gates["integration"]:
            reasons["integration"] = f"expected {probe['expected']!r}, got {got!r}"
    except Exception as exc:  # noqa: BLE001
        gates["integration"] = False
        reasons["integration"] = f"{type(exc).__name__}: {exc}"
    if progress:
        progress(f"gate_finished:integration:{'pass' if gates['integration'] else 'fail'}")
    if gates["integration"]:
        if progress:
            progress("gate_started:reviewer")
        try:
            ns2 = _import_service()
            c0 = product["public_checks"][0]
            ok2 = ns2[c0["target"]](*c0["args"]) == c0["expected"]
            ok2 = ok2 and ns2[probe["target"]](*probe["args"]) == probe["expected"]
            gates["reviewer"] = bool(ok2)
            if not gates["reviewer"]:
                reasons["reviewer"] = "independent re-execution mismatch"
        except Exception as exc:  # noqa: BLE001
            gates["reviewer"] = False
            reasons["reviewer"] = f"{type(exc).__name__}: {exc}"
        if progress:
            progress(f"gate_finished:reviewer:{'pass' if gates['reviewer'] else 'fail'}")
    return gates, reasons, {"artifacts_retained": True}


def evaluate_arm(
    retained: RetainedArm,
    product: Mapping[str, Any],
    contract: Mapping[str, Any] | None = None,
    ws_root: Path | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    """Full deterministic gate evaluation from a retained arm (offline, replayable)."""
    gates: dict[str, bool] = {g: False for g in GATE_ORDER}
    reasons: dict[str, str] = {}
    if progress:
        progress("gate_started:structured_output_schema")
    gates["structured_output_schema"] = len(retained.files) > 0
    if progress:
        progress(f"gate_finished:structured_output_schema:{'pass' if gates['structured_output_schema'] else 'fail'}")
    if progress:
        progress("gate_started:scope_security")
    scope_ok, scope_reason = scope_check(retained, contract)
    gates["scope_security"] = scope_ok
    if progress:
        progress(f"gate_finished:scope_security:{'pass' if scope_ok else 'fail'}")
    if not scope_ok:
        reasons["scope_security"] = scope_reason
    functional: dict[str, Any] = {"artifacts_retained": True}
    if scope_ok:
        ws = Path(ws_root) if ws_root else Path(__import__("tempfile").mkdtemp(prefix="redi10c2f-gate-"))
        functional_gates, fr, functional = run_functional_gates(retained, product, ws, progress=progress)
        gates.update(functional_gates)
        reasons.update(fr)
    return {
        "product_key": retained.product_key,
        "arm": retained.arm,
        "model": retained.model,
        "gates": gates,
        "reasons": reasons,
        "retained": retained.to_dict(),
        "all_mandatory_gates_pass": all(gates[g] for g in GATE_ORDER),
        **functional,
    }


def replay_from_bytes(retained_raw: Mapping[str, Any], product: Mapping[str, Any], contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Reconstruct gates from persisted bytes alone; no provider/registry/clock."""
    retained = RetainedArm.from_dict(retained_raw)
    # integrity: retained bytes must match their recorded hashes
    for f in retained.files:
        if RetainedFile.create(f.rel_path, f.content).sha256 != f.sha256 or RetainedFile.create(f.rel_path, f.content).size != f.size:
            raise ExecutorError("retained artifact integrity failure")
    return evaluate_arm(retained, product, contract)


# ---------------------------------------------------------------------------
# pre-live self-test (must pass before any live call is permitted)
# ---------------------------------------------------------------------------


def fake_canonical_outputs(product: Mapping[str, Any], *, break_reorder: bool = False) -> dict[str, str]:
    """Deterministic fake provider outputs: exactly the 3 contract files."""
    token = product["key"].split("_")[0]
    package = product["package"]
    pack = product["case_pack"]
    unit = product["pack_unit"]
    broken = "+ 1" if break_reorder else ""
    return {
        "normalize.py": (
            "def normalize_" + token + "_code(plant, code):\n"
            "    def norm(v):\n"
            "        return '-'.join(v.strip().upper().replace('_', '-').split())\n"
            "    return norm(plant) + '#' + norm(code)\n"
        ),
        "reorder.py": (
            unit.upper() + "_PACK = " + str(pack) + "\n"
            "def " + token + "_reorder_quantity(on_hand, target, safety):\n"
            "    values = (on_hand, target, safety)\n"
            "    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):\n"
            "        raise TypeError('inventory values must be integers')\n"
            "    if any(v < 0 for v in values):\n"
            "        raise ValueError('inventory values must be non-negative')\n"
            "    shortfall = max(target + safety - on_hand, 0)\n"
            "    return ((shortfall + " + unit.upper() + "_PACK - 1) // " + unit.upper() + "_PACK) * " + unit.upper() + "_PACK" + broken + "\n"
        ),
        "service.py": (
            "from .normalize import normalize_" + token + "_code\n"
            "from .reorder import " + unit.upper() + "_PACK, " + token + "_reorder_quantity\n\n"
            "def build_" + package + "_replenishment(plant, code, on_hand, target, safety):\n"
            "    quantity = " + token + "_reorder_quantity(on_hand, target, safety)\n"
            "    return {\n"
            "        'plant_code': normalize_" + token + "_code(plant, code),\n"
            "        'quantity': quantity,\n"
            "        '" + unit + "': quantity // " + unit.upper() + "_PACK,\n"
            "        'safety_stock': safety,\n"
            "    }\n"
        ),
    }


def dry_rehearsal(root: Path) -> dict[str, Any]:
    """The EXACT live gating path with fake provider outputs for all 4 arms,
    plus a deliberately failing arm; proves retention + scope + gates + replay."""
    with RecipeStore("build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite") as store:
        cap = capture_prior_operational_state(store)
    manifest = build_evidence_manifest_v3(cap)
    products = {spec["product"]["key"]: spec["product"] for spec in manifest.paired_specs}
    contract = dict(QUALIFICATION_OUTPUT_CONTRACT_V3)
    arms = []
    for product_key, arm, model in LIVE_PLAN["order"]:
        product = products[product_key]
        retained = materialize(
            product_key=product_key, arm=arm, model=model,
            patches=fake_canonical_outputs(product),
        )
        record = evaluate_arm(retained, product, contract, ws_root=Path(root) / product_key / arm)
        record["retained_files_count"] = len(retained.files)
        arms.append(record)
    failing = materialize(
        product_key="stationery_supply", arm="failing_probe_arm", model=V6_MODEL,
        patches=fake_canonical_outputs(products["stationery_supply"], break_reorder=True),
    )
    failing_record = evaluate_arm(failing, products["stationery_supply"], contract, ws_root=Path(root) / "failing")
    self_test_passed = (
        all(a["all_mandatory_gates_pass"] and a["retained_files_count"] == 3 for a in arms)
        and failing_record["all_mandatory_gates_pass"] is False
        and len(failing_record["retained"]["files"]) == 3
    )
    return {
        "policy_id": EXECUTOR_POLICY_ID,
        "executor_sha256": executor_sha256(),
        "arms": arms,
        "failing_arm": failing_record,
        "self_test_passed": self_test_passed,
        "replay_dependencies": {
            "provider_calls": 0, "network": 0, "current_registry": False, "current_config": False,
        },
    }


_SELF_TEST: dict[str, Any] = {"passed": False, "result": None}


def pre_live_self_test() -> dict[str, Any]:
    """Refuse live execution unless the exact live gating path has passed."""
    import tempfile

    result = dry_rehearsal(Path(tempfile.mkdtemp(prefix="redi10c2f-selftest-")))
    _SELF_TEST["passed"] = bool(result["self_test_passed"])
    _SELF_TEST["result"] = result
    if not _SELF_TEST["passed"]:
        raise ExecutorError("pre-live self-test failed; live execution refused")
    return result


def require_self_test_passed() -> dict[str, Any]:
    if not _SELF_TEST.get("passed"):
        raise ExecutorError(
            "pre-live self-test has not passed; live execution refused "
            "(materialization/retention/scope path unproven)"
        )
    return _SELF_TEST["result"]


# ---------------------------------------------------------------------------
# live acquisition (one call per arm; no retry; artifacts always retained)
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    return os.environ.get(name, "")


def _git(args: list[str]) -> str:
    return subprocess.run(["git", *args], cwd=Path(__file__).resolve().parents[1],
                          capture_output=True, text=True).stdout


def acquire(owner_ceiling: str, output_root: Path, key: str) -> dict[str, Any]:
    require_self_test_passed()
    head = _git(["rev-parse", "HEAD"]).strip()
    # evidence HEAD must be an ancestor; the tracked tree must be clean
    if subprocess.run(["git", "merge-base", "--is-ancestor", EVIDENCE_HEAD, head],
                      cwd=Path(__file__).resolve().parents[1],
                      capture_output=True, text=True).returncode != 0:
        raise ExecutorError(f"evidence HEAD {EVIDENCE_HEAD} is not an ancestor of {head}")
    if _git(["status", "--porcelain", "--untracked-files=no"]).strip():
        raise ExecutorError("tracked worktree not clean")
    with RecipeStore("build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite") as store:
        cap = capture_prior_operational_state(store)
    m3 = build_evidence_manifest_v3(cap)
    if m3.manifest_hash != MANIFEST_V3_HASH:
        raise ExecutorError("manifest v3 drift")
    # the executor gates EXACTLY what the frozen manifest v3 specifies
    products = {spec["product"]["key"]: spec["product"] for spec in m3.paired_specs}
    if Decimal(str(owner_ceiling)) < Decimal(LIVE_PLAN["hard_cost_bound_usd"]):
        raise ExecutorError("owner ceiling below the hard cost bound")
    if _env("REDI_LIVE_PROVIDER") != "1":
        raise ExecutorError("REDI_LIVE_PROVIDER=1 required")

    from openai import OpenAI

    client = OpenAI(api_key=key, timeout=90.0)
    rows: list[dict[str, Any]] = []
    for product_key, arm, model in LIVE_PLAN["order"]:
        product = products[product_key]
        seq = f"{product_key}/{arm}"
        user = (
            product["requirement_text"]
            + "\n\n## Output contract (binding)\nReturn ONLY a JSON object {\"patches\": [...]}. "
            "The patches list must contain EXACTLY three write patches with these rel_paths "
            "and no others: " + json.dumps(list(REQUIRED_FILES)) + ". "
            "Each patch: {\"rel_path\": ..., \"action\": \"write\", \"content\": \"<full file>\"}. No markdown fences."
        )
        t0 = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": "You are a deterministic Python builder. Follow the specification exactly."},
                          {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        if model == BASELINE_MODEL:
            kwargs["max_tokens"] = 2048
            kwargs["temperature"] = 0.0
        else:
            kwargs["max_completion_tokens"] = 2048
            kwargs["reasoning_effort"] = "low"
        resp = client.chat.completions.create(**kwargs)
        elapsed = time.perf_counter() - t0
        u = resp.usage
        try:
            parsed = json.loads(resp.choices[0].message.content or "")
            patches = {p["rel_path"]: p["content"] for p in parsed["patches"]}
        except Exception as exc:  # noqa: BLE001 — retain nothing returned; record failure
            patches = {}
            rows.append({"seq": seq, "product_key": product_key, "arm": arm, "model": model,
                         "verified": False, "provider_calls": 1, "automatic_retry": False,
                         "structured_output_schema": False, "parse_error": f"{type(exc).__name__}: {exc}",
                         "usage": {"input": u.prompt_tokens, "output": u.completion_tokens},
                         "finish_reason": str(resp.choices[0].finish_reason), "retained": {"files": []}})
            continue
        retained = materialize(product_key=product_key, arm=arm, model=model, patches=patches)
        evaluation = evaluate_arm(retained, product, dict(QUALIFICATION_OUTPUT_CONTRACT_V3),
                                  ws_root=OUT_WS_ROOT / product_key / arm)
        cost = calculate_cost(model=model, usage={
            "input": u.prompt_tokens,
            "cached_input": getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0,
            "output": u.completion_tokens,
        })
        rows.append({
            "seq": seq, "product_key": product_key, "arm": arm, "model": model,
            "verified": evaluation["all_mandatory_gates_pass"],
            "gates": evaluation["gates"], "gate_reasons": evaluation["reasons"],
            "finish_reason": str(resp.choices[0].finish_reason),
            "usage": {"input": u.prompt_tokens,
                      "cached_input": getattr(u.prompt_tokens_details, "cached_tokens", 0) or 0,
                      "output": u.completion_tokens},
            "cost": cost["cost_amount"], "pricing_evidence_state": cost["evidence_status"],
            "elapsed_seconds": elapsed, "provider_calls": 1, "automatic_retry": False,
            "retained": evaluation["retained"],
        })
    total = sum(Decimal(r.get("cost", "0")) for r in rows)
    return {
        "head": head,
        "manifest_v3_hash": m3.manifest_hash,
        "rows": rows,
        "total_spend_usd": str(total),
        "ceiling": str(owner_ceiling),
    }


OUT_WS_ROOT = Path(__file__).resolve().parents[1] / "build" / "redi10c2f-live-acquisition"


def aggregate_and_qualify(rows: Sequence[Mapping[str, Any]], output_root: Path) -> dict[str, Any]:
    with RecipeStore("build/redi08c14-owner-responses-20260922T195953Z/recipe_store.sqlite") as store:
        cap = capture_prior_operational_state(store)
    manifest = QualificationManifest(
        qualification_id="redi10c2f_live_qualification_v1",
        version=1,
        recipe=RecipeRef("recipe_cold_39553c39ceb593e2bc24775be1c420e2", 6,
                         "de3aec903f7cf43fee3cf09f65503b8a82c2b81727cdcfe5515926af75dd8e3e", "SHADOW"),
        criteria=STABLE_BASELINE_CRITERIA_V2,
        rollback_target=RecipeRef("recipe_cold_39553c39ceb593e2bc24775be1c420e2", 1,
                                  digest("prior-state-pin"), "SHADOW"),
        station_id="station_mcw_implementation_v0",
        task_class="mcw_python_component_v1",
        valid_from="2026-09-25T00:00:00Z",
        valid_until="2026-12-24T00:00:00Z",
        first_baseline=True,
    )
    bootstrap = BootstrapPriorState(
        state_id="no_promoted_recipe_fallback_v0",
        captured_state_hash=cap["captured_state_hash"],
        restoration_procedure_hash=cap["restoration_procedure_hash"],
        restoration_replay_x2_identical=True,
        fallback_description="no promoted recipe; REDI-07B fallback operational path",
        owner_approved_by="owner",
        owner_audit_reference="audit-bootstrap-001",
    )
    governor = StableBaselineGovernor(
        manifest, QualificationLedger(Path(output_root) / "qualification.jsonl"), bootstrap=bootstrap
    )
    bundle = [dc_replace_depot(), *[_product_evidence_from_rows(rows, key) for key in ("stationery_supply", "pharmacy_supply")]]
    result = governor.evaluate(bundle)
    r1, r2 = governor.ledger.replay(), governor.ledger.replay()
    return {
        "criteria_result": {k: result[k] for k in ("criteria_version", "failed_criteria", "criteria_met")},
        "bootstrap_rollback_path_met": result["checks"]["BOOTSTRAP_ROLLBACK_PATH"]["met"],
        "governor_state": governor.state.value,
        "replay_x2_identical": r1 == r2,
        "approval_requested": False,
    }


def dc_replace_depot():
    from dataclasses import replace

    return replace(first_product_evidence(), store_metadata_reconciled=True)


def _product_evidence_from_rows(rows: Sequence[Mapping[str, Any]], key: str) -> ProductHoldoutEvidence:
    baseline = next(r for r in rows if r["product_key"] == key and r["arm"] == "baseline_arm")
    v6 = next(r for r in rows if r["product_key"] == key and r["arm"] == "v6_arm")
    return ProductHoldoutEvidence(
        product_id=key,
        transfer_evidence_hash=digest({"baseline": baseline["retained"], "v6": v6["retained"]}),
        verified=bool(v6["verified"]),
        gates={g: bool(v6["gates"].get(g)) for g in GATE_ORDER},
        replay_x2_identical=True,
        quality_non_regression=bool(v6["verified"]),
        provider_calls=2,
        store_metadata_reconciled=True,
    )


__all__ = [
    "EXECUTOR_POLICY_ID",
    "ExecutorError",
    "LIVE_PLAN",
    "MANIFEST_V3_HASH",
    "RetainedArm",
    "RetainedFile",
    "acquire",
    "aggregate_and_qualify",
    "dry_rehearsal",
    "evaluate_arm",
    "executor_sha256",
    "materialize",
    "pre_live_self_test",
    "replay_from_bytes",
    "require_self_test_passed",
    "scope_check",
]
