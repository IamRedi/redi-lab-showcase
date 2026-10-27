"""REDI Workbench local server (stdlib only; local-only; no auth/billing)."""

from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import re

from .engine import BACKENDS, WorkbenchAdapter
from .runtime_identity import normal_app_runtime_status
from .presentation import present_snapshot
from .engine import WORKBENCH_ROOT
from .crew import CREW

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _backend_options_html() -> str:
    """Server-render the select options from the live registry so the
    rendered select always contains every backend, even without JS."""
    options = []
    for backend_id, meta in BACKENDS.items():
        active = meta.get("active") == "true"
        options.append(
            f'<option value="{backend_id}"{"" if active else " disabled"}>'
            f'{meta["label"]}{"" if active else " (unavailable)"}</option>'
        )
    return chr(10).join(options)
_LAST_ADAPTER: WorkbenchAdapter | None = None


def make_handler(adapter: WorkbenchAdapter) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if urlsplit(self.path).path in ("/", "/index.html"):
                page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
                page = page.replace("<!--BACKEND_OPTIONS-->", _backend_options_html())
                page = page.replace("/*WORKBENCH_STYLES*/", (STATIC_DIR / "workbench.css").read_text(encoding="utf-8"))
                script = "const WORKBENCH_PERSONAS = " + json.dumps(CREW) + ";\n"
                script += (STATIC_DIR / "view-model.js").read_text(encoding="utf-8") + "\n" + (STATIC_DIR / "workbench.js").read_text(encoding="utf-8")
                page = page.replace("//WORKBENCH_SCRIPT", script)
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/server":
                # which process is actually answering + what ITS env sees
                # (booleans only; the key value is never exposed)
                self._json({
                    "pid": os.getpid(),
                    "started_at": getattr(self.server, "created_at", None),
                    "normal_app_runtime": normal_app_runtime_status(),
                    "live_provider": {
                        "key_present": bool(os.environ.get("REDI_OPENAI_API_KEY")),
                        "live_flag_is_1": os.environ.get("REDI_LIVE_PROVIDER") == "1",
                    },
                })
            elif self.path == "/api/backends":
                self.send_response(200)
                body = json.dumps({"backends": __import__(
                    "redi_lab.workbench.engine", fromlist=["BACKENDS"]).BACKENDS}).encode("utf-8")
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/builds/"):
                segments = re.fullmatch(r"/api/builds/([a-f0-9]{12})/segments", self.path)
                if segments:
                    snap = adapter.snapshot(segments.group(1))
                    self._json({'build_id': segments.group(1), 'segments': snap.get('owner_decision', {}).get('segments', [])})
                else:
                    self._json(present_snapshot(adapter.snapshot(self.path.rsplit("/", 1)[-1]), WORKBENCH_ROOT))
            elif self.path == "/api/builds":
                self._json({"builds": adapter.build_ids()})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self) -> None:  # noqa: N802
            owner_action = re.fullmatch(r"/api/builds/([a-f0-9]{12})/(decisions|resume)", self.path)
            if owner_action:
                if self.headers.get('Origin') not in (None, 'http://' + self.headers.get('Host', '')) or self.headers.get('Content-Type') != 'application/json':
                    self._json({'error': 'same-origin JSON request required'}, 403)
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 <= length <= 16384: raise ValueError('invalid request length')
                    payload = json.loads(self.rfile.read(length) or b'{}')
                    if not isinstance(payload, dict): raise ValueError('JSON object required')
                    build_id, action = owner_action.groups()
                    result = adapter.submit_owner_decision(build_id, payload) if action == 'decisions' else adapter.resume_build(build_id)
                    self._json(result)
                except (ValueError, KeyError, OSError, json.JSONDecodeError) as exc:
                    self._json({'error': str(exc)}, 409)
                return
            action = re.fullmatch(r"/api/builds/([a-f0-9]{12})/(open|stop)", self.path)
            if action:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= 16384:
                        raise ValueError("invalid request length")
                    self.rfile.read(length)
                except ValueError as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if self.headers.get("Origin") not in (None, "http://" + self.headers.get("Host", "")) or self.headers.get("Content-Type") != "application/json":
                    self._json({"error": "same-origin JSON request required"}, 403)
                    return
                try:
                    self._json(adapter.app_action(*action.groups()))
                except Exception as exc:
                    self._json({"error": str(exc)}, 409)
                return
            if self.path != "/api/builds":
                self._json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length", 0))
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json({"error": "invalid JSON"}, 400)
                return
            self._json(adapter.start_build(
                payload.get("prompt", ""),
                backend=payload.get("backend"),
                ceiling=payload.get("cost_ceiling_usd") or payload.get("ceiling"),
                visual_ceiling=payload.get("visual_ceiling_usd"),
                max_visual_calls=payload.get("max_visual_generation_calls"),
                max_visual_recovery=payload.get("max_visual_recovery_generations"),
                key=payload.get("key"),
                live_flag=payload.get("live_flag"),
                client_factory=payload.get("client_factory"),
            ))

        def log_message(self, *_args) -> None:  # silence console
            return

    return Handler


class WorkbenchHTTPServer(ThreadingHTTPServer):
    # HTTPServer's allow_reuse_address=1 (SO_REUSEADDR) lets a SECOND server
    # silently co-bind a port that is already listening on Windows; the old
    # process keeps answering while the new (e.g. re-armed) one only appears
    # to serve. Refuse the double bind so re-arming fails loudly instead.
    allow_reuse_address = False


def serve(host: str = "127.0.0.1", port: int = 8765, adapter: WorkbenchAdapter | None = None,
          ) -> tuple[WorkbenchHTTPServer, str]:
    global _LAST_ADAPTER
    adapter = adapter or WorkbenchAdapter()
    _LAST_ADAPTER = adapter
    server = WorkbenchHTTPServer((host, port), make_handler(adapter))
    server.created_at = time.time()
    url = f"http://{host}:{server.server_port}/"
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, url


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Start the local REDI Workbench")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server, url = serve(port=args.port)
    print(url, flush=True)
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        server.shutdown(); server.server_close()
