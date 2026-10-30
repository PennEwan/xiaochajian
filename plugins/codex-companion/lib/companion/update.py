"""Updates of the bundle's own files, always under the user's authorization.

Modes (control panel, stored in state.json):
  notify  default. Check at most once a day (and when the panel opens); tell the user in the
          panel and once a day in Codex. Installing takes the user's click.
  auto    the user opted in: a found update is downloaded, verified and installed silently in
          the background — except one that needs the hooks re-trusted, which waits for a click.
  off     never contact the update server.

The hook never touches the network: it only starts a detached checker
(`pythonw -m companion update check`) when a check is due. The manifest (docs/update-api.md)
names the installer's URL, size and SHA-256; a download runs only after both match. The
request carries `User-Agent: CodexCompanion/<version>` and nothing else about the machine.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from datetime import date

from ctr import store, timeutil

from . import local, log
from .product import Product, version_key

NOTIFY, AUTO, OFF = "notify", "auto", "off"
MODES = (NOTIFY, AUTO, OFF)
CHECK_INTERVAL_S = 24 * 3600
SPAWN_INTERVAL_S = 300
MAX_MANIFEST = 64 * 1024
MAX_INSTALLER = 200 * 1024 * 1024
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d+(\.\d+){1,3}$")


class UpdateError(Exception):
    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


@dataclass
class Release:
    version: str
    url: str
    sha256: str
    size: int
    min_kernel: str | None = None
    requires_retrust: bool = False
    notes: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _is_loopback(url: str) -> bool:
    host = urllib.parse.urlsplit(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


def check_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and _is_loopback(url)):
        return url
    raise UpdateError("insecure_url")


def _opener(url: str):
    # urllib.request pulls in ssl and http: imported here, so the hook (which only reads state
    # files and maybe starts a checker) never pays for them.
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # a moved manifest / installer is an error, not a hop
            return None

    handlers = [NoRedirect()]
    if _is_loopback(url):
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _get(url: str, version: str, timeout: float):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(check_url(url), headers={"User-Agent": f"CodexCompanion/{version}"})
    try:
        return _opener(url).open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"http_{exc.code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise UpdateError("network") from None


def _read_failed(exc: Exception) -> bool:
    import http.client

    return isinstance(exc, (OSError, http.client.HTTPException))


def parse_manifest(doc: object) -> Release | None:
    """The latest release from a manifest; None when it lists none. Raises on a bad manifest."""
    if not isinstance(doc, dict) or doc.get("product") != "codex-companion":
        raise UpdateError("bad_manifest")
    latest = doc.get("latest")
    if latest is None:
        return None
    try:
        rel = Release(version=str(latest["version"]), url=check_url(str(latest["url"])),
                      sha256=str(latest["sha256"]).lower(), size=int(latest["size"]),
                      min_kernel=latest.get("min_kernel") if isinstance(latest.get("min_kernel"), str) else None,
                      requires_retrust=latest.get("requires_retrust") is True,
                      notes=str(latest.get("notes") or "")[:2000])
    except (KeyError, TypeError, ValueError):
        raise UpdateError("bad_manifest") from None
    if not _HEX64.match(rel.sha256) or not 0 < rel.size <= MAX_INSTALLER or not _VERSION.match(rel.version):
        raise UpdateError("bad_manifest")
    return rel


def fetch(product: Product, timeout: float = 10) -> Release | None:
    if not product.update_manifest_url:
        raise UpdateError("not_configured")
    try:
        with _get(product.update_manifest_url, product.version, timeout) as resp:
            raw = resp.read(MAX_MANIFEST + 1)
    except UpdateError:
        raise
    except Exception as exc:  # noqa: BLE001 - a connection dropped mid-body, a timeout while reading
        if _read_failed(exc):
            raise UpdateError("network") from None
        raise
    if len(raw) > MAX_MANIFEST:
        raise UpdateError("bad_manifest")
    try:
        return parse_manifest(json.loads(raw.decode("utf-8")))
    except ValueError:
        raise UpdateError("bad_manifest") from None


def newer(product: Product, rel: Release | None) -> bool:
    return bool(rel) and version_key(rel.version) > version_key(product.version)


# ---- state ----------------------------------------------------------------------------------------

def mode(cpaths: local.Paths) -> str:
    value = local.read_json(cpaths.state).get("update_mode")
    return value if value in MODES else NOTIFY


def set_mode(cpaths: local.Paths, value: str) -> None:
    if value not in MODES:
        raise ValueError(value)
    local.update_json(cpaths.state, update_mode=value)


def info(cpaths: local.Paths) -> dict:
    return local.read_json(cpaths.update)


def available(product: Product, cpaths: local.Paths) -> Release | None:
    """The recorded newer release, if the last check found one."""
    latest = info(cpaths).get("latest")
    if not isinstance(latest, dict):
        return None
    try:
        rel = Release(**{k: latest[k] for k in Release.__dataclass_fields__ if k in latest})
    except TypeError:
        return None
    return rel if newer(product, rel) else None


def _due(doc: dict, now: float) -> bool:
    last = doc.get("checked_ts")
    return not isinstance(last, (int, float)) or now - last >= CHECK_INTERVAL_S


def check(product: Product, cpaths: local.Paths, force: bool = False, now=time.time) -> dict:
    """Ask the update server (unless a check is not due); records and returns update.json."""
    doc = info(cpaths)
    if mode(cpaths) == OFF and not force:
        return doc
    if not force and not _due(doc, now()):
        return doc
    try:
        rel = fetch(product)
        doc = local.update_json(cpaths.update, checked_ts=now(), checked_at=timeutil.iso(timeutil.now()),
                                error=None, latest=rel.to_dict() if rel else None)
        log.write(cpaths.log, "update_check", "newer" if newer(product, rel) else "current")
    except UpdateError as exc:
        doc = local.update_json(cpaths.update, checked_ts=now(), checked_at=timeutil.iso(timeutil.now()),
                                error=exc.kind)
        log.write(cpaths.log, "update_check", exc.kind)
    return doc


def notice(product: Product, cpaths: local.Paths, today: date | None = None) -> str | None:
    """The once-a-day line for Codex when an update waits for the user."""
    if mode(cpaths) == OFF:
        return None
    rel = available(product, cpaths)
    if not rel:
        return None
    today_s = (today or date.today()).isoformat()
    if info(cpaths).get("notified_on") == today_s:
        return None
    local.update_json(cpaths.update, notified_on=today_s)
    return f"{product.name} 有新版本 {rel.version}：发送 /记录 打开控制面板即可更新。"


def kick(product: Product, cpaths: local.Paths, now=time.time) -> bool:
    """Called by the hook: start a background check when one is due. Never raises."""
    try:
        if not product.update_manifest_url or mode(cpaths) == OFF:
            return False
        if not _due(info(cpaths), now()):
            return False
        try:
            if now() - cpaths.update_stamp.stat().st_mtime < SPAWN_INTERVAL_S:
                return False
        except OSError:
            pass
        if store.is_held(cpaths.update_lock):
            return False
        cpaths.root.mkdir(parents=True, exist_ok=True)
        cpaths.update_stamp.touch()
        from . import launch

        launch.spawn_module("companion", ["update", "check", "--background"])
        return True
    except Exception:  # noqa: BLE001 - an update check is never worth breaking a hook
        return False


# ---- download and install ----------------------------------------------------------------------

def download(product: Product, cpaths: local.Paths, rel: Release, progress=None, timeout: float = 30) -> str:
    """Fetch the installer and verify size + SHA-256; returns its path. Nothing unverified stays."""
    cpaths.downloads.mkdir(parents=True, exist_ok=True)
    final = cpaths.downloads / f"CodexCompanion-Setup-{re.sub(r'[^0-9A-Za-z.-]', '_', rel.version)}.exe"
    if final.exists() and _sha256(final) == rel.sha256 and final.stat().st_size == rel.size:
        return str(final)
    part = final.with_suffix(".part")
    digest, got = hashlib.sha256(), 0
    try:
        with _get(rel.url, product.version, timeout) as resp, open(part, "wb") as out:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > rel.size:
                    raise UpdateError("size_mismatch")
                digest.update(chunk)
                out.write(chunk)
                if progress:
                    progress(got, rel.size)
        if got != rel.size:
            raise UpdateError("size_mismatch")
        if digest.hexdigest() != rel.sha256:
            raise UpdateError("hash_mismatch")
        store.replace(part, final)
    except UpdateError:
        part.unlink(missing_ok=True)
        log.write(cpaths.log, "update_download", "rejected")
        raise
    except Exception as exc:  # noqa: BLE001
        part.unlink(missing_ok=True)
        if _read_failed(exc):
            raise UpdateError("network") from None
        raise
    log.write(cpaths.log, "update_download", "ok", bytes=got)
    return str(final)


def _sha256(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def install(installer: str, silent: bool = True, auto: bool = False) -> None:
    """Start the verified installer detached; it stops the bundle's processes itself.
    /UPDATE=1 (the user asked) reopens the control panel afterwards; /UPDATE=auto does not take
    the focus away from Codex."""
    from . import launch

    args = ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"] if silent else []
    args.append("/UPDATE=auto" if auto else "/UPDATE=1")
    launch.spawn([installer, *args], cwd=tempfile.gettempdir())


def recharge_window_open() -> bool:
    """An open recharge window may be mid-redemption: an automatic update waits for it to close."""
    if os.name != "nt":
        return False
    import ctypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = ctypes.c_void_p
    handle = k32.OpenMutexW(0x00100000, False, "Local\\CodexCdkRecharge.Window")  # SYNCHRONIZE
    if handle:
        k32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def background_check(product: Product, cpaths: local.Paths, kernel_version=None) -> str:
    """Body of the detached checker: check, and in auto mode install what may go in silently.
    `kernel_version` is a string or a function returning one (only asked for when needed)."""
    fd = store.try_hold(cpaths.update_lock)
    if fd is None:
        return "already_running"
    try:
        doc = check(product, cpaths)
        rel = available(product, cpaths)
        if doc.get("error") or not rel:
            return doc.get("error") or "current"
        if mode(cpaths) != AUTO or rel.requires_retrust:
            return "notify"
        if rel.min_kernel:
            current = kernel_version() if callable(kernel_version) else kernel_version
            if current and version_key(current) < version_key(rel.min_kernel):
                return "kernel_too_old"
        if recharge_window_open():
            return "deferred"  # the next check (or the user, from the panel) installs it
        try:
            path = download(product, cpaths, rel)
        except UpdateError as exc:
            local.update_json(cpaths.update, error=exc.kind)
            return exc.kind
        log.write(cpaths.log, "update_install", "auto")
        install(path, silent=True, auto=True)
        return "installing"
    finally:
        store.release(fd)
