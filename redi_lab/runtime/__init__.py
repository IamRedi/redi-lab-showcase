"""REDI runtime abstraction (Layer C → Layer B interface + fakes + 1 live adapter).

Dependency direction: this package depends ONLY on redi_lab.domain (for typed
artifacts returned) and stdlib.  No domain module imports runtime code.
"""

from .contracts import NodeRunResult, PatchEdit, ReviewerVerdict, WorkerRunner
from .fake_worker import FakeScriptedWorker, fake_reviewer_pass, fake_reviewer_fail, fake_reviewer_escalate
from .live_provider_adapter import LiveProviderAdapter, LiveProviderConfig

__all__ = [
    "NodeRunResult",
    "PatchEdit",
    "ReviewerVerdict",
    "WorkerRunner",
    "FakeScriptedWorker",
    "fake_reviewer_pass",
    "fake_reviewer_fail",
    "fake_reviewer_escalate",
    "LiveProviderAdapter",
    "LiveProviderConfig",
]
