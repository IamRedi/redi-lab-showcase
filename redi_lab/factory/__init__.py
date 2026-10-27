"""REDI minimal factory controller, isolation, contexts — Layer C orchestration.

Depends only on: redi_lab.{domain,persistence,evidence,replay,runtime,gates} + stdlib.
Domain layer NEVER imports this package.
"""

from .isolation import (
    IsolatedWorkspace,
    ScopeViolation,
    apply_patches,
    assert_in_workspace,
    baseline_file_hashes,
)
from .builder_context import (
    HIDDEN_TEST_EXCLUSION_CODE,
    PatchEdit,
    build_builder_context,
    build_reviewer_context,
)

try:  # pragma: no cover - import guard only
    from .controller import (
        FinalRunOutcome,
        MinimalFactoryController,
        RunDeps,
        SimulatedCrash,
        make_canonical_slug_fixture_refs,
    )
    from ..gates import GateSpec  # noqa: F401 - re-exported for convenience
except Exception:  # pragma: no cover - only if controller.py not yet written
    FinalRunOutcome = None  # type: ignore[assignment,misc]
    MinimalFactoryController = None  # type: ignore[assignment,misc]
    RunDeps = None  # type: ignore[assignment,misc]
    SimulatedCrash = None  # type: ignore[assignment,misc]
    make_canonical_slug_fixture_refs = None  # type: ignore[assignment,misc]
    from ..gates import GateSpec  # noqa: F401,F811

__all__ = [
    "IsolatedWorkspace",
    "ScopeViolation",
    "apply_patches",
    "assert_in_workspace",
    "baseline_file_hashes",
    "HIDDEN_TEST_EXCLUSION_CODE",
    "PatchEdit",
    "build_builder_context",
    "build_reviewer_context",
    "FinalRunOutcome",
    "GateSpec",
    "MinimalFactoryController",
    "RunDeps",
    "SimulatedCrash",
    "make_canonical_slug_fixture_refs",
]
