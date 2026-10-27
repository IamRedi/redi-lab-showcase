from inventory_math.reorder import reorder_quantity


def test_reorder_public_contract():
    assert reorder_quantity(3, 10) == 7
    assert reorder_quantity(12, 10) == 0
