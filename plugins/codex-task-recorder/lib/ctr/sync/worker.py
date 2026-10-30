"""The uploader process: drain the outbox to the backend, then exit when idle.

Single instance (sync\\worker.lock). Each round: pending remote operations first (delete
uploaded data, revoke the device) -> stop if sync is off, or if Codex no longer runs the host
plugin (disabled, uninstalled, a hook switched off or untrusted: see hostgate) -> build a
batch -> register the device if needed -> upload. Failures back off exponentially; only an
acknowledged batch moves an upload cursor.
"""
from __future__ import annotations

import json
import os
import random
import time
import uuid

from .. import store, timeutil
from . import config, gate as gate_mod, log, vault
from . import state as sstate
from .api import CLIENT, Api, ApiError, HttpError, InsecureUrl, NotSent
from .config import SyncPaths
from .outbox import MAX_BYTES, MAX_EVENTS, Outbox

POLL_S = 2.0
IDLE_EXIT_S = 600.0
FAIL_EXIT_S = 3600.0
BACKOFF_BASE_S, BACKOFF_CAP_S = 2.0, 300.0
# Results of one round.
PROGRESS, IDLE, FAILED = "progress", "idle", "failed"
DISABLED, NOT_CONFIGURED, DEVICE_REVOKED = "disabled", "not_configured", "device_revoked"
HOST_BLOCKED = "host_blocked"
_UNSET = object()


def _default_api(base_url: str, timeout: float) -> Api:
    return Api(base_url, timeout)


class Worker:
    def __init__(self, paths, *, api_factory=None, clock=time.monotonic, sleep=time.sleep,
                 rng=random.random, poll_s: float = POLL_S, idle_exit_s: float = IDLE_EXIT_S,
                 fail_exit_s: float = FAIL_EXIT_S, gate=None):
        self.paths = paths
        self.gate = gate or gate_mod.Gate()
        self._blocked: object = _UNSET  # the first verdict is always written (clears a stale one)
        self.sp = SyncPaths(paths)
        self.outbox = Outbox(paths)
        self.api_factory = api_factory or _default_api
        self.clock, self.sleep, self.rng = clock, sleep, rng
        self.poll_s, self.idle_exit_s, self.fail_exit_s = poll_s, idle_exit_s, fail_exit_s
        self.limit = MAX_EVENTS
        self.failures = 0
        self.failing_since: float | None = None
        self.delay = 0.0
        self.op_results: dict[str, str] = {}
        self.waits: list[float] = []

    # ---- process ---------------------------------------------------------------------------
    def run(self, once: bool = False) -> str:
        fd = store.try_hold(self.sp.worker_lock)
        if fd is None:
            return "already_running"
        try:
            self._status(pid=os.getpid(), started_at=self._now())
            self.outbox.prune()
            return self._loop(once)
        finally:
            self._status(pid=None)
            store.release(fd)

    def _loop(self, once: bool) -> str:
        idle_since = self.clock()
        while True:
            result = self.step()
            if result == PROGRESS:
                idle_since = self.clock()
            elif result == IDLE:
                if once or self.clock() - idle_since >= self.idle_exit_s:
                    return IDLE
                if not self._nap(self.poll_s):
                    return DISABLED
            elif result == FAILED:
                if once:
                    return FAILED
                if self.clock() - self.failing_since >= self.fail_exit_s:
                    return "failing"
                if not self._nap(self.delay):
                    return DISABLED
            else:
                return result

    def _nap(self, seconds: float) -> bool:
        """Wait in poll-sized steps, so pause / revoke / uninstall never wait for a long backoff.
        False as soon as sync no longer needs this process."""
        self.waits.append(seconds)
        remaining = seconds
        while remaining > 0:
            step = min(self.poll_s, remaining)
            self.sleep(step)
            remaining -= step
            if not sstate.wants_worker(sstate.load(self.sp)) or self._host_blocked():
                return False
        return True

    def _host_blocked(self) -> str | None:
        reason = self.gate.check()
        if reason != self._blocked:
            self._blocked = reason
            self._status(host_blocked=reason)
            if reason:
                log.write(self.sp.log, "host", reason)
        return reason

    def step(self) -> str:
        st = sstate.load(self.sp)
        if st is None or st.corrupt:
            return DISABLED
        if st.pending_ops:
            return self.run_ops(st)
        if not sstate.is_on(st):
            return DISABLED
        if self._host_blocked():
            return HOST_BLOCKED
        batch = self.outbox.next_batch(st, self.limit, MAX_BYTES)
        if batch is None:
            return IDLE
        if not batch.events:  # only skipped lines: nothing to send
            self.outbox.commit(batch, {})
            return PROGRESS
        cfg = config.load(self.sp)
        if not cfg.base_url:
            self._status(last_error=self._error("not_configured"))
            return NOT_CONFIGURED
        if cfg.base_url != st.consent_base_url:  # config edited by hand: not the backend agreed to
            self._status(last_error=self._error("backend_changed"))
            return NOT_CONFIGURED
        try:
            api = self.api_factory(cfg.base_url, cfg.timeout)
        except InsecureUrl:
            self._status(last_error=self._error("insecure_url"))
            return NOT_CONFIGURED
        st = sstate.load(self.sp)  # the user may have paused or revoked while the batch was built
        if not sstate.is_on(st):
            return DISABLED
        if self._host_blocked():
            return HOST_BLOCKED
        try:
            token = self._token(api, st)
        except HttpError as exc:  # registration problems are never the events' fault
            return self._fail(f"register_http_{exc.status}", exc.retry_after)
        except ApiError as exc:
            return self._fail(f"register_{_kind(exc)}")
        try:
            started = time.perf_counter()
            resp = api.upload(token, batch.envelope(str(uuid.uuid4()), CLIENT))
        except HttpError as exc:
            return self._upload_error(exc, batch)
        except ApiError as exc:
            return self._fail(_kind(exc))
        statuses = _statuses(resp)
        if not all(e["event_id"] in statuses for e in batch.events):
            return self._fail("incomplete_results")
        totals = self.outbox.commit(batch, statuses)
        self._succeeded()
        log.write(self.sp.log, "upload", "ok", n=len(batch.events), ms=int((time.perf_counter() - started) * 1000))
        self._status(last_success_at=self._now(), last_error=None, consecutive_failures=0,
                     last_batch={"events": len(batch.events), **{k: totals[k] for k in ("uploaded", "rejected")}})
        self.limit = MAX_EVENTS
        return PROGRESS

    # ---- device ----------------------------------------------------------------------------
    def _token(self, api, st: sstate.SyncState) -> str:
        dev = vault.load(self.sp.device)
        if dev and dev.get("base_url") == api.base_url and isinstance(dev.get("device_token"), str):
            return dev["device_token"]
        res = api.register(st.consent_version, st.consent_accepted_at)
        token, device_id = res.get("device_token"), res.get("device_id")
        if not (isinstance(token, str) and token.startswith("ctrd_") and isinstance(device_id, str)):
            raise ApiError("bad registration")  # a token we could not recognise and redact
        vault.save(self.sp.device, {"device_id": device_id, "device_token": token, "base_url": api.base_url,
                                    "registered_at": self._now()})
        log.write(self.sp.log, "register", "ok")
        self._status(device_id=device_id)
        return token

    def _upload_error(self, exc: HttpError, batch) -> str:
        # Only the backend's own verdict on the batch (or a body-size limit) blames the events;
        # any other 400 (a proxy's error page, say) is retried like a server error.
        if exc.status == 413 or (exc.status == 400 and exc.code == "invalid_batch"):
            if len(batch.events) > 1:  # find the event the backend cannot take
                self.limit = max(1, len(batch.events) // 2)
                log.write(self.sp.log, "upload", "split", status=exc.status, n=self.limit)
                return PROGRESS
            self.outbox.commit(batch, {batch.events[0]["event_id"]: "rejected"})
            log.write(self.sp.log, "upload", "rejected", status=exc.status, n=1)
            self.limit = MAX_EVENTS
            return PROGRESS
        if exc.status == 401:  # the backend no longer knows this device: register again
            vault.clear(self.sp.device)
            log.write(self.sp.log, "register", "device_invalid", status=401)
            return self._fail("device_invalid")
        if exc.status == 403:
            vault.clear(self.sp.device)
            sstate.pause(self.sp, timeutil.now(), reason=sstate.DEVICE_REVOKED)
            self._status(last_error=self._error("device_revoked"))
            log.write(self.sp.log, "upload", "device_revoked", status=403)
            return DEVICE_REVOKED
        return self._fail(f"http_{exc.status}", exc.retry_after)

    # ---- remote operations -----------------------------------------------------------------
    def run_ops(self, st: sstate.SyncState | None = None) -> str:
        """Delete uploaded data / revoke the device, in that order. PROGRESS when all are done;
        what happened to each is in `self.op_results` (ok / no_device / unauthorized /
        rejected_<status>). Only network trouble, 429 and 5xx leave an operation pending."""
        st = st or sstate.load(self.sp)
        if st is None:
            return PROGRESS
        cfg = config.load(self.sp)
        for op in list(st.pending_ops):
            dev = vault.load(self.sp.device)
            token = dev.get("device_token") if dev else None
            if not token:  # never registered (or already revoked): nothing to do remotely
                self.op_results[op] = "no_device"
                sstate.complete_op(self.sp, op, "no_device")
                continue
            try:
                api = self.api_factory(dev.get("base_url") or cfg.base_url, cfg.timeout)
                if op == sstate.OP_DELETE_REMOTE:
                    api.delete_data(token)
                elif op == sstate.OP_REVOKE_DEVICE:
                    api.revoke(token)
            except HttpError as exc:
                if exc.status == 429 or exc.status >= 500:
                    return self._fail(f"{op}_http_{exc.status}", exc.retry_after)
                # 401/403: the device is already gone on the backend. Other 4xx: the backend will
                # never accept this request. Either way retrying cannot help; record and move on.
                outcome = "unauthorized" if exc.status in (401, 403) else f"rejected_{exc.status}"
                self.op_results[op] = outcome
                log.write(self.sp.log, op, "unauthorized" if outcome == "unauthorized" else "rejected",
                          status=exc.status)
                self._status(last_op_error=self._error(f"{op}_{outcome}"))
            except (ApiError, InsecureUrl) as exc:
                return self._fail(f"{op}_{_kind(exc)}")
            else:
                self.op_results[op] = "ok"
                log.write(self.sp.log, op, "ok")
            if op == sstate.OP_REVOKE_DEVICE:
                vault.clear(self.sp.device)
                self._status(device_id=None)
            sstate.complete_op(self.sp, op, self.op_results[op])
        self._succeeded()
        return PROGRESS

    # ---- bookkeeping -----------------------------------------------------------------------
    def _fail(self, kind: str, retry_after: int | None = None) -> str:
        self.failures += 1
        if self.failing_since is None:
            self.failing_since = self.clock()
        if retry_after is not None:  # honour the backend, within the same bounds as our backoff
            self.delay = max(BACKOFF_BASE_S, min(BACKOFF_CAP_S, float(retry_after)))
        else:
            base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (self.failures - 1))
            self.delay = min(BACKOFF_CAP_S, base * (0.8 + 0.4 * self.rng()))
        log.write(self.sp.log, "attempt", "failed", n=self.failures)
        self._status(last_error=self._error(kind), consecutive_failures=self.failures,
                     next_retry_in_s=round(self.delay, 1))
        return FAILED

    def _succeeded(self) -> None:
        self.failures, self.failing_since = 0, None

    def _error(self, kind: str) -> dict:
        return {"kind": kind, "at": self._now()}

    @staticmethod
    def _now() -> str:
        return timeutil.iso(timeutil.now())

    def _status(self, **changes) -> None:
        try:
            doc = json.loads(self.sp.status.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        doc.update(changes)
        try:
            self.sp.root.mkdir(parents=True, exist_ok=True)
            tmp = self.sp.status.with_suffix(".tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            store.replace(tmp, self.sp.status)
        except OSError:
            pass


def _kind(exc: Exception) -> str:
    if isinstance(exc, NotSent):
        return f"not_sent_{exc.kind}"
    kind = getattr(exc, "kind", None)
    return f"unknown_{kind}" if kind else type(exc).__name__.lower()


def _statuses(resp: dict) -> dict:
    out = {}
    for r in resp.get("results") or []:
        if isinstance(r, dict) and isinstance(r.get("event_id"), str) and r.get("status") in (
                "accepted", "duplicate", "rejected"):
            out[r["event_id"]] = r["status"]
    return out


OPS_LOCK_WAIT_S = 5.0


def run_ops_now(paths) -> tuple[str, dict] | None:
    """Run pending remote operations in this (CLI) process, holding the uploader lock so they
    never overlap an upload. Returns (result, op_results), or None when an uploader keeps the
    lock — it runs pending operations itself before anything else."""
    sp = SyncPaths(paths)
    deadline = time.monotonic() + OPS_LOCK_WAIT_S
    fd = store.try_hold(sp.worker_lock)
    while fd is None:  # an idle uploader notices the revocation within one poll and exits
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)
        fd = store.try_hold(sp.worker_lock)
    try:
        result = Worker(paths).run_ops()
    finally:
        store.release(fd)
    st = sstate.load(sp)  # outcomes recorded by whichever process ran each operation
    return result, dict(st.op_results) if st and not st.corrupt else {}


def read_status(sp: SyncPaths) -> dict:
    try:
        doc = json.loads(sp.status.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}
