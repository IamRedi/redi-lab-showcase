"""Local HTTP transport for the declared generated-product form contract.

All persistence and product operations are performed by generated service/core.
This host does not synthesize product data or substitute implementation results.
"""
from __future__ import annotations

import argparse
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

def make_handler(service, database, contract, identity):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, body, status=200, content_type="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers(); self.wfile.write(data)

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path == "/api/health":
                self.reply(json.dumps(identity), content_type="application/json")
            elif path.path == "/api/state":
                svc = getattr(service, contract["factory"])(path=database)
                try: state = {key: getattr(svc, method)() for key, method in contract["state"].items()}
                finally: svc.close()
                self.reply(json.dumps(state), content_type="application/json")
            elif path.path == "/":
                page = getattr(service, contract["render"])(path=database)
                notice = parse_qs(path.query).get("notice", [""])[0]
                if notice:
                    page = page.replace("</body>", "<p role='status'>" + escape(notice) + "</p></body>")
                self.reply(page)
            else: self.reply("Not found", 404)

        def do_POST(self):
            route = contract["routes"].get(self.path)
            if route is None:
                self.reply("Not found", 404); return
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + self.headers.get("Host", ""):
                self.reply("Origin rejected", 403); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384: raise ValueError("invalid form length")
                form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                values = {name: (int(form[name][0]) if kind == "int" else form[name][0])
                          for name, kind in route["fields"].items()}
                svc = getattr(service, contract["factory"])(path=database)
                try: result = getattr(svc, route["method"])(**values)
                finally: svc.close()
                notice = route["method"] + " succeeded" + (": " + str(result) if result is not None else "")
                self.send_response(303)
                self.send_header("Location", "/?" + urlencode({"notice": notice}))
                self.send_header("Content-Length", "0"); self.end_headers()
            except (ValueError, KeyError) as exc:
                self.reply("<p>" + escape(str(exc)) + "</p><a href='/'>Back</a>", 400)

        def log_message(self, *_args): pass
    return Handler


class ProductHTTPServer(HTTPServer):
    allow_reuse_address = False


def verify_package(project):
    """Verify the entire immutable delivery, excluding mutable owner data."""
    manifest_path = project / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = {p.relative_to(project).as_posix() for p in project.rglob("*")
              if p.is_file() and p.relative_to(project).parts[0] != "data" and p != manifest_path}
    if actual != set(manifest["files"]):
        raise ValueError("packaged file inventory changed")
    for name, expected in manifest["files"].items():
        path = (project / name).resolve()
        if not path.is_relative_to(project) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("packaged artifact changed: " + name)
    return hashlib.sha256(manifest_path.read_bytes()).hexdigest()


def create_server(project, database, port=8766, contract=None):
    project = Path(project).resolve()
    fingerprint = None
    if (project / "app.json").is_file():
        fingerprint = verify_package(project)
        contract = json.loads((project / "app.json").read_text(encoding="utf-8"))
    if contract is None:
        from .normal_app_contracts import WEB_CONTRACT
        contract = {"module": "mini_crm.service", "factory": "build_crm", "render": "run_ui",
                    "state": {"dashboard": "dashboard", "jobs": "jobs"}, "routes": WEB_CONTRACT}
    package = contract["module"].split(".")[0]
    if not (project / Path(*contract["module"].split("."))).with_suffix(".py").is_file():
        raise ValueError("project is missing its declared service module")
    # A dedicated process is the launch contract; never reuse another project's modules.
    if any(n == package or n.startswith(package + ".") for n in sys.modules):
        raise RuntimeError("generated package already loaded; start a fresh product host")
    sys.path.insert(0, str(project))
    service = importlib.import_module(contract["module"])
    if not all(callable(getattr(service, name, None)) for name in (contract["factory"], contract["render"])):
        raise ValueError("declared runtime entrypoints are missing")
    database = str(Path(database).resolve())
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    identity = {"pid": os.getpid(), "project_path": str(project), "database": database,
                "package_sha256": fingerprint}
    return ProductHTTPServer(("127.0.0.1", port), make_handler(service, database, contract, identity))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--database", help="SQLite file; default: app/data/app.sqlite3")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if args.database is None:
        args.database = str(Path(args.project) / "data" / "app.sqlite3")
    server = create_server(args.project, args.database, args.port)
    info = {"url": f"http://127.0.0.1:{server.server_port}/", "pid": os.getpid(),
            "project_path": str(Path(args.project).resolve()),
            "database": str(Path(args.database).resolve())}
    if args.ready_file:
        args.ready_file.write_text(json.dumps(info), encoding="utf-8")
    print(json.dumps(info), flush=True)
    if args.open_browser:
        import webbrowser
        webbrowser.open(info["url"])
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == "__main__": main()
