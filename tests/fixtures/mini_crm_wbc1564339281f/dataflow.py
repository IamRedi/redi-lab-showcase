"""Status normalization and transition rules for Mini CRM jobs."""

VALID_STATUSES = ("NEW", "ACTIVE", "DONE")

TRANSITIONS = {
    "NEW": ("ACTIVE",),
    "ACTIVE": ("DONE",),
    "DONE": (),
}


def normalize_status(status):
    """Return a canonical job status or raise ValueError if it is invalid."""
    if not isinstance(status, str):
        raise ValueError(f"Invalid job status: {status!r}")

    normalized = status.strip().upper()
    if normalized not in VALID_STATUSES:
        raise ValueError(f"Invalid job status: {status!r}")
    return normalized


def can_transition(current_status, new_status):
    """Return whether a direct transition between two valid statuses is allowed."""
    current = normalize_status(current_status)
    target = normalize_status(new_status)
    return target in TRANSITIONS[current]
