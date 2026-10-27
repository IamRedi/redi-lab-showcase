"""Isolated local temp workspace + allowlist enforcement + patch apply + baseline hash snapshot."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from ..domain import FailureCategory
from ..runtime.contracts import PatchEdit


class ScopeViolation(Exception):
    """Raised when an operation attempts to modify a path outside allowed scope.

    Attributes carry FailureCategory.SCOPE_VIOLATION so callers attach correct taxonomy.
    """

    category: FailureCategory = FailureCategory.SCOPE_VIOLATION

    def __init__(self, message: str, *, path: str | None = None) -> None:
        super().__init__(message)
        self.offending_path = path


def scope_violation_to_canonical(violation: ScopeViolation) -> dict[str, Any]:
    """Canonical JSON-primitive mapping for one ScopeViolation.

    Factory/evidence boundary form (REDI-03L3B): the runtime violation is
    converted to stable primitives HERE, before any domain contract is built.
    No Python repr, no memory addresses, no provider/runtime types.
    """
    return {
        "type": "ScopeViolation",
        "category": FailureCategory.SCOPE_VIOLATION.value,
        "message": str(violation.args[0]) if violation.args else "",
        "offending_path": violation.offending_path,
    }


def canonicalize_scope_violations(value: Any) -> Any:
    """Recursively replace ScopeViolation instances inside lists/tuples/mappings
    with their canonical primitive mappings.

    Guarantees that only JSON-compatible primitives cross into domain
    FailureRecord / LifecycleEvent payloads; the domain serializer (Layer A)
    stays free of factory runtime-type knowledge and fails closed on any other
    unsupported object.
    """
    if isinstance(value, ScopeViolation):
        return scope_violation_to_canonical(value)
    if isinstance(value, Mapping):
        return {str(k): canonicalize_scope_violations(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonicalize_scope_violations(v) for v in value]
    return value


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def baseline_file_hashes(fixture_root: Path) -> dict[str, str]:
    """Return sorted mapping {rel_posix_path: sha256 for every file under fixture_root."""
    fixture_root = Path(fixture_root).resolve()
    result: dict[str, str] = {}
    for p in sorted(fixture_root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(fixture_root).as_posix()
        result[rel] = _file_sha256(p)
    return result


def assert_in_workspace(workspace_root: Path, rel_path: str) -> Path:
    """Return resolved absolute path, raising ScopeViolation if it escapes workspace_root or uses .. traversal."""
    ws = Path(workspace_root).resolve()
    # Normalize rel_path: POSIX style to OS path then resolve absolute inside ws
    candidate = (ws / PurePosixPath(rel_path)).resolve()
    try:
        candidate.relative_to(ws)
    except ValueError as exc:
        raise ScopeViolation(
            f"Path escapes workspace: rel={rel_path!r}",
            path=rel_path,
        ) from exc
    return candidate


@dataclass
class IsolatedWorkspace:
    """Create a tempdir copy of fixture_root. destroy() cleans up deterministically."""

    root: Path
    fixture_root: Path
    allowed_scope_rel_prefixes: tuple[str, ...]
    baseline_hashes: Mapping[str, str]
    _cleanup_called: bool = field(default=False, init=False)

    @classmethod
    def create(
        cls,
        fixture_root: str | Path,
        allowed_scope_prefixes: Iterable[str] = ("",),
        dir: str | None = None,
    ) -> "IsolatedWorkspace":
        src = Path(fixture_root).resolve()
        if not src.is_dir():
            raise ValueError(f"fixture_root not a directory: {src}")
        prefixes = tuple("" if p in (None, ".", "./") else p.rstrip("/") for p in allowed_scope_prefixes)
        tmp = Path(tempfile.mkdtemp(prefix="redi_ws_", dir=dir)).resolve()
        dest = tmp / src.name
        shutil.copytree(src, dest, symlinks=False)
        return cls(
            root=dest,
            fixture_root=src,
            allowed_scope_rel_prefixes=prefixes,
            baseline_hashes=dict(baseline_file_hashes(src)),
        )

    # ------------------------------------------------------------------
    def is_allowed_rel(self, rel_path: str) -> bool:
        """Return True iff rel_path starts with any allowed scope prefix (or prefix "" = allow all).

        Also rejects any path component "..".
        """
        parts = PurePosixPath(rel_path).parts
        if ".." in parts:
            return False
        if "" in self.allowed_scope_rel_prefixes:
            return True
        norm = rel_path.lstrip("./")
        return any(
            norm == prefix or norm.startswith(prefix + "/")
            for prefix in self.allowed_scope_rel_prefixes
            if prefix
        )

    def assert_path_in_scope(self, rel_path: str) -> None:
        if not self.is_allowed_rel(rel_path):
            raise ScopeViolation(
                f"Patch rel_path not in allowed scope: {rel_path!r}",
                path=rel_path,
            )

    # ------------------------------------------------------------------
    def reset(self, fixture_root: Path | None = None) -> None:
        """Delete all contents and recopy from fixture_root. Returns fresh baseline snapshot in self.baseline_hashes."""
        src = Path(fixture_root or self.fixture_root).resolve()
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except FileNotFoundError:
                        pass
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            dest = self.root / child.name
            if child.is_dir():
                shutil.copytree(child, dest, symlinks=False)
            else:
                shutil.copy2(child, dest)
        object.__setattr__(self, "baseline_hashes", dict(baseline_file_hashes(self.root)))
        object.__setattr__(self, "fixture_root", src)

    def destroy(self) -> None:
        if self._cleanup_called:
            return
        self._cleanup_called = True
        to_delete = self.root.parent
        if to_delete.name.startswith("redi_ws_") and to_delete.is_dir():
            shutil.rmtree(to_delete, ignore_errors=True)

    def __enter__(self) -> "IsolatedWorkspace":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.destroy()


def apply_patches(
    workspace: IsolatedWorkspace,
    patches: Sequence[PatchEdit],
    *,
    allowed_scope: Iterable[str] | None = None,
) -> tuple[bool, list[ScopeViolation]]:
    """Apply a list of PatchEdits into workspace enforcing scope.

    Returns (ok, violations). If any violation, returns ok=False, partial state not rolled back.
    Callers should close_run FAILED or GATE_FAILED accordingly.
    """
    if allowed_scope is None:
        scope_prefixes = workspace.allowed_scope_rel_prefixes
    else:
        scope_prefixes = tuple(
            "" if p in (None, ".", "./") else str(p).rstrip("/")
            for p in allowed_scope
        )
    violations: list[ScopeViolation] = []
    # Scope check ALL patches FIRST before writing anything (fail-fast atomicity).
    def _check(rel: str) -> None:
        parts = PurePosixPath(rel).parts
        if ".." in parts:
            violations.append(ScopeViolation(f"contains .. traversal", path=rel))
            return
        if "" not in scope_prefixes:
            norm = rel.lstrip("./")
            ok = any(
                norm == pfx or norm.startswith(pfx + "/")
                for pfx in scope_prefixes
                if pfx
            )
            if not ok:
                violations.append(ScopeViolation(f"not in allowed scope prefixes", path=rel))
    for p in patches:
        _check(p.rel_path)
    if violations:
        return False, violations
    for p in patches:
        abs_p = assert_in_workspace(workspace.root, p.rel_path)
        if p.action == "delete":
            if abs_p.exists():
                abs_p.unlink()
            continue
        abs_p.parent.mkdir(parents=True, exist_ok=True)
        abs_p.write_text(p.content or "", encoding="utf-8")
    return True, violations


__all__ = [
    "IsolatedWorkspace",
    "ScopeViolation",
    "apply_patches",
    "assert_in_workspace",
    "baseline_file_hashes",
    "canonicalize_scope_violations",
    "scope_violation_to_canonical",
]
