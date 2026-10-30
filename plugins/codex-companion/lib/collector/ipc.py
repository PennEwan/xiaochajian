r"""Local IPC between the plugin (UI only) and the collector (background component).

Windows named pipe (`\\.\pipe\codex-companion-v1`) with an HMAC handshake keyed by a token
file only the current user can read. One request per connection; the payload is one JSON
object in each direction. The plugin never gets a handle on the recording store this way.

`COMPANION_PIPE` overrides the pipe name — tests and side-by-side installs use it so they never
fight over the single pipe name in production.

Failure is always explicit: IpcError carries a short code, never user data.
"""
from __future__ import annotations

import json
import os
import threading
from multiprocessing import connection
from pathlib import Path

PIPE_NAME = r"\\.\pipe\codex-companion-v1"
OPS = ("ping", "status", "pause", "resume", "stop", "open_panel", "open_recharge")
MAX_MESSAGE = 1 << 20  # 1 MiB is far more than any of these replies needs


class IpcError(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind  # no_server | timeout | refused | bad_reply | no_token


def family() -> str:
    return "AF_PIPE" if os.name == "nt" else "AF_UNIX"


def address(root: Path) -> str:
    override = os.environ.get("COMPANION_PIPE")
    if override:
        return override
    return PIPE_NAME if os.name == "nt" else str(Path(root) / "collector.sock")


def read_token(path: Path) -> bytes | None:
    try:
        raw = Path(path).read_bytes().strip()
    except OSError:
        return None
    return raw or None


def ensure_token(path: Path) -> bytes:
    """Create the pipe key once; 0600 on POSIX, owner-only ACL inherited on Windows."""
    path = Path(path)
    token = read_token(path)
    if token:
        return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = os.urandom(24).hex().encode("ascii")
    tmp = path.with_suffix(".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(token)
    os.replace(tmp, path)
    return token


def clear_token(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


# --- server ----------------------------------------------------------------------------------
class Server:
    """Accepts one request per connection on a daemon thread.

    `handler(request: dict) -> dict` runs on that thread, so it must only touch thread-safe
    state (the collector's control object is a small lock-guarded record).
    """

    def __init__(self, handler, token: bytes, root: Path, name: str | None = None):
        self.handler = handler
        self.token = token
        self.addr = address(root) if name is None else name
        self._listener = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.error: str | None = None

    def start(self) -> bool:
        try:
            if os.name != "nt":
                try:
                    Path(self.addr).unlink()
                except OSError:
                    pass
            self._listener = connection.Listener(self.addr, family=family(), authkey=self.token)
        except (OSError, ValueError) as exc:
            self.error = f"{type(exc).__name__}:{getattr(exc, 'winerror', '')}"
            return False
        self._thread = threading.Thread(target=self._accept_loop, name="collector-ipc", daemon=True)
        self._thread.start()
        return True

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except (OSError, EOFError):
                return  # closed by stop()
            except Exception:  # a bad handshake must not kill the pipe
                continue
            try:
                with conn:
                    try:
                        request = conn.recv()
                    except (EOFError, OSError, ValueError):
                        continue
                    reply = self._dispatch(request)
                    try:
                        conn.send(reply)
                    except (OSError, ValueError):
                        pass
            except OSError:
                pass

    def _dispatch(self, request) -> dict:
        if not isinstance(request, dict):
            return {"ok": False, "error": "bad_request"}
        op = request.get("op")
        if op not in OPS:
            return {"ok": False, "error": "unknown_op"}
        try:
            reply = self.handler(request) or {}
        except Exception as exc:  # never leak a traceback over the pipe
            return {"ok": False, "error": f"handler_{type(exc).__name__.lower()}"}
        reply.setdefault("ok", True)
        return reply

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._listener is not None:
                self._listener.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if os.name != "nt":
            try:
                Path(self.addr).unlink()
            except OSError:
                pass


# --- client ----------------------------------------------------------------------------------
def call(request: dict, *, token: bytes | None, root: Path, timeout: float = 5.0,
         address_override: str | None = None) -> dict:
    if not token:
        raise IpcError("no_token")
    addr = address_override or address(root)
    try:
        conn = connection.Client(addr, family=family(), authkey=token)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
        raise IpcError("no_server") from exc
    except Exception as exc:  # a rejected handshake is a wrong token, not a crash
        raise IpcError("refused") from exc
    try:
        conn.send(request)
        if not conn.poll(timeout):
            raise IpcError("timeout")
        reply = conn.recv()
    except (EOFError, OSError) as exc:
        raise IpcError("refused") from exc
    finally:
        try:
            conn.close()
        except OSError:
            pass
    if not isinstance(reply, dict):
        raise IpcError("bad_reply")
    return reply


def ping(*, token: bytes | None, root: Path, timeout: float = 2.0) -> bool:
    try:
        return bool(call({"op": "ping"}, token=token, root=root, timeout=timeout).get("ok"))
    except IpcError:
        return False


def dump(obj: dict) -> str:
    """Compact JSON for the wire (never used on data that could carry a token)."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
