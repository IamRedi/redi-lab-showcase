"""Deterministic JSON serialization / deserialization for all domain contracts.

Guarantees (REDI-01 acceptance minimum):
  * JSON-compatible representation (no tuples vs lists distinction; preserved via sorted keys)
  * serialize → deserialize produces semantically equal contracts (== equality)
  * Enum values survive as strings via :class:`StrEnum`; decoded by ``StrEnum(value)``
  * ``datetime`` values normalized to ISO-8601 UTC strings
  * ``Decimal`` amounts encoded as "12.34" strings to preserve precision
  * ``*_id`` NewType ids encode/decode as plain strings (NewType constructor wraps back on decode)
  * ``schema_version`` preserved in every top-level contract; bad versions raise SchemaVersionError
  * Dict/mapping key ordering does not affect semantic equality (``json.dumps(..., sort_keys=True)``)

No database persistence; output is ``str`` only.  Attach adapters later if needed.
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import asdict, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping, Sequence, Type, TypeVar, get_args, get_origin, get_type_hints

from .contracts import CANONICAL_CONTRACTS
from .enums import (
    EscalationLevel,
    FailureCategory,
    GateStatus,
    GateType,
    LifecycleEventType,
    Mode,
    RecipeRung,
    RiskTier,
    RunStatus,
    SchemaVersion,
    Severity,
)
from .events import LifecycleEvent
from .identity import (
    SCHEMA_VERSION,
    ContextManifestId,
    EvidenceId,
    EventId,
    ExperimentId,
    FactoryRunId,
    FailureId,
    GateResultId,
    HandoffManifestId,
    ProductClassId,
    ProductId,
    RecipeId,
    SchemaVersionError,
    StationId,
    TaskId,
    WorkerId,
)
from .validation import DomainValidationError


class DomainDecodeError(ValueError):
    """Raised when JSON payload cannot be decoded to a domain contract."""


T = TypeVar("T")


# Registry of contract cls-name → cls used by multi-contract JSON decode.
_CONTRACT_REGISTRY: dict[str, type[Any]] = {
    **{c.__name__: c for c in CANONICAL_CONTRACTS},
    "LifecycleEvent": LifecycleEvent,
}


# Enum classes with (StrEnum, Enum) superclass — decode by str value.
_ENUM_CLASSES: tuple[type[Enum], ...] = (
    FailureCategory,
    GateStatus,
    GateType,
    LifecycleEventType,
    Mode,
    RecipeRung,
    RiskTier,
    RunStatus,
    Severity,
    EscalationLevel,
    SchemaVersion,
)


_ID_NEWTYPE_WRAP: Mapping[str, type[Any]] = {
    "ProductId": ProductId,
    "ProductClassId": ProductClassId,
    "RecipeId": RecipeId,
    "FactoryRunId": FactoryRunId,
    "ExperimentId": ExperimentId,
    "TaskId": TaskId,
    "StationId": StationId,
    "WorkerId": WorkerId,
    "ContextManifestId": ContextManifestId,
    "HandoffManifestId": HandoffManifestId,
    "GateResultId": GateResultId,
    "EvidenceId": EvidenceId,
    "FailureId": FailureId,
    "EventId": EventId,
}


# ---------------------------------------------------------------------------
# Encoding — dataclass → JSON-ready dict
# ---------------------------------------------------------------------------


def _encode_default(obj: Any) -> Any:
    """Encoder hook for ``json.dumps(default=...)``.

    Converts:
      * dataclasses → plain dict (already handled by asdict path, but safe fallback)
      * Enum → value (StrEnum → str)
      * datetime → isoformat string
      * Decimal → str preserving precision
      * Mapping → dict (ordered, key-preserving)
      * NewType id (ProductId, etc.) → underlying str

    No generic ``str(obj)`` fallback exists: any other Python object is left
    as-is so ``json.dumps`` fails closed.  Runtime-specific types (factory
    exceptions, provider adapters, …) must be canonicalized to JSON-compatible
    primitives at their own boundary BEFORE being placed on a domain contract.
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if is_dataclass(obj):
        return {f.name: _encode_default(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Mapping):
        return {str(k): _encode_default(v) for k, v in obj.items()}
    # Treat lists and tuples as sequences.  Explicitly exclude str/bytes because
    # Python str/bytes are Sequences of their element types (which are str/bytes
    # of length 1, ...), producing infinite recursion on `isinstance(x, Sequence)`.
    if isinstance(obj, (list, tuple)):
        return [_encode_default(v) for v in obj]
    # NewType ids (ProductId, ...) are effectively aliases of str with __name__
    # matching one of the registered wrappers.
    for cls_name, _ in _ID_NEWTYPE_WRAP.items():
        if type(obj).__name__ == cls_name and isinstance(obj, str):
            return str(obj)
    # Fail closed: unsupported objects are returned unchanged so json.dumps
    # raises TypeError.  Layer A deliberately holds no knowledge of concrete
    # runtime/factory types (e.g. isolation exceptions) — callers must pass
    # canonical JSON-compatible primitives only.
    return obj


def domain_to_json(contract: Any) -> str:
    """Serialize a domain contract to UTF-8 JSON string with sorted keys.

    The resulting object carries ``__type__`` so the decoder can pick the right cls.
    """
    if not is_dataclass(contract):
        raise DomainDecodeError(
            f"Can only serialize dataclass domain contracts; got {type(contract).__name__}"
        )
    payload: dict[str, Any] = {"__type__": type(contract).__name__}
    for f in fields(contract):
        payload[f.name] = _encode_default(getattr(contract, f.name))
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Decoding — JSON str → typed dataclass
# ---------------------------------------------------------------------------


def _coerce_value(
    value: Any,
    type_hint: Any,
    field_path: str,
) -> Any:
    """Recursively coerce a JSON-loaded python value to the expected type_hint."""
    if value is None:
        return None

    origin = get_origin(type_hint)
    args = get_args(type_hint)

    # Primitive type annotations
    if type_hint is Any:
        return value
    if type_hint in (int, float, bool, str, bytes):
        try:
            return type_hint(value)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise DomainDecodeError(f"field {field_path}: {exc}") from exc

    if type_hint is Decimal:
        if not isinstance(value, (str, int, float, Decimal)):
            raise DomainDecodeError(f"field {field_path}: Decimal expected, got {type(value).__name__}")
        try:
            return Decimal(str(value))
        except Exception as exc:  # noqa: BLE001 - raise structured
            raise DomainDecodeError(f"field {field_path}: invalid Decimal {value!r}: {exc}") from exc

    if type_hint is datetime:
        if not isinstance(value, str):
            raise DomainDecodeError(f"field {field_path}: datetime iso-string expected")
        try:
            return datetime.fromisoformat(value)
        except Exception as exc:  # noqa: BLE001 - raise structured
            raise DomainDecodeError(f"field {field_path}: invalid datetime {value!r}: {exc}") from exc

    # Enum types
    if isinstance(type_hint, type) and issubclass(type_hint, Enum):
        if not isinstance(value, str):
            raise DomainDecodeError(f"field {field_path}: enum value must be str, got {type(value).__name__}")
        try:
            return type_hint(value)
        except ValueError as exc:
            raise DomainDecodeError(
                f"field {field_path}: unknown {type_hint.__name__} value {value!r}"
            ) from exc

    # NewType ids:  NewType('XId', str) will have args=() but its name matches
    type_name = getattr(type_hint, "__name__", "")
    if type_name in _ID_NEWTYPE_WRAP and isinstance(value, str):
        return _ID_NEWTYPE_WRAP[type_name](value)

    # dataclass
    if isinstance(type_hint, type) and is_dataclass(type_hint):
        if not isinstance(value, Mapping):
            raise DomainDecodeError(f"field {field_path}: expected object/dict for {type_hint.__name__}")
        return _decode_dataclass(value, type_hint, field_path)

    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        # Variable-length tuple annotation like tuple[int, ...]
        if not isinstance(value, (list, tuple)):
            raise DomainDecodeError(f"field {field_path}: expected sequence")
        return tuple(_coerce_value(v, args[0], f"{field_path}[i]") for v in value)

    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise DomainDecodeError(f"field {field_path}: expected fixed tuple of length {len(args)}")
        if len(value) != len(args):
            raise DomainDecodeError(f"field {field_path}: tuple length mismatch {len(value)} vs {len(args)}")
        return tuple(_coerce_value(v, a, f"{field_path}[{i}]") for i, (v, a) in enumerate(zip(value, args)))

    if origin is list:
        if not isinstance(value, (list, tuple)):
            raise DomainDecodeError(f"field {field_path}: expected list")
        inner = args[0] if args else Any
        return [_coerce_value(v, inner, f"{field_path}[{i}]") for i, v in enumerate(value)]

    if origin is Sequence:
        if not isinstance(value, (list, tuple)):
            raise DomainDecodeError(f"field {field_path}: expected sequence")
        inner = args[0] if args else Any
        return tuple(_coerce_value(v, inner, f"{field_path}[{i}]") for i, v in enumerate(value))

    if origin is Mapping or origin is dict:
        if not isinstance(value, Mapping):
            raise DomainDecodeError(f"field {field_path}: expected mapping")
        key_type, val_type = args if args else (str, Any)
        result: dict[Any, Any] = {}
        for k, v in value.items():
            kk = _coerce_value(k, key_type, f"{field_path} key")
            vv = _coerce_value(v, val_type, f"{field_path}.{kk}")
            result[kk] = vv
        return result

    # Union / Optional (both typing.Union and types.UnionType from `X | Y` syntax)
    import types as _types

    is_union_like = origin is Union or isinstance(type_hint, _types.UnionType) or (
        origin is not None and origin.__name__ == "UnionType"
    )
    if is_union_like:
        # Optional[X] = Union[X, None]; fall through cases that match
        errors: list[str] = []
        for a in args:
            if a is type(None):
                if value is None:
                    return None
                continue
            try:
                return _coerce_value(value, a, field_path)
            except DomainDecodeError as e:
                errors.append(str(e))
        raise DomainDecodeError(
            f"field {field_path}: value did not match any union member: " + "; ".join(errors)
        )

    # Literal[..]
    if origin is Literal:
        if value not in args:
            raise DomainDecodeError(f"field {field_path}: value {value!r} not in literal set {args}")
        return value

    # Unknown — pass through (Any-like fallback)
    return value


# Ensure 'Union' referenced in coercion is imported (typing).
from typing import Literal, Union  # noqa: E402  (late import; used at runtime inside _coerce_value)


def _decode_dataclass(data: Mapping[str, Any], cls: type[T], path: str) -> T:
    if "__type__" in data:
        declared = data["__type__"]
        if declared != cls.__name__ and declared in _CONTRACT_REGISTRY:
            cls = _CONTRACT_REGISTRY[declared]  # type: ignore[assignment]
    # With `from __future__ import annotations` dataclass field.types are plain
    # strings.  Resolve them using typing.get_type_hints, which consults the
    # module globals where the class was defined.
    try:
        resolved = get_type_hints(cls, include_extras=True)
    except Exception:  # noqa: BLE001 - fallback to dataclass string hints via eval in scope
        resolved: dict[str, Any] = {}
        globals_ns = getattr(sys.modules.get(cls.__module__, None), "__dict__", {})
        import builtins as _builtins

        combined_ns: dict[str, Any] = {**globals_ns}
        for name, _mod_name in (
            ("Any", "typing"),
            ("Mapping", "typing"),
            ("Sequence", "typing"),
            ("Optional", "typing"),
            ("Union", "typing"),
            ("Literal", "typing"),
        ):
            combined_ns.setdefault(name, getattr(importlib.import_module("typing"), name))
        for f in fields(cls):
            if isinstance(f.type, str):
                try:
                    resolved[f.name] = eval(f.type, {"__builtins__": _builtins}, combined_ns)
                except Exception:  # noqa: BLE001
                    resolved[f.name] = Any
            else:
                resolved[f.name] = f.type
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue  # use default
        raw = data[f.name]
        type_hint = resolved.get(f.name, f.type)
        coerced = _coerce_value(raw, type_hint, f"{path}.{cls.__name__}.{f.name}")
        kwargs[f.name] = coerced
    instance = cls(**kwargs)  # type: ignore[call-arg]
    # Validate schema_version for top-level contracts that own it
    sv = getattr(instance, "schema_version", None)
    if sv is not None and isinstance(sv, int) and sv != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"{cls.__name__}.schema_version={sv} unsupported during decode; expected {SCHEMA_VERSION}"
        )
    return instance


def domain_from_json(text: str) -> Any:
    """Deserialize a JSON string produced by :func:`domain_to_json` back to the right class.

    Raises :class:`DomainDecodeError` or :class:`SchemaVersionError` on failure.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DomainDecodeError(f"invalid JSON: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise DomainDecodeError("top-level JSON must be an object")
    type_name = raw.get("__type__")
    if not isinstance(type_name, str) or type_name not in _CONTRACT_REGISTRY:
        raise DomainDecodeError(f"__type__ missing or unknown: {type_name!r}")
    cls = _CONTRACT_REGISTRY[type_name]
    return _decode_dataclass(raw, cls, path="<root>")


# --- Validation helpers used by tests ------------------------------------------------


def round_trip(contract: Any) -> Any:
    """Helper: serialize then deserialize; used by REDI-01 round-trip tests."""
    return domain_from_json(domain_to_json(contract))


__all__ = [
    "DomainDecodeError",
    "domain_from_json",
    "domain_to_json",
    "round_trip",
]
