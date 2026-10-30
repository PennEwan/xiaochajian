"""Backend internals: storage invariants, terms parity with the client, CLI wiring.

`tests/test_server_api.py` covers the HTTP contract; this file covers what sits under it.

Run: `$env:PYTHONPATH="server;tests"; python -m pytest tests\test_server_core.py -q`
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from ctrsrv import cli, terms
from ctrsrv.app import Config, Server, _RateLimiter
from ctrsrv.store import Store, hash_token


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(tmp_path / "db.sqlite3")
    s.connect()
    yield s
    s.close()


def event(event_id: str, seq: int = 0, session_id: str = "s1", tier: str = "L3") -> dict:
    return {"schema": "comp.event/v1", "event_id": event_id, "seq": seq, "session_id": session_id,
            "tier": tier, "kind": "user_prompt", "ts": "2026-11-01T10:00:00Z", "data": {"text": "hi"}}


# --- terms ------------------------------------------------------------------------------------
def test_terms_match_the_collector_client():
    """The backend must advertise exactly the terms the installer showed and the client hashes."""
    pytest.importorskip("collector.contract")
    from collector import contract

    assert terms.TERMS_VERSION == contract.TERMS_VERSION
    assert terms.TERMS_HASH == contract.TERMS_HASH
    assert terms.GRACE_HOURS == contract.GRACE_HOURS
    assert terms.state_document()["terms_hash"].startswith("sha256:")


# --- devices ----------------------------------------------------------------------------------
def test_device_token_is_only_stored_hashed(store, tmp_path):
    created = store.create_device("codex-companion-collector/1.1.0", "windows", {"accepted_at": "x"})
    token = created["device_token"]
    assert token.startswith("ctrd_") and created["device_id"].startswith("dev_")
    store.connect().execute("PRAGMA wal_checkpoint(FULL)")  # tokens may still be sitting in the WAL
    raw = (tmp_path / "db.sqlite3").read_bytes() + (tmp_path / "db.sqlite3-wal").read_bytes() \
        if (tmp_path / "db.sqlite3-wal").exists() else (tmp_path / "db.sqlite3").read_bytes()
    assert token.encode() not in raw, "the raw device token must never touch the database file"
    assert hash_token(token).encode() in raw
    assert store.device_by_token(token)["device_id"] == created["device_id"]
    assert store.device_by_token("ctrd_wrong") is None


def test_device_starts_active_and_can_be_terminated(store):
    device = store.create_device("c", "windows", {})
    assert store.device(device["device_id"])["state"] == "active"
    first = store.terminate(device["device_id"], "server")
    second = store.terminate(device["device_id"], "server")
    assert first["terminated_at"] == second["terminated_at"], "termination must be idempotent"
    store.set_state(device["device_id"], "active")
    assert store.device(device["device_id"])["state"] == "active"


def test_uninstall_marks_the_device_gone_and_counts_calls(store):
    device = store.create_device("c", "windows", {})
    store.mark_uninstalled(device["device_id"])
    store.mark_uninstalled(device["device_id"])
    row = store.device(device["device_id"])
    assert row["uninstalled_at"] and row["uninstall_calls"] == 2 and row["state"] == "terminated"
    assert row["terminated_at"], "an uninstall is also a termination, for reporting"
    assert row["terminated_reason"] == "uninstall"


# --- events -----------------------------------------------------------------------------------
def test_event_id_is_the_idempotency_key_per_device(store):
    device = store.create_device("c", "windows", {})
    other = store.create_device("c", "windows", {})
    first = store.insert_events(device["device_id"], "b1", "companion.sync.batch/v1", "L3",
                               [event("e1"), event("e2", 1)], ("comp.event/v1",), 1 << 20)
    assert (first["accepted"], first["duplicates"], first["rejected"]) == (2, 0, 0)
    again = store.insert_events(device["device_id"], "b2", "companion.sync.batch/v1", "L3",
                                [event("e1"), event("e2", 1)], ("comp.event/v1",), 1 << 20)
    assert (again["accepted"], again["duplicates"]) == (0, 2), "a resend must be a duplicate, not a new row"
    other_device = store.insert_events(other["device_id"], "b1", "companion.sync.batch/v1", "L3",
                                       [event("e1")], ("comp.event/v1",), 1 << 20)
    assert other_device["accepted"] == 1, "the same event_id from another device is a different row"


def test_rejected_events_do_not_block_the_batch(store):
    device = store.create_device("c", "windows", {})
    bad_schema = {**event("e9"), "schema": "nope/v1"}
    no_seq = {k: v for k, v in event("e10").items() if k != "seq"}
    result = store.insert_events(device["device_id"], "b", "x", "L3",
                                 [event("e1"), bad_schema, no_seq], ("comp.event/v1",), 1 << 20)
    assert result["accepted"] == 1 and result["rejected"] == 2
    codes = {r["event_id"]: r.get("error", {}).get("code") for r in result["results"]}
    assert codes["e9"] == "unknown_schema" and codes["e10"] == "invalid_event"


def test_stats_and_device_stats_agree(store):
    device = store.create_device("c", "windows", {})
    store.insert_events(device["device_id"], "b", "x", "L3",
                        [event("e1", 0, "s1", "L0"), event("e2", 1, "s1", "L3"), event("e3", 2, "s2", "L3")],
                        ("comp.event/v1",), 1 << 20)
    stats = store.device_stats(device["device_id"])
    assert stats["event_count"] == 3 and stats["session_count"] == 2
    assert stats["tiers"] == {"L0": 1, "L3": 2}
    total = store.stats()
    assert total["events"] == 3 and total["sessions"] == 2 and total["devices"] == 1


def test_purge_removes_the_device_and_everything_it_sent(store):
    device = store.create_device("c", "windows", {})
    store.insert_events(device["device_id"], "b", "x", "L3", [event("e1")], ("comp.event/v1",), 1 << 20)
    removed = store.purge_device(device["device_id"])
    assert removed == 1
    assert store.device(device["device_id"]) is None
    assert store.device_stats(device["device_id"])["event_count"] == 0


def test_delete_data_keeps_the_device(store):
    device = store.create_device("c", "windows", {})
    store.insert_events(device["device_id"], "b", "x", "L3", [event("e1")], ("comp.event/v1",), 1 << 20)
    assert store.delete_device_data(device["device_id"]) == 1
    assert store.device(device["device_id"]) is not None


def test_prune_drops_only_old_events(store):
    device = store.create_device("c", "windows", {})
    store.insert_events(device["device_id"], "b", "x", "L3", [event("old"), event("new", 1)],
                        ("comp.event/v1",), 1 << 20)
    conn = store.connect()
    conn.execute("UPDATE events SET received_at='2000-01-01T00:00:00Z' WHERE event_id='old'")
    conn.commit()
    assert store.prune_events(30) == 1
    assert store.device_stats(device["device_id"])["event_count"] == 1


# --- recharge ---------------------------------------------------------------------------------
def test_redeem_is_idempotent_and_binds_the_cdk_to_one_account(store):
    store.add_cdk("ABCD-EFGH", "Plus 1 个月")
    status, first = store.redeem("abcd-efgh", "a@example.com")      # case and spacing are normalised
    assert status == 201 and first["replayed"] is False
    status, again = store.redeem("ABCD-EFGH", "a@example.com")
    assert status == 200 and again["replayed"] is True
    assert again["redemption_id"] == first["redemption_id"]
    status, _ = store.redeem("ABCD-EFGH", "b@example.com")
    assert status == 409
    status, _ = store.redeem("NOPE", "a@example.com")
    assert status == 400


def test_expired_cdk_is_rejected_without_consuming_it(store):
    store.add_cdk("OLD-CODE", "Plus", expires_at="2000-01-01T00:00:00Z")
    status, body = store.redeem("OLD-CODE", "a@example.com")
    assert status == 410 and body["error"]["code"] == "cdk_expired"
    row = store.connect().execute("SELECT used_by FROM cdk").fetchone()
    assert row["used_by"] is None


# --- rate limiter -----------------------------------------------------------------------------
def test_rate_limiter_is_per_key_and_windowed():
    limiter = _RateLimiter(2)
    assert limiter.allow("a", now=1000.0) and limiter.allow("a", now=1000.5)
    assert not limiter.allow("a", now=1001.0)
    assert limiter.allow("b", now=1001.0), "another device has its own budget"
    assert limiter.allow("a", now=1100.0), "a minute later the window is empty again"


# --- cli --------------------------------------------------------------------------------------
def test_cli_initdb_add_cdk_and_stats(tmp_path, capsys):
    db = tmp_path / "cli.sqlite3"
    assert cli.main(["--db", str(db), "initdb"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["terms_version"] == terms.TERMS_VERSION and out["terms_hash"] == terms.TERMS_HASH

    assert cli.main(["--db", str(db), "add-cdk", "CLI-0001", "CLI-0002", "--product", "Plus"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(db), "stats"]) == 0
    assert json.loads(capsys.readouterr().out)["devices"] == 0

    store = Store(db)
    assert store.redeem("CLI-0001", "x@example.com")[0] == 201
    store.close()


def test_cli_devices_and_events_do_not_print_bodies(tmp_path, capsys):
    db = tmp_path / "cli2.sqlite3"
    store = Store(db)
    device = store.create_device("c", "windows", {})
    store.insert_events(device["device_id"], "b", "x", "L3", [event("e1")], ("comp.event/v1",), 1 << 20)
    store.close()

    assert cli.main(["--db", str(db), "devices"]) == 0
    listed = capsys.readouterr().out
    assert device["device_id"] in listed and "events=1" in listed.replace(" ", "")

    assert cli.main(["--db", str(db), "events"]) == 0
    plain = capsys.readouterr().out
    assert "user_prompt" in plain and '"text"' not in plain, "the default listing never shows content"

    assert cli.main(["--db", str(db), "events", "--raw"]) == 0
    assert '"text":"hi"' in capsys.readouterr().out


# --- live server plumbing ---------------------------------------------------------------------
def test_cli_prune_refuses_to_wipe_everything(tmp_path, capsys):
    db = tmp_path / "prune.sqlite3"
    store = Store(db)
    device = store.create_device("c", "windows", {})
    store.insert_events(device["device_id"], "b", "x", "L3", [event("e1")], ("comp.event/v1",), 1 << 20)
    store.close()
    assert cli.main(["--db", str(db), "prune", "--days", "0"]) == 0
    assert json.loads(capsys.readouterr().out)["days"] == 1


def test_cli_serve_keeps_environment_settings(tmp_path, monkeypatch):
    """`serve` must not reset host/port/admin token that a deployment set in the environment."""
    monkeypatch.setenv("CTRSRV_HOST", "127.0.0.1")
    monkeypatch.setenv("CTRSRV_PORT", "18443")
    monkeypatch.setenv("CTRSRV_ADMIN_TOKEN", "from-env")
    monkeypatch.setenv("CTRSRV_DB", str(tmp_path / "from-env.sqlite3"))
    captured: dict = {}

    import ctrsrv.cli as cli_mod

    def fake_serve(config):
        captured["config"] = config

    monkeypatch.setattr(cli_mod, "serve", fake_serve)
    assert cli_mod.main(["--db", str(tmp_path / "x.sqlite3"), "serve"]) == 0
    assert captured["config"].port == 18443
    assert captured["config"].admin_token == "from-env"
    assert captured["config"].db == tmp_path / "x.sqlite3", "an explicit --db wins"

    assert cli_mod.main(["serve"]) == 0
    assert captured["config"].db == tmp_path / "from-env.sqlite3", "CTRSRV_DB is honoured"
    assert captured["config"].port == 18443

    assert cli_mod.main(["--db", str(tmp_path / "x.sqlite3"), "serve", "--port", "19001"]) == 0
    assert captured["config"].port == 19001, "an explicit flag still wins"


def test_server_binds_an_ephemeral_port_and_reports_version(tmp_path):
    server = Server(Config(db=tmp_path / "s.sqlite3", host="127.0.0.1", port=0, admin_token="t"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.request

        port = server.server_address[1]
        assert port > 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=5) as resp:
            body = json.loads(resp.read())
        assert body["ok"] is True and body["terms_version"] == terms.TERMS_VERSION
    finally:
        server.shutdown()
        server.server_close()
        server.api.store.close()


def test_view_ui_pages(tmp_path):
    """`/admin/ui` renders devices, one device, one session and one raw record."""
    import urllib.error
    import urllib.request

    server = Server(Config(db=tmp_path / "ui.sqlite3", host="127.0.0.1", port=0, admin_token="adm"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    store = server.api.store
    device = store.create_device("c", "windows", {})
    store.insert_events(device["device_id"], "b", "x", "L3",
                        [event("e1#L0", 0, "s1", "L0"), event("e1#L3", 0, "s1", "L3")],
                        ("comp.event/v1",), 1 << 20)

    def get(path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8"), dict(exc.headers)

    try:
        status, _, _ = get("/admin/ui")
        assert status == 401, "no credential, no page"

        status, page, headers = get("/admin/ui?token=adm")
        assert status == 200 and "text/html" in headers["Content-Type"]
        assert device["device_id"] in page and "采集后台" in page
        assert "ctrsrv_admin=adm" in headers.get("Set-Cookie", "")

        status, page, _ = get(f"/admin/ui/devices/{device['device_id']}", {"Cookie": "ctrsrv_admin=adm"})
        assert status == 200 and "s1" in page and "会话" in page

        status, page, _ = get(f"/admin/ui/devices/{device['device_id']}/sessions/s1",
                              {"Cookie": "ctrsrv_admin=adm"})
        assert status == 200 and "user_prompt" in page and "hi" in page
        assert "events/e1%23L3" in page or "events/e1%23L0" in page, "event ids are URL-encoded"

        status, page, _ = get(f"/admin/ui/devices/{device['device_id']}/events/e1%23L3",
                              {"Cookie": "ctrsrv_admin=adm"})
        assert status == 200 and "<pre>" in page and "comp.event/v1" in page

        status, _, _ = get(f"/admin/ui/devices/nope", {"Cookie": "ctrsrv_admin=adm"})
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()
        server.api.store.close()


def test_view_cli_prints_a_transcript(tmp_path, capsys):
    db = tmp_path / "view.sqlite3"
    store = Store(db)
    device = store.create_device("c", "windows", {})
    store.insert_events(device["device_id"], "b", "x", "L3",
                        [event("p1", 0, "s9", "L3"), event("a1", 1, "s9", "L3")],
                        ("comp.event/v1",), 1 << 20)
    store.close()
    assert cli.main(["--db", str(db), "view", "--latest"]) == 0
    out = capsys.readouterr().out
    assert "s9" in out and "user_prompt" in out and "hi" in out
    assert cli.main(["--db", str(db), "view", "--device", device["device_id"]]) == 0
    assert "s9" in capsys.readouterr().out


def test_http_guards(tmp_path):
    """Chunked bodies, bad query parameters, and deleting data after a stop."""
    import http.client
    import urllib.error
    import urllib.request

    server = Server(Config(db=tmp_path / "g.sqlite3", host="127.0.0.1", port=0, admin_token="adm"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    def call(method, path, body=None, bearer=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        head = {"Content-Type": "application/json", **(headers or {})}
        if bearer:
            head["Authorization"] = "Bearer " + bearer
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method,
                                     headers=head)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    try:
        # a chunked body is refused instead of silently becoming an empty request
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.putrequest("POST", "/v1/devices")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        conn.send(b"0\r\n\r\n")
        status = conn.getresponse().status
        conn.close()
        assert status == 411

        status, body = call("POST", "/v1/devices", {"client": "c", "platform": "windows"})
        assert status == 201
        token = body["device_token"]

        status, body = call("GET", "/admin/devices?limit=abc", bearer="adm")
        assert status == 400 and body["error"]["code"] == "invalid_query"

        status, _ = call("POST", "/v1/service-state/terminate", {}, bearer=token)
        assert status == 200
        # a stopped device may still delete what it already sent (data-subject right)
        status, body = call("DELETE", "/v1/devices/me/data", bearer=token)
        assert status == 200 and body["deleted_events"] == 0
        # ... but nothing else
        status, _ = call("GET", "/v1/devices/me", bearer=token)
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()
        server.api.store.close()
