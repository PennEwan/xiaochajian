"""Local mock of the task-data sync backend (docs/sync-api.md). For development and tests only.

    python scripts\\mock_sync_server.py [--port 18788] [--store mock-sync.json] [-v]

`--store` keeps devices and events in a JSON file across restarts (to check that a client
resuming after the backend was down creates no duplicates). The request log shows method,
path and status only — never event content or tokens.

The same process also serves the four collector endpoints of docs/collector-api.md —
`GET /v1/service-state`, `POST /v1/service-state/terminate`, `POST /v1/device/heartbeat` and
`POST /v1/device/uninstall` — plus their `_mock` inspection routes. They exist so that a
collector which outlives the plugin can be exercised end to end here: terminate is the user's
"stop recording" and must stop it, while uninstall means the device is gone for good (its
token keeps answering 403 `device_revoked`, so the client stops uploading instead of looping).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY = 4 * 1024 * 1024
EVENT_SCHEMAS = ("ctr.event/v2", "csr.event/v1",  # recorder events / native plugin events (docs/sync-api.md §8)
                 "comp.event/v1", "comp.session/v1")  # independent collector (docs/collector-api.md)

# Collector contract (docs/collector-api.md). The mock has one terms version for every device,
# and termination is recorded per device (one device token = one device).
# These values mirror `collector/contract.py`; `tests/test_collector.py` asserts they stay equal,
# so the mock cannot drift away from what the client actually shows the user.
TERMS_VERSION = 4
TERMS_TEXT = (
    "本产品为会话记录服务。"
    "本产品以低于市场价的额度提供充值，对价是你授权我们记录并上传 Codex 会话数据，"
    "含完整会话原文与运行遥测。"
    "记录：对话、模型回复、工具调用与结果、完整会话原文、时间与模型配置，"
    "以及运行遥测（会话数、工具调用次数、错误与时延）。"
    "不记录：密码、API Key、令牌、Cookie、私钥。"
    "记录由随本产品安装的后台组件提供并持续运行；在 Codex 中停用或卸载插件不影响记录。"
    "终止：控制面板「停止记录」，或「设置 → 应用 → 卸载 Codex 助手包」。"
)
TERMS_HASH = "sha256:" + hashlib.sha256(TERMS_TEXT.encode("utf-8")).hexdigest()
TERMS_EFFECTIVE_AT = "2026-11-01T10:00:00Z"
GRACE_HOURS = 72
HEARTBEAT_MAX_BODY = 4 * 1024
# These four endpoints answer 401 `unauthorized`; everything else keeps 401 `device_invalid`.
COLLECTOR_PATHS = ("/v1/service-state", "/v1/service-state/terminate",
                   "/v1/device/heartbeat", "/v1/device/uninstall")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    def __init__(self, store: Path | None = None):
        self.lock = threading.Lock()
        self.store = store
        # device_token -> {device_id, created_at, revoked, uninstalled_at?, uninstall_calls?}
        self.devices: dict[str, dict] = {}
        self.events: dict[str, dict] = {}    # device_id -> {event_id: event}
        self.stats: dict[str, dict] = {}     # device_id -> counters
        self.service_state: dict[str, dict] = {}  # device_id -> {state, terminated_at, updated_at}
        self.heartbeats: dict[str, dict] = {}     # device_id -> latest heartbeat only
        self.fail_next: list[dict] = []
        self.envelopes: list[dict] = []     # each batch without its events (schema, capabilities…)
        self.requests: list[tuple[str, str]] = []
        self.client_headers: list[str] = []
        if store and store.exists():
            doc = json.loads(store.read_text(encoding="utf-8"))
            self.devices, self.events, self.stats = doc["devices"], doc["events"], doc["stats"]
            self.service_state = doc.get("service_state") or {}  # older store files lack these
            self.heartbeats = doc.get("heartbeats") or {}

    def save(self) -> None:
        if not self.store:
            return
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps({"devices": self.devices, "events": self.events, "stats": self.stats,
                                   "service_state": self.service_state, "heartbeats": self.heartbeats},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.store)

    def counters(self, device_id: str) -> dict:
        return self.stats.setdefault(device_id, {"batches": 0, "accepted": 0, "duplicates": 0, "rejected": 0})

    def service(self, device_id: str) -> dict:
        """Per-device service state; a device nobody terminated is `active` from the start."""
        return self.service_state.setdefault(
            device_id, {"state": "active", "terminated_at": None, "updated_at": _now()})

    def devices_by_id(self) -> dict[str, dict]:
        return {dev["device_id"]: dev for dev in self.devices.values()}

    def all_events(self) -> list[dict]:
        out = []
        for device_id, events in self.events.items():
            out += [{**e, "_device_id": device_id} for e in events.values()]
        return sorted(out, key=lambda e: (e["_device_id"], str(e.get("session_id")), e.get("seq") or 0))


def _valid_event(e: object) -> bool:
    return (isinstance(e, dict) and isinstance(e.get("event_id"), str) and e["event_id"]
            and isinstance(e.get("session_id"), str) and isinstance(e.get("seq"), int)
            and e.get("schema") in EVENT_SCHEMAS)


class Handler(BaseHTTPRequestHandler):
    server_version = "ctr-sync-mock/0.1"

    @property
    def state(self) -> State:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # only method, path, status — never bodies
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # ---- plumbing -------------------------------------------------------------------------
    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, code: str, headers: dict | None = None) -> None:
        self._send(status, {"error": {"code": code}}, headers)

    def _raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, raw: bytes) -> object:
        try:
            # utf-8-sig: Windows tooling (PowerShell 5.1, some curl builds) prefixes a BOM.
            return json.loads(raw.decode("utf-8-sig")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            return None

    def _token(self) -> str:
        auth = self.headers.get("Authorization") or ""
        return auth[7:] if auth.startswith("Bearer ") else ""

    def _device(self, invalid_code: str = "device_invalid") -> dict | None:
        """The authenticated device, or None after an error response was sent.

        `invalid_code` is `device_invalid` on the recorder endpoints (docs/sync-api.md §1) and
        `unauthorized` on the collector endpoints (docs/collector-api.md §1). A device that
        reported uninstall keeps a syntactically valid token but no longer exists, so it gets
        403 `device_revoked` — the same answer the client needs to stop uploading for good.
        """
        dev = self.state.devices.get(self._token())
        if not dev:
            self._error(401, invalid_code)
            return None
        if dev.get("revoked") or dev.get("uninstalled_at"):
            self._error(403, "device_revoked")
            return None
        return dev

    def _injected_failure(self, raw: bytes) -> str | None:
        """Apply /_mock/fail-next. Returns 'drop_after_store' when the batch must be stored first."""
        if not self.state.fail_next:
            return None
        rule = self.state.fail_next[0]
        if rule.get("path") and rule["path"] != self.path:  # only fail requests to that path
            return None
        if rule.get("mode") == "drop_after_store":
            if self.path != "/v1/events/batch":
                return None
            self.state.fail_next.pop(0)
            return "drop_after_store"
        rule["count"] = rule.get("count", 1) - 1
        if rule["count"] <= 0:
            self.state.fail_next.pop(0)
        status = int(rule.get("status", 503))
        headers = {"Retry-After": str(rule.get("retry_after", 1))} if status == 429 else {}
        if rule.get("location"):
            headers["Location"] = rule["location"]
        self._error(status, rule.get("code") or ("rate_limited" if status == 429 else "injected"), headers)
        return "sent"

    def _route(self, method: str) -> None:
        self.state.requests.append((method, self.path))
        self.state.client_headers.append(self.headers.get("X-Ctr-Client") or "")
        raw = self._raw_body() if method in ("POST", "DELETE") else b""
        if self.path.startswith("/_mock/"):
            return self._control(method, raw)
        with self.state.lock:
            injected = self._injected_failure(raw)
            if injected == "sent":
                return
            if method == "POST" and self.path == "/v1/devices":
                return self._register(raw)
            if method == "POST" and self.path == "/v1/device/uninstall":
                return self._uninstall()  # repeatable, so it resolves its own token
            dev = self._device("unauthorized" if self.path in COLLECTOR_PATHS else "device_invalid")
            if dev is None:
                return
            if method == "GET" and self.path == "/v1/devices/me":
                return self._me(dev)
            if method == "GET" and self.path == "/v1/service-state":
                return self._service_state(dev)
            if method == "POST" and self.path == "/v1/service-state/terminate":
                return self._terminate(dev)
            if method == "POST" and self.path == "/v1/device/heartbeat":
                return self._heartbeat(dev, raw)
            if method == "POST" and self.path == "/v1/events/batch":
                return self._batch(dev, raw, drop=injected == "drop_after_store")
            if method == "DELETE" and self.path == "/v1/devices/me/data":
                deleted = len(self.state.events.pop(dev["device_id"], {}))
                self.state.save()
                return self._send(200, {"deleted_events": deleted})
            if method == "DELETE" and self.path == "/v1/devices/me":
                token = next(t for t, d in self.state.devices.items() if d is dev)
                del self.state.devices[token]
                self.state.save()
                return self._send(200, {"revoked": True})
        self._error(404, "not_found")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    # ---- API ------------------------------------------------------------------------------
    def _register(self, raw: bytes) -> None:
        body = self._json(raw)
        consent = body.get("consent") if isinstance(body, dict) else None
        if not isinstance(consent, dict) or not isinstance(consent.get("version"), int):
            return self._error(400, "invalid_request")
        token = "ctrd_" + secrets.token_urlsafe(24)
        dev = {"device_id": "dev_" + secrets.token_hex(8), "created_at": _now(), "revoked": False}
        self.state.devices[token] = dev
        self.state.save()
        self._send(201, {"device_id": dev["device_id"], "device_token": token, "created_at": dev["created_at"]})

    def _me(self, dev: dict) -> None:
        events = self.state.events.get(dev["device_id"], {})
        sessions = {e.get("session_id") for e in events.values()}
        last = max((e.get("recorded_at") or "" for e in events.values()), default=None)
        self._send(200, {"device_id": dev["device_id"], "event_count": len(events),
                         "session_count": len(sessions), "last_event_at": last,
                         "created_at": dev["created_at"]})

    def _batch(self, dev: dict, raw: bytes, drop: bool) -> None:
        if len(raw) > MAX_BODY:
            return self._error(413, "batch_too_large")
        body = self._json(raw)
        if not isinstance(body, dict) or not isinstance(body.get("events"), list):
            return self._error(400, "invalid_batch")
        self.state.envelopes.append({k: v for k, v in body.items() if k != "events"})
        store = self.state.events.setdefault(dev["device_id"], {})
        counters = self.state.counters(dev["device_id"])
        counters["batches"] += 1
        results = []
        for e in body["events"]:
            if not _valid_event(e):
                eid = e.get("event_id") if isinstance(e, dict) else None
                results.append({"event_id": eid, "status": "rejected", "error": {"code": "invalid_event"}})
                counters["rejected"] += 1
            elif e["event_id"] in store:
                results.append({"event_id": e["event_id"], "status": "duplicate"})
                counters["duplicates"] += 1
            else:
                store[e["event_id"]] = {**e, "_arrival": sum(len(v) for v in self.state.events.values())}
                results.append({"event_id": e["event_id"], "status": "accepted"})
                counters["accepted"] += 1
        self.state.save()
        if drop:  # stored, but the client never hears back
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return
        n = {s: sum(1 for r in results if r["status"] == s) for s in ("accepted", "duplicate", "rejected")}
        self._send(200, {"accepted": n["accepted"], "duplicates": n["duplicate"], "rejected": n["rejected"],
                         "results": results})

    # ---- collector API (docs/collector-api.md) --------------------------------------------
    def _service_state(self, dev: dict) -> None:
        rec = self.state.service(dev["device_id"])
        self._send(200, {"state": rec["state"],
                         "terms_version": TERMS_VERSION,
                         "terms_hash": TERMS_HASH,
                         "effective_at": TERMS_EFFECTIVE_AT,
                         "grace_hours": GRACE_HOURS,
                         "terminated_at": rec["terminated_at"],
                         "server_time": _now()})

    def _terminate(self, dev: dict) -> None:
        rec = self.state.service(dev["device_id"])
        if not rec["terminated_at"]:  # idempotent: the first termination time is the only one
            now = _now()
            rec.update(state="terminated", terminated_at=now, updated_at=now)
            self.state.save()
        self._send(200, {"state": "terminated", "terminated_at": rec["terminated_at"]})

    def _heartbeat(self, dev: dict, raw: bytes) -> None:
        if len(raw) > HEARTBEAT_MAX_BODY:
            return self._error(413, "too_large")
        body = self._json(raw)
        if not isinstance(body, dict):
            return self._error(400, "invalid_request")
        # Only the newest beat per device is kept: it carries the whole queue depth, so an older
        # one can never say anything a newer one does not.
        prev = self.state.heartbeats.get(dev["device_id"]) or {}
        self.state.heartbeats[dev["device_id"]] = {
            "device_id": dev["device_id"], "received_at": _now(),
            "count": int(prev.get("count") or 0) + 1, "bytes": len(raw), "heartbeat": body}
        self.state.save()
        self._send(200, {"ok": True, "server_time": _now()})

    def _uninstall(self) -> None:
        """`POST /v1/device/uninstall`: the device is gone for good, and repeating it is harmless."""
        dev = self.state.devices.get(self._token())
        if not dev:
            return self._error(401, "unauthorized")
        if dev.get("revoked"):
            return self._error(403, "device_revoked")
        if not dev.get("uninstalled_at"):
            dev["uninstalled_at"] = _now()
        dev["uninstall_calls"] = int(dev.get("uninstall_calls") or 0) + 1
        self.state.save()
        self._send(200, {"ok": True})

    def _service_view(self) -> dict[str, dict]:
        """Per-device view for `/_mock/service-state`: the answer plus when it was last written."""
        out = {}
        for device_id, dev in self.state.devices_by_id().items():
            rec = self.state.service_state.get(device_id) or {}
            out[device_id] = {"state": rec.get("state", "active"),
                              "terms_version": TERMS_VERSION,
                              "terminated_at": rec.get("terminated_at"),
                              "updated_at": rec.get("updated_at"),
                              "uninstalled_at": dev.get("uninstalled_at")}
        return out

    # ---- control --------------------------------------------------------------------------
    def _control(self, method: str, raw: bytes) -> None:
        with self.state.lock:
            if method == "POST" and self.path == "/_mock/fail-next":
                rule = self._json(raw)
                if not isinstance(rule, dict):
                    return self._error(400, "invalid_request")
                self.state.fail_next.append(rule)
                return self._send(200, {"queued": len(self.state.fail_next)})
            if method == "POST" and self.path == "/_mock/revoke-all":
                for dev in self.state.devices.values():
                    dev["revoked"] = True
                self.state.save()
                return self._send(200, {"revoked": len(self.state.devices)})
            if method == "GET" and self.path == "/_mock/events":
                return self._send(200, {"events": self.state.all_events()})
            if method == "GET" and self.path == "/_mock/envelopes":
                return self._send(200, {"envelopes": self.state.envelopes})
            if method == "GET" and self.path == "/_mock/stats":
                return self._send(200, {"devices": len(self.state.devices), "stats": self.state.stats,
                                        "events": {d: len(e) for d, e in self.state.events.items()}})
            if method == "GET" and self.path == "/_mock/service-state":
                return self._send(200, {"service_state": self._service_view()})
            if method == "GET" and self.path == "/_mock/heartbeats":
                return self._send(200, {"heartbeats": self.state.heartbeats})
            if method == "GET" and self.path == "/_mock/uninstalls":
                gone = [{"device_id": dev["device_id"], "uninstalled_at": dev["uninstalled_at"],
                         "calls": int(dev.get("uninstall_calls") or 0)}
                        for dev in self.state.devices_by_id().values() if dev.get("uninstalled_at")]
                return self._send(200, {"uninstalls": sorted(gone, key=lambda g: g["uninstalled_at"])})
        self._error(404, "not_found")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        if getattr(self, "verbose", False):
            super().handle_error(request, client_address)


def make_server(host: str, port: int, store: Path | None = None, verbose: bool = False) -> Server:
    server = Server((host, port), Handler)
    server.state = State(store)  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock task-data sync backend (development only)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18788)
    parser.add_argument("--store", type=Path,
                        help="persist devices, events, service state and heartbeats in this JSON file")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    server = make_server(args.host, args.port, args.store, args.verbose)
    print(f"mock sync backend on http://{args.host}:{server.server_address[1]}  (Ctrl+C to stop)")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
