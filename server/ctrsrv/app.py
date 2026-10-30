"""HTTP surface: the client API (docs/*-api.md) plus a token-protected /admin for operations.

Deliberate choices:
  * one handler per (method, path) in a table — no framework, no magic;
  * a failed request never returns a redirect (clients treat 3xx on a POST as "unknown outcome");
  * bodies are size-capped before they are read into memory;
  * nothing logs a token, an account or an event body.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, terms, web
from .store import Store, STATE_TERMINATED

# Event schemas a client may send: the recorder, the native plugin, the independent collector.
EVENT_SCHEMAS = ("ctr.event/v2", "csr.event/v1", "comp.event/v1", "comp.session/v1")
MAX_BODY = 8 << 20          # a batch is documented at ≤1 MiB; accept 8 and reject the rest
MAX_EVENT_BODY = 1 << 20
RATE_BATCHES_PER_MIN = 120
COLLECTOR_PATHS = ("/v1/service-state", "/v1/service-state/terminate",
                   "/v1/device/heartbeat", "/v1/device/uninstall")
# A terminated device still needs these: the verdict itself, the repeat of its own stop, the
# repeat of its uninstall report, and its right to delete what it already sent. Everything else
# is refused.
TERMINATED_ALLOWED = ("/v1/service-state", "/v1/service-state/terminate",
                      "/v1/device/uninstall", "/v1/devices/me/data")


@dataclass
class Config:
    db: Path = field(default_factory=lambda: Path(os.environ.get("CTRSRV_DB", "data/ctrsrv.sqlite3")))
    host: str = field(default_factory=lambda: os.environ.get("CTRSRV_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("CTRSRV_PORT", "8080")))
    admin_token: str | None = field(default_factory=lambda: os.environ.get("CTRSRV_ADMIN_TOKEN") or None)
    log: Path | None = None


def log_line(path: str, status: int, ms: float, note: str = "") -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{stamp} {status:3d} {ms:6.1f}ms {path}{(' ' + note) if note else ''}", flush=True)


class _RateLimiter:
    """Per-device batch budget. Small and in-memory: a restart just resets the window."""

    def __init__(self, per_minute: int):
        self.limit = per_minute
        self._lock = threading.Lock()
        self._seen: dict[str, list[float]] = {}

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            window = [t for t in self._seen.get(key, []) if now - t < 60.0]
            if len(window) >= self.limit:
                self._seen[key] = window
                return False
            window.append(now)
            self._seen[key] = window
            return True


class BadRequest(Exception):
    def __init__(self, code: str, message: str = "", status: int = 400):
        super().__init__(message or code)
        self.code, self.message, self.status = code, message or code, status


@dataclass
class Html:
    """A server-rendered page: the dispatcher sends it as text/html instead of JSON."""
    body: bytes


def _int(value, default: int, low: int, high: int) -> int:
    """Query parameters are attacker-controlled: a bad `limit` is a 400, never a 500."""
    if value is None or value == "":
        return default
    try:
        return max(low, min(high, int(str(value))))
    except (TypeError, ValueError):
        raise BadRequest("invalid_query", f"参数不是整数：{value}") from None


class Api:
    """All request handling, free of HTTP details so it can be tested directly."""

    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.db)
        self.limiter = _RateLimiter(RATE_BATCHES_PER_MIN)

    # --- helpers ----------------------------------------------------------------------------
    def error(self, code: str, message: str = "", status: int = 400) -> BadRequest:
        return BadRequest(code, message, status)

    def auth(self, token: str | None, path: str, allow_uninstalled: bool = False) -> dict:
        device = self.store.device_by_token(token or "")
        if device is None:
            code = "unauthorized" if path in COLLECTOR_PATHS else "device_invalid"
            raise BadRequest(code, "设备令牌无效", 401)
        # An uninstalled device is gone for every endpoint except the uninstall report itself,
        # which stays repeatable (docs/collector-api.md §5: 第二次及以后同样返回 200).
        if device.get("uninstalled_at") and not allow_uninstalled:
            raise BadRequest("device_revoked", "设备已卸载", 403)
        # A terminated device still gets its own verdict from /v1/service-state (that is how the
        # collector learns to stop) and may keep repeating its uninstall report; everything else
        # is refused, so no data can arrive after the user's stop.
        if (device.get("state") == STATE_TERMINATED and not allow_uninstalled
                and path not in TERMINATED_ALLOWED):
            raise BadRequest("device_revoked", "设备已终止记录", 403)
        return device

    def auth_admin(self, token: str | None) -> None:
        expected = self.config.admin_token
        if not expected:
            raise BadRequest("admin_disabled", "未设置 CTRSRV_ADMIN_TOKEN", 403)
        if token != expected:
            raise BadRequest("unauthorized", "管理令牌无效", 401)
    # --- client API -------------------------------------------------------------------------
    def health(self, body, query, token) -> tuple[int, dict]:
        return 200, {"ok": True, "version": __version__, "terms_version": terms.TERMS_VERSION}

    def register_device(self, body: dict, query, token) -> tuple[int, dict]:
        if not isinstance(body, dict):
            raise self.error("invalid_request", "正文必须是 JSON 对象")
        consent = body.get("consent") if isinstance(body.get("consent"), dict) else {}
        created = self.store.create_device(body.get("client"), body.get("platform"), consent,
                                           terms.TERMS_VERSION, terms.TERMS_HASH)
        log_line("/v1/devices", 201, 0.0, created["device_id"])
        return 201, created

    def device_me(self, body, query, token) -> tuple[int, dict]:
        device = self.auth(token, "/v1/devices/me")
        stats = self.store.device_stats(device["device_id"])
        return 200, {"device_id": device["device_id"], "event_count": stats["event_count"],
                     "session_count": stats["session_count"], "last_event_at": stats["last_event_at"],
                     "created_at": device["created_at"], "tiers": stats["tiers"]}

    def delete_data(self, body, query, token) -> tuple[int, dict]:
        device = self.auth(token, "/v1/devices/me/data")
        removed = self.store.delete_device_data(device["device_id"])
        return 200, {"ok": True, "deleted_events": removed}

    def revoke_device(self, body, query, token) -> tuple[int, dict]:
        device = self.auth(token, "/v1/devices/me")
        self.store.revoke(device["device_id"])
        # `revoked` keeps the field docs/sync-api.md §5.2 promises; `state` is the newer spelling.
        return 200, {"revoked": True, "ok": True, "state": STATE_TERMINATED}

    def upload_batch(self, body: dict, query, token) -> tuple[int, dict]:
        device = self.auth(token, "/v1/events/batch")
        if device["state"] == STATE_TERMINATED:
            raise self.error("device_revoked", "设备已终止记录", 403)
        if not isinstance(body, dict) or not isinstance(body.get("events"), list):
            raise self.error("invalid_batch", "events 必须是数组")
        if not body["events"]:
            raise self.error("invalid_batch", "events 不能为空")
        if len(body["events"]) > 500:
            raise self.error("invalid_batch", "每批最多 500 条")
        if not self.limiter.allow(device["device_id"]):
            raise BadRequest("rate_limited", "请求过于频繁", 429)
        result = self.store.insert_events(
            device["device_id"], body.get("batch_id"), body.get("schema"), body.get("tier"),
            body["events"], EVENT_SCHEMAS, MAX_EVENT_BODY)
        return 200, result

    def service_state(self, body, query, token) -> tuple[int, dict]:
        device = self.auth(token, "/v1/service-state")
        doc = terms.state_document()
        doc.update({"state": device["state"], "terminated_at": device["terminated_at"],
                    "effective_at": device.get("terms_accepted_at") or device["created_at"],
                    "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        return 200, doc

    def terminate_service(self, body, query, token) -> tuple[int, dict]:
        device = self.auth(token, "/v1/service-state/terminate")
        result = self.store.terminate(device["device_id"], "server")
        log_line("/v1/service-state/terminate", 200, 0.0, device["device_id"])
        return 200, result

    def heartbeat(self, body: dict, query, token) -> tuple[int, dict]:
        device = self.auth(token, "/v1/device/heartbeat")
        if not isinstance(body, dict):
            raise self.error("invalid_request", "正文必须是 JSON 对象")
        raw = json.dumps(body, ensure_ascii=False)
        if len(raw.encode("utf-8")) > 4096:
            raise self.error("too_large", "心跳正文超过 4 KiB", 413)
        self.store.save_heartbeat(device["device_id"], body)
        return 200, {"ok": True, "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def uninstall(self, body, query, token) -> tuple[int, dict]:
        device = self.auth(token, "/v1/device/uninstall", allow_uninstalled=True)
        self.store.mark_uninstalled(device["device_id"])
        log_line("/v1/device/uninstall", 200, 0.0, device["device_id"])
        return 200, {"ok": True}

    # --- recharge ---------------------------------------------------------------------------
    def redeem(self, body: dict, query, token) -> tuple[int, dict]:
        if not isinstance(body, dict):
            raise self.error("invalid_request", "正文必须是 JSON 对象")
        cdk, account = body.get("cdk"), body.get("account")
        if not isinstance(cdk, str) or not cdk.strip():
            raise self.error("cdk_invalid", "CDK 无效")
        if not isinstance(account, str) or not account.strip():
            raise self.error("invalid_account", "账号标识缺失")
        status, payload = self.store.redeem(cdk, account.strip())
        if status >= 400:
            raise BadRequest(payload["error"]["code"], payload["error"].get("message", ""), status)
        log_line("/v1/redemptions", status, 0.0, payload["redemption_id"])
        return status, payload

    def redemption(self, body, query, token) -> tuple[int, dict]:
        row = self.store.redemption(str(query.get("id") or ""))
        if row is None:
            raise self.error("not_found", "没有这条兑换记录", 404)
        body_doc = {"redemption_id": row["redemption_id"], "status": row["status"], "replayed": False,
                    "product": row["product"], "redeemed_at": row["created_at"], "message": None}
        if row.get("error"):
            body_doc["error"] = {"code": row["error"]}
        return 200, body_doc

    # --- admin ------------------------------------------------------------------------------
    def admin_stats(self, body, query, token) -> tuple[int, dict]:
        self.auth_admin(token)
        return 200, self.store.stats()
    def admin_devices(self, body, query, token) -> tuple[int, dict]:
        self.auth_admin(token)
        limit = _int(query.get("limit"), 200, 1, 1000)
        offset = _int(query.get("offset"), 0, 0, 10 ** 9)
        return 200, {"devices": self.store.iter_devices(limit, offset)}

    def admin_device(self, body, query, token) -> tuple[int, dict]:
        self.auth_admin(token)
        device_id = str(query.get("id") or "")
        device = self.store.device(device_id)
        if device is None:
            raise self.error("not_found", "没有这台设备", 404)
        stats = self.store.device_stats(device_id)
        return 200, {"device": device, "stats": stats}

    def admin_purge(self, body, query, token) -> tuple[int, dict]:
        self.auth_admin(token)
        removed = self.store.purge_device(str(query.get("id") or ""))
        return 200, {"ok": True, "deleted_events": removed}

    def admin_terminate(self, body, query, token) -> tuple[int, dict]:
        self.auth_admin(token)
        return 200, self.store.terminate(str(query.get("id") or ""), "server")

    def admin_resume(self, body, query, token) -> tuple[int, dict]:
        self.auth_admin(token)
        self.store.set_state(str(query.get("id") or ""), "active")
        return 200, {"ok": True, "state": "active"}

    def admin_cdk(self, body: dict, query, token) -> tuple[int, dict]:
        self.auth_admin(token)
        if not isinstance(body, dict) or not isinstance(body.get("code"), str):
            raise self.error("invalid_request", "需要 code")
        self.store.add_cdk(body["code"], body.get("product"), body.get("expires_at"))
        return 201, {"ok": True}

    def admin_prune(self, body, query, token) -> tuple[int, dict]:
        self.auth_admin(token)
        fields = body if isinstance(body, dict) else {}
        days = _int(fields.get("days") or query.get("days"), 90, 1, 3650)
        return 200, {"ok": True, "days": days, "deleted_events": self.store.prune_events(days)}

    # --- read-only UI (server-rendered; see docs/backend.md §4.7) ---------------------------
    def ui_devices(self, body, query, token) -> tuple[int, Html]:
        self.auth_ui(token, query)
        return 200, Html(web.devices_page(self.store.stats(), self.store.iter_devices(200, 0)))

    def ui_device(self, body, query, token) -> tuple[int, Html]:
        self.auth_ui(token, query)
        device = self.store.device(str(query.get("id") or ""))
        if device is None:
            raise self.error("not_found", "没有这台设备", 404)
        stats = self.store.device_stats(device["device_id"])
        page = web.device_page(device, stats, self.store.sessions_for(device["device_id"]))
        return 200, Html(page)

    def ui_session(self, body, query, token) -> tuple[int, Html]:
        self.auth_ui(token, query)
        device_id, session_id = str(query.get("id") or ""), str(query.get("sid") or "")
        events = self.store.events_for_session(device_id, session_id)
        if not events:
            raise self.error("not_found", "没有这个会话", 404)
        return 200, Html(web.session_page(device_id, session_id, events))

    def ui_event(self, body, query, token) -> tuple[int, Html]:
        self.auth_ui(token, query)
        device_id, event_id = str(query.get("id") or ""), str(query.get("eid") or "")
        event = self.store.event(device_id, event_id)
        if event is None:
            raise self.error("not_found", "没有这条记录", 404)
        return 200, Html(web.event_page(device_id, event))

    def auth_ui(self, token: str | None, query: dict) -> None:
        """A browser cannot send a bearer header on a plain link: accept `?token=`, then a cookie."""
        presented = token or query.get("token")
        self.auth_admin(presented if isinstance(presented, str) else None)


# --- HTTP glue ----------------------------------------------------------------------------------
ROUTES = [
    ("GET", r"/v1/health", "health", False),
    ("POST", r"/v1/devices", "register_device", False),
    ("GET", r"/v1/devices/me", "device_me", True),
    ("DELETE", r"/v1/devices/me/data", "delete_data", True),
    ("DELETE", r"/v1/devices/me", "revoke_device", True),
    ("POST", r"/v1/events/batch", "upload_batch", True),
    ("GET", r"/v1/service-state", "service_state", True),
    ("POST", r"/v1/service-state/terminate", "terminate_service", True),
    ("POST", r"/v1/device/heartbeat", "heartbeat", True),
    ("POST", r"/v1/device/uninstall", "uninstall", True),
    ("POST", r"/v1/redemptions", "redeem", False),
    ("GET", r"/v1/redemptions/(?P<id>[A-Za-z0-9_\-]+)", "redemption", False),
    ("GET", r"/admin/stats", "admin_stats", False),
    ("GET", r"/admin/devices", "admin_devices", False),
    ("GET", r"/admin/devices/(?P<id>[A-Za-z0-9_\-]+)", "admin_device", False),
    ("DELETE", r"/admin/devices/(?P<id>[A-Za-z0-9_\-]+)", "admin_purge", False),
    ("POST", r"/admin/devices/(?P<id>[A-Za-z0-9_\-]+)/terminate", "admin_terminate", False),
    ("POST", r"/admin/devices/(?P<id>[A-Za-z0-9_\-]+)/resume", "admin_resume", False),
    ("POST", r"/admin/cdk", "admin_cdk", False),
    ("POST", r"/admin/prune", "admin_prune", False),
    ("GET", r"/admin/ui", "ui_devices", False),
    ("GET", r"/admin/ui/devices/(?P<id>[A-Za-z0-9_\-]+)", "ui_device", False),
    ("GET", r"/admin/ui/devices/(?P<id>[A-Za-z0-9_\-]+)/sessions/(?P<sid>[A-Za-z0-9_\-]+)",
     "ui_session", False),
    ("GET", r"/admin/ui/devices/(?P<id>[A-Za-z0-9_\-]+)/events/(?P<eid>[^/]+)", "ui_event", False),
]


class Handler(BaseHTTPRequestHandler):
    server_version = f"ctrsrv/{__version__}"
    protocol_version = "HTTP/1.1"

    @property
    def api(self) -> Api:
        return self.server.api  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # silence the default per-request line
        pass

    def _bearer(self) -> str | None:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        # Ops convenience: `X-Admin-Token` is accepted as the same credential (device lookups just
        # never match it). Nothing else reads it.
        direct = (self.headers.get("X-Admin-Token") or "").strip()
        if direct:
            return direct
        # The browser form: `/admin/ui?token=…` sets this cookie, and every link then works.
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "ctrsrv_admin" and value:
                return value
        return None

    def _json_body(self) -> object:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            # No chunked decoding here, and treating it as an empty body silently created devices
            # with no fields. Say so instead.
            raise BadRequest("length_required", "请带 Content-Length（不支持 chunked）", 411)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            # The heartbeat has its own 4 KiB rule; answer with its code when that is the path.
            code = "too_large" if self.path.startswith("/v1/device/heartbeat") else "batch_too_large"
            raise BadRequest(code, "正文超过上限", 413)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            # The parsed value is returned as-is: whether a body may be a list/scalar is a
            # per-endpoint question, answered by the handler's `isinstance(body, dict)` guard.
            return json.loads(raw.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise BadRequest("invalid_request", "正文不是合法 JSON", 400) from None

    def _send(self, status: int, body=None, headers: dict | None = None) -> None:
        if isinstance(body, Html):
            payload, content_type = body.body, "text/html; charset=utf-8"
        elif body is None:
            payload, content_type = b"", None
        else:
            payload, content_type = json.dumps(body, ensure_ascii=False).encode("utf-8"), \
                "application/json; charset=utf-8"
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _ui_cookie(self, query: dict, payload) -> dict | None:
        """`/admin/ui?token=…` worked, so keep it in a cookie instead of in every link."""
        token = query.get("token")
        if isinstance(payload, Html) and isinstance(token, str) and token:
            return {"Set-Cookie": f"ctrsrv_admin={token}; Path=/; HttpOnly; SameSite=Strict"}
        return None

    def _dispatch(self, method: str) -> None:
        started = time.perf_counter()
        parts = urlsplit(self.path)
        path = parts.path.rstrip("/") or "/"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            for verb, pattern, name, _needs_token in ROUTES:
                if verb != method:
                    continue
                match = re.fullmatch(pattern, path)
                if not match:
                    continue
                query.update({k: unquote(v) for k, v in match.groupdict().items() if v is not None})
                body = self._json_body() if method in ("POST", "PUT", "PATCH") else {}
                handler = getattr(self.api, name)
                status, payload = handler(body, query, self._bearer())
                self._send(status, payload, self._ui_cookie(query, payload))
                log_line(path, status, (time.perf_counter() - started) * 1000)
                return
            self._send(404, {"error": {"code": "not_found", "message": "没有这个接口"}})
        except BadRequest as exc:
            headers = {"Retry-After": "5"} if exc.status == 429 else None
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}}, headers)
            log_line(path, exc.status, (time.perf_counter() - started) * 1000, exc.code)
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to a client
            self._send(500, {"error": {"code": "server_error", "message": "内部错误"}})
            log_line(path, 500, (time.perf_counter() - started) * 1000, type(exc).__name__)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: Config):
        self.api = Api(config)
        super().__init__((config.host, config.port), Handler)


def serve(config: Config) -> None:
    config.db.parent.mkdir(parents=True, exist_ok=True)
    server = Server(config)
    host, port = server.server_address[:2]
    print(f"ctrsrv {__version__} listening on http://{host}:{port}  db={config.db}", flush=True)
    print(f"admin: {'enabled' if config.admin_token else 'DISABLED (set CTRSRV_ADMIN_TOKEN)'}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.api.store.close()
