"""Black-box HTTP tests for `server/ctrsrv` (docs/sync-api.md, docs/collector-api.md, docs/recharge-api.md).

Every test starts the *real* server on an ephemeral loopback port inside this process and talks to it
over HTTP with `urllib.request`. Nothing about the HTTP layer is mocked: the route table, the size
caps, the sqlite file and the auth codes are the production ones. Each test gets its own temp DB and
its own server, so the file is order independent and no port is ever fixed.

Run from the repo root:

    $env:PYTHONPATH="server;tests"; python -m pytest tests\\test_server_api.py -q
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from ctrsrv import __version__, terms
from ctrsrv.app import MAX_BODY, RATE_BATCHES_PER_MIN, Config, Server

ADMIN_TOKEN = "test-admin"
_UNSET = object()


# --- tiny HTTP client ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """docs/sync-api.md §1, docs/recharge-api.md §7: these endpoints never answer 3xx."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # pragma: no cover - failure path
        raise AssertionError(f"{req.get_method()} {req.full_url} answered {code} -> {newurl}")


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class Resp:
    status: int
    doc: object
    text: str
    headers: dict

    @property
    def code(self):
        """`error.code` of an error body, else None."""
        error = self.doc.get("error") if isinstance(self.doc, dict) else None
        return error.get("code") if isinstance(error, dict) else None


def _json_or_none(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def http(base: str, method: str, path: str, *, body=_UNSET, raw: bytes | None = None,
         token: str | None = None, admin: str | None = None, claim_length: int | None = None,
         timeout: float = 15.0) -> Resp:
    """One request. `admin` uses the same bearer header the admin surface reads."""
    data = None
    if raw is not None:
        data = raw
    elif body is not _UNSET:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(base + path, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json; charset=utf-8")
    if admin is not None:
        request.add_header("Authorization", f"Bearer {admin}")
    elif token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    if claim_length is not None:
        request.add_header("Content-Length", str(claim_length))
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return Resp(response.status, _json_or_none(text), text, dict(response.headers))
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        return Resp(exc.code, _json_or_none(text), text, dict(exc.headers or {}))


# --- server harness ----------------------------------------------------------------------------
class Harness:
    """A real `Server` on 127.0.0.1:0 with its own sqlite file."""

    def __init__(self, tmp_path: Path, admin_token: str | None = ADMIN_TOKEN):
        self.admin_token = admin_token
        self.db = Path(tmp_path) / f"db-{uuid.uuid4().hex[:8]}.sqlite3"
        self.server = Server(Config(db=self.db, host="127.0.0.1", port=0, admin_token=admin_token))
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()

    # -- shortcuts used by most tests ---------------------------------------------------------
    def call(self, method: str, path: str, **kwargs) -> Resp:
        return http(self.base, method, path, **kwargs)

    def register(self, client: str = "pytest-server-api/1.0", platform: str = "windows") -> dict:
        response = self.call("POST", "/v1/devices",
                             body={"client": client, "platform": platform,
                                   "consent": {"version": terms.TERMS_VERSION,
                                               "accepted_at": "2026-11-01T10:00:00Z"}})
        assert response.status == 201, response.text
        return response.doc

    def upload(self, token: str, events, **extra) -> Resp:
        payload = {"schema": "comp.sync.batch/v1", "client": "codex-companion-collector/1.0.0",
                   "batch_id": uuid.uuid4().hex, "events": events}
        payload.update(extra)
        return self.call("POST", "/v1/events/batch", body=payload, token=token)

    def store(self):
        return self.server.api.store

    def file_bytes(self) -> bytes:
        """The database file plus its WAL/SHM siblings — everything written to disk."""
        blob = b""
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.db) + suffix)
            if path.exists():
                blob += path.read_bytes()
        return blob

    def close(self) -> None:
        self.server.shutdown()
        self._thread.join(5.0)
        self.server.server_close()
        self.server.api.store.close()


@pytest.fixture
def launch(tmp_path):
    """Start any number of servers for one test; all of them are torn down afterwards."""
    started: list[Harness] = []

    def _launch(admin_token: str | None = ADMIN_TOKEN) -> Harness:
        harness = Harness(tmp_path, admin_token)
        started.append(harness)
        return harness

    yield _launch
    for harness in started:
        harness.close()


@pytest.fixture
def srv(launch) -> Harness:
    return launch()


def event(event_id: str | None = None, seq: int = 1, session_id: str = "sess-1", tier: str = "L3",
          **extra) -> dict:
    doc = {"schema": "comp.event/v1", "event_id": event_id or uuid.uuid4().hex,
           "session_id": session_id, "seq": seq, "kind": "tool_call",
           "recorded_at": "2026-11-01T10:00:00.000Z", "tier": tier,
           "data": {"tool": "shell", "status": "ok"}}
    doc.update(extra)
    return doc


# --- devices / auth ------------------------------------------------------------------------------
def test_register_returns_prefixed_ids_and_stores_only_a_hash(srv):
    created = srv.register(client="codex-companion-collector/1.0.0")

    assert created["device_id"].startswith("dev_")
    assert created["device_token"].startswith("ctrd_")
    assert created["created_at"]

    token = created["device_token"]
    assert token.encode("utf-8") not in srv.file_bytes()

    conn = sqlite3.connect(str(srv.db))
    try:
        rows = conn.execute("SELECT device_id, token_hash FROM devices").fetchall()
    finally:
        conn.close()
    assert rows == [(created["device_id"], hashlib.sha256(token.encode("utf-8")).hexdigest())]


def test_device_me_and_per_path_auth_codes(srv):
    created = srv.register()
    token = created["device_token"]

    me = srv.call("GET", "/v1/devices/me", token=token)
    assert me.status == 200
    assert me.doc["device_id"] == created["device_id"]
    assert me.doc["event_count"] == 0
    assert me.doc["session_count"] == 0
    assert me.doc["created_at"] == created["created_at"]

    bad = srv.call("GET", "/v1/devices/me", token="ctrd_not-a-real-token")
    assert bad.status == 401
    assert bad.code == "device_invalid"

    anonymous = srv.call("GET", "/v1/devices/me")
    assert anonymous.status == 401
    assert anonymous.code == "device_invalid"

    # The collector paths use their own 401 code so logs can tell the two components apart.
    state = srv.call("GET", "/v1/service-state", token="ctrd_not-a-real-token")
    assert state.status == 401
    assert state.code == "unauthorized"


def test_revoked_token_gets_device_revoked_on_upload(srv):
    token = srv.register()["device_token"]

    revoked = srv.call("DELETE", "/v1/devices/me", token=token)
    assert revoked.status == 200

    refused = srv.upload(token, [event()])
    assert refused.status == 403
    assert refused.code == "device_revoked"


# --- ingest ---------------------------------------------------------------------------------------
def test_batch_is_idempotent_on_event_id(srv):
    token = srv.register()["device_token"]
    batch_id = uuid.uuid4().hex
    events = [event(seq=n, session_id="sess-1") for n in (1, 2, 3)]

    first = srv.upload(token, events, batch_id=batch_id)
    assert first.status == 200
    assert (first.doc["accepted"], first.doc["duplicates"], first.doc["rejected"]) == (3, 0, 0)
    assert [r["status"] for r in first.doc["results"]] == ["accepted"] * 3
    assert [r["event_id"] for r in first.doc["results"]] == [e["event_id"] for e in events]

    again = srv.upload(token, events, batch_id=batch_id)
    assert again.status == 200
    assert (again.doc["accepted"], again.doc["duplicates"], again.doc["rejected"]) == (0, 3, 0)
    assert [r["status"] for r in again.doc["results"]] == ["duplicate"] * 3

    assert srv.call("GET", "/v1/devices/me", token=token).doc["event_count"] == 3


def test_each_bad_event_is_rejected_while_the_valid_ones_are_stored(srv):
    token = srv.register()["device_token"]

    good_one = event(event_id="e-good-1", seq=1)
    good_two = event(event_id="e-good-2", seq=2)
    unknown_schema = event(event_id="e-unknown", seq=3, schema="nope.event/v9")
    no_seq = event(event_id="e-no-seq")
    del no_seq["seq"]
    no_id = event(seq=5)
    del no_id["event_id"]

    response = srv.upload(token, [good_one, unknown_schema, no_seq, no_id, good_two])
    assert response.status == 200
    assert response.doc["accepted"] == 2
    assert response.doc["rejected"] == 3
    assert response.doc["duplicates"] == 0
    assert len(response.doc["results"]) == 5

    results = {r["event_id"]: r for r in response.doc["results"]}
    assert results["e-good-1"]["status"] == "accepted"
    assert results["e-good-2"]["status"] == "accepted"
    assert results["e-unknown"]["status"] == "rejected"
    assert results["e-unknown"]["error"]["code"] == "unknown_schema"
    assert results["e-no-seq"]["status"] == "rejected"
    assert results["e-no-seq"]["error"]["code"] == "invalid_event"
    assert results[None]["status"] == "rejected"
    assert results[None]["error"]["code"] == "invalid_event"

    assert srv.call("GET", "/v1/devices/me", token=token).doc["event_count"] == 2


def test_the_same_event_id_from_two_devices_is_accepted_twice(srv):
    token_a = srv.register()["device_token"]
    token_b = srv.register()["device_token"]
    shared = "shared-event-id-across-devices"

    first = srv.upload(token_a, [event(event_id=shared, seq=1)])
    second = srv.upload(token_b, [event(event_id=shared, seq=1)])

    assert (first.status, first.doc["accepted"], first.doc["duplicates"]) == (200, 1, 0)
    assert (second.status, second.doc["accepted"], second.doc["duplicates"]) == (200, 1, 0)
    assert srv.call("GET", "/v1/devices/me", token=token_a).doc["event_count"] == 1
    assert srv.call("GET", "/v1/devices/me", token=token_b).doc["event_count"] == 1


def test_batch_body_must_be_a_non_empty_list_of_events(srv):
    token = srv.register()["device_token"]

    not_a_list = srv.upload(token, "nope")
    assert not_a_list.status == 400
    assert not_a_list.code == "invalid_batch"

    empty = srv.upload(token, [])
    assert empty.status == 400
    assert empty.code == "invalid_batch"

    missing = srv.call("POST", "/v1/events/batch", body={"batch_id": "b"}, token=token)
    assert missing.status == 400
    assert missing.code == "invalid_batch"


def test_device_me_reports_counts_and_tiers_after_upload(srv):
    token = srv.register()["device_token"]
    events = [event(seq=1, session_id="s1", tier="L0"),
              event(seq=2, session_id="s1", tier="L0"),
              event(seq=1, session_id="s2", tier="L3")]
    assert srv.upload(token, events).doc["accepted"] == 3

    me = srv.call("GET", "/v1/devices/me", token=token)
    assert me.status == 200
    assert me.doc["event_count"] == 3
    assert me.doc["session_count"] == 2
    assert me.doc["tiers"] == {"L0": 2, "L3": 1}
    assert me.doc["last_event_at"]


# --- service state --------------------------------------------------------------------------------
def test_service_state_for_a_fresh_device(srv):
    token = srv.register()["device_token"]

    response = srv.call("GET", "/v1/service-state", token=token)
    assert response.status == 200
    doc = response.doc
    assert doc["state"] == "active"
    assert doc["terminated_at"] is None
    assert doc["terms_version"] == terms.TERMS_VERSION
    assert doc["terms_hash"] == terms.TERMS_HASH
    assert doc["terms_hash"].startswith("sha256:")
    assert doc["grace_hours"] == 72
    assert doc["grace_hours"] == terms.GRACE_HOURS
    assert doc["effective_at"]
    assert doc["server_time"]


def test_terminate_is_idempotent_and_stops_uploads(srv):
    token = srv.register()["device_token"]

    first = srv.call("POST", "/v1/service-state/terminate", token=token)
    assert first.status == 200
    assert first.doc["state"] == "terminated"
    assert first.doc["terminated_at"]

    second = srv.call("POST", "/v1/service-state/terminate", token=token)
    assert second.status == 200
    assert second.doc == first.doc

    state = srv.call("GET", "/v1/service-state", token=token)
    assert state.status == 200
    assert state.doc["state"] == "terminated"
    assert state.doc["terminated_at"] == first.doc["terminated_at"]

    refused = srv.upload(token, [event()])
    assert refused.status == 403
    assert refused.code == "device_revoked"


def test_uninstall_is_repeatable_and_revokes_every_authenticated_path(srv):
    token = srv.register()["device_token"]

    assert srv.call("POST", "/v1/device/uninstall", token=token).status == 200
    second = srv.call("POST", "/v1/device/uninstall", token=token)
    assert second.status == 200
    assert second.doc == {"ok": True}

    authenticated = [
        ("GET", "/v1/devices/me"),
        ("DELETE", "/v1/devices/me/data"),
        ("DELETE", "/v1/devices/me"),
        ("POST", "/v1/events/batch"),
        ("GET", "/v1/service-state"),
        ("POST", "/v1/service-state/terminate"),
        ("POST", "/v1/device/heartbeat"),
    ]
    for method, path in authenticated:
        kwargs = {"token": token}
        if path == "/v1/events/batch":
            kwargs["body"] = {"schema": "comp.sync.batch/v1", "batch_id": "b1", "events": [event()]}
        elif method == "POST":
            kwargs["body"] = {"client": "codex-companion-collector/1.0.0"}
        response = srv.call(method, path, **kwargs)
        assert response.status == 403, (method, path, response.text)
        assert response.code == "device_revoked", (method, path, response.text)


# --- heartbeat ------------------------------------------------------------------------------------
def test_heartbeat_accepts_small_bodies_and_caps_at_4_kib(srv):
    token = srv.register()["device_token"]

    small = {"client": "codex-companion-collector/1.0.0", "queue_depth": 12, "uploaded_total": 340,
             "tiers": {"L0": 120, "L1": 80, "L2": 140, "L3": 0}, "service_state": "active"}
    ok = srv.call("POST", "/v1/device/heartbeat", body=small, token=token)
    assert ok.status == 200
    assert ok.doc["ok"] is True
    assert ok.doc["server_time"]

    big = dict(small, note="x" * 5000)
    too_large = srv.call("POST", "/v1/device/heartbeat", body=big, token=token)
    assert too_large.status == 413
    assert too_large.code == "too_large"


def test_heartbeat_rejects_a_body_that_is_not_a_json_object(srv):
    # docs/collector-api.md §4: unknown/mistyped *fields* are ignored, but a body that is not a
    # JSON object is a 400 invalid_request.
    token = srv.register()["device_token"]

    for raw in (b"[1, 2, 3]", b'"just a string"', b"null", b"42"):
        response = srv.call("POST", "/v1/device/heartbeat", raw=raw, token=token)
        assert response.status == 400, raw
        assert response.code == "invalid_request", raw

    # A scalar body must not create a device either.
    body_not_object = srv.call("POST", "/v1/devices", raw=b'"codex-task-recorder/0.2.0"')
    assert body_not_object.status == 400
    assert body_not_object.code == "invalid_request"
    assert srv.call("GET", "/admin/stats", admin=srv.admin_token).doc["devices"] == 1

    # Unknown/mistyped fields inside an object stay a 200.
    lenient = srv.call("POST", "/v1/device/heartbeat", token=token,
                       body={"client": "codex-companion-collector/1.0.0",
                             "queue_depth": "not-a-number", "extra": {"ignored": True}})
    assert lenient.status == 200


# --- recharge -------------------------------------------------------------------------------------
def test_redeem_unknown_then_seeded_then_replay_then_other_account(srv):
    unknown = srv.call("POST", "/v1/redemptions", body={"cdk": "NOPE-0000-0000", "account": "u@example.com"})
    assert unknown.status == 400
    assert unknown.code == "cdk_invalid"

    cdk, product, account = "TEST-OK01", "ChatGPT Plus 1 个月", "user@example.com"
    srv.store().add_cdk(cdk, product)

    created = srv.call("POST", "/v1/redemptions", body={"cdk": cdk, "account": account})
    assert created.status == 201
    assert created.doc["status"] == "succeeded"
    assert created.doc["replayed"] is False
    assert created.doc["redemption_id"].startswith("rdm_")
    assert created.doc["product"] == product
    assert created.doc["message"] is None
    assert cdk not in created.text  # docs/recharge-api.md §1: the CDK lives in the request body only

    replay = srv.call("POST", "/v1/redemptions", body={"cdk": cdk, "account": account})
    assert replay.status == 200
    assert replay.doc["replayed"] is True
    assert replay.doc["redemption_id"] == created.doc["redemption_id"]

    other = srv.call("POST", "/v1/redemptions", body={"cdk": cdk, "account": "other@example.com"})
    assert other.status == 409
    assert other.code == "cdk_already_redeemed"


def test_redemption_lookup_and_unknown_id(srv):
    cdk, account = "TEST-OK02", "lookup@example.com"
    srv.store().add_cdk(cdk, "ChatGPT Plus 1 个月")
    created = srv.call("POST", "/v1/redemptions", body={"cdk": cdk, "account": account}).doc

    found = srv.call("GET", f"/v1/redemptions/{created['redemption_id']}")
    assert found.status == 200
    assert found.doc["redemption_id"] == created["redemption_id"]
    assert found.doc["status"] == "succeeded"
    assert found.doc["product"] == created["product"]
    assert found.doc["redeemed_at"] == created["redeemed_at"]

    missing = srv.call("GET", "/v1/redemptions/rdm_0000000000000000")
    assert missing.status == 404
    assert missing.code == "not_found"


def test_redeem_without_a_usable_account_is_invalid_account(srv):
    cdk = "TEST-OK03"
    srv.store().add_cdk(cdk, "ChatGPT Plus 1 个月")

    for body in ({"cdk": cdk}, {"cdk": cdk, "account": ""}, {"cdk": cdk, "account": "   "},
                 {"cdk": cdk, "account": 7}):
        response = srv.call("POST", "/v1/redemptions", body=body)
        assert response.status == 400, body
        assert response.code == "invalid_account", body

    # No CDK at all is still a 400, reported as the CDK being the missing/invalid half.
    no_cdk = srv.call("POST", "/v1/redemptions", body={"account": "nobody@example.com"})
    assert no_cdk.status == 400
    assert no_cdk.code == "cdk_invalid"

    # A rejected account must not consume the CDK.
    after = srv.call("POST", "/v1/redemptions", body={"cdk": cdk, "account": "late@example.com"})
    assert after.status == 201
    assert after.doc["status"] == "succeeded"


def test_expired_cdk_is_rejected_and_stays_unused(srv):
    srv.store().add_cdk("TEST-EXPD", "ChatGPT Plus 1 个月", expires_at="2000-01-01T00:00:00Z")

    expired = srv.call("POST", "/v1/redemptions", body={"cdk": "TEST-EXPD", "account": "x@example.com"})
    assert expired.status == 410
    assert expired.code == "cdk_expired"
    assert srv.call("GET", "/admin/stats", admin=srv.admin_token).doc["redemptions"] == 0


# --- admin ----------------------------------------------------------------------------------------
def test_admin_requires_the_token_and_reports_ingested_counters(srv):
    created = srv.register()
    token, device_id = created["device_token"], created["device_id"]
    events = [event(seq=1, session_id="s1", tier="L0"),
              event(seq=2, session_id="s1", tier="L0"),
              event(seq=3, session_id="s1", tier="L0"),
              event(seq=1, session_id="s2", tier="L3")]
    assert srv.upload(token, events).doc["accepted"] == 4

    endpoints = [
        ("GET", "/admin/stats", None),
        ("GET", "/admin/devices", None),
        ("GET", f"/admin/devices/{device_id}", None),
        ("DELETE", f"/admin/devices/{device_id}", None),
        ("POST", f"/admin/devices/{device_id}/terminate", {}),
        ("POST", f"/admin/devices/{device_id}/resume", {}),
        ("POST", "/admin/cdk", {"code": "ADMIN-ONLY-0001"}),
        ("POST", "/admin/prune", {"days": 90}),
    ]
    for method, path, body in endpoints:
        kwargs = {} if body is None else {"body": body}
        anonymous = srv.call(method, path, **kwargs)
        assert anonymous.status == 401, (method, path, anonymous.text)
        assert anonymous.code == "unauthorized", (method, path, anonymous.text)

        wrong = srv.call(method, path, admin="wrong-admin-token", **kwargs)
        assert wrong.status == 401, (method, path, wrong.text)
        assert wrong.code == "unauthorized", (method, path, wrong.text)

    stats = srv.call("GET", "/admin/stats", admin=srv.admin_token)
    assert stats.status == 200
    doc = stats.doc
    assert doc["devices"] == 1
    assert doc["active_devices"] == 1
    assert doc["uninstalled_devices"] == 0
    assert doc["events"] == 4
    assert doc["sessions"] == 2
    assert doc["tiers"] == {"L0": 3, "L3": 1}
    assert doc["kinds"] == {"tool_call": 4}
    assert doc["redemptions"] == 0
    assert doc["generated_at"]

    listed = srv.call("GET", "/admin/devices", admin=srv.admin_token)
    assert listed.status == 200
    assert [d["device_id"] for d in listed.doc["devices"]] == [device_id]
    assert listed.doc["devices"][0]["event_count"] == 4

    # A body that is not an object is tolerated by an endpoint that only reads `days`.
    pruned = srv.call("POST", "/admin/prune", raw=b"[1]", admin=srv.admin_token)
    assert pruned.status == 200
    assert pruned.doc["ok"] is True


def test_admin_purge_removes_the_device_and_its_events(srv):
    created = srv.register()
    token, device_id = created["device_token"], created["device_id"]
    assert srv.upload(token, [event(seq=1, session_id="s1"), event(seq=2, session_id="s1")]).doc["accepted"] == 2

    before = srv.call("GET", f"/admin/devices/{device_id}", admin=srv.admin_token)
    assert before.status == 200
    assert before.doc["stats"]["event_count"] == 2

    purged = srv.call("DELETE", f"/admin/devices/{device_id}", admin=srv.admin_token)
    assert purged.status == 200
    assert purged.doc["deleted_events"] == 2

    gone = srv.call("GET", f"/admin/devices/{device_id}", admin=srv.admin_token)
    assert gone.status == 404
    assert gone.code == "not_found"

    assert srv.call("GET", "/v1/devices/me", token=token).code == "device_invalid"

    stats = srv.call("GET", "/admin/stats", admin=srv.admin_token).doc
    assert stats["devices"] == 0
    assert stats["events"] == 0
    assert srv.call("GET", "/admin/devices", admin=srv.admin_token).doc["devices"] == []


def test_admin_terminate_then_resume_flips_the_state_back(srv):
    created = srv.register()
    token, device_id = created["device_token"], created["device_id"]

    terminated = srv.call("POST", f"/admin/devices/{device_id}/terminate", admin=srv.admin_token)
    assert terminated.status == 200
    assert terminated.doc["state"] == "terminated"
    assert terminated.doc["terminated_at"]

    stopped = srv.call("GET", "/v1/service-state", token=token).doc
    assert stopped["state"] == "terminated"
    assert stopped["terminated_at"] == terminated.doc["terminated_at"]
    assert srv.upload(token, [event()]).code == "device_revoked"

    resumed = srv.call("POST", f"/admin/devices/{device_id}/resume", admin=srv.admin_token)
    assert resumed.status == 200
    assert resumed.doc == {"ok": True, "state": "active"}

    back = srv.call("GET", "/v1/service-state", token=token).doc
    assert back["state"] == "active"
    assert back["terminated_at"] is None

    assert srv.upload(token, [event(seq=9)]).doc["accepted"] == 1


def test_admin_is_disabled_when_no_token_is_configured(launch):
    disabled = launch(admin_token=None)

    for supplied in (None, ADMIN_TOKEN, ""):
        kwargs = {} if supplied is None else {"admin": supplied}
        response = disabled.call("GET", "/admin/stats", **kwargs)
        assert response.status == 403, response.text
        assert response.code == "admin_disabled", response.text

    prune = disabled.call("POST", "/admin/prune", body={"days": 30})
    assert prune.status == 403
    assert prune.code == "admin_disabled"

    # The client surface keeps working without an admin token.
    assert disabled.call("GET", "/v1/health").status == 200
    assert disabled.register()["device_id"].startswith("dev_")


# --- transport ------------------------------------------------------------------------------------
def test_transport_errors(srv):
    missing = srv.call("GET", "/v1/does-not-exist")
    assert missing.status == 404
    assert missing.code == "not_found"

    post_missing = srv.call("POST", "/v1/does-not-exist", body={"a": 1})
    assert post_missing.status // 100 != 3
    assert post_missing.status == 404
    assert post_missing.code == "not_found"

    # 8 MiB cap: claim a body over the limit but send a few bytes — the server rejects before reading.
    claimed = srv.call("POST", "/v1/events/batch", raw=b"{}", claim_length=MAX_BODY + 1)
    assert claimed.status == 413
    assert claimed.code == "batch_too_large"

    token = srv.register()["device_token"]
    broken = srv.call("POST", "/v1/events/batch", raw=b"{not json", token=token)
    assert broken.status == 400
    assert broken.code == "invalid_request"


# --- extra documented behaviour -------------------------------------------------------------------
def test_health_endpoint(srv):
    response = srv.call("GET", "/v1/health")
    assert response.status == 200
    assert response.doc["ok"] is True
    assert response.doc["version"] == __version__
    assert response.doc["terms_version"] == terms.TERMS_VERSION


def test_uploaded_data_can_be_deleted_and_the_second_call_reports_zero(srv):
    token = srv.register()["device_token"]
    assert srv.upload(token, [event(seq=1), event(seq=2)]).doc["accepted"] == 2

    first = srv.call("DELETE", "/v1/devices/me/data", token=token)
    assert first.status == 200
    assert first.doc["deleted_events"] == 2

    second = srv.call("DELETE", "/v1/devices/me/data", token=token)
    assert second.status == 200
    assert second.doc["deleted_events"] == 0

    assert srv.call("GET", "/v1/devices/me", token=token).doc["event_count"] == 0
    # Deleting the data is not a revocation: the device can keep recording.
    assert srv.upload(token, [event(seq=1)]).doc["accepted"] == 1


def test_batch_rate_limit_answers_429_with_retry_after(srv):
    token = srv.register()["device_token"]

    for n in range(RATE_BATCHES_PER_MIN):
        response = srv.upload(token, [event(seq=n)])
        assert response.status == 200, (n, response.text)

    limited = srv.upload(token, [event(seq=RATE_BATCHES_PER_MIN)])
    assert limited.status == 429
    assert limited.code == "rate_limited"
    assert limited.headers.get("Retry-After") == "5"
