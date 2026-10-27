import pytest

from inventory_math.reorder import reorder_quantity


def test_reorder_hidden_edges():
    assert reorder_quantity(0, 0) == 0
    with pytest.raises(ValueError):
        reorder_quantity(-1, 3)
    with pytest.raises(TypeError):
        reorder_quantity(True, 3)
