"""Minimal client for the official `codex app-server` stdio protocol.

Used by `ctr status` (read-only `hooks/list`) and by the live test scripts.
The desktop App talks to the same protocol, but we always start our *own*
app-server process from the App's kernel; nothing attaches to the running App.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from pathlib import Path


class AppServer:
    def __init__(self, exe: Path, cwd: str, log_path: Path | None = None):
        from .kernel import NO_WINDOW  # no console flash when called from a windowless process

        self.proc = subprocess.Popen([str(exe), "app-server"], cwd=cwd, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self._next_id = 0
        self._responses: dict[int, dict] = {}
        self._cond = threading.Condition()
        self.notifications: list[dict] = []
        self.server_requests: queue.Queue = queue.Queue()
        self._log = open(log_path, "w", encoding="utf-8") if log_path else None
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if self._log:
                self._log.write(json.dumps(msg, ensure_ascii=False) + "\n")
                self._log.flush()
            with self._cond:
                if "id" in msg and "method" in msg:
                    self.server_requests.put(msg)
                elif "id" in msg:
                    self._responses[msg["id"]] = msg
                else:
                    self.notifications.append(msg)
                self._cond.notify_all()

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None, timeout: float = 120) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"id": rid, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        with self._cond:
            while rid not in self._responses:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(method)
                self._cond.wait(left)
            resp = self._responses.pop(rid)
        if "error" in resp:
            raise RuntimeError(f"{method}: {resp['error']}")
        return resp.get("result") or {}

    def respond(self, request_id: object, result: dict) -> None:
        """Answer a server-initiated request (e.g. a command approval)."""
        self._send({"id": request_id, "result": result})

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def mark(self) -> int:
        """Position to pass as `after=` so wait_for ignores earlier notifications."""
        with self._cond:
            return len(self.notifications)

    def wait_for(self, pred, timeout: float = 300, after: int = 0) -> dict:
        deadline = time.monotonic() + timeout
        seen = after
        with self._cond:
            while True:
                for msg in self.notifications[seen:]:
                    if pred(msg):
                        return msg
                seen = len(self.notifications)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("notification")
                self._cond.wait(left)

    def initialize(self) -> dict:
        result = self.request("initialize", {"clientInfo": {"name": "ctr_live_tests", "title": "CTR live tests",
                                                            "version": "0.1.0"}})
        self.notify("initialized")
        return result

    def plugin_hooks(self, cwd: str, plugin_id: str) -> list[dict]:
        result = self.request("hooks/list", {"cwds": [cwd]})
        return [h for entry in result.get("data", []) for h in entry.get("hooks", [])
                if h.get("pluginId") == plugin_id or str(h.get("key", "")).startswith(plugin_id + ":")]

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        # stdout is left to the reader thread: app-server's own children (a marketplace `git fetch`,
        # say) inherit the pipe, so closing it here could wait for them indefinitely.
        if self._log:
            self._log.close()
