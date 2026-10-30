"""Local mock of the CDK recharge backend (docs/recharge-api.md). In-memory, for development only.

    python scripts\\mock_recharge_server.py [--port 18787]

Never logs CDKs or binding tokens: the request log shows method, path and status only.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

OK_CDKS = {f"TEST-OK0{i}" for i in range(1, 6)}
SLOW, PROC, PROC_FAIL, FAIL, EXPIRED = "TEST-SLOW", "TEST-PROC", "TEST-PFAL", "TEST-FAIL", "TEST-EXPD"
WAIT = "TEST-WAIT"  # submissions: answers after 2 s, long enough to watch the loading state
PRODUCT = "ChatGPT Plus 1 个月（模拟）"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.bindings: dict[str, str] = {}         # binding_token -> chatgpt_account_id
        self.by_key: dict[tuple, dict] = {}         # (cdk, account) -> redemption
        self.by_id: dict[str, dict] = {}
        self.owner: dict[str, str] = {}             # cdk -> account that redeemed it
        self.polls: dict[str, int] = {}
        self.fail_next: dict | None = None
        self.slow_seconds = 20.0
        self.error_code_override: str | None = None  # tests: a backend echoing odd codes
        self.paths: list[str] = []
        self.client_headers: list[str] = []
        self.authorizations: list[str] = []
        self.submissions: list[dict] = []           # request bodies of /v1/submissions (memory only)
        self.session_tokens: list[int] = []         # length of each session_token seen (never the value)

    def consumed(self, cdk: str) -> int:
        return sum(1 for (c, _), r in self.by_key.items() if c == cdk and r["status"] == "succeeded")


class Handler(BaseHTTPRequestHandler):
    server_version = "cdkr-mock/0.1"

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

    def _error(self, status: int, code: str, message: str | None = None, headers: dict | None = None):
        self._send(status, {"error": {"code": code, "message": message}}, headers)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            value = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def _account_for_token(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        token = auth[7:] if auth.startswith("Bearer ") else ""
        with self.state.lock:
            return self.state.bindings.get(token)

    def _record(self) -> bool:
        """Common bookkeeping; returns False when a forced failure was sent instead."""
        if self.path.startswith("/_mock/"):
            return True
        with self.state.lock:
            self.state.paths.append(self.path)
            self.state.client_headers.append(self.headers.get("X-Cdkr-Client") or "")
            self.state.authorizations.append(self.headers.get("Authorization") or "")
            forced, self.state.fail_next = self.state.fail_next, None
        if not forced:
            return True
        status = int(forced.get("status", 503))
        if "location" in forced:  # a redirect, e.g. {"status": 303, "location": "http://elsewhere/…"}
            self.send_response(status)
            self.send_header("Location", forced["location"])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        headers = {"Retry-After": str(forced["retry_after"])} if "retry_after" in forced else None
        code = "rate_limited" if status == 429 else "server_error"
        code = self.state.error_code_override or code
        self._error(status, code, None, headers)
        return False

    # ---- routes ---------------------------------------------------------------------------
    def do_POST(self):
        body = self._body()
        if not self._record():
            return
        if self.path == "/_mock/expire-bindings":
            with self.state.lock:
                self.state.bindings.clear()
            return self._send(200, {"ok": True})
        if self.path == "/_mock/session-tokens":
            with self.state.lock:
                return self._send(200, {"lengths": list(self.state.session_tokens)})
        if self.path == "/_mock/fail-next":
            with self.state.lock:
                self.state.fail_next = body or {"status": 503}
            return self._send(200, {"ok": True})
        if self.path == "/v1/bindings":
            return self._bind(body)
        if self.path == "/v1/redemptions":
            return self._redeem(body)
        if self.path == "/v1/submissions":
            return self._submit(body)
        self._error(404, "not_found")

    def do_GET(self):
        if not self._record():
            return
        prefix = "/v1/redemptions/"
        if not self.path.startswith(prefix):
            return self._error(404, "not_found")
        account = self._account_for_token()
        if account is None and self.headers.get("Authorization"):
            return self._error(401, "binding_invalid", "绑定已失效，请重新绑定")
        rid = self.path[len(prefix):]
        with self.state.lock:
            rec = self.state.by_id.get(rid)
            if rec is None or account not in (None, rec["_account"]):
                return self._error(404, "not_found")
            if rec["status"] == "processing" and rec["_cdk"] in (PROC, PROC_FAIL):
                self.state.polls[rid] = self.state.polls.get(rid, 0) + 1
                if self.state.polls[rid] >= 2 and rec["_cdk"] == PROC:
                    rec["status"] = "succeeded"
                elif self.state.polls[rid] >= 2:  # processing, then fails; the CDK is released again
                    rec["status"] = "failed"
                    rec["error"] = {"code": "fulfillment_failed", "message": "上游开通失败（模拟），CDK 未扣除"}
            out = self._public(rec, replayed=False)
        self._send(200, out)

    def _bind(self, body: dict):
        account, email = body.get("chatgpt_account_id"), body.get("email")
        if not isinstance(account, str) or not account or not isinstance(email, str) or "@" not in email:
            return self._error(400, "invalid_account", "账号信息不完整")
        token = "cdkb_" + secrets.token_urlsafe(24)
        with self.state.lock:
            self.state.bindings[token] = account
        self._send(201, {"binding_token": token, "account": {"chatgpt_account_id": account, "email": email},
                         "expires_at": None})

    def _redeem(self, body: dict):
        cdk, target = body.get("cdk"), body.get("account")
        token = body.get("session_token")
        with self.state.lock:
            self.state.session_tokens.append(len(token) if isinstance(token, str) else 0)
        if target is None:  # legacy callers, including the native helper
            target = body.get("chatgpt_account_id")
        if self.headers.get("Authorization"):  # legacy: the native 小助手 still binds first
            account = self._account_for_token()
            if account is None:
                return self._error(401, "binding_invalid", "绑定已失效，请重新绑定")
            if target != account:
                return self._error(403, "account_mismatch", "绑定的账号与请求的账号不一致")
        elif not isinstance(target, str) or not target:
            return self._error(400, "invalid_account", "账号信息不完整")
        account = target
        if not isinstance(cdk, str) or not cdk:
            return self._error(400, "cdk_invalid", "CDK 无效")
        with self.state.lock:
            existing = self.state.by_key.get((cdk, account))
            if existing is not None:
                return self._send(200, self._public(existing, replayed=True))
            owner = self.state.owner.get(cdk)
            if owner is not None and owner != account:
                return self._error(409, "cdk_already_redeemed", "此 CDK 已被其他账号使用")
            if cdk == EXPIRED:
                return self._error(410, "cdk_expired", "此 CDK 已过期")
            if cdk == FAIL:
                return self._error(422, "fulfillment_failed", "上游开通失败（模拟），CDK 未扣除")
            if cdk not in OK_CDKS | {SLOW, PROC, PROC_FAIL}:
                return self._error(400, "cdk_invalid", "CDK 无效")
            rec = {"redemption_id": "rdm_" + secrets.token_hex(8), "status": "processing",
                   "product": PRODUCT, "redeemed_at": _now(), "message": None, "_cdk": cdk, "_account": account}
            self.state.by_key[(cdk, account)] = rec
            self.state.by_id[rec["redemption_id"]] = rec
            self.state.owner[cdk] = account
            slow = cdk == SLOW
            if cdk not in (PROC, PROC_FAIL) and not slow:
                rec["status"] = "succeeded"
        if slow:  # the request outlives the client's timeout, but the redemption still completes
            time.sleep(self.state.slow_seconds)
            with self.state.lock:
                rec["status"] = "succeeded"
        with self.state.lock:
            out = self._public(rec, replayed=False)
        self._send(201, out)

    def _submit(self, body: dict):
        code = body.get("code")
        with self.state.lock:
            self.state.submissions.append(body)
        if code == WAIT:
            time.sleep(2)
        elif code == FAIL:
            return self._error(503, "server_error")
        elif code not in OK_CDKS:
            return self._error(400, "invalid_code", "订单号或兑换码无效")
        self._send(201, {"submission_id": "sub_" + secrets.token_hex(8), "status": "received"})

    @staticmethod
    def _public(rec: dict, replayed: bool) -> dict:
        out = {k: v for k, v in rec.items() if not k.startswith("_")}
        out["replayed"] = replayed
        return out


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # A client that timed out and hung up (TEST-SLOW) is expected, not an error.
        if not isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            super().handle_error(request, client_address)


def make_server(host: str, port: int, verbose: bool = False) -> Server:
    server = Server((host, port), Handler)
    server.state = State()  # type: ignore[attr-defined]
    server.verbose = verbose  # type: ignore[attr-defined]
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description="CDK 充值模拟后台（仅供开发测试）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18787)
    args = parser.parse_args()
    server = make_server(args.host, args.port, verbose=True)
    print(f"模拟充值后台：http://{args.host}:{args.port}  （Ctrl+C 退出）")
    print("测试 CDK：TEST-OK01..05 成功 / TEST-SLOW 超时 / TEST-PROC 处理中 / TEST-PFAL 处理中后失败 / TEST-FAIL 履约失败 / "
          "TEST-EXPD 过期 / 其他无效（兑换直接带账号，无需绑定；/v1/bindings 只为原生小助手保留）")
    print("提交（CDKR_SUBMIT_MOCK=1）：TEST-OK01..05 成功 / TEST-WAIT 2 秒后成功 / TEST-FAIL 服务错误 / 其他无效")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
