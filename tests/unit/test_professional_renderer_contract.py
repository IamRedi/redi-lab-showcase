"""Executable shared-renderer contract against a frozen, owner-derived spec."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from redi_lab.workbench.professional.continuation import review_defect_class
from redi_lab.workbench.professional.review import envelope, evaluate, requirements
from redi_lab.workbench.professional.runtime import (
    DataService, ValidationError, metric_values, presentation_contract,
    render_app, select_records,
)


ROOT = Path(__file__).parents[2]


def frozen_spec():
    return json.loads((ROOT / 'build/workbench-builds/wb-6f4a8ab91ec6/product_spec.json').read_text(encoding='utf-8'))['spec']


def design(spec):
    return {'accent': '#155e75', 'views': [{'id': view['id']} for view in spec['views']]}


def test_field_constraints_are_server_side_and_optional_values_remain_valid(tmp_path):
    spec = frozen_spec();service = DataService(str(tmp_path / 'owner.sqlite3'), spec)
    try:
        series = service.save('series', {'name': 'First', 'description': '', 'color': '#2DD4BF'})
        video = service.save('video', {'title': 'First video', 'series': series, 'status': 'Idea',
                                      'short_description': '', 'production_notes': '', 'planned_date': ''})
        before = service.state()
        for name, record_id, key, value in [('series', series, 'name', 'x' * 81),
                                             ('video', video, 'title', 'x' * 121),
                                             ('video', video, 'production_notes', 'x' * 20001),
                                             ('video', video, 'title', '   ')]:
            with pytest.raises(ValidationError, match='characters|required'):
                service.save(name, {key: value}, record_id)
            assert service.state() == before
        assert service.record('video', video)['planned_date'] == ''
        assert service.record('video', video)['production_notes'] == ''
        service.save('video', {'planned_date': None}, video)
        assert service.record('video', video)['planned_date'] is None
    finally:
        service.close()


def test_search_filters_board_and_summaries_compose_from_spec(tmp_path):
    spec = frozen_spec();caps = presentation_contract(spec)
    board = caps['production_board']
    assert board['search_fields'] == ['title']
    assert set(board['filter_fields']) == {'series', 'status'}
    assert board['metrics'] and board['workflow_field'] == 'status'
    service = DataService(str(tmp_path / 'owner.sqlite3'), spec)
    try:
        first = service.save('series', {'name': 'First', 'description': '', 'color': '#2DD4BF'})
        second = service.save('series', {'name': 'Second', 'description': '', 'color': '#2DD4BF'})
        one = service.save('video', {'title': 'Morning launch', 'series': first, 'status': 'Idea'})
        two = service.save('video', {'title': 'Evening launch', 'series': second, 'status': 'Idea'})
        service.save('video', {'status': 'Script'}, two)
        state = service.state()
        rows = select_records(spec['entities'][1], state['records']['video'], board,
                              'evening', {'series': str(second), 'status': 'Script'})
        assert [row['id'] for row in rows] == [two]
        assert {row['id'] for row in select_records(spec['entities'][1], state['records']['video'], board)} == {one, two}
        filtered = metric_values(spec, {**state['records'], 'video': rows})
        assert filtered == {'in_progress_videos': 1, 'ready_videos': 0, 'published_videos': 0}
        html = render_app(spec, design(spec), state, view='production_board', search='evening',
                          filters={'series': str(second), 'status': 'Script'})
        assert 'name="search"' in html and 'name="filter_series"' in html and 'name="filter_status"' in html
        assert 'data-stage="Published"' in html and 'data-stage-count="Script">1' in html
        assert 'Morning launch' not in html and 'Evening launch' in html
        assert 'data-metric="in_progress_videos">1' in html
        assert service.state() == state
        empty = render_app(spec, design(spec), state, view='production_board', search='no such title')
        assert 'No matching records' in empty and 'data-metric="in_progress_videos">0' in empty
    finally:
        service.close()


def test_reviewer_requires_real_capability_observations():
    spec = frozen_spec();rows = {row['requirement_id']: row for row in requirements(spec)}
    assert {'board_columns'} <= set(rows['acceptance:7']['assertion_ids'])
    assert {'board_card_fields'} <= set(rows['acceptance:8']['assertion_ids'])
    assert {'search','filter_reference','filter_status','combined_filters','clear_filters'} <= set(rows['acceptance:9']['assertion_ids'])
    assert {'detail_panel','cancel_edit','detail_escape'} <= set(rows['acceptance:16']['assertion_ids'])
    assert {'board_mobile','long_title_saved','overflow'} <= set(rows['acceptance:17']['assertion_ids'])
    assert {'relationship_delete_block'} <= set(rows['acceptance:14']['assertion_ids'])
    evidence = envelope(spec, 'frozen-source', {'package_sha256': 'local'}, 'offline-run', [],
                        browser_identity={'engine': 'offline'}, references={'unread': 'local'})
    result = evaluate(evidence)
    assert result['verdict'] == 'FAIL'
    assert result['counts']['NOT_RUN'] > 0


def test_shared_renderer_failure_class_needs_requalification_provenance():
    failed = {'counts': {'FAIL': 1}}
    assert review_defect_class(failed, {'new_package_sha256': 'fresh'}) == 'SHARED_RENDERER_OR_PACKAGE_CONTRACT_DEFECT'
    assert review_defect_class(failed, None) == 'PRODUCT_DEFECT'
    assert review_defect_class({'counts': {'NOT_RUN': 1}}, None) == 'HARNESS_OR_GATE_DEFECT'


def test_empty_board_keeps_workflow_columns_and_empty_state(tmp_path):
    # Browser acceptance audits boards on an empty database; stages must still render.
    spec = frozen_spec();service = DataService(str(tmp_path / 'owner.sqlite3'), spec)
    try:
        html = render_app(spec, design(spec), service.state(), view='production_board')
        stages = next(f for f in spec['entities'][1]['fields'] if f['id'] == 'status')['choices']
        assert html.count('class="board-column"') == len(stages)
        assert all('data-stage-count="' + stage + '">0' in html for stage in stages)
        assert 'class="empty"' in html and 'No records in this stage.' in html
        assert '<table' not in html
    finally:
        service.close()
