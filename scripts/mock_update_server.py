"""Mock update server for Codex 助手包 (contract: docs/update-api.md). Development and tests only.

    GET  /manifest.json          {"product": "codex-companion", "latest": {...} | null}
    GET  /files/<name>           the installer named by the current release

Control (tests / acceptance):
    POST /_mock/release    {"version": "1.0.1", "path": "D:\\...\\setup.exe", "requires_retrust": false,
                            "min_kernel": null, "notes": "...", "tamper": false}   (null body: no release)
    POST /_mock/fail-next  {"status": 503, "count": 1}  or  {"mode": "redirect", "count": 1}
    GET  /_mock/requests   what was asked, with the User-Agent of each request

    python scripts\\mock_update_server.py [--port 18789] [--release 1.0.1=dist\\CodexCompanion-Setup-1.0.1.exe]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.release: dict | None = None
        self.fail: list[dict] = []
        self.requests: list[dict] = []

    def set_release(self, body: dict | None) -> None:
        if body is None:
            self.release = None
            return
        path = Path(body["path"])
        data = path.read_bytes()
        self.release = {"version": body["version"], "path": str(path), "name": path.name,
                        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                        "requires_retrust": bool(body.get("requires_retrust")), "min_kernel": body.get("min_kernel"),
                        "notes": body.get("notes") or f"Codex 助手包 {body['version']}", "tamper": bool(body.get("tamper"))}


class Handler(BaseHTTPRequestHandler):
    server_version = "MockUpdate/1"

    def log_message(self, fmt, *args):  # quiet
        pass

    @property
    def state(self) -> State:
        return self.server.state

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, doc) -> None:
        self._send(status, json.dumps(doc, ensure_ascii=False).encode("utf-8"))

    def _failure(self) -> bool:
        with self.state.lock:
            if not self.state.fail:
                return False
            rule = self.state.fail[0]
            rule["count"] = rule.get("count", 1) - 1
            if rule["count"] <= 0:
                self.state.fail.pop(0)
        if rule.get("mode") == "redirect":
            self._send(302, headers={"Location": "http://127.0.0.1:1/elsewhere"})
        else:
            self._json(int(rule.get("status", 503)), {"error": "mock_failure"})
        return True

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/_mock/requests":
            with self.state.lock:
                return self._json(200, {"requests": list(self.state.requests)})
        with self.state.lock:
            self.state.requests.append({"path": path, "user_agent": self.headers.get("User-Agent")})
        if self._failure():
            return None
        rel = self.state.release
        if path == "/manifest.json":
            host = f"http://{self.headers.get('Host')}"
            latest = None
            if rel:
                latest = {k: rel[k] for k in ("version", "sha256", "size", "requires_retrust", "min_kernel", "notes")}
                latest["url"] = f"{host}/files/{rel['name']}"
                latest["published_at"] = "2026-10-29T00:00:00Z"
            return self._json(200, {"product": "codex-companion", "channel": "stable", "latest": latest})
        if path.startswith("/files/") and rel and path == f"/files/{rel['name']}":
            data = Path(rel["path"]).read_bytes()
            if rel["tamper"]:
                data = data[:-1] + bytes([data[-1] ^ 0xFF])
            return self._send(200, data, "application/octet-stream")
        return self._json(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"null")
        if self.path == "/_mock/release":
            with self.state.lock:
                self.state.set_release(body)
            return self._json(200, {"ok": True, "release": self.state.release})
        if self.path == "/_mock/fail-next":
            with self.state.lock:
                self.state.fail.append(dict(body))
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not_found"})


def make_server(host: str = "127.0.0.1", port: int = 18789) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.state = State()
    return server


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18789)
    parser.add_argument("--release", help="VERSION=PATH of the installer to offer")
    args = parser.parse_args()
    server = make_server("127.0.0.1", args.port)
    if args.release:
        version, path = args.release.split("=", 1)
        server.state.set_release({"version": version, "path": path})
    print(f"mock update server on http://127.0.0.1:{args.port}/manifest.json", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
