"""Acceptance checks follow the ProductSpec's declared semantics.

A prohibition such as "cannot move backward" must not become a required
reverse move, and filtered summaries are only checked where the trusted
runtime renders them (primary view and dashboards).
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from redi_lab.workbench.professional.review import (
    NEGATED_REVERSE, NEGATED_REVERSE_SHORT, has_reverse_transition, requirements,
)
from redi_lab.workbench.professional.runtime import presentation_contract


ROOT = Path(__file__).parents[2]
BROWSER = ROOT / 'redi_lab' / 'workbench' / 'professional' / 'acceptance_browser.js'
FORWARD_ONLY = 'A repair job can move from new to in progress and from in progress to done, but cannot move backward.'
FORWARD_FLOW = {'entity': 'video', 'field': 'status', 'initial': 'Idea',
                'transitions': {'Idea': ['Script'], 'Script': ['Production'], 'Production': ['Ready'], 'Ready': ['Published'], 'Published': []}}
REVERSIBLE_FLOW = {**FORWARD_FLOW, 'transitions': {**FORWARD_FLOW['transitions'], 'Script': ['Production', 'Idea']}}


def workshop_spec():
    """Minimal forward-only workshop spec with metrics on the dashboard only."""
    spec = json.loads((ROOT / 'build/workbench-builds/wb-6f4a8ab91ec6/product_spec.json').read_text(encoding='utf-8'))['spec']
    spec = copy.deepcopy(spec)
    spec['acceptance_criteria'] = [FORWARD_ONLY]
    return spec


def assertion_ids(spec):
    return {key for row in requirements(spec) for key in row['assertion_ids']}


@pytest.mark.parametrize('text, negated', [
    (FORWARD_ONLY, True),
    ('Jobs must not go back to an earlier stage.', True),
    ('Done jobs can never return backward.', True),
    ('A repair job can move from new to in progress and from in progress to done, but not backward.', True),
    ('A user can move a video back to an earlier stage.', False),
    ('An editor may send an item back to an earlier stage for rework.', False),
])
def test_reverse_prohibition_is_not_a_reverse_requirement(text, negated):
    lower = text.lower()
    assert bool(NEGATED_REVERSE.search(lower) or NEGATED_REVERSE_SHORT.search(lower)) is negated


def test_reverse_transition_is_read_from_the_declared_workflow():
    assert not has_reverse_transition(FORWARD_FLOW)
    assert has_reverse_transition(REVERSIBLE_FLOW)


def test_review_selects_reverse_check_only_for_positive_criteria():
    if not (ROOT / 'build/workbench-builds/wb-6f4a8ab91ec6/product_spec.json').is_file():
        pytest.skip('retained ProductSpec is not available')
    spec = workshop_spec()
    spec['workflows'] = [REVERSIBLE_FLOW]
    assert 'workflow_reverse' not in assertion_ids(spec)
    spec['acceptance_criteria'] = ['A user can move a video back to an earlier stage.']
    assert 'workflow_reverse' in assertion_ids(spec)
    spec['workflows'] = [FORWARD_FLOW]  # prose cannot demand moves the structured workflow forbids
    assert 'workflow_reverse' not in assertion_ids(spec)


def test_browser_checker_uses_the_same_reverse_semantics():
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not available')
    source = BROWSER.read_text(encoding='utf-8')
    helpers = '\n'.join(line for line in source.splitlines()
                        if line.startswith(('const NEGATED_REVERSE=', 'const NEGATED_REVERSE_SHORT=',
                                            'const hasReverseTransition=', 'const reverseRequired=')))
    assert len(helpers.splitlines()) == 4
    script = ("const assert=require('node:assert/strict');let spec={acceptance_criteria:[]};" + helpers +
              "\nconst forward=" + json.dumps(FORWARD_FLOW) + ",reversible=" + json.dumps(REVERSIBLE_FLOW) + ";"
              "spec.acceptance_criteria=[" + json.dumps(FORWARD_ONLY) + "];assert.equal(reverseRequired(reversible),false);"
              "spec.acceptance_criteria=['Jobs move forward, but not backward.'];assert.equal(reverseRequired(reversible),false);"
              "spec.acceptance_criteria=['A user can move a video back to an earlier stage.'];"
              "assert.equal(reverseRequired(reversible),true);assert.equal(reverseRequired(forward),false);")
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_filtered_board_metrics_follow_runtime_placement():
    source = BROWSER.read_text(encoding='utf-8')
    gate = re.search(r"if\(spec\.metrics\.length&&board\.id===spec\.navigation\[0\]\)\{const displayed=shownMetrics\(\)", source)
    assert gate, 'board filtered_metrics must be gated on the runtime metric placement'
    if not (ROOT / 'build/workbench-builds/wb-6f4a8ab91ec6/product_spec.json').is_file():
        pytest.skip('retained ProductSpec is not available')
    spec = workshop_spec()
    board = next(view for view in spec['views'] if view['kind'] == 'board')
    contract = presentation_contract(spec)
    # The gate mirrors the renderer: a board shows summaries exactly when it is the primary view.
    assert contract[board['id']]['metrics'] == (board['id'] == spec['navigation'][0])
    moved = copy.deepcopy(spec)
    moved['navigation'] = [view['id'] for view in spec['views'] if view['id'] != board['id']] + [board['id']]
    assert presentation_contract(moved)[board['id']]['metrics'] is False


def test_referenced_parent_deletion_criterion_maps_to_delete_block():
    if not (ROOT / 'build/workbench-builds/wb-6f4a8ab91ec6/product_spec.json').is_file():
        pytest.skip('retained ProductSpec is not available')
    spec = workshop_spec()
    spec['acceptance_criteria'] = ['Deleting a series that is referenced by a child record is prevented by referential integrity.']
    assert 'relationship_delete_block' in assertion_ids(spec)
    assert not any(key.startswith('unmapped_acceptance') for key in assertion_ids(spec))


def test_optional_date_check_needs_an_optional_date_field():
    if not (ROOT / 'build/workbench-builds/wb-6f4a8ab91ec6/product_spec.json').is_file():
        pytest.skip('retained ProductSpec is not available')
    spec = workshop_spec()
    spec['acceptance_criteria'] = ['A user can create videos with a planned date and optional notes.']
    dates = [f for e in spec['entities'] for f in e['fields'] if f['type'] == 'date']
    assert dates, 'fixture needs a date field'
    for field in dates: field['required'] = False
    assert 'optional_date' in assertion_ids(spec)
    for field in dates: field['required'] = True  # a required date cannot be observed accepting empty input
    assert 'optional_date' not in assertion_ids(spec)
