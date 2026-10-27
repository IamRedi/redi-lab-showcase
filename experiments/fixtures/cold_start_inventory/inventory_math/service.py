"""Stable integration surface for the two independently produced components."""

from .normalize import normalize_sku
from .reorder import reorder_quantity


def replenishment_order(raw_sku: str, on_hand: int, target: int) -> dict[str, object]:
    return {
        "sku": normalize_sku(raw_sku),
        "quantity": reorder_quantity(on_hand, target),
    }
