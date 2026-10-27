"""REDI-09D — MCW N=8 expansion design freeze (offline; zero provider/network).

Extends the verified REDI-09C pilot (N=5 per condition) to N=8 by adding a
deterministically sampled, result-independent set of 3 new tasks × 3 frozen
worker conditions = 9 future live cells.  Nothing here may execute a provider
call; live execution remains an owner-authorized later stage (REDI-09E).

Frozen in this module (all rebuildable byte-identically from code):

- expansion sampling policy (seed derived ONLY from pre-result identifiers)
- 6-candidate neutral sampling frame for stratum mcw_python_component_v1
- deterministic 3-of-6 selection with full proof
- immutable expanded task set mcw_python_component_v1_expanded_n8 (5 + 3)
- exact 9-cell counterbalanced execution order (global positions 16-24)
- V2 observability row schema (forward-only; V1 pilot rows remain valid)
- predeclared N=8 margin method (Clopper-Pearson two-sided 95%)
- N=8 aggregation plan combining locked first-5 + new 3 without reinterpretation
- expansion manifest, hash-frozen before any provider call

The historical REDI-09A/09B/09C modules are imported, never modified.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

from .mcw_experiment import (
    GATE_ORDER,
    MCW_QUALITY_CRITERION,
    MCW_SUFFICIENCY_MIN_TASKS,
    MCW_WORKER_CONDITIONS,
    _compile_context,
    _evaluate_cell_gates,
    _run_fake_condition_cell,
    classify_mcw_failure,
    condition_order_for_task,
    default_stratum,
    default_task_set,
    frontier_state,
    task_set_hash,
    task_set_id,
)
from .mcw_live_pilot import (
    FAKE_USAGE_BY_OUTCOME,
    condition_runtime_by_id,
    build_live_pilot_freeze,
)
from .mcw_pricing import (
    canonical_json,
    calculate_cost,
    cell_cost_bound,
    live_cost_plan,
    pricing_schedule,
    pricing_schedule_hash,
)
from .runtime.contracts import PatchOutputContract

EXPANSION_POLICY_ID = "redi09d_mcw_expansion_v0"
EXPANSION_POLICY_VERSION = 1
EXPANSION_SAMPLING_POLICY_ID = "redi09d_expansion_sampling_v1"
EXPANSION_SAMPLING_POLICY_VERSION = 1
EXPANSION_SEED_DOMAIN_TAG = "REDI-09D-expansion-v1"
EXPANDED_TASK_SET_ID = "mcw_python_component_v1_expanded_n8"
EXPANDED_TASK_SET_VERSION = 1
EXPANSION_ROW_SCHEMA_VERSION = 2
EXPANSION_STAGE = "REDI-09D_EXPANSION_FREEZE"

# The design base revision is the accepted REDI-09C evidence HEAD.  It is a
# frozen constant, not the moving repository HEAD, so the expansion freeze
# hash stays stable across future commits.
EVIDENCE_BASE_REVISION = "c82c47e3b634a13ae084d37ef368b4a193569e7d"

# Accepted parent bindings, verified at design freeze (fail-closed drift
# checks live in scripts/run_redi_09d_expansion_design.py).
ACCEPTED_REDI09A_MANIFEST_SHA256 = "77d4f106364015a67861c216a091108a325843c501c1ea3b46ee95a074112eb0"
ACCEPTED_REDI09B_FREEZE_SHA256 = "5142cf2da50cd5143a97cf2a75615f6bdb71f6603ee9bc8aa37a514ed9947fde"
ACCEPTED_REDI09C_TASK_SET_SHA256 = "8a980e621b206beeea069ac28ff7c72833deff823088f24b7ad23d9b3c30fa94"
ACCEPTED_REDI09C_PILOT_STATE_SHA256 = "5bb70a642cf09217107c3d951725d59c90bb99c267037888056bc35323739c9e"
ACCEPTED_REDI09C1_AUDIT_VERDICT = "VALID"

# Predeclared aggregate margin method (frozen BEFORE any N=8 outcome exists).
MARGIN_METHOD_ID = "clopper_pearson_two_sided_95_v1"
MARGIN_METHOD_ALPHA = "0.05"

# Stratum semantic families of the five locked pilot tasks (used by the
# similarity audit; expansion candidates must occupy disjoint families).
PILOT_TASK_FAMILIES: Mapping[str, str] = {
    "line_wrap": "text_layout_packing",
    "interval_merge": "interval_algebra",
    "roman_numerals": "ancient_numeral_conversion",
    "scoreboard": "record_aggregation_validation",
    "lru_cache_sim": "cache_simulation",
}


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 1. Expansion sampling frame: 6 neutral candidate concepts
# ---------------------------------------------------------------------------
# Every candidate independently satisfies stratum mcw_python_component_v1
# (small_bounded_pure_python_component): pure Python, stdlib only,
# deterministic, one bounded module, objective semantics, public+private
# verification possible, no network/filesystem/database/clock/random, not
# derived from REDI-07/08 holdouts, not a clone of the five pilot tasks.
# Candidates are NOT scored by which worker might fail; they cover ordinary
# variety in the stratum (encoding, stack scanning, substitution, array
# transform, calendar arithmetic, predicate classification).

_STUB_BODY = 'raise NotImplementedError("assigned station implements this component")\n'


def _pkg_init(package: str) -> str:
    return f'"""{package} — MCW expansion component (baseline stub)."""\n'


def _signature_stub(*signatures: str) -> str:
    return "".join(f"{sig}\n    {_STUB_BODY}" for sig in signatures)


def _make_task(
    *,
    key: str,
    title: str,
    package: str,
    module_name: str,
    requirement_text: str,
    interface_markers: tuple[str, ...],
    module_source: str,
    reference: str,
    public_checks: tuple[Mapping[str, Any], ...],
    private_checks: tuple[Mapping[str, Any], ...],
    integration_probe: Mapping[str, Any],
    reviewer_checks: tuple[Mapping[str, Any], ...],
) -> Any:
    from .mcw_experiment import MCWTask

    return MCWTask(
        key=key,
        title=title,
        package=package,
        module_name=module_name,
        requirement_text=requirement_text,
        interface_markers=interface_markers,
        baseline_files={
            f"{package}/__init__.py": _pkg_init(package),
            f"{package}/{module_name}.py": module_source,
        },
        reference_implementation=reference,
        public_checks=public_checks,
        private_checks=private_checks,
        integration_probe=integration_probe,
        reviewer_checks=reviewer_checks,
    )


def _candidate_run_length_codec() -> Any:
    reference = (
        "def encode_rle(text):\n"
        '    """Run-length encode a non-empty letters-only string."""\n'
        "    if not text:\n"
        '        raise ValueError("text must be non-empty")\n'
        "    for ch in text:\n"
        '        if not (("a" <= ch <= "z") or ("A" <= ch <= "Z")):\n'
        '            raise ValueError("text must contain letters only")\n'
        "    out = []\n"
        "    i = 0\n"
        "    while i < len(text):\n"
        "        j = i\n"
        "        while j < len(text) and text[j] == text[i]:\n"
        "            j += 1\n"
        "        out.append(text[i] + str(j - i))\n"
        "        i = j\n"
        '    return "".join(out)\n'
        "\n"
        "def decode_rle(code):\n"
        '    """Decode a canonical run-length encoded string."""\n'
        "    if not code:\n"
        '        raise ValueError("code must be non-empty")\n'
        '    digits = "0123456789"\n'
        "    out = []\n"
        "    i = 0\n"
        "    while i < len(code):\n"
        "        ch = code[i]\n"
        '        if not (("a" <= ch <= "z") or ("A" <= ch <= "Z")):\n'
        '            raise ValueError("letter expected at run start")\n'
        "        j = i + 1\n"
        "        while j < len(code) and code[j] in digits:\n"
        "            j += 1\n"
        "        count_text = code[i + 1:j]\n"
        "        if not count_text or int(count_text) < 1:\n"
        '            raise ValueError("run count must be a positive integer")\n'
        "        out.append(ch * int(count_text))\n"
        "        i = j\n"
        '    return "".join(out)\n'
    )
    return _make_task(
        key="run_length_codec",
        title="Run-length encoding codec",
        package="run_length_codec",
        module_name="rle",
        requirement_text=(
            "Implement encode_rle(text) -> str and decode_rle(code) -> str. "
            "Encoding compresses a non-empty string of ASCII letters into "
            "runs: each maximal run of one repeated letter becomes the letter "
            "followed by its run length written in ASCII digits (a run of one "
            "is still written with count 1). Encoding raises ValueError for "
            "empty input or any non-letter character. Decoding inverts the "
            "encoding exactly: it parses letter-then-count units, expands "
            "each run, and raises ValueError for empty input, a missing or "
            "zero run count, or a unit that does not start with a letter. "
            "decode_rle(encode_rle(t)) == t for every valid input."
        ),
        interface_markers=("def encode_rle(text):", "def decode_rle(code):"),
        module_source=(
            "def encode_rle(text):\n"
            '    """Run-length encode a non-empty letters-only string."""\n'
            + _STUB_BODY
            + "\n"
            "def decode_rle(code):\n"
            '    """Decode a canonical run-length encoded string."""\n'
            + _STUB_BODY
        ),
        reference=reference,
        public_checks=(
            {"kind": "call_equals", "target": "encode_rle", "args": ["aaabbc"], "expected": "a3b2c1"},
            {"kind": "call_equals", "target": "encode_rle", "args": ["abc"], "expected": "a1b1c1"},
            {"kind": "call_equals", "target": "decode_rle", "args": ["a3b2c1"], "expected": "aaabbc"},
            {"kind": "call_equals", "target": "decode_rle", "args": ["x1y1"], "expected": "xy"},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "encode_rle", "args": ["WWWW"], "expected": "W4"},
            {"kind": "call_equals", "target": "encode_rle", "args": ["aAaA"], "expected": "a1A1a1A1"},
            {"kind": "call_equals", "target": "decode_rle", "args": ["a12b1"], "expected": "aaaaaaaaaaaab"},
            {"kind": "call_raises", "target": "encode_rle", "args": [""], "exception": "ValueError"},
            {"kind": "call_raises", "target": "decode_rle", "args": ["a0"], "exception": "ValueError"},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "decode_rle", "args": ["z2q3"], "expected": "zzqqq"}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "decode_rle", "args": ["b10"], "expected": "bbbbbbbbbb"},
            {"kind": "call_equals", "target": "encode_rle", "args": ["pq"], "expected": "p1q1"},
        ),
    )


def _candidate_paren_matcher() -> Any:
    reference = (
        '_OPEN = {"(": ")", "[": "]", "{": "}"}\n'
        "\n"
        "def first_unbalanced(s):\n"
        '    """Index of the first unbalanced bracket, or -1 when balanced."""\n'
        "    stack = []\n"
        "    for i, ch in enumerate(s):\n"
        "        if ch in _OPEN:\n"
        "            stack.append((i, ch))\n"
        "        elif ch in \")]}\" :\n"
        "            if not stack or _OPEN[stack[-1][1]] != ch:\n"
        "                return i\n"
        "            stack.pop()\n"
        "    return stack[0][0] if stack else -1\n"
    )
    return _make_task(
        key="paren_matcher",
        title="Bracket balance scanner",
        package="paren_matcher",
        module_name="balance",
        requirement_text=(
            "Implement first_unbalanced(s) -> int. The input is a string that "
            "may contain the brackets round (), square [], and curly {} plus "
            "arbitrary other characters, which are ignored. Scanning left to "
            "right, return the index of the first closer that does not match "
            "the most recent unclosed opener, or -1 immediately when such a "
            "closer is found; if the scan completes with openers still "
            "unclosed, return the index of the earliest unclosed opener; if "
            "every bracket is matched, return -1. The empty string is "
            "balanced."
        ),
        interface_markers=("def first_unbalanced(s):",),
        module_source=(
            "def first_unbalanced(s):\n"
            '    """Index of the first unbalanced bracket, or -1 when balanced."""\n'
            + _STUB_BODY
        ),
        reference=reference,
        public_checks=(
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["(a[b]{c})"], "expected": -1},
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["(]"], "expected": 1},
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["([)]"], "expected": 2},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["((("], "expected": 0},
            {"kind": "call_equals", "target": "first_unbalanced", "args": [")"], "expected": 0},
            {"kind": "call_equals", "target": "first_unbalanced", "args": [""], "expected": -1},
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["[{()}]"], "expected": -1},
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["(a(b)c"], "expected": 0},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["x = arr[i]; f(y)"], "expected": -1}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["{[}]"], "expected": 2},
            {"kind": "call_equals", "target": "first_unbalanced", "args": ["()[]{}"], "expected": -1},
        ),
    )


def _candidate_caesar_cipher() -> Any:
    reference = (
        "def shift_text(text, k):\n"
        '    """Shift letters by k positions; case preserved, others kept."""\n'
        "    k = k % 26\n"
        "    out = []\n"
        "    for ch in text:\n"
        '        if "a" <= ch <= "z":\n'
        "            out.append(chr((ord(ch) - 97 + k) % 26 + 97))\n"
        '        elif "A" <= ch <= "Z":\n'
        "            out.append(chr((ord(ch) - 65 + k) % 26 + 65))\n"
        "        else:\n"
        "            out.append(ch)\n"
        '    return "".join(out)\n'
    )
    return _make_task(
        key="caesar_cipher",
        title="Case-preserving letter shift",
        package="caesar_cipher",
        module_name="shift",
        requirement_text=(
            "Implement shift_text(text, k) -> str. Every lowercase ASCII "
            "letter is replaced by the letter k positions later in the "
            "alphabet and every uppercase letter likewise, preserving case; "
            "the shift wraps around the end of the alphabet, k is taken "
            "modulo 26 (any integer k is allowed, including zero and "
            "negative), and all non-letter characters are copied through "
            "unchanged. shift_text(shift_text(t, k), -k) == t."
        ),
        interface_markers=("def shift_text(text, k):",),
        module_source=(
            "def shift_text(text, k):\n"
            '    """Shift letters by k positions; case preserved, others kept."""\n'
            + _STUB_BODY
        ),
        reference=reference,
        public_checks=(
            {"kind": "call_equals", "target": "shift_text", "args": ["abc", 1], "expected": "bcd"},
            {"kind": "call_equals", "target": "shift_text", "args": ["xyz", 3], "expected": "abc"},
            {"kind": "call_equals", "target": "shift_text", "args": ["AbC", 2], "expected": "CdE"},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "shift_text", "args": ["Hello, World!", 13], "expected": "Uryyb, Jbeyq!"},
            {"kind": "call_equals", "target": "shift_text", "args": ["abc", 0], "expected": "abc"},
            {"kind": "call_equals", "target": "shift_text", "args": ["Abc", 52], "expected": "Abc"},
            {"kind": "call_equals", "target": "shift_text", "args": ["Zza", -1], "expected": "Yyz"},
            {"kind": "call_equals", "target": "shift_text", "args": ["", 7], "expected": ""},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "shift_text", "args": ["Rotation?", 1], "expected": "Spubujpo?"}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "shift_text", "args": ["mno", 13], "expected": "zab"},
            {"kind": "call_equals", "target": "shift_text", "args": ["MiXeD 42", 5], "expected": "RnCjI 42"},
        ),
    )


def _candidate_grid_rotate() -> Any:
    reference = (
        "def rotate_cw(grid):\n"
        '    """Rotate a square grid 90 degrees clockwise into a new grid."""\n'
        "    n = len(grid)\n"
        "    if n == 0 or any(len(row) != n for row in grid):\n"
        '        raise ValueError("grid must be a non-empty square")\n'
        "    return [[grid[n - 1 - c][r] for c in range(n)] for r in range(n)]\n"
    )
    return _make_task(
        key="grid_rotate",
        title="Square grid rotation",
        package="grid_rotate",
        module_name="rotate",
        requirement_text=(
            "Implement rotate_cw(grid) -> list[list[int]]. The input is an "
            "NxN grid given as a list of N row lists. Return a NEW grid "
            "containing the input rotated 90 degrees clockwise; the input "
            "grid must not be mutated. Raise ValueError for an empty grid or "
            "a ragged grid whose rows do not all have length N."
        ),
        interface_markers=("def rotate_cw(grid):",),
        module_source=(
            "def rotate_cw(grid):\n"
            '    """Rotate a square grid 90 degrees clockwise into a new grid."""\n'
            + _STUB_BODY
        ),
        reference=reference,
        public_checks=(
            {"kind": "call_equals", "target": "rotate_cw", "args": [[[1, 2], [3, 4]]], "expected": [[3, 1], [4, 2]]},
            {"kind": "call_equals", "target": "rotate_cw", "args": [[[1]]], "expected": [[1]]},
            {
                "kind": "call_equals",
                "target": "rotate_cw",
                "args": [[[1, 2, 3], [4, 5, 6], [7, 8, 9]]],
                "expected": [[7, 4, 1], [8, 5, 2], [9, 6, 3]],
            },
        ),
        private_checks=(
            {"kind": "call_raises", "target": "rotate_cw", "args": [[[1, 2], [3, 4], [5, 6], [7, 8], [9, 10]]], "exception": "ValueError"},
            {"kind": "call_raises", "target": "rotate_cw", "args": [[]], "exception": "ValueError"},
            {"kind": "call_raises", "target": "rotate_cw", "args": [[[1, 2, 3], [4, 5]]], "exception": "ValueError"},
            {
                "kind": "call_equals",
                "target": "rotate_cw",
                "args": [[[0, 0], [0, 0]]],
                "expected": [[0, 0], [0, 0]],
            },
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "rotate_cw", "args": [[[1, 2, 3, 4], [5, 6, 7, 8], [9, 10, 11, 12], [13, 14, 15, 16]]],
             "expected": [[13, 9, 5, 1], [14, 10, 6, 2], [15, 11, 7, 3], [16, 12, 8, 4]]}
        ),
        reviewer_checks=(
            {"kind": "call_raises", "target": "rotate_cw", "args": [[[1, 2, 3], [4, 5, 6]]], "exception": "ValueError"},
            {"kind": "call_equals", "target": "rotate_cw", "args": [[[7, 8], [9, 10]]], "expected": [[9, 7], [10, 8]]},
        ),
    )


def _candidate_weekday_span() -> Any:
    reference = (
        "from datetime import date, timedelta\n"
        "\n"
        "def count_weekdays(start_iso, end_iso):\n"
        '    """Count Mon-Fri days in the half-open ISO date range."""\n'
        "    start = date.fromisoformat(start_iso)\n"
        "    end = date.fromisoformat(end_iso)\n"
        "    if end < start:\n"
        '        raise ValueError("end must not be before start")\n'
        "    count = 0\n"
        "    current = start\n"
        "    one_day = timedelta(days=1)\n"
        "    while current < end:\n"
        "        if current.weekday() < 5:\n"
        "            count += 1\n"
        "        current += one_day\n"
        "    return count\n"
    )
    return _make_task(
        key="weekday_span",
        title="Weekday counting over a date range",
        package="weekday_span",
        module_name="span",
        requirement_text=(
            "Implement count_weekdays(start_iso, end_iso) -> int. Both "
            "arguments are calendar dates in ISO format YYYY-MM-DD; parse "
            "them and count how many days in the half-open range (including "
            "the start date, excluding the end date) are Monday through "
            "Friday. Raise ValueError when the end date is before the start "
            "date or when either argument is not a valid ISO date."
        ),
        interface_markers=("def count_weekdays(start_iso, end_iso):",),
        module_source=(
            "def count_weekdays(start_iso, end_iso):\n"
            '    """Count Mon-Fri days in the half-open ISO date range."""\n'
            + _STUB_BODY
        ),
        reference=reference,
        public_checks=(
            {"kind": "call_equals", "target": "count_weekdays", "args": ["2026-01-05", "2026-01-06"], "expected": 1},
            {"kind": "call_equals", "target": "count_weekdays", "args": ["2026-01-05", "2026-01-12"], "expected": 5},
            {"kind": "call_equals", "target": "count_weekdays", "args": ["2026-01-10", "2026-01-11"], "expected": 0},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "count_weekdays", "args": ["2026-03-01", "2026-03-01"], "expected": 0},
            {"kind": "call_equals", "target": "count_weekdays", "args": ["2026-01-29", "2026-02-03"], "expected": 3},
            {"kind": "call_raises", "target": "count_weekdays", "args": ["2026-02-10", "2026-02-09"], "exception": "ValueError"},
            {"kind": "call_raises", "target": "count_weekdays", "args": ["2026-13-40", "2026-12-24"], "exception": "ValueError"},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "count_weekdays", "args": ["2024-02-26", "2024-03-04"], "expected": 5}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "count_weekdays", "args": ["2026-01-01", "2026-01-02"], "expected": 1},
            {"kind": "call_equals", "target": "count_weekdays", "args": ["2026-12-28", "2027-01-04"], "expected": 5},
        ),
    )


def _candidate_triangle_classify() -> Any:
    reference = (
        "def classify_triangle(a, b, c):\n"
        '    """Classify a triangle from its three side lengths."""\n'
        "    if a <= 0 or b <= 0 or c <= 0:\n"
        '        raise ValueError("sides must be positive")\n'
        "    if a + b <= c or a + c <= b or b + c <= a:\n"
        '        return "degenerate"\n'
        "    if a == b == c:\n"
        '        return "equilateral"\n'
        "    if a == b or b == c or a == c:\n"
        '        return "isosceles"\n'
        '    return "scalene"\n'
    )
    return _make_task(
        key="triangle_classify",
        title="Triangle classification by sides",
        package="triangle_classify",
        module_name="shape",
        requirement_text=(
            "Implement classify_triangle(a, b, c) -> str. The arguments are "
            "the three side lengths of a triangle. If any side is zero or "
            "negative, raise ValueError. If the three sides cannot form a "
            "non-degenerate triangle (the longest side is at least the sum "
            "of the other two), return the exact string for a collapsed "
            "triangle. Otherwise return the exact classification string: "
            "all sides equal, exactly two sides equal, or all sides "
            "different."
        ),
        interface_markers=("def classify_triangle(a, b, c):",),
        module_source=(
            "def classify_triangle(a, b, c):\n"
            '    """Classify a triangle from its three side lengths."""\n'
            + _STUB_BODY
        ),
        reference=reference,
        public_checks=(
            {"kind": "call_equals", "target": "classify_triangle", "args": [3, 3, 3], "expected": "equilateral"},
            {"kind": "call_equals", "target": "classify_triangle", "args": [3, 4, 5], "expected": "scalene"},
            {"kind": "call_equals", "target": "classify_triangle", "args": [5, 5, 8], "expected": "isosceles"},
        ),
        private_checks=(
            {"kind": "call_equals", "target": "classify_triangle", "args": [1, 2, 3], "expected": "degenerate"},
            {"kind": "call_equals", "target": "classify_triangle", "args": [2.5, 2.5, 2.5], "expected": "equilateral"},
            {"kind": "call_equals", "target": "classify_triangle", "args": [10, 6, 4], "expected": "degenerate"},
            {"kind": "call_raises", "target": "classify_triangle", "args": [0, 1, 1], "exception": "ValueError"},
            {"kind": "call_equals", "target": "classify_triangle", "args": [2, 3, 2], "expected": "isosceles"},
        ),
        integration_probe=(
            {"kind": "call_equals", "target": "classify_triangle", "args": [7, 7, 7], "expected": "equilateral"}
        ),
        reviewer_checks=(
            {"kind": "call_equals", "target": "classify_triangle", "args": [4, 7, 9], "expected": "scalene"},
            {"kind": "call_raises", "target": "classify_triangle", "args": [-3, 4, 5], "exception": "ValueError"},
        ),
    )


CANDIDATE_BUILDERS: Mapping[str, Any] = {
    "run_length_codec": _candidate_run_length_codec,
    "paren_matcher": _candidate_paren_matcher,
    "caesar_cipher": _candidate_caesar_cipher,
    "grid_rotate": _candidate_grid_rotate,
    "weekday_span": _candidate_weekday_span,
    "triangle_classify": _candidate_triangle_classify,
}

CANDIDATE_FAMILIES: Mapping[str, str] = {
    "run_length_codec": "string_run_encoding",
    "paren_matcher": "stack_scan_validation",
    "caesar_cipher": "character_substitution",
    "grid_rotate": "array_transform",
    "weekday_span": "calendar_arithmetic",
    "triangle_classify": "predicate_classification",
}


def candidate_frame() -> tuple[Any, ...]:
    """The immutable 6-candidate frame, in fixed key order."""
    return tuple(CANDIDATE_BUILDERS[key]() for key in sorted(CANDIDATE_BUILDERS))


def sampling_frame_hash() -> str:
    payload = {
        "stratum_id": default_stratum().stratum_id,
        "candidates": [task.to_dict() for task in candidate_frame()],
    }
    return _hash(payload)


def expansion_seed() -> str:
    """Seed derived ONLY from immutable pre-result identifiers."""
    material = (
        task_set_hash()
        + default_stratum().canonical_hash
        + EXPANSION_SEED_DOMAIN_TAG
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def selection_ranks() -> dict[str, str]:
    seed = expansion_seed()
    return {
        key: hashlib.sha256(f"{seed}:{key}".encode("utf-8")).hexdigest()
        for key in sorted(CANDIDATE_BUILDERS)
    }


def select_expansion_tasks() -> tuple[str, ...]:
    """Deterministic 3-of-6 selection: smallest rank hex wins; key breaks ties."""
    ranks = selection_ranks()
    ordered = sorted(ranks, key=lambda k: (ranks[k], k))
    return tuple(ordered[:3])


def expansion_tasks() -> tuple[Any, ...]:
    """The 3 selected tasks in deterministic selection order."""
    builders = CANDIDATE_BUILDERS
    return tuple(builders[key]() for key in select_expansion_tasks())


def expanded_task_set() -> tuple[Any, ...]:
    """Original 5 immutable tasks + the 3 selected expansion tasks."""
    return tuple(default_task_set()) + tuple(expansion_tasks())


def expanded_task_set_hash() -> str:
    payload = {
        "task_set_id": EXPANDED_TASK_SET_ID,
        "version": EXPANDED_TASK_SET_VERSION,
        "parent_task_set_id": task_set_id(),
        "parent_task_set_sha256": task_set_hash(),
        "tasks": [task.to_dict() for task in expanded_task_set()],
    }
    return _hash(payload)


# ---------------------------------------------------------------------------
# 2. Expansion execution order: 9 cells, global positions 16-24
# ---------------------------------------------------------------------------


def expansion_execution_sequence() -> list[dict[str, Any]]:
    """Counterbalanced 3x3 order continuing the frozen rotation scheme.

    Global task indices continue at 5 (the pilot used 0..4), so the
    condition rotation offsets differ per task; no task always runs
    SMALL->MID->FRONTIER.
    """
    runtime = condition_runtime_by_id()
    sequence = []
    expansion_seq = 0
    for offset_index, task in enumerate(expansion_tasks()):
        global_index = 5 + offset_index
        order = condition_order_for_task(global_index, MCW_WORKER_CONDITIONS)
        for condition_id in order:
            expansion_seq += 1
            sequence.append(
                {
                    "cell_seq": 15 + expansion_seq,
                    "expansion_seq": expansion_seq,
                    "task_key": task.key,
                    "task_hash": task.canonical_hash,
                    "condition_id": condition_id,
                    "worker_id": runtime[condition_id]["worker_id"],
                    "model": runtime[condition_id]["model"],
                }
            )
    return sequence


# ---------------------------------------------------------------------------
# 3. Predeclared N=8 margin method (frozen before any expansion outcome)
# ---------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    max_it, eps, fpmin = 300, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, max_it + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _betainc_reg(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_beta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    bt = math.exp(ln_beta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _beta_inv(p: float, a: float, b: float) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _betainc_reg(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def clopper_pearson_interval(successes: int, n: int, alpha: float = 0.05) -> dict[str, Any]:
    """Exact two-sided Clopper-Pearson interval [lower, upper].

    Predeclared margin method for the N=8 determination.  Chosen before any
    expansion cell ran and justified independently of worker results: it is
    an exact small-sample interval with no asymptotic assumptions, matching
    N=8, and it consumes only the predeclared per-task verified-outcome
    definition - never REDI-09C outcomes.
    """
    if n <= 0 or successes < 0 or successes > n:
        raise ValueError("require 0 <= successes <= n and n > 0")
    lower = 0.0 if successes == 0 else _beta_inv(alpha / 2.0, successes, n - successes + 1)
    upper = 1.0 if successes == n else _beta_inv(1.0 - alpha / 2.0, successes + 1, n - successes)
    return {
        "method": MARGIN_METHOD_ID,
        "alpha": MARGIN_METHOD_ALPHA,
        "successes": successes,
        "n": n,
        "lower": round(lower, 6),
        "upper": round(upper, 6),
    }


# ---------------------------------------------------------------------------
# 4. V2 observability row schema (forward-only; V1 pilot rows stay valid)
# ---------------------------------------------------------------------------

V2_ADDED_FIELDS: tuple[str, ...] = (
    "schema_version",
    "expansion_seq",
    "provider_calls",
    "network_calls",
    "automatic_retry",
    "provider_completion_status",
    "finish_reason",
    "incomplete",
    "refusal",
    "reasoning_tokens",
    "pricing_evidence_state",
)


def _completion_status(provider_completion: Mapping[str, Any]) -> str:
    if provider_completion.get("refusal_present"):
        return "refused"
    if provider_completion.get("incomplete"):
        return "incomplete"
    if provider_completion.get("finish_reason"):
        return "completed"
    return "no_completion"


def _reasoning_tokens(provider_completion: Mapping[str, Any]) -> int | None:
    """Persist reasoning tokens only when the provider exposes them; never infer."""
    details = provider_completion.get("completion_token_details") or {}
    if "reasoning_tokens" in details:
        return int(details["reasoning_tokens"] or 0)
    return None


def build_expansion_row(
    *,
    cell: Mapping[str, Any],
    cell_result: Mapping[str, Any],
    gate_outcomes: Mapping[str, str],
    gate_reasons: Mapping[str, str],
    verified: bool,
    classification: str | None,
    usage: Mapping[str, int],
    cost: Mapping[str, Any],
    elapsed_seconds: float,
    called: bool,
) -> dict[str, Any]:
    """V2 row: V1 payload fields plus explicit forward-only observability."""
    provider_completion = cell_result.get("provider_completion", {})
    evidence_state = pricing_schedule()["evidence_state"][cell["model"]]
    row = {
        "schema_version": EXPANSION_ROW_SCHEMA_VERSION,
        "cell_seq": cell["cell_seq"],
        "expansion_seq": cell["expansion_seq"],
        "task_key": cell["task_key"],
        "condition_id": cell["condition_id"],
        "worker_id": cell["worker_id"],
        "model": cell["model"],
        "verified": verified,
        "gates": dict(gate_outcomes),
        "gate_reasons": dict(gate_reasons),
        "failure_classification": classification,
        "usage": {
            "input": int(usage.get("input", 0) or 0),
            "cached_input": int(usage.get("cached_input", 0) or 0),
            "output": int(usage.get("output", 0) or 0),
        },
        "cost": {
            "schedule_id": cost["schedule_id"],
            "schedule_hash": cost["schedule_hash"],
            "amount": cost["cost_amount"],
            "currency": cost["currency"],
            "calculation_version": cost["calculation_version"],
            "raw_usage": cost["raw_usage"],
            "pricing_evidence_state": cost["evidence_status"],
        },
        "context_bytes": cell_result["context"]["compiled_bytes"],
        "total_wall_seconds": elapsed_seconds,
        "stop_reason": None,
        "provider_calls": 1 if called else 0,
        "network_calls": 1 if called else 0,
        "automatic_retry": False,
        "provider_completion_status": _completion_status(provider_completion),
        "finish_reason": provider_completion.get("finish_reason"),
        "incomplete": bool(provider_completion.get("incomplete", False)),
        "refusal": bool(provider_completion.get("refusal_present", False)),
        "reasoning_tokens": _reasoning_tokens(provider_completion),
        "pricing_evidence_state": evidence_state,
    }
    return row


# ---------------------------------------------------------------------------
# 5. Expansion controller (rehearsal default; live strictly owner-gated)
# ---------------------------------------------------------------------------


class ExpansionController:
    """Deterministic 9-cell expansion executor (live gated; rehearsal default)."""

    def __init__(
        self,
        *,
        output_root: Path | str,
        owner_budget_ceiling: str | None = None,
        live: bool = False,
        scripted_outcomes: Mapping[str, str] | None = None,
        pinned_schedule_hash: str | None = None,
    ) -> None:
        self.root = Path(output_root).resolve()
        self.freeze = build_expansion_freeze()
        self.owner_budget_ceiling = owner_budget_ceiling
        self.live = live
        self.scripted_outcomes = dict(
            DEFAULT_EXPANSION_DRY_OUTCOMES if scripted_outcomes is None else scripted_outcomes
        )
        self.pinned_schedule_hash = pinned_schedule_hash or pricing_schedule_hash()

    # -- gates ------------------------------------------------------------

    def budget_gate(self, *, conservative_bound: Decimal) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        cells = self.freeze["execution_order"]
        if len(cells) != 9 or self.freeze["call_budget"]["max_provider_calls"] != 9:
            reasons.append("planned expansion calls must equal 9")
        if self.freeze["call_budget"]["automatic_retry"] is not False:
            reasons.append("automatic retry must be false")
        if self.freeze["pricing"]["schedule_hash"] != self.pinned_schedule_hash:
            reasons.append("pricing schedule is not pinned to the canonical hash")
        if self.owner_budget_ceiling is None:
            reasons.append("owner budget ceiling not supplied (never chosen silently)")
        else:
            try:
                if Decimal(str(self.owner_budget_ceiling)) < conservative_bound:
                    reasons.append(
                        f"owner ceiling {self.owner_budget_ceiling} is below the "
                        f"expansion conservative bound {conservative_bound}"
                    )
            except Exception:  # noqa: BLE001
                reasons.append("owner budget ceiling is not a valid decimal amount")
        return (not reasons), reasons

    def conservative_bound(self) -> str:
        conditions = {c: r["model"] for c, r in self.freeze["worker_conditions"].items()}
        return live_cost_plan(conditions=conditions, n_tasks=3)["experiment_hard_upper_bound"]

    def preflight(self) -> dict[str, Any]:
        conditions = {c: r["model"] for c, r in self.freeze["worker_conditions"].items()}
        plan = live_cost_plan(conditions=conditions, n_tasks=3)
        ok, reasons = self.budget_gate(conservative_bound=Decimal(plan["experiment_hard_upper_bound"]))
        return {
            "experiment_family": self.freeze["experiment_family"],
            "stage": self.freeze["stage"],
            "expansion_freeze_hash": self.freeze["freeze_hash"],
            "expanded_task_set_id": self.freeze["expanded_task_set"]["task_set_id"],
            "expanded_task_set_sha256": self.freeze["expanded_task_set"]["sha256"],
            "sampling_policy_id": self.freeze["sampling_policy"]["policy_id"],
            "selection_seed": self.freeze["sampling_policy"]["selection_seed"],
            "selected_task_ids": list(self.freeze["sampling_policy"]["selected_task_ids"]),
            "n_new_cells": len(self.freeze["execution_order"]),
            "max_provider_calls": self.freeze["call_budget"]["max_provider_calls"],
            "automatic_retry": self.freeze["call_budget"]["automatic_retry"],
            "row_schema_version": EXPANSION_ROW_SCHEMA_VERSION,
            "expected_expansion_cost": plan["experiment_expected_estimate"],
            "hard_expansion_bound": plan["experiment_hard_upper_bound"],
            "cumulative_n8_hard_bound": self.freeze["cost_bounds"]["cumulative_n8_hard_bound"],
            "owner_budget_ceiling": self.owner_budget_ceiling,
            "budget_gate_ok": ok,
            "budget_gate_reasons": reasons,
        }

    # -- execution ---------------------------------------------------------

    def run(self) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        state = _ExpansionState(self.root / "expansion_state.sqlite")
        if self.live:
            from .mcw_live_pilot import build_live_pilot_freeze  # noqa: F401  (parent pin)

            conservative = Decimal(self.conservative_bound())
            ok, reasons = self.budget_gate(conservative_bound=conservative)
            if not ok:
                raise RuntimeError("budget gate failed: " + "; ".join(reasons))
            if os_environ_live_flag() != "1":
                raise RuntimeError("live blocked: REDI_LIVE_PROVIDER=1 is required")
        rows: list[dict[str, Any]] = []
        for cell in self.freeze["execution_order"]:
            if state.is_completed(cell["cell_seq"]):
                continue  # completed expansion cells are immutable, never rerun
            row = self._execute_cell(cell)
            state.record(cell["cell_seq"], row)
            rows.append(row)
            if row.get("stop_reason"):
                break
        return {"rows": rows, "report": expansion_report(rows), "stop_reason": None}

    def _execute_cell(self, cell: Mapping[str, Any]) -> dict[str, Any]:
        task = next(t for t in expanded_task_set() if t.key == cell["task_key"])
        condition = next(
            c for c in MCW_WORKER_CONDITIONS if c.condition_id == cell["condition_id"]
        )
        cell_root = self.root / "conditions" / condition.condition_id / task.key
        workspace_root = cell_root / "workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        for rel, content in task.baseline_files.items():
            path = workspace_root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="")
        scripted = self.scripted_outcomes.get((condition.condition_id, task.key), "success")
        try:
            cell_result = _run_fake_condition_cell(
                task=task,
                condition=condition,
                workspace_root=workspace_root,
                scripted_outcome=scripted,
            )
        except Exception as exc:  # noqa: BLE001 - harness defects stop the expansion
            return {
                "cell_seq": cell["cell_seq"],
                "expansion_seq": cell["expansion_seq"],
                "task_key": cell["task_key"],
                "condition_id": cell["condition_id"],
                "verified": False,
                "stop_reason": "HARNESS_DEFECT",
                "stop_detail": f"{type(exc).__name__}: {exc}",
            }
        gate_outcomes, gate_reasons = _evaluate_cell_gates(
            task=task, cell=cell_result, workspace_root=workspace_root
        )
        verified = all(gate_outcomes[gate] == "PASS" for gate in GATE_ORDER)
        classification, _ = classify_mcw_failure(
            provider_incomplete=bool(cell_result["provider_completion"].get("incomplete")),
            gate_outcomes=gate_outcomes,
            structured_error=cell_result["structured_output"].get("safe_error"),
            unexpected_error=None,
        )
        usage = dict(FAKE_USAGE_BY_OUTCOME[scripted])
        cost = calculate_cost(model=condition.model, usage=usage)
        return build_expansion_row(
            cell=cell,
            cell_result=cell_result,
            gate_outcomes=gate_outcomes,
            gate_reasons=gate_reasons,
            verified=verified,
            classification=None if verified else classification,
            usage=usage,
            cost=cost,
            elapsed_seconds=cell_result["elapsed_seconds"],
            called=False,
        )


def os_environ_live_flag() -> str:
    import os

    return os.environ.get("REDI_LIVE_PROVIDER", "")


class _ExpansionState:
    """Durable per-cell expansion state: completed cells immutable, never rerun."""

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expansion_cells (
                cell_seq INTEGER PRIMARY KEY,
                cell_id TEXT NOT NULL,
                status TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                row_json TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def is_completed(self, cell_seq: int) -> bool:
        row = self.conn.execute(
            "SELECT status FROM expansion_cells WHERE cell_seq=?", (cell_seq,)
        ).fetchone()
        return bool(row and row[0] == "completed")

    def record(self, cell_seq: int, row: Mapping[str, Any]) -> None:
        if self.is_completed(cell_seq):
            raise RuntimeError(
                f"expansion cell {cell_seq} is already completed; completed cells are immutable"
            )
        encoded = json.dumps(dict(row), sort_keys=True, default=str)
        self.conn.execute(
            "INSERT INTO expansion_cells (cell_seq, cell_id, status, result_sha256, row_json)"
            " VALUES (?, ?, 'completed', ?, ?)",
            (
                cell_seq,
                f"{row.get('condition_id')}::{row.get('task_key')}",
                hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                encoded,
            ),
        )
        self.conn.commit()


# Neutral, arbitrary rehearsal fixtures (harness validation only; they do not
# model or target any pilot result).
DEFAULT_EXPANSION_DRY_OUTCOMES: Mapping[tuple[str, str], str] = {
    **{(cond, key): "success" for cond in ("SMALL", "MID", "FRONTIER") for key in CANDIDATE_BUILDERS},
    ("SMALL", "paren_matcher"): "implementation_failure",
    ("MID", "triangle_classify"): "provider_incomplete",
}


# ---------------------------------------------------------------------------
# 6. N=8 aggregation plan: locked first 5 + new 3, never reinterpreted
# ---------------------------------------------------------------------------


def load_pilot_rows_v1(pilot_state_path: Path | str) -> list[dict[str, Any]]:
    """Read-only load of the immutable REDI-09C V1 rows."""
    conn = sqlite3.connect(
        "file:" + Path(pilot_state_path).resolve().as_posix() + "?mode=ro&immutable=1",
        uri=True,
    )
    try:
        return [
            json.loads(rj)
            for (rj,) in conn.execute("SELECT row_json FROM pilot_cells ORDER BY cell_seq")
        ]
    finally:
        conn.close()


def _normalise(row: Mapping[str, Any], schema_version: int) -> dict[str, Any]:
    return {
        "schema_version": schema_version,
        "cell_seq": row["cell_seq"],
        "task_key": row["task_key"],
        "condition_id": row["condition_id"],
        "model": row["model"],
        "verified": row["verified"],
        "failure_classification": row.get("failure_classification"),
        "usage": row["usage"],
        "reasoning_tokens": row.get("reasoning_tokens"),
        "total_wall_seconds": row.get("total_wall_seconds", 0.0) or 0.0,
        "cost_amount": row["cost"]["amount"],
        "pricing_evidence_state": row.get(
            "pricing_evidence_state",
            pricing_schedule()["evidence_state"].get(row["model"], "NOT_IN_PINNED_SCHEDULE"),
        ),
    }


def n8_aggregation(pilot_rows: Sequence[Mapping[str, Any]], expansion_rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine locked V1 first-5 rows with V2 expansion rows per condition.

    The first five are consumed exactly as persisted; they are never
    reinterpreted, reclassified, or re-costed under a different schedule.
    """
    normalised = [_normalise(r, 1) for r in pilot_rows] + [
        _normalise(r, 2) for r in expansion_rows
    ]
    evidence = pricing_schedule()["evidence_state"]
    frontier = []
    for condition in MCW_WORKER_CONDITIONS:
        rows = [r for r in normalised if r["condition_id"] == condition.condition_id]
        verified = sum(1 for r in rows if r["verified"])
        failures: dict[str, int] = {}
        for r in rows:
            if r["verified"] is not True and r["failure_classification"]:
                key = str(r["failure_classification"])
                failures[key] = failures.get(key, 0) + 1
        reasoning_values = [r["reasoning_tokens"] for r in rows if r["reasoning_tokens"] is not None]
        interval = clopper_pearson_interval(verified, len(rows)) if len(rows) == MCW_SUFFICIENCY_MIN_TASKS else None
        frontier.append(
            {
                "worker_condition": condition.condition_id,
                "model": condition.model,
                "n_tasks": len(rows),
                "verified_successes": verified,
                "failures_by_class": failures,
                "observed_success_proportion": (verified / len(rows)) if rows else None,
                "token_totals": {
                    "input": sum(r["usage"]["input"] for r in rows),
                    "cached": sum(r["usage"].get("cached_input", 0) for r in rows),
                    "output": sum(r["usage"]["output"] for r in rows),
                },
                "reasoning_tokens_total": (
                    sum(reasoning_values) if reasoning_values else None
                ),
                "reasoning_tokens_note": (
                    "sum over rows where the provider exposed reasoning tokens;"
                    " V1 pilot rows did not persist reasoning tokens"
                    if reasoning_values
                    else "not available: no row persisted reasoning tokens"
                ),
                "latency_seconds_total": sum(r["total_wall_seconds"] for r in rows),
                "cost": {
                    "amount": str(
                        sum((Decimal(r["cost_amount"]) for r in rows), Decimal("0"))
                    ),
                    "currency": "USD",
                    "schedule_id": pricing_schedule()["schedule_id"],
                    "per_model_pricing_evidence_state": evidence[condition.model],
                },
                "uncertainty_metadata": interval
                or {
                    "method": MARGIN_METHOD_ID,
                    "status": "NOT_COMPUTABLE_AT_N<8",
                },
                "criterion_satisfaction_state": frontier_state(
                    verified_count=verified,
                    n_tasks=len(rows),
                    observed_verified_failure=any(failures),
                    margin_estimated=interval is not None,
                ),
            }
        )
    return {
        "experiment_family": "MCW",
        "combination": "locked_first_5 (V1, immutable) + expansion_3 (V2)",
        "min_tasks_for_sufficiency": MCW_SUFFICIENCY_MIN_TASKS,
        "frontier": frontier,
        "claims": {
            "no_winner_selected": True,
            "minimum_capable_declared": False,
            "final_mcw_scope": {
                "station": default_stratum().station_id,
                "task_class": default_stratum().task_class,
                "experiment": "REDI-09 N8 expansion",
            },
        },
    }


def expansion_report(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "stage": "EXPANSION_REHEARSAL_RESULTS",
        "cells_executed": len(rows),
        "schema_version": EXPANSION_ROW_SCHEMA_VERSION,
        "frontier": [
            {
                "worker_condition": condition.condition_id,
                "n_tasks": sum(1 for r in rows if r.get("condition_id") == condition.condition_id),
                "verified": sum(
                    1 for r in rows if r.get("condition_id") == condition.condition_id and r.get("verified")
                ),
            }
            for condition in MCW_WORKER_CONDITIONS
        ],
        "claims": {
            "pilot_only": True,
            "no_winner_selected": True,
            "minimum_capable_declared": False,
        },
    }


# ---------------------------------------------------------------------------
# 7. Expansion freeze manifest (hash-frozen before any provider call)
# ---------------------------------------------------------------------------


def build_expansion_freeze() -> dict[str, Any]:
    """The complete frozen expansion definition, hash-bound and deterministic."""
    stratum = default_stratum()
    original_tasks = tuple(default_task_set())
    new_tasks = expansion_tasks()
    all_tasks = original_tasks + new_tasks
    conditions_map = {c: r["model"] for c, r in condition_runtime_by_id().items()}
    expansion_plan = live_cost_plan(conditions=conditions_map, n_tasks=3)
    n8_plan = live_cost_plan(conditions=conditions_map, n_tasks=8)
    sequence = expansion_execution_sequence()
    selected = select_expansion_tasks()
    freeze = {
        "experiment_family": "MCW",
        "policy": {"id": EXPANSION_POLICY_ID, "version": EXPANSION_POLICY_VERSION},
        "stage": EXPANSION_STAGE,
        "source_revision": EVIDENCE_BASE_REVISION,
        "stratum": stratum.to_dict(),
        "stratum_sha256": stratum.canonical_hash,
        "parent_bindings": {
            "redi09a_manifest_sha256": ACCEPTED_REDI09A_MANIFEST_SHA256,
            "redi09b_live_pilot_freeze_sha256": ACCEPTED_REDI09B_FREEZE_SHA256,
            "redi09c_task_set_sha256": ACCEPTED_REDI09C_TASK_SET_SHA256,
            "redi09c_pilot_state_sha256": ACCEPTED_REDI09C_PILOT_STATE_SHA256,
            "redi09c1_audit_verdict": ACCEPTED_REDI09C1_AUDIT_VERDICT,
        },
        "sampling_policy": {
            "policy_id": EXPANSION_SAMPLING_POLICY_ID,
            "policy_version": EXPANSION_SAMPLING_POLICY_VERSION,
            "frame_hash": sampling_frame_hash(),
            "frame_candidate_keys": sorted(CANDIDATE_BUILDERS),
            "seed_derivation": (
                "SHA256(original_task_set_sha256 + stratum_sha256 + "
                "'REDI-09D-expansion-v1'); inputs are pre-result identifiers only"
            ),
            "selection_seed": expansion_seed(),
            "selection_rule": (
                "rank each candidate key by SHA256(selection_seed + ':' + key); "
                "take the 3 smallest ranks; ties broken by key order"
            ),
            "selection_ranks": selection_ranks(),
            "selected_task_ids": list(selected),
            "result_independence": (
                "seed, frame, and ranks exclude pilot outcomes, worker ids, "
                "model names, and failure diagnostics"
            ),
        },
        "original_task_set": {
            "task_set_id": task_set_id(),
            "sha256": task_set_hash(),
            "n_tasks": len(original_tasks),
            "task_hashes": {t.key: t.canonical_hash for t in original_tasks},
            "immutable": True,
        },
        "expansion_tasks": [
            {"key": t.key, "sha256": t.canonical_hash, "family": CANDIDATE_FAMILIES[t.key]}
            for t in new_tasks
        ],
        "expanded_task_set": {
            "task_set_id": EXPANDED_TASK_SET_ID,
            "version": EXPANDED_TASK_SET_VERSION,
            "parent_task_set_id": task_set_id(),
            "parent_task_set_sha256": task_set_hash(),
            "sha256": expanded_task_set_hash(),
            "n_tasks": len(all_tasks),
            "task_hashes": {t.key: t.canonical_hash for t in all_tasks},
        },
        "worker_conditions": condition_runtime_by_id(),
        "context_policy": {
            "id": "mcw_component_context_v1",
            "budget_bytes": 6000,
            "includes": ["task requirement", "interface signatures", "builder output contract"],
            "excludes": [
                "private checks",
                "reference implementation",
                "other-condition results",
                "prior-cell outcomes (result blindness)",
            ],
        },
        "output_contract": {"policy_id": "redi_builder_patch_contract_v1", "policy_version": 1},
        "verification_gates": list(GATE_ORDER),
        "quality_criterion": dict(MCW_QUALITY_CRITERION),
        "aggregate_sufficiency_rule": {
            "rule_id": "redi09a_frontier_state_v1",
            "source": (
                "predeclared in REDI-09A (commit 7c2955d) before any live result; "
                "reused exactly, not reinvented"
            ),
            "per_task_quality_criterion": (
                "a task is verified iff all 9 required gates PASS"
            ),
            "min_tasks": MCW_SUFFICIENCY_MIN_TASKS,
            "transitions": {
                "NOT_SUFFICIENT_OBSERVED": "any observed verified failure at any N",
                "CANDIDATE_SUFFICIENT": (
                    "all N >= 8 tasks verified AND aggregate margin estimated"
                ),
                "INSUFFICIENT_EVIDENCE": "everything else",
            },
            "allowed_verified_failures_for_candidate_sufficient": 0,
            "margin_method": {
                "id": MARGIN_METHOD_ID,
                "alpha": MARGIN_METHOD_ALPHA,
                "predeclared_in": "REDI-09D (before any expansion outcome existed)",
                "justification": (
                    "exact small-sample binomial interval; no asymptotic "
                    "assumptions; consumes only the predeclared per-task "
                    "verified-outcome definition, never REDI-09C outcomes"
                ),
            },
            "final_mcw_scope": {
                "station": stratum.station_id,
                "task_class": stratum.task_class,
                "experiment": "REDI-09 N8 expansion",
                "broader_inference": "forbidden",
            },
        },
        "execution_order": sequence,
        "call_budget": {
            "planned_cells": len(sequence),
            "max_provider_calls": len(sequence),
            "per_cell_max_calls": 1,
            "automatic_retry": False,
            "worker_fallback": "forbidden",
            "historical_pilot_calls_immutable": 15,
        },
        "stop_rules": {
            "continue_outcome_classes": [
                "WORKER_IMPLEMENTATION_FAILURE",
                "PROVIDER_OUTPUT_TRUNCATED_OR_INCOMPLETE",
                "SCOPE_VIOLATION",
                "REVIEWER_REJECTION",
                "STRUCTURED_OUTPUT_SCHEMA_FAILURE",
            ],
            "stop_conditions": [
                "HARNESS_DEFECT",
                "EVIDENCE_CORRUPTION",
                "PRICING_MISMATCH",
                "MANIFEST_DRIFT",
                "WRONG_WORKER_OR_RUNTIME",
            ],
            "no_paid_automatic_retries": True,
        },
        "resume_policy": {
            "completed_cells_immutable": True,
            "rerun_completed_cell": "forbidden",
            "historical_pilot_cells_immutable": True,
            "resume_may_rerun_pilot_cell": False,
            "restart_continues_at_next_expansion_position": True,
        },
        "result_blindness": {
            "cell_configuration_fixed_before_first_cell": True,
            "adaptive_routing": "forbidden",
            "orchestrator_may_not_alter": [
                "prompts",
                "context",
                "worker_selection",
                "budgets",
                "task_ordering",
                "criteria",
            ],
        },
        "pricing": {
            "schedule_id": pricing_schedule()["schedule_id"],
            "schedule_hash": pricing_schedule_hash(),
            "currency": pricing_schedule()["currency"],
            "evidence_state": pricing_schedule()["evidence_state"],
            "historical_and_expansion_cells_comparable": True,
        },
        "cost_bounds": {
            "expansion_expected_estimate": expansion_plan["experiment_expected_estimate"],
            "expansion_hard_upper_bound": expansion_plan["experiment_hard_upper_bound"],
            "cumulative_n8_expected_estimate": n8_plan["experiment_expected_estimate"],
            "cumulative_n8_hard_bound": n8_plan["experiment_hard_upper_bound"],
            "owner_ceiling": "chosen later by the owner, never by the harness",
        },
        "observability": {
            "row_schema_version": EXPANSION_ROW_SCHEMA_VERSION,
            "v1_rows_remain_valid": True,
            "v1_rows_backfilled_or_mutated": False,
            "fields_added": list(V2_ADDED_FIELDS),
            "reasoning_token_rule": (
                "persist only provider-exposed reasoning tokens; null when the "
                "runtime does not expose them; never inferred"
            ),
            "pricing_evidence_state_rule": (
                "persist the literal per-model evidence state on every new cost row"
            ),
        },
        "claim_boundary": {
            "pilot_only": True,
            "no_winner_selected": True,
            "minimum_capable_declared_here": False,
            "allowed_final_states": [
                "NOT_SUFFICIENT_OBSERVED",
                "CANDIDATE_SUFFICIENT",
                "INSUFFICIENT_EVIDENCE",
            ],
            "forbidden_claims": [
                "superlative quality claims about tested models",
                "any winning-model-across-conditions claim",
                "percent-of-intelligence numeric claims",
                "cost-minimum claims across conditions",
                "any capability ordering of tested models",
            ],
            "lowest_cost_claim_allowed_only_as": (
                "lowest-cost tested worker satisfying the frozen criterion for "
                "station/station_mcw_implementation_v0, task class "
                "mcw_python_component_v1, experiment REDI-09 N8 expansion"
            ),
        },
    }
    freeze["freeze_hash"] = _hash(freeze)
    return freeze


# ---------------------------------------------------------------------------
# 8. Read-only replay (V1 + V2), independent of worker registry state
# ---------------------------------------------------------------------------


def replay_n8(pilot_rows: Sequence[Mapping[str, Any]], expansion_rows: Sequence[Mapping[str, Any]]) -> str:
    """Deterministic digest over historical V1 rows and V2 expansion rows.

    Uses only persisted row content and the code-pinned pricing schedule; a
    worker-registry change cannot influence the digest.
    """
    normalised = [_normalise(r, 1) for r in pilot_rows] + [
        _normalise(r, 2) for r in expansion_rows
    ]
    payload = {
        "rows": normalised,
        "schedule_hash": pricing_schedule_hash(),
        "trajectory": [
            [r["cell_seq"], r["task_key"], r["condition_id"], r["model"]] for r in normalised
        ],
    }
    return _hash(payload)


# ---------------------------------------------------------------------------
# 9. REDI-09F — triangle_classify_v2 rerun preregistration (forward-only)
# ---------------------------------------------------------------------------
# REDI-09E1 confirmed TASK_SPECIFICATION_DEFECT: the v1 requirement described
# the classes by phrase ("all sides equal, exactly two sides equal, all sides
# different") while the tests require the exact tokens.  Cells 19/20/21 are
# preserved immutably and excluded from capability evidence.  v2 changes ONLY
# the requirement_text so the four exact labels are stated; everything else
# is inherited from v1 by construction (dataclasses.replace).

ACCEPTED_REDI09E_EXPANSION_STATE_SHA256 = "1f4074d78129df6dbcb76ca77528b5f2be6ab17a7f229076caaa86c4a6a1e1cd"
ACCEPTED_REDI09D_MANIFEST_SHA256 = "c7d5606c340911fcbf5f2a233ac968de3768b1ef5eae13f108c9c23bc4bfa335"
ACCEPTED_REDI09E1_VERDICT = "TASK_SPECIFICATION_DEFECT_CONFIRMED"
RERUN_TASK_SET_ID = "mcw_python_component_v1_expanded_n8_r2"
RERUN_TASK_SET_VERSION = 2
RERUN_POLICY_ID = "redi09f_triangle_v2_rerun_v0"
RERUN_STAGE = "REDI-09F_TRIANGLE_V2_RERUN_FREEZE"
EXCLUDED_V1_TRIANGLE_CELLS = (19, 20, 21)

TRIANGLE_V2_REQUIREMENT_TEXT = (
    "Implement classify_triangle(a, b, c) -> str. The arguments are "
    "the three side lengths of a triangle. If any side is zero or "
    "negative, raise ValueError. If the three sides cannot form a "
    "non-degenerate triangle (the longest side is at least the sum "
    "of the other two), return exactly the string \"degenerate\". "
    "Otherwise return exactly one of these strings: \"equilateral\" "
    "(all sides equal), \"isosceles\" (exactly two sides equal), or "
    "\"scalene\" (all sides different)."
)


def triangle_classify_v2_task():
    """v2 = v1 with ONLY key and requirement_text changed (provably minimal)."""
    import dataclasses

    v1 = CANDIDATE_BUILDERS["triangle_classify"]()
    return dataclasses.replace(
        v1, key="triangle_classify_v2", requirement_text=TRIANGLE_V2_REQUIREMENT_TEXT
    )


def rerun_task_set() -> tuple[Any, ...]:
    """N8 revision 2: original 5 + grid_rotate + caesar_cipher + triangle v2."""
    return (
        tuple(default_task_set())
        + (CANDIDATE_BUILDERS["grid_rotate"](), CANDIDATE_BUILDERS["caesar_cipher"]())
        + (triangle_classify_v2_task(),)
    )


def rerun_task_set_hash() -> str:
    payload = {
        "task_set_id": RERUN_TASK_SET_ID,
        "version": RERUN_TASK_SET_VERSION,
        "parent_task_set_id": EXPANDED_TASK_SET_ID,
        "parent_task_set_sha256": expanded_task_set_hash(),
        "tasks": [task.to_dict() for task in rerun_task_set()],
    }
    return _hash(payload)


def rerun_execution_sequence() -> list[dict[str, Any]]:
    """Exact 3-cell rerun order: 25 SMALL, 26 MID, 27 FRONTIER.

    The v1 triangle cells ran SMALL->MID->FRONTIER (19/20/21); the rerun
    preserves that order as a paired replacement for direct per-condition
    comparability.  Global sequence numbering continues at 25.
    """
    runtime = condition_runtime_by_id()
    sequence = []
    for rerun_seq, condition_id in enumerate(("SMALL", "MID", "FRONTIER"), start=1):
        task = triangle_classify_v2_task()
        sequence.append(
            {
                "cell_seq": 24 + rerun_seq,
                # expansion_seq aliases rerun_seq so the frozen V2 row builder
                # (build_expansion_row) accepts rerun cells unchanged
                "expansion_seq": rerun_seq,
                "rerun_seq": rerun_seq,
                "task_key": task.key,
                "task_hash": task.canonical_hash,
                "condition_id": condition_id,
                "worker_id": runtime[condition_id]["worker_id"],
                "model": runtime[condition_id]["model"],
            }
        )
    return sequence


def v2_delta_audit() -> dict[str, Any]:
    """Prove v1 -> v2 changes ONLY key and requirement_text."""
    v1 = CANDIDATE_BUILDERS["triangle_classify"]()
    v2 = triangle_classify_v2_task()
    d1, d2 = v1.to_dict(), v2.to_dict()
    changed = sorted(k for k in d1 if json.dumps(d1[k], sort_keys=True) != json.dumps(d2.get(k), sort_keys=True))
    return {
        "changed_fields": changed,
        "expected_changed_fields": ["key", "requirement_text"],
        "minimal": changed == ["key", "requirement_text"],
        "labels_stated_in_v2": all(
            label in d2["requirement_text"]
            for label in ("\"degenerate\"", "\"equilateral\"", "\"isosceles\"", "\"scalene\"")
        ),
        "labels_in_v1_context": all(
            label in _compile_context(v1)["context_body"]
            for label in ("equilateral", "isosceles", "scalene")
        ),
    }


def build_rerun_freeze() -> dict[str, Any]:
    """Frozen REDI-09F rerun manifest (deterministic; hash-bound)."""
    stratum = default_stratum()
    v2 = triangle_classify_v2_task()
    conditions_map = {c: r["model"] for c, r in condition_runtime_by_id().items()}
    hard_bounds = {
        cid: cell_cost_bound(model=model)["hard_upper_bound"]["cost_amount"]
        for cid, model in conditions_map.items()
    }
    expected_bounds = {
        cid: cell_cost_bound(model=model)["expected_estimate"]["cost_amount"]
        for cid, model in conditions_map.items()
    }
    freeze = {
        "experiment_family": "MCW",
        "policy": {"id": RERUN_POLICY_ID, "version": 1},
        "stage": RERUN_STAGE,
        "source_revision": EVIDENCE_BASE_REVISION,
        "stratum": stratum.to_dict(),
        "parent_bindings": {
            "redi09d_expansion_manifest_sha256": ACCEPTED_REDI09D_MANIFEST_SHA256,
            "redi09e_expansion_state_sha256": ACCEPTED_REDI09E_EXPANSION_STATE_SHA256,
            "redi09e1_verdict": ACCEPTED_REDI09E1_VERDICT,
        },
        "exclusion_policy": {
            "excluded_cells": [
                {"cell_seq": seq, "task_key": "triangle_classify",
                 "condition_id": cond,
                 "reason": "TASK_SPECIFICATION_DEFECT_CONFIRMED (REDI-09E1)",
                 "preserved_immutably": True,
                 "excluded_from_capability_evidence": True}
                for seq, cond in ((19, "SMALL"), (20, "MID"), (21, "FRONTIER"))
            ],
            "cost_usage_runtime_evidence_retained": True,
            "reclassified_as_worker_weakness": False,
            "valid_tasks_per_condition_before_rerun": 7,
        },
        "v2_task": {
            "key": v2.key,
            "sha256": v2.canonical_hash,
            "delta_audit": v2_delta_audit(),
        },
        "rerun_task_set": {
            "task_set_id": RERUN_TASK_SET_ID,
            "version": RERUN_TASK_SET_VERSION,
            "parent_task_set_id": EXPANDED_TASK_SET_ID,
            "parent_task_set_sha256": expanded_task_set_hash(),
            "sha256": rerun_task_set_hash(),
            "n_tasks": len(rerun_task_set()),
        },
        "worker_conditions": condition_runtime_by_id(),
        "context_policy": {
            "id": "mcw_component_context_v1",
            "budget_bytes": 6000,
            "includes": ["task requirement", "interface signatures", "builder output contract"],
            "excludes": [
                "private checks",
                "reference implementation",
                "other-condition results",
                "prior-cell outcomes (result blindness)",
            ],
        },
        "output_contract": {"policy_id": "redi_builder_patch_contract_v1", "policy_version": 1},
        "verification_gates": list(GATE_ORDER),
        "quality_criterion": dict(MCW_QUALITY_CRITERION),
        "aggregate_sufficiency_rule": build_expansion_freeze()["aggregate_sufficiency_rule"],
        "execution_order": rerun_execution_sequence(),
        "call_budget": {
            "planned_cells": 3,
            "max_provider_calls": 3,
            "per_cell_max_calls": 1,
            "automatic_retry": False,
            "worker_fallback": "forbidden",
            "historical_cells_immutable": "01-24",
        },
        "stop_rules": build_expansion_freeze()["stop_rules"],
        "resume_policy": build_expansion_freeze()["resume_policy"],
        "result_blindness": build_expansion_freeze()["result_blindness"],
        "pricing": {
            "schedule_id": pricing_schedule()["schedule_id"],
            "schedule_hash": pricing_schedule_hash(),
            "currency": pricing_schedule()["currency"],
            "evidence_state": pricing_schedule()["evidence_state"],
        },
        "cost_bounds": {
            "per_cell_hard_bound": hard_bounds,
            "per_cell_expected_estimate": expected_bounds,
            "rerun_expected_estimate": str(
                sum((Decimal(v) for v in expected_bounds.values()), Decimal("0"))
            ),
            "rerun_hard_upper_bound": str(
                sum((Decimal(v) for v in hard_bounds.values()), Decimal("0"))
            ),
            "owner_ceiling": "chosen later by the owner, never by the harness",
        },
        "observability": build_expansion_freeze()["observability"],
        "claim_boundary": build_expansion_freeze()["claim_boundary"],
    }
    freeze["freeze_hash"] = _hash(freeze)
    return freeze


# ---------------------------------------------------------------------------
# 10. REDI-09F rerun replay
# ---------------------------------------------------------------------------


def replay_rerun(pilot_rows, expansion_rows, rerun_rows) -> str:
    """Deterministic digest over V1 pilot + V2 expansion (minus excluded) + V2 rerun."""
    kept_expansion = [r for r in expansion_rows if r.get("cell_seq") not in EXCLUDED_V1_TRIANGLE_CELLS]
    normalised = (
        [_normalise(r, 1) for r in pilot_rows]
        + [_normalise(r, 2) for r in kept_expansion]
        + [_normalise(r, 2) for r in rerun_rows]
    )
    payload = {
        "rows": normalised,
        "schedule_hash": pricing_schedule_hash(),
        "trajectory": [[r["cell_seq"], r["task_key"], r["condition_id"], r["model"]] for r in normalised],
    }
    return _hash(payload)


__all__ = [
    "ACCEPTED_REDI09E1_VERDICT",
    "ACCEPTED_REDI09E_EXPANSION_STATE_SHA256",
    "EXCLUDED_V1_TRIANGLE_CELLS",
    "RERUN_POLICY_ID",
    "RERUN_STAGE",
    "RERUN_TASK_SET_ID",
    "TRIANGLE_V2_REQUIREMENT_TEXT",
    "build_rerun_freeze",
    "rerun_execution_sequence",
    "rerun_task_set",
    "rerun_task_set_hash",
    "replay_rerun",
    "triangle_classify_v2_task",
    "v2_delta_audit",

    "ACCEPTED_REDI09A_MANIFEST_SHA256",
    "ACCEPTED_REDI09B_FREEZE_SHA256",
    "ACCEPTED_REDI09C_PILOT_STATE_SHA256",
    "ACCEPTED_REDI09C_TASK_SET_SHA256",
    "CANDIDATE_BUILDERS",
    "CANDIDATE_FAMILIES",
    "DEFAULT_EXPANSION_DRY_OUTCOMES",
    "EXPANDED_TASK_SET_ID",
    "EXPANSION_POLICY_ID",
    "EXPANSION_ROW_SCHEMA_VERSION",
    "EXPANSION_SAMPLING_POLICY_ID",
    "EXPANSION_SEED_DOMAIN_TAG",
    "EVIDENCE_BASE_REVISION",
    "MARGIN_METHOD_ID",
    "V2_ADDED_FIELDS",
    "ExpansionController",
    "build_expansion_freeze",
    "build_expansion_row",
    "clopper_pearson_interval",
    "candidate_frame",
    "expansion_execution_sequence",
    "expansion_report",
    "expansion_seed",
    "expansion_tasks",
    "expanded_task_set",
    "expanded_task_set_hash",
    "load_pilot_rows_v1",
    "n8_aggregation",
    "replay_n8",
    "sampling_frame_hash",
    "select_expansion_tasks",
    "selection_ranks",
]
