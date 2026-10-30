"""Collector tests: tiers, spool, contract, reader, IPC and one end-to-end round.

Run from the repository root:

    set PYTHONPATH=plugins\\codex-companion\\lib;plugins\\codex-task-recorder\\lib;tools\\session-reader;tests
    python -m pytest tests\\test_collector.py -q
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from collector import config as ccfg
from collector import contract, ipc, tiers
from collector.paths import CollectorPaths
from collector.reader import Reader
from collector.run import Collector
from collector.spool import Spool, TIERS
from collector.tasks import build_xml
from native_rollout import Rollout, name_for


@pytest.fixture(autouse=True)
def unique_pipe(monkeypatch, tmp_path):
    """Never touch the production pipe: a collector from another run (or a real install) may own it."""
    if os.name == "nt":
        monkeypatch.setenv("COMPANION_PIPE", rf"\\.\pipe\codex-companion-test-{os.getpid()}-{tmp_path.name}")
    else:
        monkeypatch.setenv("COMPANION_PIPE", str(tmp_path / "test.sock"))


# --- fixtures ---------------------------------------------------------------------------------
def make_event(kind: str, text: str | None = None, **extra) -> dict:
    event = {
        "schema": "csr.event/v1",
        "event_key": f"sid-1:3:0",
        "session_id": "sid-1",
        "turn_id": "turn-1",
        "turn_id_inferred": False,
        "ordinal": 3,
        "ts": "2026-11-01T10:00:00.000Z",
        "kind": kind,
        "model": "gpt-5-codex",
        "item_id": "item-1",
        "call_id": None,
        "data": {"text": text} if text is not None else {"exit_code": 0, "command": "pytest -q"},
        "missing": [],
        "source": {"file": "rollout-1.jsonl", "offset": 0, "record": "event_msg/item_completed", "codex_version": "0.163.0"},
        "redactions": {"api_key": 1},
        "truncated": [],
    }
    event.update(extra)
    return event


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    path = tmp_path / "companion"
    path.mkdir()
    return path


@pytest.fixture()
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codexhome"
    (home / "sessions" / "2026" / "11" / "01").mkdir(parents=True)
    return home


class FakeApi:
    """Records what would have been sent; never touches the network."""

    def __init__(self, base_url: str = "http://127.0.0.1:18788"):
        self.base_url = base_url
        self.batches: list[dict] = []
        self.heartbeats: list[dict] = []
        self.terminated = False

    def register(self, consent_version, consent_accepted_at):
        return {"device_id": "dev-1", "device_token": "ctrd_test_token"}

    def upload(self, token, batch):
        self.batches.append(batch)
        return {"results": [{"event_id": e["event_id"], "status": "accepted"} for e in batch["events"]]}

    def service_state(self, token):
        return {"state": "active", "terms_version": 3, "terms_hash": contract.TERMS_HASH,
                "effective_at": "2026-11-01T00:00:00Z", "grace_hours": 72, "terminated_at": None}

    def heartbeat(self, token, body):
        self.heartbeats.append(body)
        return {"ok": True}

    def terminate_service(self, token):
        self.terminated = True
        return {"state": "terminated"}

    def uninstall(self, token):
        return {"ok": True}


# --- tiers ------------------------------------------------------------------------------------
def test_l0_never_carries_text():
    rec = tiers.l0_record(make_event("user_prompt", "秘密的提示词"))
    assert rec["tier"] == "L0"
    assert "data" not in rec
    assert "秘密的提示词" not in json.dumps(rec, ensure_ascii=False)
    assert rec["meta"]["text_chars"] == len("秘密的提示词")
    assert rec["meta"]["redactions"] == {"api_key": 1}


def test_content_event_yields_l0_and_l2():
    records = tiers.records_for(make_event("user_prompt", "refactor the auth module"),
                                tiers=("L0", "L2"))
    by_tier = {r["tier"]: r for r in records}
    assert set(by_tier) == {"L0", "L2"}
    assert by_tier["L2"]["data"]["text"] == "refactor the auth module"
    assert by_tier["L2"]["event_id"].endswith("#L2")


def test_full_session_tier_carries_every_field():
    event = make_event("user_prompt", "refactor the auth module")
    records = tiers.records_for(event, tiers=("L0", "L3"))
    by_tier = {r["tier"]: r for r in records}
    assert set(by_tier) == {"L0", "L3"}
    rec = by_tier["L3"]
    assert rec["data"]["text"] == "refactor the auth module"
    assert rec["raw"]["redactions"] == {"api_key": 1}
    assert rec["raw"]["source"]["codex_version"] == "0.163.0"
    assert rec["raw"]["schema"] == "csr.event/v1"


def test_l3_supersedes_l2_instead_of_duplicating_it():
    records = tiers.records_for(make_event("assistant_message", "reply"),
                                tiers=("L0", "L1", "L2", "L3"))
    assert [r["tier"] for r in records] == ["L0", "L3"]


def test_tool_output_is_included_at_l3():
    event = make_event("tool_execution", text=None, data={"command": "pytest -q", "output": "12 passed"})
    records = tiers.records_for(event, tiers=("L0", "L1", "L3"))
    by_tier = {r["tier"]: r for r in records}
    assert by_tier["L1"]["data"]["command"] == "pytest -q"
    assert by_tier["L3"]["data"]["output"] == "12 passed"


def test_session_summary_counts_without_text():
    events = [make_event("user_prompt", "a"), make_event("assistant_message", "b"),
              make_event("tool_execution", text=None, data={"exit_code": 0})]
    summary = tiers.session_summary("sid-1", events)
    assert summary["schema"] == "comp.session/v1"
    assert summary["event_id"] == "summary:sid-1:3"
    assert summary["meta"]["events"] == 3
    assert summary["meta"]["tool_calls"] == 1
    assert summary["meta"]["models"] == ["gpt-5-codex"]
    assert "data" not in summary  # telemetry never echoes content
    assert summary["meta"]["kinds"] == {"user_prompt": 1, "assistant_message": 1,
                                        "tool_execution": 1}


def test_tool_event_yields_structure_not_text():
    records = tiers.records_for(make_event("tool_execution", text=None), tiers=("L0", "L1"))
    by_tier = {r["tier"]: r for r in records}
    assert set(by_tier) == {"L0", "L1"}
    assert by_tier["L1"]["data"]["command"] == "pytest -q"


def test_structure_mode_fingerprints_long_text():
    long_text = "line one\nline two\nline three"
    rec = tiers.l2_record(make_event("assistant_message", long_text), code_mode="structure")
    assert rec["data"]["text"]["__str__"]["lines"] == 3
    assert long_text not in json.dumps(rec, ensure_ascii=False)


def test_sample_ratio_zero_drops_content_but_keeps_metadata():
    records = tiers.records_for(make_event("user_prompt", "hello"), sample_ratio=0.0)
    assert [r["tier"] for r in records] == ["L0"]


def test_sample_ratio_is_stable_per_session():
    event = make_event("user_prompt", "hello", session_id="stable-session")
    first = tiers.records_for(event, sample_ratio=0.5)
    second = tiers.records_for(event, sample_ratio=0.5)
    assert [r["tier"] for r in first] == [r["tier"] for r in second]


def test_tiers_can_be_disabled():
    records = tiers.records_for(make_event("user_prompt", "hello"), tiers=("L0",))
    assert [r["tier"] for r in records] == ["L0"]


# --- spool ------------------------------------------------------------------------------------
def test_spool_roundtrip_and_commit(root):
    spool = Spool(root / "spool")
    spool.append_many([tiers.l0_record(make_event("user_prompt", f"p{i}")) for i in range(5)])
    batch = spool.next_batch(limit_events=2)
    assert batch.tier == "L0" and batch.events == 2
    spool.commit(batch)
    assert spool.depth()["L0"] == 3
    batch = spool.next_batch(limit_events=10)
    spool.commit(batch)
    assert spool.depth()["L0"] == 0
    assert spool.next_batch() is None


def test_spool_keeps_tiers_apart_and_orders_l0_first(root):
    spool = Spool(root / "spool")
    spool.append(tiers.l2_record(make_event("assistant_message", "reply")))
    spool.append(tiers.l0_record(make_event("user_prompt", "prompt")))
    first = spool.next_batch()
    assert first.tier == "L0"  # metadata is never queued behind content
    spool.commit(first)
    assert spool.next_batch().tier == "L2"


def test_prune_drops_oldest_segment_when_over_budget(root, monkeypatch):
    from collector import spool as spool_mod
    monkeypatch.setitem(spool_mod.CAPS, "L0", 1)  # any segment is over budget
    spool = Spool(root / "spool")
    spool.append(tiers.l0_record(make_event("user_prompt", "a")))
    (path,) = spool.segments("L0")
    path.write_text("x" * 2048, encoding="utf-8")
    removed = spool.prune()
    assert removed["L0"] == 1
    assert spool.depth()["L0"] == 0


# --- contract ---------------------------------------------------------------------------------
def test_contract_is_idempotent_and_hashes_the_text(root):
    first = contract.ensure_contract(root, product_version="1.1.0")
    second = contract.ensure_contract(root)
    assert first["effective_at"] == second["effective_at"]
    assert second["terms_hash"] == contract.TERMS_HASH
    assert second["terms_version"] == contract.TERMS_VERSION
    assert second["product_version"] == "1.1.0"


def test_may_record_requires_a_service_state_or_grace(root):
    contract.ensure_contract(root)
    allowed, why = contract.may_record(root)
    assert (allowed, why) == (False, "no_service_state")
    contract.save_service_state(root, {"state": "active", "grace_hours": 72})
    assert contract.may_record(root) == (True, "active")


def test_grace_window_expires(root):
    contract.ensure_contract(root)
    contract.save_service_state(root, {"state": "unknown", "grace_hours": 1})
    doc = json.loads((root / "service-state.json").read_text(encoding="utf-8"))
    doc["fetched_ts"] = time.time() - 7200
    (root / "service-state.json").write_text(json.dumps(doc), encoding="utf-8")
    assert contract.may_record(root) == (False, "no_service_state")


def test_terminate_wins_over_everything(root):
    contract.ensure_contract(root)
    contract.save_service_state(root, {"state": "active", "grace_hours": 72})
    contract.terminate(root, "user")
    assert contract.may_record(root) == (False, "contract_terminated")


def test_reinstall_after_uninstall_resumes_recording(root):
    """Uninstall ends the service; installing again must start a new contract."""
    contract.ensure_contract(root)
    contract.save_service_state(root, {"state": "active", "grace_hours": 72})
    contract.terminate(root, "uninstall")
    assert contract.may_record(root) == (False, "contract_terminated")
    doc = contract.ensure_contract(root, product_version="1.1.0")
    assert doc["terminated_at"] is None and doc["resumed_at"]
    assert contract.may_record(root) == (True, "active")


def test_a_user_stop_survives_a_reinstall_until_explicitly_resumed(root):
    contract.ensure_contract(root)
    contract.save_service_state(root, {"state": "active", "grace_hours": 72})
    contract.terminate(root, "user")
    contract.ensure_contract(root)  # a repair must not silently undo the user's stop
    assert contract.may_record(root) == (False, "contract_terminated")
    assert contract.resume(root) == (True, "resumed")
    assert contract.may_record(root) == (True, "active")


def test_a_backend_stop_cannot_be_resumed_by_the_client(root):
    contract.ensure_contract(root)
    contract.save_service_state(root, {"state": "terminated", "grace_hours": 72})
    contract.terminate(root, "server")
    assert contract.resume(root) == (False, "service_terminated")
    assert contract.may_record(root) == (False, "contract_terminated")


# --- reader -----------------------------------------------------------------------------------
def write_rollout(home: Path, sid: str) -> Path:
    rollout = Rollout(sid, datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc))
    rollout.meta().turn("turn-1", "please refactor auth", "done: refactored", command="pytest -q")
    path = home / "sessions" / "2026" / "11" / "01" / name_for(sid)
    path.write_text(rollout.text(), encoding="utf-8")
    return path


def test_reader_scans_rollout_files_without_the_plugin(root, codex_home):
    write_rollout(codex_home, "11111111-2222-3333-4444-555555555555")
    with Reader(root / "reader.sqlite3", home=codex_home) as reader:
        result = reader.scan()
        assert result["present"] and result["files"] == 1 and result["new_events"] > 0
        events = reader.take(500)
        assert any(e["kind"] == "user_prompt" for e in events)
        assert any(e["kind"] == "assistant_message" for e in events)
        assert reader.take(500) == []          # handed off exactly once
        assert reader.unread() == 0


def test_reader_is_idempotent_across_rescans(root, codex_home):
    write_rollout(codex_home, "11111111-2222-3333-4444-555555555555")
    with Reader(root / "reader.sqlite3", home=codex_home) as reader:
        reader.scan()
        first = len(reader.take(500))
    with Reader(root / "reader.sqlite3", home=codex_home) as reader:
        reader.scan()
        assert reader.take(500) == []
        assert first > 0


# --- end to end -------------------------------------------------------------------------------
def build_collector(root: Path, codex_home: Path, api: FakeApi) -> Collector:
    contract.ensure_contract(root, product_version="1.1.0")
    contract.save_service_state(root, {"state": "active", "grace_hours": 72})
    cfg = ccfg.load(root)
    cfg.base_url = api.base_url
    ccfg.save(root, cfg)
    return Collector(root, api_factory=lambda url, timeout: api, home=codex_home)


def test_one_round_collects_and_uploads(root, codex_home):
    write_rollout(codex_home, "11111111-2222-3333-4444-555555555555")
    api = FakeApi()
    collector = build_collector(root, codex_home, api)
    collector.control.stop("test")  # `collect` and `upload` are called directly, not the loop
    assert collector.collect(0.0) > 0
    while collector.upload(0.0) == "progress":
        pass
    sent = [e for batch in api.batches for e in batch["events"]]
    assert sent, "nothing was uploaded"
    assert {e["tier"] for e in sent} >= {"L0", "L3"}
    assert any(e["schema"] == "comp.session/v1" for e in sent)  # telemetry rides along
    assert all(b["schema"] == "companion.sync.batch/v1" for b in api.batches)
    assert all(b["service"]["terms_version"] == contract.TERMS_VERSION for b in api.batches)
    assert collector.spool.depth()["L3"] == 0
    collector.reader.close()


def test_paused_collector_uploads_nothing(root, codex_home):
    write_rollout(codex_home, "11111111-2222-3333-4444-555555555555")
    api = FakeApi()
    collector = build_collector(root, codex_home, api)
    collector.control.stop("test")
    collector.cfg = ccfg.set_paused(root, True)
    collector.tick()
    assert api.batches == []
    collector.reader.close()


class RevokedApi(FakeApi):
    """A backend that has already forgotten this device."""

    def __init__(self):
        super().__init__()
        self.registrations = 0

    def register(self, consent_version, consent_accepted_at):
        self.registrations += 1
        return super().register(consent_version, consent_accepted_at)

    def upload(self, token, batch):
        from ctr.sync.api import HttpError
        raise HttpError(403, "device_revoked", None)


def test_device_revoked_keeps_local_data_and_the_token(root, codex_home):
    write_rollout(codex_home, "11111111-2222-3333-4444-555555555555")
    api = RevokedApi()
    collector = build_collector(root, codex_home, api)
    collector.control.stop("test")
    collector.collect(0.0)
    before = sum(collector.spool.depth().values())
    assert collector.upload(0.0) == "device_revoked"
    assert sum(collector.spool.depth().values()) == before, "a revoked device must not lose data"
    # no re-registration: the token stays, so the device stays gone until the backend says otherwise
    assert collector.upload(0.0) == "device_revoked"
    assert api.registrations <= 1
    collector.reader.close()


def test_terminated_contract_stops_the_loop(root, codex_home):
    api = FakeApi()
    collector = build_collector(root, codex_home, api)
    contract.terminate(root, "user")
    assert collector.run(once=True) == "terminated"
    assert api.batches == []
    collector.reader.close()


def test_ipc_roundtrip_reaches_the_collector(root):
    contract.ensure_contract(root)
    collector = Collector(root, api_factory=lambda url, timeout: FakeApi())
    assert collector.start_ipc()
    token = ipc.read_token(CollectorPaths(root).token)
    try:
        reply = ipc.call({"op": "status"}, token=token, root=root)
        assert reply["ok"] and reply["contract"]["terms_version"] == contract.TERMS_VERSION
        assert ipc.call({"op": "pause"}, token=token, root=root)["paused"] is True
        assert ipc.call({"op": "resume"}, token=token, root=root)["paused"] is False
        assert ipc.call({"op": "nope"}, token=token, root=root)["error"] == "unknown_op"
    finally:
        collector.server.stop()
        collector.reader.close()


def test_ipc_rejects_a_wrong_token(root):
    collector = Collector(root, api_factory=lambda url, timeout: FakeApi())
    collector.start_ipc()
    try:
        with pytest.raises(ipc.IpcError):
            ipc.call({"op": "status"}, token=b"wrong-token", root=root, timeout=1.0)
    finally:
        collector.server.stop()
        collector.reader.close()


# --- scheduled task ---------------------------------------------------------------------------
def test_task_xml_points_at_the_launcher_and_runs_at_logon(root, tmp_path):
    (root / "collector-config.json").write_text(
        json.dumps({"install_dir": str(tmp_path / "Program Files" / "CodexCompanion")}), encoding="utf-8")
    xml = build_xml(root, user="PC\\tester")
    assert "<LogonTrigger>" in xml
    assert "collector.cmd" in xml
    assert "LeastPrivilege" in xml
    assert "PC\\tester" in xml
    assert "Program Files" in xml  # a space in the path must survive quoting


def test_mock_backend_terms_match_the_client():
    """The dev backend must advertise the same terms the client shows and hashes."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts" / "mock_sync_server.py"
    spec = importlib.util.spec_from_file_location("mock_sync_server_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.TERMS_VERSION == contract.TERMS_VERSION
    assert module.TERMS_HASH == contract.TERMS_HASH
    assert module.GRACE_HOURS == contract.GRACE_HOURS
    # the backend must accept the collector's own schemas, or every upload is rejected
    assert {"comp.event/v1", "comp.session/v1"} <= set(module.EVENT_SCHEMAS)
