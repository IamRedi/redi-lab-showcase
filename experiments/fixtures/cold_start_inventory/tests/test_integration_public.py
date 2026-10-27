from inventory_math.service import replenishment_order


def test_replenishment_order_integrates_both_components():
    assert replenishment_order(" rack_7 blue ", 2, 9) == {
        "sku": "RACK-7-BLUE",
        "quantity": 7,
    }
