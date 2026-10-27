from inventory_math.service import replenishment_order


def test_integration_hidden_contract():
    assert replenishment_order("__zone  4__", 4, 11) == {
        "sku": "ZONE-4",
        "quantity": 7,
    }
