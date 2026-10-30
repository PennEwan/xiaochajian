"""Shared fixtures for the sync tests: an in-process mock sync backend and recorded sessions."""
import json
import sys
import threading
import urllib.request
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest import mock

from ctr import state
from ctr.sync import config as sconfig
from ctr.sync import state as sstate
from ctr.sync.config import SyncPaths

from helpers import T0, TempPathsCase, write_rollout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import mock_sync_server  # noqa: E402

SID = "01a1634e-e806-7252-8f31-c4c0d9bf63c2"
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class MockSync:
    def __init__(self, store: Path | None = None, port: int = 0):
        self.server = mock_sync_server.make_server("127.0.0.1", port, store)
        self.state = self.server.state
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self._thread.start()

    def call(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with _DIRECT.open(req, timeout=5) as resp:
            return json.loads(resp.read() or b"{}")

    def fail_next(self, **rule):
        return self.call("POST", "/_mock/fail-next", rule)

    def events(self):
        return self.call("GET", "/_mock/events")["events"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class SyncCase(TempPathsCase):
    """Recording on since T0-1h; a rollout file; helpers to run hooks and enable sync."""

    def setUp(self):
        super().setUp()
        self.sp = SyncPaths(self.paths)
        patcher = mock.patch("ctr.sync.launcher.spawn")  # never start real uploader processes
        self.spawn = patcher.start()
        self.addCleanup(patcher.stop)
        self.enable(at=-3600)
        self.rollout = self.tmp / "rollout.jsonl"
        write_rollout(self.rollout, [])

    def hook(self, event, sid=SID, transcript=True, **kw):
        p = {"hook_event_name": event, "session_id": sid, "cwd": r"D:\ctr-testbed", "model": "gpt-a",
             "permission_mode": "default", "transcript_path": str(self.rollout) if transcript else None}
        if event not in ("SessionStart", "SessionEnd"):
            p["turn_id"] = "t1"
        p.update(kw)
        return self.run_hook({k: v for k, v in p.items() if v is not None or k == "transcript_path"})

    def sync_on(self, at=-60, base_url="http://127.0.0.1:9"):
        sconfig.save(self.sp, sconfig.Config(base_url=base_url, timeout=5))
        return sstate.enable(self.sp, T0 + timedelta(seconds=at), base_url)

    def sync_pause(self, at=0):
        return sstate.pause(self.sp, T0 + timedelta(seconds=at))

    def recording_off(self, at=0):
        return state.disable(self.paths.state, T0 + timedelta(seconds=at))

    @contextmanager
    def clock(self, seconds):
        """Freeze the recorder clock (recorded_at) at T0 + seconds."""
        with mock.patch("ctr.timeutil.now", return_value=T0 + timedelta(seconds=seconds)):
            yield

    def session_file(self, sid=SID):
        return next(self.paths.sessions.glob(f"*/{sid}.jsonl"))

    def append_raw(self, lines, sid=SID):
        with open(self.session_file(sid), "a", encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write(line if isinstance(line, str) else json.dumps(line) + "\n")
