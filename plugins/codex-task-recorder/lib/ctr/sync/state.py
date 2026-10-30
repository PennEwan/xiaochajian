"""Sync consent: whether uploading is on, the windows in which it was on, pending remote ops.

Separate from the recording consent (ctr.state). Missing file -> never enabled; corrupt
file -> off (fail closed). Read-modify-write happens under sync\\state.lock because the
CLI and the uploader both write it.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable

from .. import store, timeutil
from ..state import Window, in_window as _in_window
from .config import SyncPaths

CONSENT_VERSION = 1
OP_DELETE_REMOTE, OP_REVOKE_DEVICE = "delete_remote", "revoke_device"
PAUSED, REVOKED, DEVICE_REVOKED = "paused", "revoked", "device_revoked"


@dataclass
class SyncState:
    enabled: bool = False
    consent_version: int | None = None
    consent_accepted_at: str | None = None
    consent_base_url: str | None = None  # the backend named in the consent text
    windows: list[Window] = field(default_factory=list)
    pending_ops: list[str] = field(default_factory=list)
    op_results: dict[str, str] = field(default_factory=dict)  # outcome of each op since the revocation
    stopped_reason: str | None = None
    revoked_at: str | None = None
    corrupt: bool = False


def _parse(raw: str) -> SyncState:
    try:
        doc = json.loads(raw)
        return SyncState(
            enabled=doc.get("enabled") is True,
            consent_version=doc.get("consent_version"),
            consent_accepted_at=doc.get("consent_accepted_at"),
            consent_base_url=doc.get("consent_base_url"),
            windows=[Window(w["start"], w.get("end")) for w in doc.get("windows") or []],
            pending_ops=[str(op) for op in doc.get("pending_ops") or []],
            op_results={str(k): str(v) for k, v in (doc.get("op_results") or {}).items()},
            stopped_reason=doc.get("stopped_reason"),
            revoked_at=doc.get("revoked_at"),
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return SyncState(corrupt=True)


def load(sp: SyncPaths) -> SyncState | None:
    """For readers (hooks, uploader): anything unreadable means off."""
    try:
        return _parse(sp.state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError:
        return SyncState(corrupt=True)


def _load_for_update(sp: SyncPaths, attempts: int = 50) -> SyncState:
    """For writers: a file that is momentarily unreadable (antivirus, a sharing violation) must
    not be replaced by a blank state — that would drop consent records and queued revocations."""
    for attempt in range(attempts):
        try:
            st = _parse(sp.state.read_text(encoding="utf-8"))
            return SyncState() if st.corrupt else st
        except FileNotFoundError:
            return SyncState()
        except OSError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


def _save(sp: SyncPaths, st: SyncState) -> None:
    sp.root.mkdir(parents=True, exist_ok=True)
    doc = asdict(st)
    doc.pop("corrupt", None)
    tmp = sp.state.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    store.replace(tmp, sp.state)


def update(sp: SyncPaths, change: Callable[[SyncState], None]) -> SyncState:
    with store.locked(sp.state_lock, label="sync-state"):
        st = _load_for_update(sp)
        change(st)
        _save(sp, st)
        return st


def _close_window(st: SyncState, stamp: str) -> None:
    if st.windows and st.windows[-1].end is None:
        st.windows[-1].end = stamp


def needs_new_consent(st: SyncState | None, base_url: str) -> bool:
    """A new consent (not just a resume) is needed the first time, after a revocation, when the
    consent text changed, and when the backend differs from the one the user agreed to."""
    return (st is None or st.corrupt or st.consent_version != CONSENT_VERSION
            or st.consent_base_url != base_url)


def enable(sp: SyncPaths, now: datetime, base_url: str) -> SyncState:
    """Open a sync window. Under a new consent the earlier windows are dropped, so content
    queued under the old consent (or for another backend) is never uploaded."""
    stamp = timeutil.iso(now)

    def change(st: SyncState) -> None:
        if needs_new_consent(st, base_url):
            st.consent_version, st.consent_accepted_at, st.consent_base_url = CONSENT_VERSION, stamp, base_url
            st.windows = []
        if not st.windows or st.windows[-1].end is not None:
            st.windows.append(Window(start=stamp))
        st.enabled, st.stopped_reason = True, None

    return update(sp, change)


def pause(sp: SyncPaths, now: datetime, reason: str = PAUSED) -> SyncState:
    def change(st: SyncState) -> None:
        _close_window(st, timeutil.iso(now))
        st.enabled, st.stopped_reason = False, reason

    return update(sp, change)


def revoke(sp: SyncPaths, now: datetime, delete_remote: bool) -> SyncState:
    """Stop for good: forget consent and windows, so nothing queued is ever uploaded, and queue
    the remote operations (delete the uploaded data first, then revoke the device token)."""
    stamp = timeutil.iso(now)

    def change(st: SyncState) -> None:
        st.enabled, st.stopped_reason, st.revoked_at = False, REVOKED, stamp
        st.consent_version = st.consent_accepted_at = st.consent_base_url = None
        st.windows = []
        delete = delete_remote or OP_DELETE_REMOTE in st.pending_ops
        st.pending_ops = ([OP_DELETE_REMOTE] if delete else []) + [OP_REVOKE_DEVICE]
        st.op_results = {}

    return update(sp, change)


def complete_op(sp: SyncPaths, op: str, outcome: str = "ok") -> SyncState:
    """Mark a remote operation done, keeping its outcome for whoever reports it (the CLI may
    be waiting while the uploader runs it)."""
    def change(st: SyncState) -> None:
        st.pending_ops = [o for o in st.pending_ops if o != op]
        st.op_results[op] = outcome

    return update(sp, change)


def is_on(st: SyncState | None) -> bool:
    return bool(st and st.enabled and not st.corrupt and st.consent_version == CONSENT_VERSION)


def wants_worker(st: SyncState | None) -> bool:
    return is_on(st) or bool(st and not st.corrupt and st.pending_ops)


def in_window(st: SyncState, ts: object) -> bool:
    return _in_window(st, ts)  # the same window rule as the recording consent
