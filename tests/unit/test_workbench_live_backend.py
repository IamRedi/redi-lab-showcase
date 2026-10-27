"""REDI Workbench openai-live backend bridge tests — offline; fake provider only."""

import json
from pathlib import Path

import pytest

from redi_lab.workbench.adapter import WorkbenchAdapter
from redi_lab.workbench.live_backend import (
    FIRST_BUILD_BRIEF,
    LIVE_BACKEND_ID,
    check_live_gates_env,
    live_cost_estimate,
    resolve_scope,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("workbench live bridge tests must not touch network")

    import socket

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _fake_client_factory(calls: list):
    """Fake provider through the SAME live adapter boundary (no network)."""
    class _Usage:
        prompt_tokens = 321
        completion_tokens = 722
        prompt_tokens_details = None

        def __init__(self):
            from types import SimpleNamespace

            self.prompt_tokens_details = SimpleNamespace(cached_tokens=0)

    class _Choice:
        finish_reason = "stop"
        message = None

    class _Resp:
        usage = _Usage()

    class _Msg:
        content = json.dumps({"patches": [
            {"rel_path": "normalize.py", "action": "write", "content": CANON["normalize.py"]},
            {"rel_path": "reorder.py", "action": "write", "content": CANON["reorder.py"]},
            {"rel_path": "service.py", "action": "write", "content": CANON["service.py"]},
        ]})

    _Resp.choices = [type("C", (), {"finish_reason": "stop", "message": _Msg()})()]

    class _Client:
        chat = type("Chat", (), {})()

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _Resp()

    _Client.chat = type("Chat", (), {"completions": _Completions()})()
    return _Client()


CANON = {}


def _canon(product: dict) -> dict[str, str]:
    token = product["key"].split("_")[0]
    pack = product["case_pack"]
    unit = product["pack_unit"]
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
            "    shortfall = max(target + safety - on_hand, 0)\n"
            "    return ((shortfall + " + unit.upper() + "_PACK - 1) // " + unit.upper() + "_PACK) * " + unit.upper() + "_PACK\n"
        ),
        "service.py": (
            "from .normalize import normalize_" + token + "_code\n"
            "from .reorder import " + unit.upper() + "_PACK, " + token + "_reorder_quantity\n\n"
            "def build_" + product["package"] + "_replenishment(plant, code, on_hand, target, safety):\n"
            "    quantity = " + token + "_reorder_quantity(on_hand, target, safety)\n"
            "    return {'plant_code': normalize_" + token + "_code(plant, code), 'quantity': quantity, '"
            + unit + "': quantity // " + unit.upper() + "_PACK, 'safety_stock': safety}\n"
        ),
    }


@pytest.fixture(scope="module", autouse=True)
def _load_canon():
    from redi_lab.workbench.live_backend import governed_products

    products = governed_products()
    global CANON
    CANON = _canon(products[FIRST_BUILD_BRIEF["product_key"]])


def test_backend_registered_and_gate_env_order() -> None:
    from redi_lab.workbench.engine import BACKENDS

    assert BACKENDS[LIVE_BACKEND_ID]["active"] == "true"
    missing = check_live_gates_env(key=None, live_flag=None, ceiling=None)
    assert missing == ["REDI_OPENAI_API_KEY", "REDI_LIVE_PROVIDER=1", "owner cost ceiling"]


def test_scope_rejects_unqualified_prompts_before_any_call() -> None:
    from redi_lab.workbench.live_backend import governed_products

    products = governed_products()
    scope = resolve_scope("Build me a twitter clone", products)
    assert scope["supported"] is False
    assert "inventory/replenishment" in scope["reason"]
    scope2 = resolve_scope(FIRST_BUILD_BRIEF["prompt"], products)
    assert scope2["supported"] is True and scope2["product_key"] == "pharmacy_supply"


def test_missing_credential_fails_before_provider_call(tmp_path) -> None:
    def _boom():
        raise AssertionError("provider must not be called")

    from redi_lab.workbench.live_backend import OpenAILiveBackend, governed_products

    result = OpenAILiveBackend(client_factory=_boom).run_build(
        prompt=FIRST_BUILD_BRIEF["prompt"], key=None, live_flag=None, ceiling=None,
        output_root=tmp_path, products=governed_products(), ws_root=tmp_path)
    assert result["status"] == "BLOCKED" and len(result["missing"]) == 3


def test_offline_fake_provider_full_pipeline_to_app_ready(tmp_path) -> None:
    calls: list = []
    factory_calls: list = []

    def client_factory():
        factory_calls.append(1)
        return _fake_client_factory(calls)

    adapter = WorkbenchAdapter(active_backend="dry-fake")
    started = adapter.start_build(
        FIRST_BUILD_BRIEF["prompt"], backend="openai-live", ceiling="0.08",
        key="k" * 30, live_flag="1", client_factory=client_factory)
    build = adapter._builds[started["build_id"]]
    build._thread.join(timeout=30)
    snap = adapter.snapshot(started["build_id"])
    assert snap["status"] == "VERIFIED"
    assert snap["verdict"] == "APP READY"
    assert snap["active_worker"] == "gpt-5.6-terra"
    assert all(snap["gates"].values())
    assert snap["provider_calls"] == 1
    assert len(factory_calls) == 1  # exactly one governed call
    assert float(snap["cost_usd"]) > 0 and float(snap["cost_usd"]) < 0.03
    assert snap["pricing_evidence_state"] == "SNAPSHOT_PROVEN"


def test_cost_estimate_frozen() -> None:
    est = live_cost_estimate()
    assert est["hard_cost_bound_usd"] == "0.0286720"
    assert est["expected_cost_usd"] == "0.0102400"
    assert est["model"] == "gpt-5.6-terra"
