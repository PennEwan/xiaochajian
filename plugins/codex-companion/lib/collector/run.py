"""The collector process: scan, classify, spool, upload, forever.

Started by a login-triggered scheduled task, not by the plugin and not by Codex. Its only inputs
are the rollout files Codex writes and the backend's copy of the service contract; its only
outputs are the spool and the status file. The plugin talks to it over the local pipe (`ipc.py`)
and can pause, resume, stop and read status — it cannot make the collector do anything else.

Stopping is real: `stop` writes `terminated_at` to the contract, tells the backend, and the
process exits. Nothing restarts it except the user running the task or reinstalling.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from . import __version__, config, contract, ipc, tiers
from .paths import CollectorPaths
from .reader import CsrMissing, Reader
from .spool import Spool
from .tasks import pythonw
from .uploader import DEVICE_REVOKED, FAILED, IDLE, NOT_CONFIGURED, PROGRESS, Uploader

POLL_S = 1.0
STATUS_EVERY_S = 10.0
READER_BATCH = 400


class Control:
    """What the plugin is allowed to change, and nothing else."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.stop_reason: str | None = None

    def stop(self, reason: str) -> None:
        with self.lock:
            if not self.stop_reason:
                self.stop_reason = reason
        self.stopped.set()


class Collector:
    def __init__(self, root: Path, *, api_factory=None, environ=None, home: Path | None = None):
        self.root = Path(root)
        self.paths = CollectorPaths(self.root)
        self.paths.ensure()
        self.cfg = config.load(self.root)
        self.spool = Spool(self.paths.spool)
        self.reader = Reader(self.paths.db, home=home)
        self.uploader = Uploader(self.paths, self.spool, self.cfg, api_factory=api_factory)
        self.control = Control()
        self.started_at = _now()
        self.server: ipc.Server | None = None
        # Cached reader numbers: the IPC thread must never touch the SQLite connection, which
        # was opened on the main thread and belongs to it.
        self.counts: dict = {"events": 0, "sessions": 0}
        self.unread = 0
        self.last_scan = float("-inf")
        self.last_upload = float("-inf")
        self.last_heartbeat = 0.0
        self.last_state_pull = float("-inf")
        self.last_status = 0.0
        self.scan_result: dict = {}
        self.gate = "unknown"
        self.ticks = 0

    # --- status -----------------------------------------------------------------------------
    def status(self) -> dict:
        contract_doc = contract.load_contract(self.root)
        depth = self.spool.depth()
        return {
            "ok": True,
            "collector_running": True,
            "version": __version__,
            "pid": os.getpid(),
            "started_at": self.started_at,
            "paused": bool(self.cfg.paused),
            "recording": self.gate in ("active", "grace") and not self.cfg.paused,
            "gate": self.gate,
            "service_state": contract.state_of(self.root),
            "contract": {k: contract_doc.get(k) for k in
                         ("terms_version", "terms_hash", "effective_at", "first_run_at", "terminated_at")},
            "reader": {**self.counts, "unread": self.unread, **(self.scan_result or {})},
            "spool": {"depth": depth, "records": sum(depth.values()),
                      "bytes": self.spool.size(), "dropped": self.spool.dropped},
            "upload": {"configured": bool(self.cfg.base_url), "base_url": self.cfg.base_url,
                       "uploaded": self.uploader.uploaded, "failures": self.uploader.failures,
                       "last_error": self.uploader.last_error,
                       "last_success_at": _iso(self.uploader.last_success_at)},
            "tiers": list(self.cfg.tiers),
            "sample_ratio": self.cfg.sample_ratio,
            "code_mode": self.cfg.code_mode,
        }

    def _unread(self) -> int:
        try:
            return self.reader.unread()
        except Exception:
            return -1

    def _refresh_counts(self) -> None:
        """Reader numbers, refreshed on the main thread only."""
        try:
            self.counts = self.reader.counts()
            self.unread = self.reader.unread()
        except Exception:
            pass

    def _write_status(self, extra: dict | None = None) -> None:
        doc = self.status()
        if extra:
            doc.update(extra)
        doc["updated_at"] = _now()
        tmp = self.paths.status.with_suffix(".tmp")
        try:
            self.paths.status.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.paths.status)
        except OSError:
            pass

    def log(self, kind: str, **fields) -> None:
        """Operation kinds and numbers only — never content, never a token."""
        doc = {"at": _now(), "event": kind, "v": __version__}
        doc.update({k: v for k, v in fields.items() if isinstance(v, (int, float, str, bool)) or v is None})
        try:
            with open(self.paths.log, "a", encoding="utf-8") as f:
                f.write(json.dumps(doc, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # --- IPC --------------------------------------------------------------------------------
    def handle(self, request: dict) -> dict:
        op = request.get("op")
        if op == "ping":
            return {"ok": True, "pid": os.getpid(), "version": __version__}
        if op == "status":
            return self.status()
        if op == "pause":
            self.cfg = config.set_paused(self.root, True)
            self.log("pause")
            return self.status()
        if op == "resume":
            ok, why = contract.resume(self.root)
            if not ok:  # the backend ended this service: do not talk ourselves back on
                self.log("resume", refused=why)
                reply = self.status()
                reply.update({"ok": False, "error": why})
                return reply
            self.cfg = config.set_paused(self.root, False)
            self.gate = "active" if contract.state_of(self.root) == "active" else "grace"
            self.log("resume", detail=why)
            return self.status()
        if op == "stop":
            self.cfg = config.set_paused(self.root, True)
            contract.terminate(self.root, "user")
            self.uploader.terminate_service()
            self.control.stop("user")
            self.gate = "contract_terminated"  # report the verdict, not the one from the last tick
            self.log("stop", reason="user")
            return self.status()
        if op == "open_panel":
            return {"ok": self._open_panel("companion", ["panel"])}
        if op == "open_recharge":
            return {"ok": self._open_panel("cdkr.window", [])}
        return {"ok": False, "error": "unknown_op"}

    def _open_panel(self, module: str = "companion", args: list[str] | None = None) -> bool:
        import subprocess
        lib = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(p for p in (str(lib), env.get("PYTHONPATH")) if p)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("COMPANION_DATA_DIR", str(self.root))
        try:
            subprocess.Popen([pythonw(), "-m", module, *(args or [])], env=env, close_fds=True,
                             cwd=tempfile.gettempdir(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except OSError:
            return False

    def start_ipc(self) -> bool:
        token = ipc.ensure_token(self.paths.token)
        self.server = ipc.Server(self.handle, token, self.root)
        ok = self.server.start()
        self.log("ipc", ok=ok, error=self.server.error)
        return ok

    # --- one round --------------------------------------------------------------------------
    def refresh_service_state(self, now: float) -> None:
        if now - self.last_state_pull < self.cfg.service_state_s:
            return
        self.last_state_pull = now
        state = self.uploader.service_state()
        if isinstance(state, dict) and state.get("state") in ("active", "terminated"):
            contract.save_service_state(self.root, state)
            if state.get("state") == "terminated":
                contract.terminate(self.root, "server")
                self.control.stop("server")
                self.log("terminate", by="server")

    def collect(self, now: float) -> int:
        if now - self.last_scan < self.cfg.scan_interval_s:
            return 0
        self.last_scan = now
        try:
            self.scan_result = self.reader.scan()
        except CsrMissing as exc:
            self.control.stop("csr_missing")
            self.log("csr_missing", detail=str(exc)[:120])
            return 0
        except Exception as exc:  # a broken read must not stop the process
            self.log("scan_error", detail=type(exc).__name__)
            return 0
        written = 0
        for _ in range(4):  # bounded work per tick
            events = self.reader.take(READER_BATCH)
            if not events:
                break
            records = []
            for event in events:
                records.extend(tiers.records_for(event, tiers=self.cfg.tiers,
                                                 sample_ratio=self.cfg.sample_ratio,
                                                 code_mode=self.cfg.code_mode))
            records.extend(_summaries(events))
            self.spool.append_many(records)
            written += len(records)
            if len(events) < READER_BATCH:
                break
        if written:
            self.log("collect", records=written)
        self._refresh_counts()
        return written

    def upload(self, now: float) -> str:
        if self.cfg.paused:
            return "paused"
        backlog = sum(self.spool.depth().values()) > 0
        # The interval is a poll rate, not a rate limit: a backlog is drained as fast as the
        # backend answers, so a device that was offline for a week catches up in one sitting.
        if not backlog and now - self.last_upload < self.cfg.upload_interval_s and self.uploader.failures == 0:
            return IDLE
        result = self.uploader.push(contract.load_contract(self.root), tiers=self.cfg.tiers)
        self.last_upload = now
        if result == FAILED:
            self.last_upload = now + self.uploader.delay - self.cfg.upload_interval_s
        elif result == PROGRESS:
            self.log("upload", n=self.uploader.uploaded)
        return result

    def heartbeat(self, now: float) -> None:
        if now - self.last_heartbeat < self.cfg.heartbeat_s:
            return
        self.last_heartbeat = now
        depth = self.spool.depth()
        st = self.status()
        self.uploader.heartbeat({
            "client": st.get("version") and f"codex-companion-collector/{st['version']}",
            "queue_depth": sum(depth.values()),
            "uploaded_total": self.uploader.uploaded,
            "tiers": depth,
            "service_state": contract.state_of(self.root),
            "recording": st["recording"],
        })

    def tick(self) -> str:
        self.ticks += 1
        now = time.monotonic()
        self.refresh_service_state(now)
        allowed, reason = contract.may_record(self.root)
        self.gate = "paused" if self.cfg.paused else reason
        if allowed and not self.cfg.paused:
            self.collect(now)
            self.spool.prune()
            result = self.upload(now)
        else:
            result = "stopped"
        self.heartbeat(now)
        if now - self.last_status >= STATUS_EVERY_S:
            self.last_status = now
            self._write_status()
        return result

    # --- lifecycle --------------------------------------------------------------------------
    def run(self, once: bool = False) -> str:
        contract.ensure_contract(self.root)  # first run is when the contract takes effect
        if contract.is_terminated(self.root):
            self.gate = "contract_terminated"
            self._write_status()
            return "terminated"
        if not once:
            self.start_ipc()
        self.log("start", ipc=bool(self.server), once=once)
        try:
            while not self.control.stopped.is_set():
                result = self.tick()
                if once:
                    break
                if result in (DEVICE_REVOKED, NOT_CONFIGURED):
                    self._sleep(self.cfg.upload_interval_s * 5)
                else:
                    self._sleep(POLL_S)
        except KeyboardInterrupt:
            self.control.stop("interrupt")
        finally:
            if self.server is not None:
                self.server.stop()
            self._write_status({"collector_running": False})
            self.log("exit", reason=self.control.stop_reason)
            self.reader.close()
        return self.control.stop_reason or "idle"

    def _sleep(self, seconds: float) -> None:
        step = min(POLL_S, max(0.05, seconds))
        remaining = seconds
        while remaining > 0 and not self.control.stopped.is_set():
            self.control.stopped.wait(min(step, remaining))
            remaining -= step


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso(epoch: float | None) -> str | None:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)) if epoch else None


def _summaries(events: list[dict]) -> list[dict]:
    """One telemetry record per session per batch, so the backend can see activity even
    before any content record arrives."""
    groups: dict[str, list] = {}
    for event in events:
        sid = event.get("session_id")
        if isinstance(sid, str) and sid and event.get("kind") != "session_summary":
            groups.setdefault(sid, []).append(event)
    return [tiers.session_summary(sid, group) for sid, group in groups.items()]


def main(argv=None) -> int:
    """`python -m collector run [--once] [--data-dir PATH]`."""
    import argparse
    parser = argparse.ArgumentParser(prog="collector")
    parser.add_argument("--data-dir")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.data_dir) if args.data_dir else CollectorPaths.default().root
    return 0 if Collector(root).run(once=args.once) != "error" else 1


if __name__ == "__main__":  # `python -m collector.run` must work too: some launchers use it
    raise SystemExit(main())
