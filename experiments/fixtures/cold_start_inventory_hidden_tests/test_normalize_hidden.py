import pytest

from inventory_math.normalize import normalize_sku


def test_normalize_hidden_edges():
    assert normalize_sku("__a   b__") == "A-B"
    with pytest.raises(ValueError):
        normalize_sku("___   ")
    with pytest.raises(TypeError):
        normalize_sku(17)
