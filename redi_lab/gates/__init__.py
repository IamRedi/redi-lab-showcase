"""REDI-03 deterministic gate engine.

Four mandatory gates in order, short-circuiting on mandatory failure.
All subprocess gates use Python sys.executable for reproducibility across platforms.
"""

from __future__ import annotations

import hashlib
import py_compile
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from ..domain import (
    AcceptanceCriterion,
    FailureCategory,
    GateResult,
    GateResultId,
    GateStatus,
    GateType,
    new_id,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cap_bytes(data: bytes, cap: int) -> tuple[bytes, bool]:
    if cap is None or cap <= 0:
        return data, False
    if len(data) <= cap:
        return data, False
    return data[:cap], True


@dataclass
class GateSpec:
    public_test_cmd: list[str] = field(default_factory=lambda: [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider", "tests/"])
    hidden_test_cmd: list[str] = field(default_factory=lambda: [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"])
    hidden_test_cwd: Path | None = None
    hidden_test_env_extra: dict[str, str] = field(default_factory=dict)
    per_gate_timeout_s: float = 30.0
    output_cap_bytes: int = 65536
    allowed_scope_prefixes: tuple[str, ...] = ("slugify", "tests")
    baseline_hashes: dict[str, str] = field(default_factory=dict)


def _mk_gate(
    ident: str,
    *,
    mandatory: bool,
    status: GateStatus,
    gate_type: GateType = GateType.DETERMINISTIC,
    verifier_type: str = "deterministic_code",
    verifier_identity: str = "redi-03-gate-engine",
    reasons: Iterable[str] = (),
    metadata: dict[str, Any] | None = None,
    target_task_id: Any | None = None,
    target_node_id: str | None = None,
) -> GateResult:
    crit = AcceptanceCriterion(description=f"{ident} mandatory={mandatory}", criterion_type="deterministic")
    return GateResult(
        id=GateResultId(new_id("gate_result")),
        gate_identity=ident,
        gate_type=gate_type,
        target_node_id=target_node_id,
        target_task_id=target_task_id,
        criteria=(crit,),
        status=status,
        verifier_type=verifier_type,
        verifier_identity=verifier_identity,
        failure_reasons=tuple(reasons),
        metadata=dict(metadata or {}) | {"mandatory": mandatory},
    )


def _changed_relpaths(workspace_root: Path, baseline: dict[str, str]) -> list[str]:
    ws = Path(workspace_root).resolve()
    changes: list[str] = []
    for p in sorted(ws.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ws).as_posix()
        if (
            rel.startswith("__pycache__")
            or "/__pycache__/" in rel
            or rel.endswith(".pyc")
            or rel.startswith(".pytest_cache")
            or "/.pytest_cache/" in rel
            or rel.startswith(".mypy_cache")
            or "/.mypy_cache/" in rel
            or rel.startswith(".ruff_cache")
            or "/.ruff_cache/" in rel
            or rel.startswith(".coverage")
            or "/.coverage" in rel
        ):
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        if baseline.get(rel) != h:
            changes.append(rel)
    return changes


class DeterministicGateEngine:
    def __init__(self, spec: GateSpec) -> None:
        self.spec = spec

    def run_all(
        self,
        workspace_root: Path,
        *,
        target_node_id: str | None = None,
        target_task_id: Any | None = None,
    ) -> list[GateResult]:
        out: list[GateResult] = []
        g1 = self._gate_allowed_scope(workspace_root, target_node_id, target_task_id)
        out.append(g1)
        if g1.status != GateStatus.PASS:
            return out
        g2 = self._gate_syntax(workspace_root, target_node_id, target_task_id)
        out.append(g2)
        if g2.status != GateStatus.PASS:
            return out
        g3 = self._gate_cmd(
            "public_test_execution",
            cmd=list(self.spec.public_test_cmd),
            cwd=workspace_root,
            env_extra={},
            nid=target_node_id,
            tid=target_task_id,
            workspace_root=workspace_root,
        )
        out.append(g3)
        if g3.status != GateStatus.PASS:
            return out
        if self.spec.hidden_test_cwd is None or not list(self.spec.hidden_test_cmd):
            g4 = _mk_gate(
                "hidden_test_execution",
                mandatory=True,
                status=GateStatus.PASS,
                reasons=("Skipped: no hidden_test_cwd configured.",),
                target_node_id=target_node_id,
                target_task_id=target_task_id,
                metadata={"skipped": True},
            )
        else:
            g4 = self._gate_cmd(
                "hidden_test_execution",
                cmd=list(self.spec.hidden_test_cmd),
                cwd=self.spec.hidden_test_cwd,
                env_extra=dict(self.spec.hidden_test_env_extra),
                nid=target_node_id,
                tid=target_task_id,
                workspace_root=workspace_root,
            )
        out.append(g4)
        return out

    def _gate_allowed_scope(self, ws: Path, nid, tid) -> GateResult:
        changed = _changed_relpaths(ws, self.spec.baseline_hashes)
        prefixes = tuple("" if p in (None, ".", "./") else str(p).rstrip("/") for p in self.spec.allowed_scope_prefixes)
        violations: list[str] = []
        for rel in changed:
            if "" in prefixes:
                continue
            norm = rel.lstrip("./")
            ok = any(norm == pfx or norm.startswith(pfx + "/") for pfx in prefixes if pfx)
            if not ok:
                violations.append(rel)
        if violations:
            return _mk_gate(
                "allowed_file_change_scope",
                mandatory=True,
                status=GateStatus.FAIL,
                reasons=["out-of-scope change: " + v for v in violations],
                target_node_id=nid,
                target_task_id=tid,
                metadata={"changed_files": changed, "allowed_prefixes": list(prefixes)},
            )
        return _mk_gate(
            "allowed_file_change_scope",
            mandatory=True,
            status=GateStatus.PASS,
            reasons=["scope ok: " + str(len(changed)) + " changed within allowed"],
            target_node_id=nid,
            target_task_id=tid,
            metadata={"changed_files": list(changed)},
        )

    def _gate_syntax(self, ws: Path, nid, tid) -> GateResult:
        changed = _changed_relpaths(ws, self.spec.baseline_hashes)
        pyfiles = [ws / PurePosixPath(r) for r in changed if r.endswith(".py")]
        reasons: list[str] = []
        for f in pyfiles:
            try:
                py_compile.compile(str(f), doraise=True)
            except py_compile.PyCompileError as exc:
                msg = str(exc.msg) if hasattr(exc, "msg") else str(exc)
                reasons.append(f.relative_to(ws).as_posix() + ": " + msg.splitlines()[:1][0])
            except Exception as exc:  # noqa: BLE001
                reasons.append(f.relative_to(ws).as_posix() + ": " + str(exc))
        if reasons:
            return _mk_gate(
                "python_syntax_compile",
                mandatory=True,
                status=GateStatus.FAIL,
                reasons=reasons,
                target_node_id=nid,
                target_task_id=tid,
            )
        return _mk_gate(
            "python_syntax_compile",
            mandatory=True,
            status=GateStatus.PASS,
            reasons=["compiled " + str(len(pyfiles)) + " changed .py files"],
            target_node_id=nid,
            target_task_id=tid,
        )

    def _gate_cmd(
        self,
        ident: str,
        *,
        cmd: list[str],
        cwd: Path,
        env_extra: dict[str, str],
        nid: str | None,
        tid: Any | None,
        workspace_root: Path | None = None,
    ) -> GateResult:
        import os
        env = os.environ.copy()
        env.update(env_extra or {})
        if ident == "hidden_test_execution" and workspace_root is not None:
            parts = [str(Path(workspace_root).resolve())]
            cwd_resolved = str(Path(cwd).resolve())
            if cwd_resolved not in parts:
                parts.append(cwd_resolved)
            env["PYTHONPATH"] = os.pathsep.join(parts)
        elif ident == "public_test_execution":
            cwd_resolved = str(Path(cwd).resolve())
            if env.get("PYTHONPATH"):
                env["PYTHONPATH"] = cwd_resolved + os.pathsep + env["PYTHONPATH"]
            else:
                env["PYTHONPATH"] = cwd_resolved
        cap = self.spec.output_cap_bytes
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                env=env,
                capture_output=True,
                timeout=self.spec.per_gate_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            so, tr = _cap_bytes(exc.stdout or b"", cap)
            se, _ = _cap_bytes(exc.stderr or b"", cap)
            return _mk_gate(
                ident,
                mandatory=True,
                status=GateStatus.ERROR,
                reasons=["timeout after " + str(self.spec.per_gate_timeout_s) + "s: " + str(exc)],
                target_node_id=nid,
                target_task_id=tid,
                metadata={
                    "stdout": so.decode("utf-8", errors="replace"),
                    "stderr": se.decode("utf-8", errors="replace"),
                    "stdout_truncated": tr,
                    "stderr_truncated": False,
                    "failure_category": FailureCategory.TIMEOUT_OR_BUDGET_EXHAUSTION.value,
                    "exit_code": -1,
                },
            )
        except Exception as exc:  # noqa: BLE001
            return _mk_gate(
                ident,
                mandatory=True,
                status=GateStatus.ERROR,
                reasons=["subprocess error: " + str(exc)],
                target_node_id=nid,
                target_task_id=tid,
                metadata={
                    "failure_category": FailureCategory.ENVIRONMENT_FAILURE.value,
                    "exit_code": -1,
                },
            )
        stdout, so_trunc = _cap_bytes(proc.stdout or b"", cap)
        stderr, se_trunc = _cap_bytes(proc.stderr or b"", cap)
        status = GateStatus.PASS if proc.returncode == 0 else GateStatus.FAIL
        reasons: list[str] = []
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", errors="replace").strip().splitlines()[-5:]
            reasons = tail or ["non-zero exit: " + str(proc.returncode)]
        return _mk_gate(
            ident,
            mandatory=True,
            status=status,
            reasons=reasons,
            target_node_id=nid,
            target_task_id=tid,
            metadata={
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
                "stdout_truncated": so_trunc,
                "stderr_truncated": se_trunc,
                "exit_code": proc.returncode,
            },
        )




# ---------------------------------------------------------------------------
# REDI-03L9 — public-safe actionable retry diagnostic.
# Builds a bounded, sanitized excerpt from an ALREADY-PERSISTED public gate
# result so the retrying Builder learns WHY the public tests failed
# (exception type/message, failing public module, concise pytest summary).
# Deterministic; never applied to hidden-gate output.
# ---------------------------------------------------------------------------

PUBLIC_DIAGNOSTIC_MAX_CHARS = 800
_PUBLIC_DIAGNOSTIC_MAX_LINES = 25

_DIAG_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{8,}|(?i:\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*\S+))"
)
_DIAG_ABSWIN_RE = re.compile(r"[A-Za-z]:\\[^\s'\"]*")
_DIAG_POSIX_HOME_RE = re.compile(r"(?<![\w])/(?:home|Users)/[^\s'\":]+")
_DIAG_ENV_RE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}=\S+")


def _sanitize_diagnostic_text(text: str, workspace_root: str | Path | None) -> str:
    """Deterministic scrub: workspace/home/absolute paths, secrets, env values.

    Relative fixture paths (slugify/__init__.py, tests/test_slugify_public.py)
    are intentionally preserved.
    """
    out = text
    if workspace_root is not None:
        root = str(workspace_root)
        for variant in (root, root.replace("\\", "/"), root.replace("/", "\\")):
            if variant:
                out = out.replace(variant, "")
    out = _DIAG_ABSWIN_RE.sub("[ABS_PATH]", out)
    out = _DIAG_POSIX_HOME_RE.sub("[HOME_PATH]", out)
    out = _DIAG_SECRET_RE.sub("[REDACTED]", out)
    out = _DIAG_ENV_RE.sub("[ENV_REDACTED]", out)
    return out.replace("\\", "/")


def _gate_field(gate: Any, name: str) -> Any:
    """Read a gate field from either a GateResult object or its persisted dict."""
    if isinstance(gate, Mapping):
        return gate.get(name)
    return getattr(gate, name, None)


def public_failure_diagnostic(
    gate: Any,
    *,
    workspace_root: str | Path | None = None,
) -> str | None:
    """Bounded sanitized diagnostic excerpt for a failed PUBLIC test gate.

    Prefers the pytest ``short test summary info`` section (exception type +
    message + failing public test/module); falls back to the last
    :data:`_PUBLIC_DIAGNOSTIC_MAX_LINES` non-empty output lines, then stderr,
    then the gate's own failure reasons.  Returns None for any gate other
    than ``public_test_execution`` — hidden-gate output must never reach
    retry feedback.
    """
    if _gate_field(gate, "gate_identity") != "public_test_execution":
        return None
    md = _gate_field(gate, "metadata") or {}
    lines = [ln.rstrip() for ln in str(md.get("stdout") or "").splitlines() if ln.strip()]
    chosen: list[str] = []
    start = None
    for i, ln in enumerate(lines):
        if "short test summary info" in ln:
            start = i
            break
    if start is not None:
        chosen = lines[start:]
    else:
        chosen = lines[-_PUBLIC_DIAGNOSTIC_MAX_LINES:]
    if not chosen:
        chosen = [ln.rstrip() for ln in str(md.get("stderr") or "").splitlines() if ln.strip()]
        chosen = chosen[-_PUBLIC_DIAGNOSTIC_MAX_LINES:]
    if not chosen:
        chosen = [str(r) for r in (_gate_field(gate, "failure_reasons") or [])]
    text = _sanitize_diagnostic_text("\n".join(chosen), workspace_root)
    if len(text) > PUBLIC_DIAGNOSTIC_MAX_CHARS:
        text = text[: PUBLIC_DIAGNOSTIC_MAX_CHARS - 3].rstrip() + "..."
    return text or None


__all__ = ["DeterministicGateEngine", "GateSpec", "public_failure_diagnostic"]

