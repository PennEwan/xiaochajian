"""SQLite storage: devices, events, service state, heartbeats, CDK redemptions.

One file, WAL, one connection guarded by a lock. That is deliberately boring: the ingest path is
`INSERT OR IGNORE` on `(device_id, event_id)`, which is exactly the idempotency key the clients
rely on, and it needs no coordination beyond a write lock.

Nothing here knows about HTTP.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from pathlib import Path

TOKEN_PREFIX = "ctrd_"
DEVICE_PREFIX = "dev_"
REDEMPTION_PREFIX = "rdm_"
STATE_ACTIVE, STATE_TERMINATED = "active", "terminated"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    client TEXT, platform TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    terms_version INTEGER, terms_hash TEXT, terms_accepted_at TEXT,
    state TEXT NOT NULL DEFAULT 'active',
    terminated_at TEXT, terminated_reason TEXT,
    uninstalled_at TEXT,
    uninstall_calls INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    device_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    schema TEXT NOT NULL,
    tier TEXT,
    session_id TEXT,
    seq INTEGER,
    kind TEXT,
    ts TEXT,
    model TEXT,
    batch_id TEXT,
    received_at TEXT NOT NULL,
    body TEXT NOT NULL,
    PRIMARY KEY (device_id, event_id)
);
CREATE INDEX IF NOT EXISTS events_by_session ON events(device_id, session_id, seq);
CREATE INDEX IF NOT EXISTS events_by_time ON events(received_at);
CREATE INDEX IF NOT EXISTS events_by_tier ON events(device_id, tier);
CREATE TABLE IF NOT EXISTS batches (
    device_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    schema TEXT, tier TEXT,
    events INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    duplicates INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    received_at TEXT NOT NULL,
    PRIMARY KEY (device_id, batch_id)
);
CREATE TABLE IF NOT EXISTS heartbeats (
    device_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS redemptions (
    redemption_id TEXT PRIMARY KEY,
    cdk_hash TEXT NOT NULL,
    account TEXT NOT NULL,
    status TEXT NOT NULL,
    product TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (cdk_hash, account)
);
CREATE TABLE IF NOT EXISTS cdk (
    code_hash TEXT PRIMARY KEY,
    product TEXT,
    expires_at TEXT,
    used_by TEXT,
    used_at TEXT,
    redemption_id TEXT
);
"""


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # --- plumbing ---------------------------------------------------------------------------
    def connect(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(_SCHEMA)
                conn.commit()
                self._conn = conn
            return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # --- devices ----------------------------------------------------------------------------
    def create_device(self, client: str | None, platform: str | None, consent: dict | None,
                      terms_version: int | None = None, terms_hash: str | None = None) -> dict:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        device_id = DEVICE_PREFIX + secrets.token_hex(8)
        created = now()
        consent = consent if isinstance(consent, dict) else {}
        with self._lock:
            conn = self.connect()
            conn.execute(
                "INSERT INTO devices(device_id, token_hash, client, platform, created_at, last_seen_at,"
                " terms_version, terms_hash, terms_accepted_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (device_id, hash_token(token), client, platform, created, created,
                 terms_version, terms_hash,
                 consent.get("accepted_at") if isinstance(consent.get("accepted_at"), str) else created))
            conn.commit()
        return {"device_id": device_id, "device_token": token, "created_at": created}

    def device_by_token(self, token: str) -> dict | None:
        if not token:
            return None
        with self._lock:
            row = self.connect().execute("SELECT * FROM devices WHERE token_hash=?",
                                         (hash_token(token),)).fetchone()
        return dict(row) if row else None

    def device(self, device_id: str) -> dict | None:
        with self._lock:
            row = self.connect().execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        return dict(row) if row else None

    def touch(self, device_id: str) -> None:
        with self._lock:
            conn = self.connect()
            conn.execute("UPDATE devices SET last_seen_at=? WHERE device_id=?", (now(), device_id))
            conn.commit()

    def revoke(self, device_id: str) -> None:
        with self._lock:
            conn = self.connect()
            conn.execute("UPDATE devices SET state=?, terminated_at=COALESCE(terminated_at,?),"
                         " terminated_reason=COALESCE(terminated_reason,'revoked') WHERE device_id=?",
                         (STATE_TERMINATED, now(), device_id))
            conn.commit()

    def purge_device(self, device_id: str) -> int:
        """Delete everything about one device (a data-subject request)."""
        with self._lock:
            conn = self.connect()
            cur = conn.execute("DELETE FROM events WHERE device_id=?", (device_id,))
            removed = cur.rowcount or 0
            for table in ("batches", "heartbeats", "devices"):
                conn.execute(f"DELETE FROM {table} WHERE device_id=?", (device_id,))
            conn.commit()
        return removed

    def delete_device_data(self, device_id: str) -> int:
        with self._lock:
            conn = self.connect()
            cur = conn.execute("DELETE FROM events WHERE device_id=?", (device_id,))
            removed = cur.rowcount or 0
            conn.commit()
        return removed

    def iter_devices(self, limit: int = 200, offset: int = 0) -> list[dict]:
        with self._lock:
            rows = self.connect().execute(
                "SELECT d.*, (SELECT COUNT(*) FROM events e WHERE e.device_id=d.device_id) AS event_count,"
                " (SELECT COUNT(DISTINCT e.session_id) FROM events e WHERE e.device_id=d.device_id)"
                " AS session_count"
                " FROM devices d ORDER BY d.created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(r) for r in rows]

    def device_stats(self, device_id: str) -> dict:
        with self._lock:
            conn = self.connect()
            row = conn.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT session_id) AS sessions, MAX(ts) AS last_ts,"
                " MAX(received_at) AS last_received FROM events WHERE device_id=?", (device_id,)).fetchone()
            tiers = {r["tier"] or "?": r["n"] for r in conn.execute(
                "SELECT tier, COUNT(*) n FROM events WHERE device_id=? GROUP BY tier", (device_id,))}
        return {"event_count": row["n"] if row else 0, "session_count": row["sessions"] if row else 0,
                "last_event_at": (row["last_ts"] if row else None) or (row["last_received"] if row else None),
                "tiers": tiers}

    # --- events -----------------------------------------------------------------------------
    def insert_events(self, device_id: str, batch_id: str | None, batch_schema: str | None,
                      batch_tier: str | None, events: list[dict],
                      schemas: tuple, max_body: int) -> dict:
        accepted = duplicates = rejected = 0
        results: list[dict] = []
        stamp = now()
        with self._lock:
            conn = self.connect()
            for event in events:
                event_id = event.get("event_id") if isinstance(event, dict) else None
                ok, why = validate_event(event, schemas)
                if not ok:
                    rejected += 1
                    results.append({"event_id": event_id, "status": "rejected", "error": {"code": why}})
                    continue
                body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                if len(body.encode("utf-8")) > max_body:
                    rejected += 1
                    results.append({"event_id": event_id, "status": "rejected",
                                    "error": {"code": "event_too_large"}})
                    continue
                cur = conn.execute(
                    "INSERT OR IGNORE INTO events(device_id, event_id, schema, tier, session_id, seq, kind,"
                    " ts, model, batch_id, received_at, body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id, event_id, event.get("schema"), event.get("tier"),
                     event.get("session_id"), event.get("seq") if isinstance(event.get("seq"), int) else None,
                     event.get("kind"), event.get("ts"), event.get("model"), batch_id, stamp, body))
                if cur.rowcount:
                    accepted += 1
                    results.append({"event_id": event_id, "status": "accepted"})
                else:
                    duplicates += 1
                    results.append({"event_id": event_id, "status": "duplicate"})
            conn.execute(
                "INSERT INTO batches(device_id, batch_id, schema, tier, events, accepted, duplicates,"
                " rejected, received_at) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(device_id, batch_id) DO UPDATE SET events=events+excluded.events,"
                " accepted=accepted+excluded.accepted, duplicates=duplicates+excluded.duplicates,"
                " rejected=rejected+excluded.rejected",
                (device_id, batch_id or "-", batch_schema, batch_tier, len(events), accepted, duplicates,
                 rejected, stamp))
            conn.execute("UPDATE devices SET last_seen_at=? WHERE device_id=?", (stamp, device_id))
            conn.commit()
        return {"accepted": accepted, "duplicates": duplicates, "rejected": rejected, "results": results}

    def iter_events(self, device_id: str | None = None, limit: int = 1000, offset: int = 0):
        sql = "SELECT * FROM events"
        args: list = []
        if device_id:
            sql += " WHERE device_id=?"
            args.append(device_id)
        sql += " ORDER BY device_id, session_id, seq LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self._lock:
            rows = self.connect().execute(sql, args).fetchall()
        for row in rows:
            yield dict(row)

    def prune_events(self, days: int) -> int:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))
        with self._lock:
            conn = self.connect()
            cur = conn.execute("DELETE FROM events WHERE received_at < ?", (cutoff,))
            removed = cur.rowcount or 0
            conn.commit()
        return removed

    # --- reading one device (the /admin/ui pages and `view`) --------------------------------
    def sessions_for(self, device_id: str) -> list[dict]:
        with self._lock:
            rows = self.connect().execute(
                "SELECT session_id, COUNT(*) AS events, MIN(ts) AS first_ts, MAX(ts) AS last_ts"
                " FROM events WHERE device_id=? GROUP BY session_id ORDER BY last_ts DESC",
                (device_id,)).fetchall()
            out = []
            for row in rows:
                tiers = {r["tier"] or "?": r["n"] for r in self.connect().execute(
                    "SELECT tier, COUNT(*) n FROM events WHERE device_id=? AND session_id=?"
                    " GROUP BY tier", (device_id, row["session_id"]))}
                kinds = {r["kind"] or "?": r["n"] for r in self.connect().execute(
                    "SELECT kind, COUNT(*) n FROM events WHERE device_id=? AND session_id=?"
                    " GROUP BY kind ORDER BY n DESC", (device_id, row["session_id"]))}
                out.append({"session_id": row["session_id"], "events": row["events"],
                            "first_ts": row["first_ts"], "last_ts": row["last_ts"],
                            "tiers": tiers, "kinds": kinds})
        return out

    def events_for_session(self, device_id: str, session_id: str) -> list[dict]:
        """Records of one session, newest tier first within the same ordinal, for display."""
        with self._lock:
            rows = self.connect().execute(
                "SELECT * FROM events WHERE device_id=? AND session_id=?"
                " ORDER BY seq, CASE tier WHEN 'L0' THEN 0 WHEN 'L1' THEN 1 WHEN 'L2' THEN 2 ELSE 3 END",
                (device_id, session_id)).fetchall()
        return [dict(r) for r in rows]

    def event(self, device_id: str, event_id: str) -> dict | None:
        with self._lock:
            row = self.connect().execute("SELECT * FROM events WHERE device_id=? AND event_id=?",
                                         (device_id, event_id)).fetchone()
        return dict(row) if row else None

    def latest_session(self) -> tuple[str, str] | None:
        """(device_id, session_id) of the most recently received event — `view --latest`."""
        with self._lock:
            row = self.connect().execute(
                "SELECT device_id, session_id FROM events ORDER BY received_at DESC, rowid DESC"
                " LIMIT 1").fetchone()
        return (row["device_id"], row["session_id"]) if row else None

    # --- service state ----------------------------------------------------------------------
    def terminate(self, device_id: str, reason: str = "server") -> dict:
        with self._lock:
            conn = self.connect()
            conn.execute("UPDATE devices SET state=?, terminated_at=COALESCE(terminated_at,?),"
                         " terminated_reason=COALESCE(terminated_reason,?) WHERE device_id=?",
                         (STATE_TERMINATED, now(), reason, device_id))
            conn.commit()
            row = conn.execute("SELECT state, terminated_at FROM devices WHERE device_id=?",
                               (device_id,)).fetchone()
        return {"state": row["state"], "terminated_at": row["terminated_at"]} if row else {}

    def set_state(self, device_id: str, state: str) -> None:
        with self._lock:
            conn = self.connect()
            conn.execute("UPDATE devices SET state=?, terminated_at=NULL, terminated_reason=NULL"
                         " WHERE device_id=?", (state, device_id))
            conn.commit()

    def mark_uninstalled(self, device_id: str) -> None:
        with self._lock:
            conn = self.connect()
            conn.execute("UPDATE devices SET uninstalled_at=COALESCE(uninstalled_at,?),"
                         " terminated_at=COALESCE(terminated_at,?),"
                         " terminated_reason=COALESCE(terminated_reason,'uninstall'),"
                         " uninstall_calls=uninstall_calls+1, state=? WHERE device_id=?",
                         (now(), now(), STATE_TERMINATED, device_id))
            conn.commit()

    def save_heartbeat(self, device_id: str, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            conn = self.connect()
            conn.execute(
                "INSERT INTO heartbeats(device_id, received_at, count, body) VALUES (?,?,1,?)"
                " ON CONFLICT(device_id) DO UPDATE SET received_at=excluded.received_at,"
                " count=count+1, body=excluded.body", (device_id, now(), payload))
            conn.execute("UPDATE devices SET last_seen_at=? WHERE device_id=?", (now(), device_id))
            conn.commit()

    def heartbeats(self) -> dict:
        with self._lock:
            rows = self.connect().execute("SELECT * FROM heartbeats").fetchall()
        out = {}
        for row in rows:
            try:
                body = json.loads(row["body"])
            except ValueError:
                body = {}
            out[row["device_id"]] = {"device_id": row["device_id"], "received_at": row["received_at"],
                                     "count": row["count"], "heartbeat": body}
        return out

    # --- redemptions ------------------------------------------------------------------------
    def add_cdk(self, code: str, product: str | None = None, expires_at: str | None = None) -> None:
        with self._lock:
            conn = self.connect()
            conn.execute("INSERT OR IGNORE INTO cdk(code_hash, product, expires_at) VALUES (?,?,?)",
                         (hash_token(code.upper().strip()), product, expires_at))
            conn.commit()

    def redemption(self, redemption_id: str) -> dict | None:
        with self._lock:
            row = self.connect().execute("SELECT * FROM redemptions WHERE redemption_id=?",
                                         (redemption_id,)).fetchone()
        return dict(row) if row else None

    def redeem(self, code: str, account: str) -> tuple[int, dict]:
        """(status_code, body). Idempotent on `(cdk, account)` — a retry returns the first record."""
        code_hash = hash_token(code.upper().strip())
        stamp = now()
        with self._lock:
            conn = self.connect()
            existing = conn.execute("SELECT * FROM redemptions WHERE cdk_hash=? AND account=?",
                                    (code_hash, account)).fetchone()
            if existing:
                return 200, _redemption_body(dict(existing), replayed=True)
            row = conn.execute("SELECT * FROM cdk WHERE code_hash=?", (code_hash,)).fetchone()
            if row is None:
                return 400, _error("cdk_invalid", "CDK 无效")
            if row["expires_at"] and row["expires_at"] < stamp:
                return 410, _error("cdk_expired", "此 CDK 已过期")
            if row["used_by"] and row["used_by"] != account:
                return 409, _error("cdk_already_redeemed", "此 CDK 已被其他账号使用")
            redemption_id = REDEMPTION_PREFIX + secrets.token_hex(12)
            conn.execute(
                "INSERT INTO redemptions(redemption_id, cdk_hash, account, status, product, created_at,"
                " updated_at) VALUES (?,?,?,'succeeded',?,?,?)",
                (redemption_id, code_hash, account, row["product"], stamp, stamp))
            conn.execute("UPDATE cdk SET used_by=?, used_at=?, redemption_id=? WHERE code_hash=?",
                         (account, stamp, redemption_id, code_hash))
            conn.commit()
        return 201, {"redemption_id": redemption_id, "status": "succeeded", "replayed": False,
                     "product": row["product"], "redeemed_at": stamp, "message": None}

    def stats(self) -> dict:
        with self._lock:
            conn = self.connect()
            devices = conn.execute("SELECT COUNT(*) n FROM devices").fetchone()["n"]
            active = conn.execute("SELECT COUNT(*) n FROM devices WHERE state='active'").fetchone()["n"]
            events = conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
            sessions = conn.execute("SELECT COUNT(DISTINCT session_id) n FROM events").fetchone()["n"]
            tiers = {r["tier"] or "?": r["n"] for r in conn.execute(
                "SELECT tier, COUNT(*) n FROM events GROUP BY tier")}
            kinds = {r["kind"] or "?": r["n"] for r in conn.execute(
                "SELECT kind, COUNT(*) n FROM events GROUP BY kind ORDER BY n DESC LIMIT 20")}
            charges = conn.execute("SELECT COUNT(*) n FROM redemptions WHERE status='succeeded'").fetchone()["n"]
            uninstalled = conn.execute(
                "SELECT COUNT(*) n FROM devices WHERE uninstalled_at IS NOT NULL").fetchone()["n"]
        return {"devices": devices, "active_devices": active, "uninstalled_devices": uninstalled,
                "events": events, "sessions": sessions, "tiers": tiers, "kinds": kinds,
                "redemptions": charges, "generated_at": now()}


def validate_event(event: object, schemas: tuple) -> tuple[bool, str]:
    if not isinstance(event, dict):
        return False, "invalid_event"
    if not isinstance(event.get("event_id"), str) or not event["event_id"]:
        return False, "invalid_event"
    if not isinstance(event.get("session_id"), str) or not event["session_id"]:
        return False, "invalid_event"
    if not isinstance(event.get("seq"), int) or isinstance(event.get("seq"), bool):
        return False, "invalid_event"
    if event.get("schema") not in schemas:
        return False, "unknown_schema"
    return True, ""


def _redemption_body(row: dict, replayed: bool) -> dict:
    body = {"redemption_id": row["redemption_id"], "status": row["status"], "replayed": replayed,
            "product": row["product"], "redeemed_at": row["created_at"], "message": None}
    if row.get("error"):
        body["error"] = {"code": row["error"]}
    return body


def _error(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}
