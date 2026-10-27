"""Normal-app process/source identity, captured once and checked before dispatch.

Editing the checkout does not reload Python modules in a running Workbench.
Require a process restart if any member of this execution cohort changes.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from types import MappingProxyType


SOURCE_NAMES = (
    "runtime_identity.py", "engine.py", "server.py", "crew.py", "presentation.py", "product_identity.py",
    "normal_app.py", "normal_app_contracts.py", "normal_app_gates.py",
    "product_integrity.py", "product_host.py", "product_packaging.py",
    "professional/spec.py", "professional/spec_schema.py", "professional/contracts.py", "professional/runtime.py",
    "professional/browser.py", "professional/quality.py", "professional/provider.py", "professional/pipeline.py", "professional/recovery.py",
    "professional/review.py", "professional/acceptance.py", "professional/acceptance_browser.js",
    "professional/qualification.py", "professional/continuation.py", "professional/requalification.py",
    "professional/openai_visual.py", "professional/visual.py", "build_budget.py",
    "static/index.html", "static/workbench.css", "static/view-model.js", "static/workbench.js",
    "../qualification_executor.py", "../mcw_pricing.py",
)


class StaleRuntimeError(RuntimeError):
    pass


class RuntimeIdentity:
    def __init__(self, paths):
        self.paths = MappingProxyType({name: Path(path).resolve() for name, path in paths.items()})
        self.loaded_source_hashes = MappingProxyType(self._read_hashes())
        self.loaded_source_metadata = MappingProxyType(self._read_metadata())
        self.captured_at = time.time()
        self.fingerprint = hashlib.sha256(json.dumps(
            dict(self.loaded_source_hashes), sort_keys=True).encode()).hexdigest()

    def _read_hashes(self):
        hashes = {}
        for name, path in self.paths.items():
            try:
                hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                hashes[name] = None
        return hashes

    def _read_metadata(self):
        metadata = {}
        for name, path in self.paths.items():
            try:
                stat = path.stat()
                metadata[name] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            except OSError:
                metadata[name] = None
        return metadata

    def status(self):
        disk = self._read_hashes()
        metadata = self._read_metadata()
        changed = [name for name, sha in self.loaded_source_hashes.items()
                   if sha is None or disk[name] != sha]
        changes = [{"name": name, "path": str(self.paths[name]),
                    "startup_sha256": self.loaded_source_hashes[name], "current_sha256": disk[name],
                    "startup_metadata": self.loaded_source_metadata[name], "current_metadata": metadata[name],
                    "content_changed": self.loaded_source_hashes[name] != disk[name]}
                   for name in changed]
        return {"fresh": not changed, "fingerprint": self.fingerprint,
                "process_id": os.getpid(),
                "captured_at": self.captured_at, "changed_files": changed,
                "source_contract": "resolved_immutable_source_bytes_v2", "changed_source_details": changes,
                "loaded_source_hashes": dict(self.loaded_source_hashes),
                "disk_source_hashes": disk}

    def require_current(self):
        status = self.status()
        if not status["fresh"]:
            raise StaleRuntimeError("normal-app runtime source changed or is unreadable; "
                                    "restart Workbench before building: " + ", ".join(status["changed_files"]))
        return status


# Captured when the engine/normal-app is first imported, never at run completion.
NORMAL_APP_RUNTIME = RuntimeIdentity({name: Path(__file__).parent / name for name in SOURCE_NAMES})


def normal_app_runtime_status():
    return NORMAL_APP_RUNTIME.status()


def require_current_normal_app_runtime():
    return NORMAL_APP_RUNTIME.require_current()
