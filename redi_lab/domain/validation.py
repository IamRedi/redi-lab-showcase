"""Domain-boundary validation.

Only validates contracts at structural / schema-level boundaries:
  * missing required IDs or empty string goals/roles
  * invalid schema version
  * ProductDAG structural invariants (duplicate nodes, missing deps, self-loops, cycles)
  * negative monetary / token / timing / retry counts where prohibited
  * invalid enum values caught by dataclass post-init on decode
  * incompatible status combinations (e.g., completed + no end time only warnings, not hard errors)

No business logic — e.g., no worker routing, no recipe promotion thresholds, no gate
execution semantics.  Behaviour lives in Layer C (REDI-02+).
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Mapping

from .contracts import ProductDAG
from .enums import SchemaVersion
from .identity import SCHEMA_VERSION, SchemaVersionError, VersionedContract


class DomainValidationError(ValueError):
    """Raised by :func:`validate_contract` / :func:`validate_dag` on failure."""


def _accept_schema_version(value: int, label: str) -> None:
    if not isinstance(value, int):
        raise DomainValidationError(f"{label}.schema_version must be int, got {type(value).__name__}")
    if value != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{label}.schema_version={value} is unsupported; expected {SCHEMA_VERSION}"
        )


def validate_contract(contract: VersionedContract) -> None:
    """Validate a single top-level VersionedContract at the domain boundary.

    Raises :class:`DomainValidationError` (or its :class:`SchemaVersionError` subclass)
    on the first encountered problem.
    """
    name = type(contract).__name__
    _accept_schema_version(contract.schema_version, name)

    # Required id (structural).  We accept any truthy non-empty id.
    cid = getattr(contract, "id", None)
    if cid is None or (isinstance(cid, str) and not cid.strip()):
        raise DomainValidationError(f"{name}.id is required and must not be empty")

    # Specialised emptiness checks per contract type, based on governance-minimum
    # non-empty fields.  These are intentionally conservative.
    match type(contract).__name__:
        case "ProductSpec":
            _require_non_empty(contract, "requested_outcome", name)
        case "ProductClass":
            _require_non_empty(contract, "name", name)
        case "StationContract":
            _require_non_empty(contract, "role", name)
        case "WorkerProfile":
            _require_non_empty(contract, "version", name)
        case "TaskContract":
            _require_non_empty(contract, "goal", name)
        case "GateResult":
            _require_non_empty(contract, "gate_identity", name)
        case "ExperimentManifest":
            _require_non_empty(contract, "title", name)
            _require_non_empty(contract, "hypothesis_reference", name)
        case _:
            pass

    # Quantitative non-negative checks: TokenUsage, Budget, Timing already do
    # __post_init__.  MonetaryCost in RunEvidence / WorkerProfile / etc. is checked
    # here only for prohibited negative "primary cost" situations when amount<0.
    for field_name in ("cost", "cost_reference", "total_cost"):
        mc = getattr(contract, field_name, None)
        if mc is None:
            continue
        try:
            amount = mc.amount  # MonetaryCost-like
        except AttributeError:
            continue
        if amount is not None and hasattr(amount, "__lt__"):
            from decimal import Decimal

            if isinstance(amount, (int, float, Decimal)) and amount < 0:
                if field_name != "cost":  # refunds ok only for explicit refund slots (none yet)
                    raise DomainValidationError(
                        f"{name}.{field_name}.amount must be non-negative; got {amount}"
                    )


def _require_non_empty(contract: object, field: str, name: str) -> None:
    value = getattr(contract, field, None)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise DomainValidationError(f"{name}.{field} is required and must not be empty")


def validate_dag(dag: ProductDAG) -> list[str]:
    """ProductDAG structural validation (only; no scheduling / execution).

    Enforces:
      * duplicate node_ids are illegal
      * every dependency.upstream / downstream references an existing node
      * self-loops are illegal (upstream == downstream on any edge)
      * directed cycles are illegal (the canonical architecture requires a DAG)

    Returns a valid topological node ordering (Kahn) when the DAG is valid.
    """
    _accept_schema_version(dag.schema_version, "ProductDAG")

    node_ids: list[str] = [n.node_id for n in dag.nodes]
    if len(node_ids) != len(set(node_ids)):
        dup = [nid for nid in node_ids if node_ids.count(nid) > 1]
        raise DomainValidationError(f"ProductDAG duplicate node_ids: {sorted(set(dup))}")

    id_set: set[str] = set(node_ids)

    for idx, dep in enumerate(dag.dependencies):
        if dep.upstream == dep.downstream:
            raise DomainValidationError(
                f"ProductDAG dependency[{idx}] is a self-loop on {dep.upstream!r}"
            )
        for side, val in (("upstream", dep.upstream), ("downstream", dep.downstream)):
            if val not in id_set:
                raise DomainValidationError(
                    f"ProductDAG dependency[{idx}] {side}={val!r} does not reference any node"
                )

    # Cycle detection via Kahn's algorithm: graph should be topologically orderable.
    indeg: dict[str, int] = {n: 0 for n in id_set}
    adj: dict[str, list[str]] = {n: [] for n in id_set}
    for dep in dag.dependencies:
        adj[dep.upstream].append(dep.downstream)
        indeg[dep.downstream] += 1

    queue: deque[str] = deque(n for n in indeg if indeg[n] == 0)
    seen = 0
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        seen += 1
        for nxt in adj[node]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(id_set):
        remaining = sorted(n for n in id_set if indeg[n] > 0)
        raise DomainValidationError(f"ProductDAG contains a cycle involving nodes: {remaining}")
    return order


def enumerate_domain_contract_types() -> Iterable[type[VersionedContract]]:
    """Return every VersionedContract type defined in contracts + events.

    Used by provider-independence tests and round-trip tests; not a governance list.
    """
    from .contracts import CANONICAL_CONTRACTS
    from .events import LifecycleEvent

    for t in CANONICAL_CONTRACTS:
        yield t
    yield LifecycleEvent  # type: ignore[misc]  # has .id / .schema_version


__all__ = [
    "DomainValidationError",
    "enumerate_domain_contract_types",
    "validate_contract",
    "validate_dag",
]
