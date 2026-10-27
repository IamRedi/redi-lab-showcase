"""REDI Workbench adapter layer — the clean API between any UI and the engine.

The UI talks ONLY to this adapter (HTTP API in server.py).  Worker/provider
backends register here; v0 ships the dry factory backend.  Later OpenAI /
Claude / Gemini / Ollama backends plug in without touching the UI.
"""

from __future__ import annotations

from .engine import BACKENDS, WorkbenchAdapter, WorkbenchBuild

__all__ = ["BACKENDS", "WorkbenchAdapter", "WorkbenchBuild"]
