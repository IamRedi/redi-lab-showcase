"""Job status normalization and transition rules."""

VALID_STATUSES = frozenset({"NEW", "ACTIVE", "DONE"})

TRANSITIONS = {
    "NEW": frozenset({"ACTIVE"}),
    "ACTIVE": frozenset({"DONE"}),
    "DONE": frozenset(),
}


def normalize_status(status):
    """Return a canonical job status or raise ValueError if it is invalid."""
    if not isinstance(status, str):
        raise ValueError("Invalid job status: {!r}".format(status))

    normalized = status.strip().upper()
    if normalized not in VALID_STATUSES:
        raise ValueError("Invalid job status: {!r}".format(status))
    return normalized


def can_transition(current_status, new_status):
    """Return whether a job may move from current_status to new_status."""
    current = normalize_status(current_status)
    new = normalize_status(new_status)
    return new in TRANSITIONS[current]
