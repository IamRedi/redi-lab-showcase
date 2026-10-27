import pytest

from inventory_math.normalize import normalize_sku


def test_normalize_public_contract():
    assert normalize_sku("  ab 12_blue ") == "AB-12-BLUE"
    assert normalize_sku("A---B") == "A-B"
    assert normalize_sku(" --north__ 42-- ") == "NORTH-42"

    with pytest.raises(TypeError):
        normalize_sku(42)
    with pytest.raises(ValueError):
        normalize_sku(" __---__ ")
