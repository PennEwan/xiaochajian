"""Push spooled records to the backend, one tier at a time, in order.

Reuses the sync client (`ctr.sync.api`), the DPAPI device vault (`ctr.sync.vault`) and the
backend's error semantics from `docs/sync-api.md`: batches are idempotent on
`(device, event_id)`, retries are exponential with jitter, `429` honours `Retry-After`, `401`
re-registers, and `403 device_revoked` stops uploading while keeping local data.
"""
from __future__ import annotations

import random
import time
import uuid
from pathlib import Path

from ctr.sync import vault
from ctr.sync.api import Api, ApiError, HttpError, InsecureUrl, NotSent, Unknown

from . import __version__
from .spool import TIERS

CLIENT = f"codex-companion-collector/{__version__}"
SCHEMA_BATCH = "companion.sync.batch/v1"

PROGRESS, IDLE, FAILED = "progress", "idle", "failed"
NOT_CONFIGURED, DEVICE_REVOKED = "not_configured", "device_revoked"
BACKOFF_BASE_S, BACKOFF_CAP_S = 2.0, 300.0
MAX_EVENTS, MAX_BYTES = 200, 1 << 20
REVOKED_PROBE_S = 6 * 3600.0  # a revoked device retries rarely, and never re-registers


class Uploader:
    def __init__(self, paths, spool, cfg, *, api_factory=None, clock=time.monotonic,
                 rng=random.random):
        self.paths = paths
        self.spool = spool
        self.cfg = cfg
        self.api_factory = api_factory or (lambda url, timeout: Api(url, timeout))
        self.clock, self.rng = clock, rng
        self.failures = 0
        self.delay = 0.0
        self.revoked = False
        self.revoked_at = 0.0
        self.uploaded = 0
        self.last_error: str | None = None
        self.last_success_at: float | None = None

    # --- plumbing ---------------------------------------------------------------------------
    def make_api(self) -> Api | None:
        if not self.cfg.base_url:
            return None
        try:
            return self.api_factory(self.cfg.base_url, self.cfg.timeout)
        except InsecureUrl:
            self.last_error = "insecure_url"
            return None

    def token(self, api: Api, contract: dict) -> str:
        dev = vault.load(self.paths.device)
        if dev and dev.get("base_url") == api.base_url and isinstance(dev.get("device_token"), str):
            return dev["device_token"]
        res = api.register(int(contract.get("terms_version") or 0), contract.get("effective_at"))
        token, device_id = res.get("device_token"), res.get("device_id")
        if not (isinstance(token, str) and token.startswith("ctrd_") and isinstance(device_id, str)):
            raise ApiError("bad registration")
        vault.save(self.paths.device, {"device_id": device_id, "device_token": token,
                                       "base_url": api.base_url, "registered_at": _now()})
        return token

    def device_id(self) -> str | None:
        dev = vault.load(self.paths.device)
        return dev.get("device_id") if dev else None

    # --- upload -----------------------------------------------------------------------------
    def push(self, contract: dict, *, limit_events: int = MAX_EVENTS,
             limit_bytes: int = MAX_BYTES, tiers=TIERS) -> str:
        if self.revoked and self.clock() - self.revoked_at < REVOKED_PROBE_S:
            return DEVICE_REVOKED
        api = self.make_api()
        if api is None:
            return NOT_CONFIGURED
        batch = self.spool.next_batch(limit_events, limit_bytes, tiers)
        if batch is None:
            return IDLE
        try:
            token = self.token(api, contract)
        except HttpError as exc:
            return self._fail(f"register_http_{exc.status}", exc.retry_after)
        except (ApiError, InsecureUrl) as exc:
            return self._fail(f"register_{_kind(exc)}")
        envelope = {
            "schema": SCHEMA_BATCH,
            "batch_id": str(uuid.uuid4()),
            "client": CLIENT,
            "tier": batch.tier,
            "service": {"terms_version": contract.get("terms_version"),
                        "terms_hash": contract.get("terms_hash")},
            "events": batch.items,
        }
        try:
            started = time.perf_counter()
            resp = api.upload(token, envelope)
        except HttpError as exc:
            return self._http_error(exc, api, batch)
        except (ApiError, InsecureUrl) as exc:
            return self._fail(_kind(exc))
        if not _all_reported(resp, batch.items):
            return self._fail("incomplete_results")
        self.spool.commit(batch)
        self.uploaded += len(batch.items)
        self.failures, self.delay = 0, 0.0
        self.last_error = None
        # Wall clock, not the monotonic clock used for retry timing: this value is shown to
        # people (collector_status / the panel), and a monotonic reading means nothing outside
        # this process.
        self.last_success_at = time.time()
        return PROGRESS

    def _http_error(self, exc: HttpError, api: Api, batch) -> str:
        if exc.status == 413 or (exc.status == 400 and exc.code == "invalid_batch"):
            if len(batch.items) > 1:  # find the record the backend cannot take
                self.spool.commit(type(batch)(tier=batch.tier, path=batch.path,
                                              items=batch.items[: max(1, len(batch.items) // 2)], size=0))
                return PROGRESS
            self.spool.commit(batch)  # a single bad record must not block the queue
            self.last_error = "rejected"
            return PROGRESS
        if exc.status == 401:
            vault.clear(self.paths.device)
            return self._fail("device_invalid")
        if exc.status == 403:
            # The backend says this device does not exist any more. Keep the token: re-registering
            # would resurrect it. Keep the spool too — the data is still the user's.
            self.revoked, self.revoked_at = True, self.clock()
            self.last_error = "device_revoked"
            return DEVICE_REVOKED
        return self._fail(f"http_{exc.status}", exc.retry_after)

    def _fail(self, kind: str, retry_after: int | None = None) -> str:
        self.failures += 1
        if retry_after is not None:
            self.delay = max(BACKOFF_BASE_S, min(BACKOFF_CAP_S, float(retry_after)))
        else:
            base = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (self.failures - 1))
            self.delay = min(BACKOFF_CAP_S, base * (0.8 + 0.4 * self.rng()))
        self.last_error = kind
        return FAILED

    # --- cheap calls ------------------------------------------------------------------------
    def _call(self, method: str, *args) -> dict | None:
        api = self.make_api()
        if api is None:
            return None
        try:
            token = self.token(api, {"terms_version": 0, "effective_at": None})
            return getattr(api, method)(token, *args)
        except (ApiError, InsecureUrl, AttributeError):
            return None

    def service_state(self) -> dict | None:
        return self._call("service_state")

    def heartbeat(self, body: dict) -> dict | None:
        return self._call("heartbeat", body)

    def terminate_service(self) -> dict | None:
        return self._call("terminate_service")

    def uninstall(self) -> dict | None:
        return self._call("uninstall")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _kind(exc: Exception) -> str:
    if isinstance(exc, NotSent):
        return f"not_sent_{exc.kind}"
    if isinstance(exc, Unknown):
        return f"unknown_{exc.kind}"
    kind = getattr(exc, "kind", None)
    return f"unknown_{kind}" if kind else type(exc).__name__.lower()


def _all_reported(resp: dict, items: list) -> bool:
    seen = {r.get("event_id") for r in (resp.get("results") or []) if isinstance(r, dict)}
    return all(item.get("event_id") in seen for item in items)
