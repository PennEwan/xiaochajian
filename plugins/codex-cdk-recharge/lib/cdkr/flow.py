"""The recharge flow behind the window: user-entered account, redemption, retries, messages.

No tkinter here, so it is tested directly. Everything runs on the caller's (worker) thread;
`progress` callbacks report intermediate status lines.

The account is entered by the user and sent to the backend with the CDK. No Codex account lookup
is performed. The CDK only lives in the arguments of `redeem`: it is never stored, logged or put
in a message (messages show `mask(cdk)`).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import api, log
from .config import Config, Paths

# ---- user-entered account ------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    account_id: str
    email: str
    plan: str | None


KERNEL_MISSING, NOT_LOGGED_IN, NOT_CHATGPT, READ_FAILED = "kernel_missing", "not_logged_in", "not_chatgpt", "read_failed"
ACCOUNT_MESSAGES = {
    KERNEL_MISSING: "未找到 Codex 桌面 App，请先安装并打开一次 Codex。",
    NOT_LOGGED_IN: "Codex 还没有登录 ChatGPT 账号，请先在 Codex 中登录。",
    NOT_CHATGPT: "Codex 当前不是用 ChatGPT 账号登录（例如用 API Key），CDK 只能充值 ChatGPT 账号。",
    READ_FAILED: "读取 Codex 登录账号失败，请稍后重新打开充值窗口。",
}


class AccountError(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind if kind in ACCOUNT_MESSAGES else READ_FAILED


PLAN_NAMES = {"free": "Free", "go": "Go", "plus": "Plus", "pro": "Pro", "team": "Team",
              "business": "Business", "enterprise": "Enterprise", "edu": "Edu"}


def plan_name(plan: str | None) -> str | None:
    return PLAN_NAMES.get(plan, plan) if plan else None


# ---- what the window shows -----------------------------------------------------------------

NOT_CONFIGURED, NO_ACCOUNT, READY = "not_configured", "no_account", "ready"
NOT_CONFIGURED_MESSAGE = "尚未配置充值服务地址。请让管理员运行：cdkr.cmd config --base-url <充值服务地址>"
ACCOUNT_REQUIRED_MESSAGE = "请输入要充值的账号。"


@dataclass
class View:
    state: str
    message: str
    email: str | None = None
    plan: str | None = None


(SUCCEEDED, REPLAYED, PROCESSING, INVALID_FORMAT, CDK_INVALID, CDK_USED, CDK_EXPIRED, FULFILLMENT_FAILED,
 NOT_SENT, UNKNOWN, INVALID_ACCOUNT, RATE_LIMITED, SERVER_ERROR, BAD_RESPONSE, NOT_READY) = (
    "succeeded", "replayed", "processing", "invalid_format", "cdk_invalid", "cdk_used", "cdk_expired",
    "fulfillment_failed", "not_sent", "unknown", "invalid_account", "rate_limited", "server_error",
    "bad_response", "not_ready")


# Error codes from docs/recharge-api.md; anything else a backend sends is logged as `http_error`.
KNOWN_CODES = {"invalid_account", "rate_limited", "cdk_invalid", "cdk_already_redeemed", "cdk_expired",
               "fulfillment_failed", "not_found", "server_error"}
KNOWN_STATUSES = {"succeeded", "processing", "failed"}


@dataclass
class Outcome:
    kind: str
    message: str

    @property
    def ok(self) -> bool:
        return self.kind in (SUCCEEDED, REPLAYED)


def mask(cdk: str) -> str:
    """Show at most about half of the CDK."""
    if len(cdk) >= 12:
        return f"{cdk[:4]}…{cdk[-4:]}"
    if len(cdk) >= 8:
        return f"{cdk[:2]}…{cdk[-2:]}"
    return f"{cdk[:2]}…"


def normalize(cdk: str) -> str | None:
    value = "".join((cdk or "").split())
    if 4 <= len(value) <= 128 and all(33 <= ord(c) <= 126 for c in value):
        return value
    return None


def _local_time(iso: object) -> str:
    try:
        when = datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return "之前"
    return when.astimezone().strftime("%Y-%m-%d %H:%M")


class Recharge:
    def __init__(self, paths: Paths, config: Config, read_account: Callable[[], Account] | None = None, *,
                 sleep: Callable[[float], None] = time.sleep, retry_delays=(2, 5, 10),
                 poll_interval: float = 3, poll_timeout: float = 60):
        self.paths, self.config = paths, config
        self._sleep, self._retry_delays = sleep, tuple(retry_delays)
        self._poll_interval, self._poll_timeout = poll_interval, poll_timeout
        self.account: str | None = None

    # ---- account ------------------------------------------------------------------------------

    def load(self) -> View:
        if not self.config.base_url:
            return View(NOT_CONFIGURED, NOT_CONFIGURED_MESSAGE)
        self._log("ready", "ok")
        return View(READY, "请输入充值账号和 CDK，然后点「确认提交」。")

    # ---- redemption -----------------------------------------------------------------------------

    def redeem(self, cdk: str, account: str | None = None,
               progress: Callable[[str], None] | None = None) -> Outcome:
        progress = progress or (lambda _msg: None)
        value = normalize(cdk)
        if value is None:
            return Outcome(INVALID_FORMAT, "CDK 格式不对：请完整复制 CDK（4–128 位字母、数字或符号，不含中文）。")
        target = normalize_account(account if account is not None else self.account)
        if target is None:
            return Outcome(NOT_READY, ACCOUNT_REQUIRED_MESSAGE)
        self.account = target
        try:
            client = self._api()
        except api.InsecureUrl as exc:
            return Outcome(SERVER_ERROR, str(exc))
        shown = mask(value)

        record, uncertain = None, False
        for attempt, delay in enumerate((0, *self._retry_delays)):
            if attempt:
                progress(f"网络不稳定，正在用同一请求重试（第 {attempt} 次），不会重复扣除…")
                self._sleep(delay)
            started = time.monotonic()
            try:
                record = client.redeem(value, target)
                self._log("redeem", _status_id(record), started=started)
                break
            except api.NotSent as exc:
                self._log("redeem", NOT_SENT, exc, started)
                if not uncertain:
                    return Outcome(NOT_SENT, "网络连接失败，兑换请求没有发出，CDK 未使用。请检查网络后重试。")
            except api.Unknown as exc:
                self._log("redeem", UNKNOWN, exc, started)
                uncertain = True
            except api.HttpError as exc:
                self._log("redeem", exc.code if exc.code in KNOWN_CODES else "http_error", exc, started)
                return self._http_outcome(exc, shown, uncertain)
            except api.BadResponse as exc:
                self._log("redeem", BAD_RESPONSE, exc, started)
                return Outcome(BAD_RESPONSE, "充值服务返回了无法识别的结果。请稍后再次提交同一 CDK 查看结果，不会重复扣除。")
        if record is None:
            return Outcome(UNKNOWN, "网络中断，充值结果没有确认。联网后再次提交同一 CDK 即可确认结果，不会重复扣除。")

        if record.get("status") == "processing":
            progress("已提交，充值服务正在处理…")
            record = self._poll(client, record)
            if isinstance(record, Outcome):
                return record
        return self._final(record, shown, replay_is_duplicate=not uncertain)

    def _poll(self, client: api.Api, record: dict) -> dict | Outcome:
        rid = str(record.get("redemption_id") or "")
        deadline = time.monotonic() + self._poll_timeout
        while rid and time.monotonic() < deadline:
            self._sleep(self._poll_interval)
            started = time.monotonic()
            try:
                latest = client.get_redemption(rid)
            except api.HttpError as exc:
                self._log("poll", "http_error", exc, started)
                continue
            except api.ApiError as exc:
                self._log("poll", "error", exc, started)
                continue
            self._log("poll", _status_id(latest), started=started)
            if latest.get("status") != "processing":
                return {**latest, "replayed": record.get("replayed")}
        return Outcome(PROCESSING, "已提交，充值服务仍在处理。稍后再次提交同一 CDK 可查看结果，不会重复扣除。")

    def _final(self, record: dict, shown: str, replay_is_duplicate: bool) -> Outcome:
        status = record.get("status")
        if status == "failed":
            err = record.get("error") if isinstance(record.get("error"), dict) else {}
            reason = err.get("message") if isinstance(err.get("message"), str) else "开通没有成功"
            return Outcome(FULFILLMENT_FAILED, f"充值失败：{reason}。CDK 未被扣除，可稍后重试。")
        if status != "succeeded":
            return Outcome(BAD_RESPONSE, "充值服务返回了无法识别的结果。请稍后再次提交同一 CDK 查看结果，不会重复扣除。")
        product = record.get("product") if isinstance(record.get("product"), str) else "充值"
        if record.get("replayed") and replay_is_duplicate:
            return Outcome(REPLAYED, f"重复兑换：CDK {shown} 已于 {_local_time(record.get('redeemed_at'))} "
                                     f"充值到当前账号（{product}），未重复扣除。")
        return Outcome(SUCCEEDED, f"充值成功：{product}。CDK {shown} 已核销。")

    def plan_note(self) -> str:
        """Compatibility shim: manual account mode never performs a post-success lookup."""
        return ""

    def _http_outcome(self, exc: api.HttpError, shown: str, uncertain: bool) -> Outcome:
        if exc.code == "invalid_account":
            return Outcome(INVALID_ACCOUNT, "充值服务不接受这个账号信息，请联系客服。")
        if exc.code == "cdk_invalid" or (exc.status == 400 and not exc.code.startswith("invalid_")):
            return Outcome(CDK_INVALID, f"CDK {shown} 无效，请检查是否输错。")
        if exc.code == "cdk_already_redeemed":
            return Outcome(CDK_USED, f"CDK {shown} 已被其他账号使用。如有疑问请联系发卡方。")
        if exc.code == "cdk_expired":
            return Outcome(CDK_EXPIRED, f"CDK {shown} 已过期。")
        if exc.code == "fulfillment_failed":
            return Outcome(FULFILLMENT_FAILED, f"充值失败：{exc.message or '开通没有成功'}。CDK 未被扣除，可稍后重试。")
        if uncertain:  # an earlier attempt may have gone through
            return Outcome(UNKNOWN, "网络中断，充值结果没有确认。稍后再次提交同一 CDK 即可确认结果，不会重复扣除。")
        if exc.status == 429:
            return Outcome(RATE_LIMITED, _rate_message(exc))
        return Outcome(SERVER_ERROR, f"充值服务暂时不可用（{exc.status}），请稍后重试。")

    # ---- helpers ------------------------------------------------------------------------------

    def _api(self) -> api.Api:
        return api.Api(self.config.base_url, timeout=self.config.timeout)

    def _log(self, op: str, outcome: str, exc: Exception | None = None, started: float | None = None) -> None:
        status = getattr(exc, "status", None)
        ms = int((time.monotonic() - started) * 1000) if started is not None else None
        log.write(self.paths.log, op, outcome, status, ms)


def _status_id(record: dict) -> str:
    status = record.get("status")
    return status if status in KNOWN_STATUSES else "other"


def _rate_message(exc: api.HttpError) -> str:
    wait = f" {exc.retry_after} 秒" if exc.retry_after else "一会儿"
    return f"操作太频繁，请{wait}后再试。"


def normalize_account(value: str | None) -> str | None:
    """Normalize the user-entered account without interpreting or looking it up."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 256 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value
