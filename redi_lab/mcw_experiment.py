"""REDI-09A — Minimum Capable Worker (MCW) experiment foundation.

This is the LABORATORY ONLY: preregistration, paired task execution across
worker tiers, evidence accounting, failure taxonomy, descriptive pilot
confidence, and a factual frontier representation.  No live provider call is
made anywhere in this module's dry path, no winner is selected, no
"minimum capable" conclusion is available, and no model ranking exists.

Scientific framing (canonical REDI-09):

  1. collect paired tasks          -> predeclared task set, one stratum
  2. evaluate multiple worker tiers -> SMALL / MID / FRONTIER conditions
  3. compare verified success      -> deterministic gates only
  4. measure cost and latency      -> tokens/time; cost requires a versioned
                                      pricing schedule (never invented)
  5. estimate uncertainty          -> descriptive pilot counts only
  6. identify the cheapest worker satisfying a PREDECLARED quality criterion
                                   -> NOT DONE HERE (INSUFFICIENT_EVIDENCE)

Treatment variable is the worker configuration alone.  Every condition sees
byte-identical task semantics; conditions are isolated; no condition observes
another condition's result or failure hints.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .domain import (
    AcceptanceCriterion,
    Budget,
    ExperimentManifest,
    FailureCategory,
    FailureRecord,
    FactoryRun,
    FactoryRunId,
    GateResult,
    GateResultId,
    GateStatus,
    GateType,
    Mode,
    RunEvidence,
    RunStatus,
    Severity,
    StationId,
    TaskId,
    Timing,
    TokenUsage,
    WorkerId,
    domain_to_json,
)
from .persistence import EvidenceLedger, FactoryRunRepository, bootstrap_schema
from .persistence.integrity import payload_hash_from_json
from .persistence.repositories import ExperimentRepository
from .runtime.contracts import ParsedPatchObservation, PatchOutputContract
from .runtime.live_provider_adapter import parse_builder_output_object


MCW_EXPERIMENT_FAMILY = "MCW"
MCW_POLICY_ID = "redi09a_mcw_experiment_v0"
MCW_POLICY_VERSION = 1

# ---------------------------------------------------------------------------
# 3. First MCW stratum (versioned, narrower than "coding", broader than one
#    fixture; never stratified by model identity)
# ---------------------------------------------------------------------------

MCW_STRATUM_ID = "mcw_python_component_v1"
MCW_STRATUM_VERSION = 1
MCW_STATION_ID = "station_mcw_implementation_v0"
MCW_TASK_CLASS = "small_bounded_pure_python_component"

STRATUM_INCLUSION_RULES = (
    "single pure-Python module inside one new package (one write target)",
    "deterministic pure functions only: same inputs -> same outputs",
    "standard library only; no third-party imports",
    "declared interface signatures fixed by the task contract",
    "3..8 public deterministic checks and 3..8 private deterministic checks",
    "baseline stub raises NotImplementedError; tests exercise behavior only",
)
STRATUM_EXCLUSION_RULES = (
    "multi-module services or integration-heavy products",
    "file/network/clock/random/environment dependence in the target module",
    "tasks copied from REDI-07/REDI-08 live holdouts (depot_replenishment, "
    "bulk_inventory, clinic_replenishment, inventory_math)",
    "subjective or model-scored quality assessment",
    "any stratification by model identity",
)


@dataclass(frozen=True)
class MCWStratum:
    stratum_id: str
    version: int
    station_id: str
    task_class: str
    station_role: str
    inclusion_rules: tuple[str, ...]
    exclusion_rules: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stratum_id": self.stratum_id,
            "version": self.version,
            "station_id": self.station_id,
            "task_class": self.task_class,
            "station_role": self.station_role,
            "inclusion_rules": list(self.inclusion_rules),
            "exclusion_rules": list(self.exclusion_rules),
        }

    @property
    def canonical_hash(self) -> str:
        return _hash(self.to_dict())


def default_stratum() -> MCWStratum:
    return MCWStratum(
        stratum_id=MCW_STRATUM_ID,
        version=MCW_STRATUM_VERSION,
        station_id=MCW_STATION_ID,
        task_class=MCW_TASK_CLASS,
        station_role="implementation_builder",
        inclusion_rules=STRATUM_INCLUSION_RULES,
        exclusion_rules=STRATUM_EXCLUSION_RULES,
    )


# ---------------------------------------------------------------------------
# 4. Predeclared task set (5 tasks, one stratum, no holdout reuse)
# ---------------------------------------------------------------------------

_STUB_BODY = 'raise NotImplementedError("assigned station implements this component")\n'


def _signature_stub(*signatures: str) -> str:
    return "".join(f"{sig}\n    {_STUB_BODY}" for sig in signatures)


def _pkg_init(package: str) -> str:
    return f'"""{package} — MCW pilot component (baseline stub)."""\n'


@dataclass(frozen=True)
class MCWTask:
    key: str
    title: str
    package: str
    module_name: str
    requirement_text: str
    interface_markers: tuple[str, ...]
    baseline_files: Mapping[str, str]
    reference_implementation: str
    public_checks: tuple[Mapping[str, Any], ...]
    private_checks: tuple[Mapping[str, Any], ...]
    integration_probe: Mapping[str, Any]
    reviewer_checks: tuple[Mapping[str, Any], ...]

    @property
    def rel_path(self) -> str:
        return f"{self.package}/{self.module_name}.py"

    @property
    def contract(self) -> PatchOutputContract:
        return PatchOutputContract(
            policy_id="redi_builder_patch_contract_v1",
            policy_version=1,
            expected_edit_count=1,
            expected_action="write",
            expected_path=self.rel_path,
            content_required=True,
            extra_edits_forbidden=True,
            allowed_scope=(self.package,),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "package": self.package,
            "module_name": self.module_name,
            "requirement_text": self.requirement_text,
            "interface_markers": list(self.interface_markers),
            "baseline_files": dict(self.baseline_files),
            "public_checks": [dict(c) for c in self.public_checks],
            "private_checks": [dict(c) for c in self.private_checks],
            "integration_probe": dict(self.integration_probe),
            "reviewer_checks": [dict(c) for c in self.reviewer_checks],
            "expected_path": self.rel_path,
        }

    @property
    def canonical_hash(self) -> str:
        return _hash(self.to_dict())


def _task_line_wrap() -> MCWTask:
    package = "line_wrap"
    reference = (
        "def wrap_text(text, width):\n"
        '    """Greedy word wrap: lines of at most `width` chars."""\n'
        "    if width <= 0:\n"
        '        raise ValueError("width must be positive")\n'
        "    lines = []\n"
        '    current = ""\n'
        "    for word in text.split():\n"
        "        candidate = word if not current else current + ' ' + word\n"
        "        if len(candidate) <= width:\n"
        "            current = candidate\n"
        "        else:\n"
        "            if current:\n"
        "                lines.append(current)\n"
        "            if len(word) > width:\n"
        "                lines.append(word)\n"
        '                current = ""\n'
        "            else:\n"
        "                current = word\n"
        "    if current:\n"
        "        lines.append(current)\n"
        "    return lines\n"
    )
    return MCWTask(
        key="line_wrap",
        title="Greedy text wrapping utility",
        package=package,
        module_name="wrapper",
        requirement_text=(
            "Implement wrap_text(text, width) -> list[str]. Greedy word wrap: "
            "split on whitespace, pack words onto lines of at most `width` "
            "characters joined by single spaces; a word longer than `width` "
            "occupies its own line; `width <= 0` raises ValueError; empty "
            "text yields an empty list. Order of words is preserved."
        ),
        interface_markers=("def wrap_text(text, width):",),
        baseline_files={
            f"{package}/__init__.py": _pkg_init(package),
            f"{package}/wrapper.py": (
                "def wrap_text(text, width):\n"
                '    """Greedy word wrap: lines of at most `width` chars."""\n'
                + _STUB_BODY
            ),
        },
        reference_implementation=reference,
        public_checks=(
            {"kind": "call_equals", "target": "wrap_text", "args": ["a b c", 5], "expected": ["a b c"]},
            {"kind": "call_equals", "target": "wrap_text", "args": ["alpha beta gamma", 5], "expected": ["alpha", "beta", "gamma"]},
            {"kind": "call_equals", "target": "wrap_text", "args": ["hello world", 5], "expected": ["hello", "world"]},
            {"kind": "call_equals", "target": "wrap_text", "args": ["one two three four", 8], "expected": ["one two", "three", "four"]},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "wrap_text", "args": ["", 5], "expected": []},
            {"kind": "call_equals", "target": "wrap_text", "args": ["supercalifragilistic", 5], "expected": ["supercalifragilistic"]},
            {"kind": "call_equals", "target": "wrap_text", "args": ["a bb c", 2], "expected": ["a", "bb", "c"]},
            {"kind": "call_raises", "target": "wrap_text", "args": ["a", 0], "exception": "ValueError"},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "wrap_text", "args": ["a bb ccc d", 3], "expected": ["a", "bb", "ccc", "d"]}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "wrap_text", "args": ["aa bb aa bb", 5], "expected": ["aa bb", "aa bb"]},
            {"kind": "call_equals", "target": "wrap_text", "args": ["x  y", 3], "expected": ["x y"]},
        ),
    )


def _task_interval_merge() -> MCWTask:
    package = "interval_merge"
    reference = (
        "def merge_intervals(intervals):\n"
        '    """Merge overlapping or touching [start, end] intervals."""\n'
        "    if not intervals:\n"
        "        return []\n"
        "    ordered = sorted([list(i) for i in intervals])\n"
        "    merged = [list(ordered[0])]\n"
        "    for start, end in ordered[1:]:\n"
        "        last = merged[-1]\n"
        "        if start <= last[1]:\n"
        "            last[1] = max(last[1], end)\n"
        "        else:\n"
        "            merged.append([start, end])\n"
        "    return merged\n"
    )
    return MCWTask(
        key="interval_merge",
        title="Interval merging utility",
        package=package,
        module_name="merge",
        requirement_text=(
            "Implement merge_intervals(intervals) -> list[list[int]]. Input is "
            "a list of [start, end] integer pairs in any order. Merge pairs "
            "that overlap or touch (end == next start), return the merged "
            "intervals sorted by start; empty input returns []."
        ),
        interface_markers=("def merge_intervals(intervals):",),
        baseline_files={
            f"{package}/__init__.py": _pkg_init(package),
            f"{package}/merge.py": (
                "def merge_intervals(intervals):\n"
                '    """Merge overlapping or touching [start, end] intervals."""\n'
                + _STUB_BODY
            ),
        },
        reference_implementation=reference,
        public_checks=(
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[1, 3], [2, 6], [8, 10], [15, 18]]], "expected": [[1, 6], [8, 10], [15, 18]]},
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[1, 4], [4, 5]]], "expected": [[1, 5]]},
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[5, 7], [1, 3]]], "expected": [[1, 3], [5, 7]]},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "merge_intervals", "args": [[]], "expected": []},
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[1, 10], [2, 3]]], "expected": [[1, 10]]},
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[1, 4], [0, 4]]], "expected": [[0, 4]]},
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[1, 2], [3, 4]]], "expected": [[1, 2], [3, 4]]},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[3, 5], [1, 3], [5, 8], [10, 12]]], "expected": [[1, 8], [10, 12]]}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[2, 2], [2, 3]]], "expected": [[2, 3]]},
            {"kind": "call_equals", "target": "merge_intervals", "args": [[[-5, 0], [-7, -6]]], "expected": [[-7, -6], [-5, 0]]},
        ),
    )


def _task_roman_numerals() -> MCWTask:
    package = "roman_numerals"
    reference = (
        "_VALUES = [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'), (100, 'C'),\n"
        "           (90, 'XC'), (50, 'L'), (40, 'XL'), (10, 'X'), (9, 'IX'),\n"
        "           (5, 'V'), (4, 'IV'), (1, 'I')]\n"
        "_RANKS = {'I': 1, 'V': 5, 'X': 10, 'L': 50, 'C': 100, 'D': 500, 'M': 1000}\n"
        "\n"
        "def to_roman(n):\n"
        '    """Convert 1..3999 to canonical uppercase Roman numerals."""\n'
        "    if not isinstance(n, int) or isinstance(n, bool) or n < 1 or n > 3999:\n"
        '        raise ValueError("n must be an integer in 1..3999")\n'
        "    out = []\n"
        "    for value, symbol in _VALUES:\n"
        "        while n >= value:\n"
        "            out.append(symbol)\n"
        "            n -= value\n"
        "    return ''.join(out)\n"
        "\n"
        "def from_roman(s):\n"
        '    """Parse a canonical Roman numeral back to an integer."""\n'
        "    if not isinstance(s, str) or not s:\n"
        '        raise ValueError("s must be a non-empty string")\n'
        "    total = 0\n"
        "    for i, ch in enumerate(s):\n"
        "        if ch not in _RANKS:\n"
        '            raise ValueError("invalid roman symbol")\n'
        "        value = _RANKS[ch]\n"
        "        if i + 1 < len(s) and _RANKS[s[i + 1]] > value:\n"
        "            total -= value\n"
        "        else:\n"
        "            total += value\n"
        "    if total < 1 or total > 3999 or to_roman(total) != s:\n"
        '        raise ValueError("not a canonical roman numeral")\n'
        "    return total\n"
    )
    return MCWTask(
        key="roman_numerals",
        title="Roman numeral conversion",
        package=package,
        module_name="roman",
        requirement_text=(
            "Implement to_roman(n) and from_roman(s) for canonical uppercase "
            "Roman numerals in 1..3999. to_roman raises ValueError for "
            "non-integers or out-of-range values; from_roman raises ValueError "
            "for empty strings, unknown symbols, or non-canonical forms "
            "(e.g. 'IIII')."
        ),
        interface_markers=("def to_roman(n):", "def from_roman(s):"),
        baseline_files={
            f"{package}/__init__.py": _pkg_init(package),
            f"{package}/roman.py": (
                "def to_roman(n):\n"
                + _STUB_BODY
                + "\n"
                + "def from_roman(s):\n"
                + _STUB_BODY
            ),
        },
        reference_implementation=reference,
        public_checks=(
            {"kind": "call_equals", "target": "to_roman", "args": [9], "expected": "IX"},
            {"kind": "call_equals", "target": "to_roman", "args": [2024], "expected": "MMXXIV"},
            {"kind": "call_equals", "target": "from_roman", "args": ["XIV"], "expected": 14},
            {"kind": "call_equals", "target": "to_roman", "args": [1], "expected": "I"},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "to_roman", "args": [3999], "expected": "MMMCMXCIX"},
            {"kind": "call_equals", "target": "to_roman", "args": [40], "expected": "XL"},
            {"kind": "call_equals", "target": "from_roman", "args": ["MMMCMXCIX"], "expected": 3999},
            {"kind": "call_raises", "target": "to_roman", "args": [0], "exception": "ValueError"},
            {"kind": "call_raises", "target": "from_roman", "args": ["IIII"], "exception": "ValueError"},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "from_roman", "args": [to_roman_probe(3888)], "expected": 3888}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "from_roman", "args": ["CDXLIV"], "expected": 444},
            {"kind": "call_equals", "target": "to_roman", "args": [444], "expected": "CDXLIV"},
        ),
    )


def to_roman_probe(n: int) -> str:
    """Deterministic probe input for the roman integration gate (harness-side)."""
    return "MMMDCCCLXXXVIII"


def _task_scoreboard() -> MCWTask:
    package = "scoreboard"
    reference = (
        "def aggregate_scores(rows, key_field, value_field):\n"
        '    """Sum integer values per key; result keyed in sorted key order."""\n'
        "    totals = {}\n"
        "    for row in rows:\n"
        "        if key_field not in row or value_field not in row:\n"
        '            raise KeyError("missing required field")\n'
        "        value = row[value_field]\n"
        "        if not isinstance(value, int) or isinstance(value, bool):\n"
        '            raise TypeError("values must be integers")\n'
        "        key = row[key_field]\n"
        "        totals[key] = totals.get(key, 0) + value\n"
        "    return {key: totals[key] for key in sorted(totals)}\n"
    )
    return MCWTask(
        key="scoreboard",
        title="Row aggregation utility",
        package=package,
        module_name="aggregate",
        requirement_text=(
            "Implement aggregate_scores(rows, key_field, value_field) -> dict. "
            "rows is a list of dicts. Sum the integer `value_field` per distinct "
            "`key_field`; missing fields raise KeyError; non-integer values "
            "(including bool) raise TypeError; the result dict is keyed in "
            "sorted key order; empty rows give {}."
        ),
        interface_markers=("def aggregate_scores(rows, key_field, value_field):",),
        baseline_files={
            f"{package}/__init__.py": _pkg_init(package),
            f"{package}/aggregate.py": (
                "def aggregate_scores(rows, key_field, value_field):\n"
                + _STUB_BODY
            ),
        },
        reference_implementation=reference,
        public_checks=(
            {"kind": "call_equals", "target": "aggregate_scores", "args": [[{"t": "a", "s": 1}, {"t": "b", "s": 2}, {"t": "a", "s": 3}], "t", "s"], "expected": {"a": 4, "b": 2}},
            {"kind": "call_equals", "target": "aggregate_scores", "args": [[{"k": 2, "v": 5}], "k", "v"], "expected": {2: 5}},
            {"kind": "call_equals", "target": "aggregate_scores", "args": [[], "t", "s"], "expected": {}},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "aggregate_scores", "args": [[{"k": "x", "v": -2}, {"k": "x", "v": -3}], "k", "v"], "expected": {"x": -5}},
            {"kind": "call_raises", "target": "aggregate_scores", "args": [[{"k": "a"}], "k", "v"], "exception": "KeyError"},
            {"kind": "call_raises", "target": "aggregate_scores", "args": [[{"k": "a", "v": True}], "k", "v"], "exception": "TypeError"},
            {"kind": "call_equals", "target": "aggregate_scores", "args": [[{"t": "b", "v": 1}, {"t": "a", "v": 2}], "t", "v"], "expected": {"a": 2, "b": 1}},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "aggregate_scores", "args": [[{"p": "w", "n": 4}, {"p": "z", "n": 1}, {"p": "w", "n": 6}], "p", "n"], "expected": {"w": 10, "z": 1}}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "aggregate_scores", "args": [[{"t": "m", "s": 0}], "t", "s"], "expected": {"m": 0}},
            {"kind": "call_equals", "target": "aggregate_scores", "args": [[{"t": 10, "s": 1}, {"t": 2, "s": 1}], "t", "s"], "expected": {2: 1, 10: 1}},
        ),
    )


def _task_lru_cache_sim() -> MCWTask:
    package = "lru_cache_sim"
    reference = (
        "def simulate_lru(capacity, operations):\n"
        '    """Pure LRU simulation; returns the list of `get` results."""\n'
        "    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:\n"
        '        raise ValueError("capacity must be a positive integer")\n'
        "    order = []\n"
        "    store = {}\n"
        "    results = []\n"
        "    for op in operations:\n"
        "        kind = op[0]\n"
        "        if kind == 'put':\n"
        "            _, key, value = op\n"
        "            if key in store:\n"
        "                order.remove(key)\n"
        "            elif len(store) >= capacity:\n"
        "                evicted = order.pop(0)\n"
        "                store.pop(evicted)\n"
        "            order.append(key)\n"
        "            store[key] = value\n"
        "        elif kind == 'get':\n"
        "            _, key = op\n"
        "            if key in store:\n"
        "                order.remove(key)\n"
        "                order.append(key)\n"
        "                results.append(store[key])\n"
        "            else:\n"
        "                results.append(-1)\n"
        "        else:\n"
        '            raise ValueError("unknown operation")\n'
        "    return results\n"
    )
    classic_ops = [
        ("put", 1, 10),
        ("put", 2, 20),
        ("get", 1),
        ("put", 3, 30),
        ("get", 2),
        ("get", 3),
    ]
    recency_ops = [("put", 1, 10), ("put", 2, 20), ("put", 1, 11), ("put", 3, 30), ("get", 2), ("get", 1)]
    return MCWTask(
        key="lru_cache_sim",
        title="LRU cache simulation",
        package=package,
        module_name="lru",
        requirement_text=(
            "Implement simulate_lru(capacity, operations) -> list. operations "
            "are ('put', key, value) and ('get', key) tuples processed in "
            "order against a fixed-capacity LRU cache; 'put' overwrites "
            "values and refreshes recency; inserting into a full cache "
            "evicts the least-recently-used key; 'get' refreshes recency and "
            "returns the value or -1 on miss; unknown operations or "
            "capacity <= 0 raise ValueError; the result lists every 'get' "
            "result in order."
        ),
        interface_markers=("def simulate_lru(capacity, operations):",),
        baseline_files={
            f"{package}/__init__.py": _pkg_init(package),
            f"{package}/lru.py": (
                "def simulate_lru(capacity, operations):\n"
                + _STUB_BODY
            ),
        },
        reference_implementation=reference,
        public_checks=(
            {"kind": "call_equals", "target": "simulate_lru", "args": [2, classic_ops], "expected": [10, -1, 30]},
            {"kind": "call_equals", "target": "simulate_lru", "args": [2, recency_ops], "expected": [-1, 11]},
            {"kind": "call_equals", "target": "simulate_lru", "args": [1, [("put", "a", 1), ("get", "a")]], "expected": [1]},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "simulate_lru", "args": [2, []], "expected": []},
            {"kind": "call_equals", "target": "simulate_lru", "args": [1, [("put", 1, 1), ("put", 2, 2), ("get", 1), ("get", 2)]], "expected": [-1, 2]},
            {"kind": "call_raises", "target": "simulate_lru", "args": [0, []], "exception": "ValueError"},
            {"kind": "call_raises", "target": "simulate_lru", "args": [2, [("take", 1)]], "exception": "ValueError"},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "simulate_lru", "args": [3, [("put", 1, 1), ("put", 2, 2), ("put", 3, 3), ("get", 1), ("put", 4, 4), ("get", 2), ("get", 1)]], "expected": [1, -1, 1]}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "simulate_lru", "args": [2, [("put", "k", "v"), ("get", "k"), ("get", "z")]], "expected": ["v", -1]},
            {"kind": "call_equals", "target": "simulate_lru", "args": [2, [("put", 1, 10), ("put", 1, 20), ("get", 1)]], "expected": [20]},
        ),
    )


def default_task_set() -> tuple[MCWTask, ...]:
    return (
        _task_line_wrap(),
        _task_interval_merge(),
        _task_roman_numerals(),
        _task_scoreboard(),
        _task_lru_cache_sim(),
    )


MCW_TASK_SET_VERSION = 1


def task_set_id() -> str:
    return f"mcw_pilot_python_component_v{MCW_TASK_SET_VERSION}"


def task_set_hash(tasks: Sequence[MCWTask] | None = None) -> str:
    tasks = tuple(tasks) if tasks is not None else default_task_set()
    payload = {
        "task_set_id": task_set_id(),
        "version": MCW_TASK_SET_VERSION,
        "tasks": [task.to_dict() for task in tasks],
    }
    return _hash(payload)


# ---------------------------------------------------------------------------
# 5. Worker tiers (descriptive experiment roles over the authorized registry;
#    exact provider/model/runtime persisted; tiers are cost/role classes only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCWWorkerCondition:
    condition_id: str
    role_description: str
    worker_id: str
    provider: str
    model: str
    planned_runtime: Mapping[str, Any]
    pricing_reference: Mapping[str, Any] = field(default_factory=dict)


MCW_WORKER_CONDITIONS: tuple[MCWWorkerCondition, ...] = (
    MCWWorkerCondition(
        condition_id="SMALL",
        role_description="lower-cost tier role label; NOT an intelligence claim",
        worker_id="worker_cold_openai_live_fallback_v0",
        provider="openai",
        model="gpt-4o-mini-2024-07-18",
        planned_runtime={
            "api": "chat_completions",
            "api_capability_profile": "openai_chat_legacy_v1",
            "max_output_parameter": "max_tokens",
            "max_output_tokens": 2048,
            "response_format": "json_schema_strict",
            "reasoning_effort": None,
            "temperature": 0.0,
            "top_p": None,
        },
    ),
    MCWWorkerCondition(
        condition_id="MID",
        role_description="mid-cost tier role label; NOT an intelligence claim",
        worker_id="worker_pair_provider_candidate_v0",
        provider="openai",
        model="gpt-4.1-2025-04-14",
        planned_runtime={
            "api": "chat_completions",
            "api_capability_profile": "openai_chat_legacy_v1",
            "max_output_parameter": "max_tokens",
            "max_output_tokens": 2048,
            "response_format": "json_schema_strict",
            "reasoning_effort": None,
            "temperature": 0.0,
            "top_p": None,
        },
    ),
    MCWWorkerCondition(
        condition_id="FRONTIER",
        role_description="highest-cost tier role label; NOT an intelligence claim",
        worker_id="worker_openai_gpt56_terra_holdout_v0",
        provider="openai",
        model="gpt-5.6-terra",
        planned_runtime={
            "api": "chat_completions",
            "api_capability_profile": "openai_chat_gpt56_v1",
            "max_output_parameter": "max_completion_tokens",
            "max_output_tokens": 2048,
            "response_format": "json_schema_strict",
            "reasoning_effort": "low",
            "temperature": None,
            "top_p": None,
        },
    ),
)


def worker_conditions_hash() -> str:
    return _hash(
        {
            "conditions": [
                {
                    "condition_id": c.condition_id,
                    "worker_id": c.worker_id,
                    "provider": c.provider,
                    "model": c.model,
                    "planned_runtime": dict(c.planned_runtime),
                    "pricing_reference": dict(c.pricing_reference),
                }
                for c in MCW_WORKER_CONDITIONS
            ]
        }
    )


# ---------------------------------------------------------------------------
# 8. Predeclared quality criterion (framework now; margins PILOT)
# ---------------------------------------------------------------------------

MCW_SUFFICIENCY_MIN_TASKS = 8  # pilot has 5 -> INSUFFICIENT_EVIDENCE by design

MCW_QUALITY_CRITERION: Mapping[str, Any] = {
    "primary": "mandatory deterministic verification success",
    "required_gates": [
        "structured_output_schema",
        "patch_output_contract",
        "allowed_file_change_scope",
        "python_syntax_compile",
        "public_test_execution",
        "private_test_execution",
        "handoff_artifact_validation",
        "integration_verification",
        "independent_final_reviewer",
    ],
    "verified_task_definition": (
        "all required gates PASS for the task under the condition"
    ),
    "components": {
        "private_verification_pass": True,
        "no_scope_violation": True,
        "integration_reviewer_pass": True,
        "subjective_model_scores_used": False,
    },
    "statistical_margin": {
        "status": "PILOT_NOT_YET_ESTIMATED",
        "method": None,
        "value": None,
        "non_inferiority_claimed": False,
    },
}

# ---------------------------------------------------------------------------
# 11. Frontier states + 10. confidence reporting
# ---------------------------------------------------------------------------

FRONTIER_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
FRONTIER_CANDIDATE_SUFFICIENT = "CANDIDATE_SUFFICIENT"
FRONTIER_NOT_SUFFICIENT_OBSERVED = "NOT_SUFFICIENT_OBSERVED"


def frontier_state(
    *, verified_count: int, n_tasks: int, observed_verified_failure: bool, margin_estimated: bool
) -> str:
    """Predeclared state rule; REDI-09A can never declare a winner."""
    if observed_verified_failure:
        return FRONTIER_NOT_SUFFICIENT_OBSERVED
    if verified_count == n_tasks and n_tasks >= MCW_SUFFICIENCY_MIN_TASKS and margin_estimated:
        return FRONTIER_CANDIDATE_SUFFICIENT
    return FRONTIER_INSUFFICIENT_EVIDENCE


def pilot_confidence(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Descriptive pilot confidence — no global capability score exists."""
    n = len(rows)
    successes = sum(1 for row in rows if row.get("verified") is True)
    failure_classes: dict[str, int] = {}
    for row in rows:
        if row.get("verified") is not True and row.get("failure_classification"):
            key = str(row["failure_classification"])
            failure_classes[key] = failure_classes.get(key, 0) + 1
    return {
        "method": "descriptive_pilot_only",
        "n_tasks": n,
        "successes": successes,
        "failures": n - successes,
        "failure_classes": failure_classes,
        "observed_success_proportion": (successes / n) if n else None,
        "uncertainty_interval": None,
        "interval_method": "NOT_ESTIMATED_PILOT_SAMPLE_ASSUMPTIONS_NOT_MET",
        "coverage_warning": (
            "pilot sample is too small for coverage claims; counts are "
            "descriptive only and must not be read as capability estimates"
        ),
    }


# ---------------------------------------------------------------------------
# 17. Reporting guardrails (hardcoded, enforced at build and replay time)
# ---------------------------------------------------------------------------

FORBIDDEN_REPORT_SUBSTRINGS: tuple[str, ...] = (
    "smarter",
    "% intelligence",
    "intelligence score",
    "best model",
    "globally cheapest",
    "global winner",
    "model ranking",
    "leaderboard",
    "superior model",
    "minimum capable model",
)
FORBIDDEN_REPORT_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "winner",
        "best_model",
        "global_rank",
        "global_ranking",
        "ranking",
        "intelligence_score",
        "leaderboard",
        "minimum_capable_model",
        "capability_score",
    }
)
ALLOWED_CLAIM_TEMPLATE = (
    "For station {station} / task class {task_class} / experiment {experiment}, "
    "worker {worker} satisfied the predeclared verified-quality criterion on "
    "{n} tasks with these observed cost/latency outcomes."
)


class MCWReportGuardrailError(ValueError):
    """A report attempted a forbidden claim shape."""


def enforce_report_guardrails(report: Mapping[str, Any]) -> None:
    """Raise MCWReportGuardrailError on forbidden fields or claim phrases."""

    def walk(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                key_str = str(key)
                if key_str.lower() in FORBIDDEN_REPORT_FIELD_NAMES:
                    raise MCWReportGuardrailError(f"forbidden report field: {path}/{key_str}")
                walk(value, f"{path}/{key_str}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")
        elif isinstance(node, str):
            lowered = node.lower()
            for phrase in FORBIDDEN_REPORT_SUBSTRINGS:
                if phrase in lowered:
                    raise MCWReportGuardrailError(
                        f"forbidden claim phrase {phrase!r} at {path}"
                    )

    walk(report, "$")


# ---------------------------------------------------------------------------
# 12. Failure taxonomy (canonical FailureCategory reuse)
# ---------------------------------------------------------------------------


def classify_mcw_failure(
    *,
    provider_incomplete: bool,
    gate_outcomes: Mapping[str, str],
    structured_error: str | None,
    unexpected_error: str | None,
) -> tuple[str, FailureCategory]:
    """Predeclared mapping; failures never default to a model-weakness claim."""
    if unexpected_error is not None:
        return "ENVIRONMENT_TOOL_FAILURE", FailureCategory.TOOL_FAILURE
    if provider_incomplete:
        return "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE", FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION
    if (
        gate_outcomes["patch_output_contract"] == "FAIL"
        or (
            structured_error is not None
            and "patch_0_path_does_not_match_contract" in structured_error
        )
    ):
        return "SCOPE_VIOLATION", FailureCategory.SCOPE_VIOLATION
    if gate_outcomes["structured_output_schema"] == "FAIL":
        return "STRUCTURED_OUTPUT_SCHEMA_FAILURE", FailureCategory.DETERMINISTIC_TEST_FAILURE
    if gate_outcomes["independent_final_reviewer"] == "FAIL":
        return "REVIEWER_REJECTION", FailureCategory.DETERMINISTIC_TEST_FAILURE
    if any(
        gate_outcomes[gate] == "FAIL"
        for gate in (
            "public_test_execution",
            "private_test_execution",
            "python_syntax_compile",
            "integration_verification",
            "handoff_artifact_validation",
        )
    ):
        return "WORKER_IMPLEMENTATION_FAILURE", FailureCategory.DETERMINISTIC_TEST_FAILURE
    return "WORKER_IMPLEMENTATION_FAILURE", FailureCategory.DETERMINISTIC_TEST_FAILURE


# ---------------------------------------------------------------------------
# 13. Deterministic condition order (preregistered counterbalancing)
# ---------------------------------------------------------------------------


def condition_order_for_task(task_index: int, conditions: Sequence[MCWWorkerCondition]) -> tuple[str, ...]:
    ids = [c.condition_id for c in conditions]
    offset = task_index % len(ids)
    return tuple(ids[offset:] + ids[:offset])


# ---------------------------------------------------------------------------
# 7. Preregistered ExperimentManifest
# ---------------------------------------------------------------------------


def build_mcw_manifest(
    *,
    stratum: MCWStratum | None = None,
    tasks: Sequence[MCWTask] | None = None,
    source_revision: str = "",
) -> ExperimentManifest:
    stratum = stratum or default_stratum()
    tasks = tuple(tasks) if tasks is not None else default_task_set()
    tset_hash = task_set_hash(tasks)
    return ExperimentManifest(
        title="REDI-09A MCW pilot — small bounded pure-Python component stratum",
        hypothesis_reference="REDI-09",
        research_question=(
            "For station station_mcw_implementation_v0 / task class "
            "small_bounded_pure_python_component, which authorized worker "
            "tiers satisfy the predeclared verified-quality criterion, at "
            "what observed token/latency cost?"
        ),
        arms={
            c.condition_id: {
                "role_description": c.role_description,
                "worker_id": c.worker_id,
                "provider": c.provider,
                "model": c.model,
                "planned_runtime": dict(c.planned_runtime),
            }
            for c in MCW_WORKER_CONDITIONS
        },
        task_selection={
            "policy": "predeclared_fixed_task_set_selected_before_any_result",
            "selection_before_results": True,
            "task_set_id": task_set_id(),
            "task_set_version": MCW_TASK_SET_VERSION,
            "task_set_sha256": tset_hash,
            "n_tasks": len(tasks),
            "stratum": stratum.to_dict(),
            "stratum_sha256": stratum.canonical_hash,
            "holdout_reuse_forbidden": True,
        },
        worker_declarations={
            c.condition_id: {
                "worker_id": c.worker_id,
                "provider": c.provider,
                "model": c.model,
                "planned_runtime": dict(c.planned_runtime),
                "pricing_reference": dict(c.pricing_reference),
            }
            for c in MCW_WORKER_CONDITIONS
        },
        context_strategy={
            "id": "mcw_component_context_v1",
            "budget_bytes": 6000,
            "includes": ["task requirement", "interface signatures", "builder output contract"],
            "excludes": ["private checks", "reference implementation", "other-condition results"],
        },
        budgets={"live_provider_calls": Budget(tokens_total=0, metadata={"planned_calls": len(tasks) * len(MCW_WORKER_CONDITIONS), "hard_max": MCW_MAX_LIVE_CALLS})},
        acceptance_criteria=(
            AcceptanceCriterion(description=json.dumps(MCW_QUALITY_CRITERION, sort_keys=True)),
        ),
        preregistered_thresholds={
            "quality_criterion": dict(MCW_QUALITY_CRITERION),
            "sufficiency_min_tasks": MCW_SUFFICIENCY_MIN_TASKS,
            "frontier_states": [
                FRONTIER_INSUFFICIENT_EVIDENCE,
                FRONTIER_CANDIDATE_SUFFICIENT,
                FRONTIER_NOT_SUFFICIENT_OBSERVED,
            ],
        },
        randomization={
            "policy": "deterministic_counterbalance_rotation",
            "rule": "condition_order_for_task(task_index) rotates the condition tuple",
            "seeded_prng_used": False,
            "fixed_before_results": True,
        },
        repetition_config={"repetitions_per_cell": 1, "pilot": True},
        measurement_plan={
            "quality": ["verified", "gate_outcomes", "failure_classification"],
            "efficiency": ["provider_calls", "tokens_in_out_cached", "provider_wall_seconds", "total_wall_seconds", "retries", "escalations"],
            "context": ["compiled_bytes", "estimated_tokens", "provider_input_tokens"],
            "cost": ["pricing_schedule_version", "calculated_cost", "cost_status"],
        },
        pricing_assumptions={
            "versioned_schedule_available": False,
            "cost_status": "unavailable_unversioned",
            "policy": "never invent cost; persist tokens until an authoritative versioned schedule exists",
        },
        version_assumptions={
            "exact_model_identifiers_persisted": True,
            "worker_registry_drift": "must not rewrite historical results",
        },
        human_intervention_policy={"automatic_retry": False, "escalation": "owner_only"},
        replay_policy={"read_only": True, "repetitions": 2, "drift_immune": True},
        validity_controls={
            "condition_isolation": "separate workspace and evidence per condition",
            "cross_worker_contamination": "forbidden",
            "failure_hint_leakage_to_other_conditions": "forbidden",
            "same_task_semantics_all_conditions": True,
            "worker_only_treatment_variable": True,
        },
        metadata={
            "experiment_family": MCW_EXPERIMENT_FAMILY,
            "policy_id": MCW_POLICY_ID,
            "policy_version": MCW_POLICY_VERSION,
            "source_revision": source_revision,
            "winner_selection": "forbidden_in_redi09a",
        },
    )


# ---------------------------------------------------------------------------
# 14/16. Dry experiment engine + replay
# ---------------------------------------------------------------------------

MCW_MAX_LIVE_CALLS = 30
GATE_ORDER: tuple[str, ...] = (
    "structured_output_schema",
    "patch_output_contract",
    "allowed_file_change_scope",
    "python_syntax_compile",
    "public_test_execution",
    "private_test_execution",
    "handoff_artifact_validation",
    "integration_verification",
    "independent_final_reviewer",
)


@dataclass
class MCWExperimentOutcome:
    root: Path
    db_path: Path
    result_path: Path
    manifest_id: str
    manifest_sha256: str
    factory_run_id: str
    verified: bool
    provider_calls: int
    network_calls: int
    rows: list[dict[str, Any]]
    report: dict[str, Any]


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _apply_checks(namespace: Mapping[str, Any], checks: Sequence[Mapping[str, Any]]) -> None:
    for check in checks:
        target = namespace[str(check["target"])]
        args = list(check["args"])
        if check["kind"] == "call_equals":
            actual = target(*args)
            if actual != check["expected"]:
                raise AssertionError(
                    f"{check['target']} check mismatch: expected {check['expected']!r}, got {actual!r}"
                )
        elif check["kind"] == "call_raises":
            expected_type = {"ValueError": ValueError, "TypeError": TypeError, "KeyError": KeyError}[
                str(check["exception"])
            ]
            try:
                target(*args)
            except expected_type:
                continue
            raise AssertionError(
                f"{check['target']} check mismatch: expected {check['exception']} for args {args!r}"
            )
        else:
            raise ValueError(f"unknown check kind {check['kind']!r}")


def _compile_context(task: MCWTask) -> dict[str, Any]:
    """Deterministic worker context: requirement + interface + contract only."""
    parts = [
        f"## Task: {task.title}",
        "## Requirements",
        task.requirement_text,
        "## Exact interface",
        *[line for line in task.interface_markers],
        "## Output",
        "Return exactly one patch/edit writing "
        f"`{task.rel_path}` implementing the interface above.",
    ]
    body = "\n".join(parts)
    reference_leak = task.reference_implementation in body
    return {
        "context_body": body,
        "compiled_bytes": len(body.encode("utf-8")),
        "estimated_tokens": max(1, len(body.encode("utf-8")) // 4),
        "private_check_args_in_context": any(
            json.dumps(check["args"], sort_keys=True) in body
            for check in tuple(task.private_checks) + tuple(task.public_checks)
        ),
        "reference_leak_detected": reference_leak,
    }


def _run_fake_condition_cell(
    *,
    task: MCWTask,
    condition: MCWWorkerCondition,
    workspace_root: Path,
    scripted_outcome: str,
) -> dict[str, Any]:
    """Deterministic fake provider slot (dry). Zero provider/network."""
    started = time.perf_counter()
    context = _compile_context(task)
    contract = task.contract
    parsed_error: str | None = None
    patches: tuple = ()
    observed: tuple[ParsedPatchObservation, ...] = ()
    output_shape: Mapping[str, Any] = {}
    provider_completion: dict[str, Any] = {
        "finish_reason": "deterministic_fake",
        "refusal_present": False,
        "incomplete": False,
        "completion_token_details": {},
        "provider_call_elapsed_seconds": 0.000001,
    }
    if scripted_outcome == "provider_incomplete":
        # No visible content: the shared parse path sees an empty string.
        interpreted = parse_builder_output_object(None, patch_output_contract=contract)
        patches = interpreted["patches"]
        observed = interpreted["observed_patches"]
        output_shape = interpreted["output_shape"]
        parsed_error = "STRUCTURED_OUTPUT_SCHEMA_FAILURE role=builder category=JSONDecodeError"
        provider_completion = {
            "finish_reason": "length",
            "refusal_present": False,
            "incomplete": True,
            "completion_token_details": {"reasoning_tokens": 2048},
            "provider_call_elapsed_seconds": 0.000001,
        }
    else:
        if scripted_outcome == "scope_violation":
            raw = {
                "patches": [
                    {
                        "rel_path": f"forbidden_elsewhere/{task.module_name}.py",
                        "action": "write",
                        "content": task.reference_implementation,
                    }
                ]
            }
        elif scripted_outcome == "implementation_failure":
            # Plausible-looking but wrong: re-shadows the primary entry point
            # with an incorrect deterministic implementation. Compiles and
            # imports cleanly; public verification must reject it.
            broken = (
                task.reference_implementation
                + "\n\n# simulated worker defect (dry harness validation only)\n"
                + task.interface_markers[0]
                + "\n    return None\n"
            )
            raw = {
                "patches": [
                    {"rel_path": task.rel_path, "action": "write", "content": broken}
                ]
            }
        else:  # success (dry fake emits the predeclared reference content)
            raw = {
                "patches": [
                    {"rel_path": task.rel_path, "action": "write", "content": task.reference_implementation}
                ]
            }
        parsed_output = parse_builder_output_object(raw, patch_output_contract=contract)
        patches = parsed_output["patches"]
        observed = parsed_output["observed_patches"]
        output_shape = parsed_output["output_shape"]
        parsed_error = parsed_output["safe_error"]
    elapsed = time.perf_counter() - started
    return {
        "patches": list(patches),
        "observed_patches": list(observed),
        "output_shape": output_shape,
        "context": context,
        "structured_output": {
            "validated": parsed_error is None,
            "schema": "RediBuilderOutput",
            "safe_error": parsed_error,
            "source": "deterministic_fake_mcw_adapter",
        },
        "provider_completion": provider_completion,
        "provider": {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_input_tokens": 0,
            "provider": None,
            "model": None,
            "observed_provider_access": "not_observed_dry_rehearsal",
        },
        "elapsed_seconds": elapsed,
        "execution_kind": "governed_fake_mcw_slot",
    }


def _evaluate_cell_gates(
    *,
    task: MCWTask,
    cell: Mapping[str, Any],
    workspace_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    outcomes = {gate: "NOT_RUN" for gate in GATE_ORDER}
    reasons: dict[str, str] = {}
    patch = None

    def fail(gate: str, message: str) -> None:
        outcomes[gate] = "FAIL"
        reasons[gate] = message

    structured = cell.get("structured_output", {})
    if structured.get("validated"):
        outcomes["structured_output_schema"] = "PASS"
    else:
        fail(
            "structured_output_schema",
            str(structured.get("safe_error") or "structured output was not validated"),
        )
    if outcomes["structured_output_schema"] == "PASS":
        patches = list(cell.get("patches", ()))
        contract_reason = task.contract.validate_patches(patches)
        if contract_reason is not None:
            fail("patch_output_contract", contract_reason)
        else:
            patch = patches[0]
            outcomes["patch_output_contract"] = "PASS"
    if patch is not None:
        if not patch.rel_path.startswith(f"{task.package}/"):
            fail("allowed_file_change_scope", "patch escaped the task package scope")
        else:
            outcomes["allowed_file_change_scope"] = "PASS"
    if outcomes["allowed_file_change_scope"] == "PASS":
        try:
            compile(patch.content or "", task.rel_path, "exec")
            compile(task.baseline_files[f"{task.package}/__init__.py"], "__init__.py", "exec")
            outcomes["python_syntax_compile"] = "PASS"
        except Exception as exc:  # noqa: BLE001
            fail("python_syntax_compile", f"syntax failure: {type(exc).__name__}: {exc}")
    namespace: dict[str, Any] = {}
    if outcomes["python_syntax_compile"] == "PASS":
        try:
            exec(patch.content or "", namespace)  # noqa: S102 - deterministic pilot sandbox
            _apply_checks(namespace, task.public_checks)
            outcomes["public_test_execution"] = "PASS"
        except Exception as exc:  # noqa: BLE001
            fail("public_test_execution", f"public verification failure: {type(exc).__name__}: {exc}")
    if outcomes["public_test_execution"] == "PASS":
        try:
            private_namespace: dict[str, Any] = {}
            exec(patch.content or "", private_namespace)  # noqa: S102
            _apply_checks(private_namespace, task.private_checks)
            outcomes["private_test_execution"] = "PASS"
        except Exception as exc:  # noqa: BLE001
            fail("private_test_execution", f"private verification failure: {type(exc).__name__}: {exc}")
    if outcomes["private_test_execution"] == "PASS":
        try:
            digest = hashlib.sha256((patch.content or "").encode("utf-8")).hexdigest()
            if len(digest) != 64:
                raise AssertionError("artifact digest malformed")
            outcomes["handoff_artifact_validation"] = "PASS"
            target = workspace_root / task.rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((patch.content or "").encode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            fail("handoff_artifact_validation", f"handoff failure: {type(exc).__name__}: {exc}")
    if outcomes["handoff_artifact_validation"] == "PASS":
        try:
            module_name = f"{task.package}.{task.module_name}"
            for name in (task.package, module_name):
                sys.modules.pop(name, None)
            sys.path.insert(0, str(workspace_root))
            try:
                module = importlib.import_module(module_name)
                probe = task.integration_probe
                target_callable = getattr(module, str(probe["target"]))
                actual = target_callable(*list(probe["args"]))
                if actual != probe["expected"]:
                    raise AssertionError(f"integration mismatch: expected {probe['expected']!r}, got {actual!r}")
                outcomes["integration_verification"] = "PASS"
            finally:
                sys.path.remove(str(workspace_root))
                for name in (task.package, module_name):
                    sys.modules.pop(name, None)
        except Exception as exc:  # noqa: BLE001
            fail("integration_verification", f"integration failure: {type(exc).__name__}: {exc}")
    if outcomes["integration_verification"] == "PASS":
        try:
            reviewer_namespace: dict[str, Any] = {}
            exec(patch.content or "", reviewer_namespace)  # noqa: S102
            _apply_checks(reviewer_namespace, task.reviewer_checks)
            outcomes["independent_final_reviewer"] = "PASS"
        except Exception as exc:  # noqa: BLE001
            fail("independent_final_reviewer", f"reviewer rejection: {type(exc).__name__}: {exc}")
    return outcomes, reasons


DEFAULT_DRY_OUTCOMES: Mapping[str, str] = {
    # Condition-controlled outcomes: one tier succeeds on all tasks, one on a
    # subset, one fails representative tasks (harness validation only).
    ("FRONTIER", "line_wrap"): "success",
    ("FRONTIER", "interval_merge"): "success",
    ("FRONTIER", "roman_numerals"): "success",
    ("FRONTIER", "scoreboard"): "success",
    ("FRONTIER", "lru_cache_sim"): "success",
    ("MID", "line_wrap"): "success",
    ("MID", "interval_merge"): "success",
    ("MID", "roman_numerals"): "success",
    ("MID", "scoreboard"): "success",
    ("MID", "lru_cache_sim"): "implementation_failure",
    ("SMALL", "line_wrap"): "success",
    ("SMALL", "interval_merge"): "provider_incomplete",
    ("SMALL", "roman_numerals"): "success",
    ("SMALL", "scoreboard"): "scope_violation",
    ("SMALL", "lru_cache_sim"): "implementation_failure",
}


def run_mcw_experiment(
    *,
    output_root: Path | str,
    dry: bool = True,
    scripted_outcomes: Mapping[str, str] | None = None,
    source_revision: str = "",
) -> MCWExperimentOutcome:
    """Run the MCW pilot harness. Only dry=True is supported (no live calls)."""
    if not dry:
        raise ValueError("REDI-09A is harness-only; live execution is a later owner-authorized pilot")
    root = Path(output_root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError(f"MCW output root must be new or empty: {root}")
    root.mkdir(parents=True)
    outcomes_map = dict(DEFAULT_DRY_OUTCOMES if scripted_outcomes is None else scripted_outcomes)

    stratum = default_stratum()
    tasks = default_task_set()
    conditions = MCW_WORKER_CONDITIONS

    conn = sqlite3.connect(root / "mcw_evidence.sqlite")
    bootstrap_schema(conn)
    ledger = EvidenceLedger(conn)
    runs_repo = FactoryRunRepository(conn, ledger)
    experiments_repo = ExperimentRepository(conn, ledger)

    manifest = build_mcw_manifest(stratum=stratum, tasks=tasks, source_revision=source_revision)
    manifest_id = experiments_repo.store_preregistered(manifest)
    experiments_repo.freeze(manifest_id)
    stored = experiments_repo.read(manifest_id)
    manifest_sha = payload_hash_from_json(domain_to_json(manifest))

    factory_run = FactoryRun(
        mode=Mode.EXPLORATION,
        final_status=RunStatus.PENDING,
        verified=False,
        total_tokens=TokenUsage(input=0, output=0, cached_input=0, reasoning=0),
        metadata={
            "experiment_family": MCW_EXPERIMENT_FAMILY,
            "experiment_id": manifest_id,
            "execution_kind": "HARNESS_REHEARSAL_ONLY",
            "counts_as_transfer_evidence": False,
        },
    )
    runs_repo.store(factory_run, experiment_id=manifest_id)
    run_id = str(factory_run.id)

    tset_hash = task_set_hash(tasks)
    rows: list[dict[str, Any]] = []
    gate_rows: list[GateResult] = []
    provider_calls = 0
    network_calls = 0
    any_verified_failure = False

    for task_index, task in enumerate(tasks):
        order = condition_order_for_task(task_index, conditions)
        for condition_id in order:
            condition = next(c for c in conditions if c.condition_id == condition_id)
            cell_root = root / "conditions" / condition_id / task.key
            workspace_root = cell_root / "workspace"
            workspace_root.mkdir(parents=True, exist_ok=True)
            for rel, content in task.baseline_files.items():
                path = workspace_root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            scripted = outcomes_map.get((condition_id, task.key), "success")
            cell = _run_fake_condition_cell(
                task=task,
                condition=condition,
                workspace_root=workspace_root,
                scripted_outcome=scripted,
            )
            gate_outcomes, gate_reasons = _evaluate_cell_gates(
                task=task, cell=cell, workspace_root=workspace_root
            )
            all_pass = all(gate_outcomes[gate] == "PASS" for gate in GATE_ORDER)
            verified = all_pass
            if not verified:
                any_verified_failure = True
            classification, failure_category = classify_mcw_failure(
                provider_incomplete=bool(cell["provider_completion"].get("incomplete")),
                gate_outcomes=gate_outcomes,
                structured_error=cell["structured_output"].get("safe_error"),
                unexpected_error=None,
            )
            context = cell["context"]
            elapsed = cell["elapsed_seconds"]
            for gate in GATE_ORDER:
                gate_rows.append(
                    GateResult(
                        gate_identity=f"mcw::{condition_id}::{task.key}::{gate}",
                        gate_type=GateType.DETERMINISTIC,
                        target_node_id=f"{condition_id}/{task.key}",
                        criteria=(AcceptanceCriterion(description=f"{gate} must pass"),),
                        status=GateStatus.PASS
                        if gate_outcomes[gate] == "PASS"
                        else (GateStatus.FAIL if gate_outcomes[gate] == "FAIL" else GateStatus.BLOCKED),
                        verifier_type="deterministic_code",
                        verifier_identity=f"{MCW_POLICY_ID}@{MCW_POLICY_VERSION}",
                        failure_reasons=()
                        if gate_outcomes[gate] == "PASS"
                        else ((gate_reasons.get(gate) or "not reached"),),
                        metadata={
                            "condition_id": condition_id,
                            "task_key": task.key,
                            "outcome": gate_outcomes[gate],
                            "mandatory": True,
                        },
                    )
                )
            attempt_row = {
                "condition_id": condition_id,
                "worker_id": condition.worker_id,
                "model": condition.model,
                "task_key": task.key,
                "task_hash": task.canonical_hash,
                "condition_order": list(order),
                "verified": verified,
                "gates": dict(gate_outcomes),
                "gate_reasons": {k: v for k, v in gate_reasons.items()},
                "failure_classification": None if verified else classification,
                "failure_category": None if verified else failure_category.value,
                "context": {
                    "compiled_bytes": context["compiled_bytes"],
                    "estimated_tokens": context["estimated_tokens"],
                    "private_check_args_in_context": context["private_check_args_in_context"],
                    "reference_leak_detected": context["reference_leak_detected"],
                },
                "tokens": {"input": 0, "output": 0, "cached_input": 0, "reasoning": 0},
                "provider_calls": 0,
                "provider_wall_seconds": cell["provider_completion"]["provider_call_elapsed_seconds"],
                "total_wall_seconds": elapsed,
                "local_compute_seconds": elapsed,
                "retries": 0,
                "escalations": 0,
                "cost": {"status": "unavailable_unversioned", "pricing_schedule_version": None, "amount": None},
                "execution_kind": cell["execution_kind"],
                "patch": [
                    {
                        "index": o.index,
                        "action": o.action,
                        "rel_path": o.rel_path,
                        "content_sha256": hashlib.sha256(o.content.encode("utf-8")).hexdigest()
                        if isinstance(o.content, str)
                        else None,
                    }
                    for o in cell["observed_patches"]
                ],
            }
            rows.append(attempt_row)
            evidence = RunEvidence(
                experiment_id=stored.id if hasattr(stored, "id") else None,
                factory_run_id=factory_run.id,
                node_id=f"{condition_id}/{task.key}",
                station_id=StationId(stratum.station_id),
                task_class=stratum.task_class,
                worker_id=WorkerId(condition.worker_id),
                worker_version=str(condition.condition_id),
                token_usage=TokenUsage(input=0, output=0, cached_input=0, reasoning=0),
                timing=Timing(
                    started_at=None,
                    ended_at=None,
                    wall_clock_seconds=max(elapsed, 0.000001),
                    compute_seconds=max(elapsed, 0.000001),
                ),
                tests_executed=("public_checks", "private_checks", "integration_probe", "reviewer_checks"),
                retry_count=0,
                escalation=False,
                human_intervention=False,
                final_status=RunStatus.COMPLETED if verified else RunStatus.FAILED,
                failure_category=None if verified else failure_category,
                metadata={
                    "evidence_kind": "mcw_worker_attempt",
                    "manifest_id": manifest_id,
                    "manifest_sha256": manifest_sha,
                    "stratum": stratum.to_dict(),
                    "task_set_sha256": tset_hash,
                    "attempt": _canonical_row(attempt_row),
                    "condition_isolation": "separate_workspace_per_condition",
                    "dry_harness_rehearsal_only": True,
                },
            )
            ledger.append(evidence, factory_run_id=run_id, experiment_id=manifest_id)
            if not verified:
                ledger.append(
                    FailureRecord(
                        factory_run_id=factory_run.id,
                        node_id=f"{condition_id}/{task.key}",
                        task_id=TaskId(f"mcw_{task.key}"),
                        category=failure_category,
                        severity=Severity.ERROR,
                        origin="mcw_fake_condition_cell",
                        recoverable=False,
                        escalation_target="owner_only",
                        details={
                            "condition_id": condition_id,
                            "task_key": task.key,
                            "failed_gates": [g for g, v in gate_outcomes.items() if v == "FAIL"],
                            "failure_classification": classification,
                        },
                        metadata={"experiment_family": MCW_EXPERIMENT_FAMILY},
                    ),
                    factory_run_id=run_id,
                    experiment_id=manifest_id,
                )
    for gate in gate_rows:
        ledger.append(gate, factory_run_id=run_id, experiment_id=manifest_id)

    verified_all = all(row["verified"] for row in rows)
    runs_repo.close_run(
        run_id,
        final_status=RunStatus.COMPLETED,
        verified=verified_all,
        total_tokens=TokenUsage(input=0, output=0, cached_input=0, reasoning=0),
    )

    report = _build_report(
        manifest_id=manifest_id,
        manifest_sha256=manifest_sha,
        stratum=stratum,
        tasks=tasks,
        conditions=conditions,
        rows=rows,
        tset_hash=tset_hash,
        source_revision=source_revision,
        provider_calls=provider_calls,
        network_calls=network_calls,
        dry=True,
    )
    result_payload = {
        "factory_run_id": run_id,
        "experiment_id": manifest_id,
        "manifest_sha256": manifest_sha,
        "report": report,
        "rows": rows,
        "replay_dependencies": {
            "workers": 0,
            "provider_calls": 0,
            "network": 0,
            "writes": 0,
            "recipe_reselection": False,
            "current_worker_registry": False,
            "current_pricing": False,
        },
    }
    result_payload["canonical_hash"] = _hash(result_payload)
    result_path = root / "mcw_result.json"
    result_path.write_text(json.dumps(result_payload, sort_keys=True, indent=2), encoding="utf-8")
    conn.close()
    return MCWExperimentOutcome(
        root=root,
        db_path=root / "mcw_evidence.sqlite",
        result_path=result_path,
        manifest_id=manifest_id,
        manifest_sha256=manifest_sha,
        factory_run_id=run_id,
        verified=verified_all,
        provider_calls=provider_calls,
        network_calls=network_calls,
        rows=rows,
        report=report,
    )


def _canonical_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(dict(row), sort_keys=True, default=str))


def _build_report(
    *,
    manifest_id: str,
    manifest_sha256: str,
    stratum: MCWStratum,
    tasks: Sequence[MCWTask],
    conditions: Sequence[MCWWorkerCondition],
    rows: Sequence[Mapping[str, Any]],
    tset_hash: str,
    source_revision: str,
    provider_calls: int,
    network_calls: int,
    dry: bool,
) -> dict[str, Any]:
    frontier = []
    per_condition = {}
    for condition in conditions:
        cond_rows = [row for row in rows if row["condition_id"] == condition.condition_id]
        verified_count = sum(1 for row in cond_rows if row["verified"] is True)
        confidence = pilot_confidence(cond_rows)
        per_condition[condition.condition_id] = {
            "worker_id": condition.worker_id,
            "model": condition.model,
            "planned_runtime": dict(condition.planned_runtime),
            "verified_outcomes": {row["task_key"]: row["verified"] for row in cond_rows},
            "failure_classes": confidence["failure_classes"],
            "tokens_total": sum(
                row["tokens"]["input"] + row["tokens"]["output"] for row in cond_rows
            ),
            "latency_seconds_total": sum(row["total_wall_seconds"] for row in cond_rows),
            "cost": {
                "status": "unavailable_unversioned",
                "pricing_schedule_version": None,
            },
            "confidence": confidence,
        }
        frontier.append(
            {
                "station": stratum.station_id,
                "task_class": stratum.task_class,
                "stratum": stratum.stratum_id,
                "experiment": manifest_id,
                "worker": condition.worker_id,
                "model": condition.model,
                "condition_id": condition.condition_id,
                "verified_count": verified_count,
                "n_tasks": len(cond_rows),
                "tokens_total": per_condition[condition.condition_id]["tokens_total"],
                "latency_seconds_total": per_condition[condition.condition_id]["latency_seconds_total"],
                "cost": {"status": "unavailable_unversioned", "pricing_schedule_version": None},
                "failures": confidence["failure_classes"],
                "state": frontier_state(
                    verified_count=verified_count,
                    n_tasks=len(cond_rows),
                    observed_verified_failure=verified_count < len(cond_rows),
                    margin_estimated=False,
                ),
                "confidence_ref": f"{condition.condition_id}/confidence",
            }
        )
    report = {
        "experiment_family": MCW_EXPERIMENT_FAMILY,
        "policy": {"id": MCW_POLICY_ID, "version": MCW_POLICY_VERSION},
        "stratum": stratum.to_dict(),
        "task_set": {"id": task_set_id(), "version": MCW_TASK_SET_VERSION, "sha256": tset_hash, "n_tasks": len(tasks)},
        "manifest_id": manifest_id,
        "manifest_sha256": manifest_sha256,
        "quality_criterion": dict(MCW_QUALITY_CRITERION),
        "execution_mode": "dry_harness_rehearsal_only" if dry else "live",
        "counts_as_mcw_conclusion": False,
        "conditions": per_condition,
        "frontier": frontier,
        "claims": {
            "no_winner_selected": True,
            "minimum_capable_declared": False,
            "model_superiority_claimed": False,
            "recipe_advantage_claimed": False,
            "statistical_non_inferiority_claimed": False,
            "allowed_claim_example": ALLOWED_CLAIM_TEMPLATE.format(
                station=stratum.station_id,
                task_class=stratum.task_class,
                experiment=manifest_id,
                worker="<worker_id>",
                n="<n>",
            ),
        },
        "live_budget_plan": live_budget_plan(tasks=tasks, conditions=conditions),
        "source_revision": source_revision,
        "provider_calls": provider_calls,
        "network_calls": network_calls,
    }
    enforce_report_guardrails(report)
    return report


def live_budget_plan(
    *, tasks: Sequence[MCWTask] | None = None, conditions: Sequence[MCWWorkerCondition] | None = None
) -> dict[str, Any]:
    """15. Future live-pilot budget — prepared, never executed here."""
    tasks = tuple(tasks) if tasks is not None else default_task_set()
    conditions = tuple(conditions) if conditions is not None else MCW_WORKER_CONDITIONS
    planned_calls = len(tasks) * len(conditions)
    per_condition = {c.condition_id: len(tasks) for c in conditions}
    estimated_input_upper = sum(_compile_context(t)["estimated_tokens"] for t in tasks) * len(conditions) + 256 * planned_calls
    output_ceiling = max(int(c.planned_runtime["max_output_tokens"]) for c in conditions)
    estimated_output_upper = output_ceiling * planned_calls
    within_budget = planned_calls <= MCW_MAX_LIVE_CALLS
    return {
        "n_tasks": len(tasks),
        "n_worker_conditions": len(conditions),
        "max_live_calls": planned_calls,
        "per_condition_call_budget": per_condition,
        "estimated_input_token_upper_bound": estimated_input_upper,
        "estimated_output_token_upper_bound": estimated_output_upper,
        "provider_models": [
            {"condition_id": c.condition_id, "provider": c.provider, "model": c.model}
            for c in conditions
        ],
        "automatic_retries": False,
        "block_if_excessive": {"threshold_calls": MCW_MAX_LIVE_CALLS, "within_budget": within_budget, "action": "allow" if within_budget else "BLOCK"},
        "pricing_status": "unavailable_unversioned — no cost estimate is possible or claimed",
    }


def replay_mcw_result(path: Path | str) -> dict[str, Any]:
    """Read-only ×2-safe result replay with canonical hash verification."""
    result_path = Path(path).resolve()
    before = result_path.stat()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    expected = payload.pop("canonical_hash")
    if _hash(payload) != expected:
        raise ValueError("MCW result canonical hash mismatch")
    after = result_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("MCW result replay was not read-only")
    enforce_report_guardrails(payload["report"])
    return payload | {"canonical_hash": expected}


def replay_mcw_snapshot(db_path: Path | str, factory_run_id: str) -> list[dict[str, Any]]:
    """Reconstruct attempt rows from the ledger (read-only, drift-immune)."""
    conn = sqlite3.connect("file:" + Path(db_path).resolve().as_posix() + "?mode=ro&immutable=1", uri=True)
    try:
        rows = []
        for (pj,) in conn.execute(
            "SELECT payload_json FROM evidence_entries WHERE entity_type='RunEvidence' ORDER BY global_sequence"
        ):
            payload = json.loads(pj)
            metadata = payload.get("metadata") or {}
            if metadata.get("evidence_kind") == "mcw_worker_attempt":
                rows.append(metadata["attempt"])
        return rows
    finally:
        conn.close()


__all__ = [
    "ALLOWED_CLAIM_TEMPLATE",
    "DEFAULT_DRY_OUTCOMES",
    "FORBIDDEN_REPORT_FIELD_NAMES",
    "FORBIDDEN_REPORT_SUBSTRINGS",
    "FRONTIER_CANDIDATE_SUFFICIENT",
    "FRONTIER_INSUFFICIENT_EVIDENCE",
    "FRONTIER_NOT_SUFFICIENT_OBSERVED",
    "GATE_ORDER",
    "MCW_EXPERIMENT_FAMILY",
    "MCW_MAX_LIVE_CALLS",
    "MCW_POLICY_ID",
    "MCW_QUALITY_CRITERION",
    "MCW_STRATUM_ID",
    "MCW_SUFFICIENCY_MIN_TASKS",
    "MCW_WORKER_CONDITIONS",
    "MCWReportGuardrailError",
    "MCWTask",
    "MCWWorkerCondition",
    "build_mcw_manifest",
    "classify_mcw_failure",
    "condition_order_for_task",
    "default_stratum",
    "default_task_set",
    "enforce_report_guardrails",
    "frontier_state",
    "live_budget_plan",
    "pilot_confidence",
    "replay_mcw_result",
    "replay_mcw_snapshot",
    "run_mcw_experiment",
    "task_set_hash",
    "task_set_id",
    "worker_conditions_hash",
]
