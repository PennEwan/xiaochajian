"""Install, repair, uninstall: the steps the Inno Setup installer runs (`python -m companion setup …`).

Nothing here prompts — the installer asks its questions first and passes the answers as flags —
and every step returns a JSON-able result that the installer turns into messages.

Codex's config is changed only through the App kernel's own commands (`codex plugin …`), except
the hook-trust entries those commands leave behind on removal (see codexcfg). The bundle never
writes trust: the user grants it in the App.

The independent collector (`collector.*`, a per-user logon task) is registered here too: install and
repair create it, uninstall stops it and deletes it before anything else is touched. It is a separate
component on purpose — recording must outlive the plugin, and must really stop when the product goes.
"""
from __future__ import annotations

import importlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ctr import kernel
from ctr.paths import Paths as RecorderPaths
from ctr.sync import api as sapi
from ctr.sync import config as sconfig
from ctr.sync import launcher, vault
from ctr.sync import state as sstate
from ctr.sync.config import SyncPaths

from . import codexcfg, compat, local, log, procs
from .product import Product

LEGACY_MARKETPLACE = "ctr-local"
LEGACY_PLUGINS = ("codex-task-recorder@ctr-local", "codex-cdk-recharge@ctr-local")
OK, FAILED = "ok", "failed"


@dataclass
class Context:
    product: Product
    home: Path                                  # CODEX_HOME
    cpaths: local.Paths
    rpaths: RecorderPaths
    cdkr_root: Path
    install_dir: Path | None = None             # %LOCALAPPDATA%\Programs\CodexCompanion
    notes: list[str] = field(default_factory=list)

    @classmethod
    def default(cls, product: Product, install_dir: Path | None = None) -> "Context":
        """The control panel may run from Codex's plugin cache (/记录): the install directory then
        comes from what the installer recorded, never from the cache path."""
        from cdkr import config as cconfig

        cpaths = local.Paths.default()
        recorded = local.read_json(cpaths.state).get("install_dir")
        return cls(product=product, home=codexcfg.codex_home(), cpaths=cpaths,
                   rpaths=RecorderPaths.default(), cdkr_root=cconfig.Paths.default().root,
                   install_dir=install_dir or _install_dir_of(product.root) or (Path(recorded) if recorded else None))

    @property
    def marketplace_dir(self) -> Path | None:
        """<install dir>\\marketplace — the source Codex installs the plugin from."""
        return self.install_dir / "marketplace" if self.install_dir else None

    @property
    def cache_dir(self) -> Path:
        return self.home / "plugins" / "cache" / self.product.marketplace

    @property
    def plugin_data_dir(self) -> Path:
        return self.home / "plugins" / "data" / f"{self.product.plugin}-{self.product.marketplace}"

    def state(self) -> codexcfg.PluginState:
        return codexcfg.plugin_state(self.home, self.product.root, self.product.plugin_id, self.product.marketplace)


def _install_dir_of(plugin_root: Path) -> Path | None:
    """<install dir>\\marketplace\\plugins\\<plugin> -> <install dir>; None inside Codex's cache."""
    parents = plugin_root.parents
    if len(parents) >= 3 and parents[0].name == "plugins" and parents[1].name == "marketplace":
        return parents[2]
    return None


def _run(exe: Path, args: list[str]) -> tuple[bool, str]:
    try:
        proc = kernel.run(exe, args, timeout=180)
    except Exception as exc:  # noqa: BLE001 - reported, never raised into the installer
        return False, type(exc).__name__
    text = "\n".join(line for line in (proc.stdout + proc.stderr).splitlines()
                     if line.strip() and "stale arg0" not in line)
    return proc.returncode == 0, text[-600:]


def _hooks(exe: Path, ctx: Context) -> list[dict] | None:
    from ctr.appserver import AppServer

    server = None
    try:
        server = AppServer(exe, str(Path.home()))
        server.initialize()
        return server.plugin_hooks(str(Path.home()), ctx.product.plugin_id)
    except Exception:  # noqa: BLE001
        return None
    finally:
        if server:
            server.close()


# ---- independent collector ----------------------------------------------------------------------
# `collector` sits next to `companion` in lib\ and is a component of its own: it records from the
# rollout files Codex writes, runs from a per-user logon task, and therefore keeps running after the
# plugin is disabled or removed. A checkout (or an older build) without it must still install, so a
# missing module becomes a recorded detail code — never an exception.

COLLECTOR_MISSING = "unavailable"


def collector_module(name: str):
    """`collector.<name>`, or None when this build has no such module."""
    try:
        return importlib.import_module(f"collector.{name}")
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 - a module that cannot load is a detail, not a crash
        return None


def collector_paths():
    """`collector.paths.CollectorPaths.default()`: root plus the file names the status line reads."""
    paths = collector_module("paths")
    if paths is None:
        return None
    try:
        return paths.CollectorPaths.default()
    except Exception:  # noqa: BLE001
        return None


def collector_root() -> Path | None:
    """%LOCALAPPDATA%\\CodexCompanion — the collector's contract, spool, status and IPC token."""
    cpaths = collector_paths()
    return None if cpaths is None else Path(cpaths.root)


def _scheduler_is_real() -> bool:
    """Whether the machine's Task Scheduler belongs to this run.

    A data-directory override (COMPANION_DATA_DIR: the test suite, portable runs) means this is not
    an installation, so no task is registered — and, just as important, none is deleted: running the
    tests must never remove a real user's task or stop their recording. COMPANION_COLLECTOR_TASKS=1
    forces the real thing for a manual end-to-end check.
    """
    if os.environ.get("COMPANION_COLLECTOR_TASKS", "").strip().lower() in ("1", "on", "yes"):
        return True
    return not os.environ.get("COMPANION_DATA_DIR")


def collector_call(module: str, func: str, *args, **kwargs) -> tuple[bool, str]:
    """Call one collector function; always (ok, short detail code), never an exception."""
    mod = collector_module(module)
    fn = getattr(mod, func, None) if mod is not None else None
    if not callable(fn):
        return False, COLLECTOR_MISSING
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - a detail code, never a crash in the installer
        return False, type(exc).__name__.lower()
    if isinstance(result, tuple) and len(result) == 2:
        ok, detail = result
        return bool(ok), str(detail or "ok")
    return bool(result), "ok" if result else "failed"


def _collector_launcher(root: Path) -> Path | None:
    """The file the logon task runs; None when `tasks` does not say (an unknown layout)."""
    mod = collector_module("tasks")
    launcher = getattr(mod, "launcher", None) if mod is not None else None
    if not callable(launcher):
        return None
    try:
        return Path(launcher(root))
    except Exception:  # noqa: BLE001
        return None


def _ensure_contract(root: Path, version: str) -> tuple[dict, str]:
    """`collector.contract.ensure_contract` — idempotent, so a repair keeps the original times."""
    mod = collector_module("contract")
    fn = getattr(mod, "ensure_contract", None) if mod is not None else None
    if not callable(fn):
        return {}, COLLECTOR_MISSING
    try:
        doc = fn(root, product_version=version)
    except Exception as exc:  # noqa: BLE001
        return {}, type(exc).__name__.lower()
    return (doc if isinstance(doc, dict) else {}), "ok" if doc else "empty"


def _configure_collector(root: Path, product) -> str:
    """Write `collector-config.json`: where to upload, and where the product is installed.

    The collector reads its own config, not `ctr.sync`'s: it is a separate component and must keep
    working when the plugin's files (and their settings) are gone. `(detail)`.
    """
    mod = collector_module("config")
    if mod is None or not callable(getattr(mod, "load", None)):
        return COLLECTOR_MISSING
    try:
        cfg = mod.load(root)
        if getattr(product, "sync_base_url", None):
            cfg.base_url = product.sync_base_url
        cfg.install_dir = str(product.root)
        mod.save(root, cfg)
    except Exception as exc:  # noqa: BLE001 - a bad config must not abort an install
        return type(exc).__name__.lower()
    return "configured" if cfg.base_url else "no_backend"


def collector_install(ctx: Context, result: dict) -> None:
    """Record the service contract, write the collector's own config, register its logon task.

    All three are what makes recording outlive the plugin. None may abort an install: the plugin is
    the UI, the collector is the product, and every outcome lands in `result` as a detail code.
    """
    root = collector_root()
    if root is None:
        result.update(collector_task=COLLECTOR_MISSING, collector_contract=COLLECTOR_MISSING)
        return
    doc, detail = _ensure_contract(root, ctx.product.version)
    if doc:
        result["terms_version"] = doc.get("terms_version")
    else:
        result["collector_contract"] = detail
    result["collector_config"] = _configure_collector(root, ctx.product)
    if not _scheduler_is_real():
        result["collector_task"] = "skipped_dev"
        return
    if ctx.marketplace_dir is None or not ctx.marketplace_dir.exists():
        result["collector_task"] = "no_install_dir"
        return
    launcher = _collector_launcher(root)
    if launcher is not None and not launcher.exists():  # never register a task that cannot start
        result["collector_task"] = "no_launcher"
        return
    detail = collector_call("tasks", "create", root)[1]
    result["collector_task"] = detail
    log.write(ctx.cpaths.log, "setup", "collector_registered", task=detail, terms=result.get("terms_version") or 0)


def collector_ask_stop(cpaths) -> str:
    """Stop a running collector now; a short detail code, never an exception.

    `collector.service.stop()` when this build has it, otherwise the same request (`op: stop`) over
    the local pipe the collector listens on. Best effort either way: the durable stop is
    `contract.terminate()`, which the collector reads before every pass and cannot miss.
    """
    root = Path(cpaths.root)
    if callable(getattr(collector_module("service"), "stop", None)):
        ok, detail = collector_call("service", "stop", root)
        return "stopped" if ok else detail
    ipc = collector_module("ipc")
    if ipc is None:
        return COLLECTOR_MISSING
    try:
        reply = ipc.call({"op": "stop"}, token=ipc.read_token(cpaths.token), root=root, timeout=3.0)
    except Exception as exc:  # noqa: BLE001 - not answering is an outcome, not a crash
        return type(exc).__name__.lower()
    return "stopped" if isinstance(reply, dict) and reply.get("ok") else str((reply or {}).get("error") or "refused")


def collector_uninstall(ctx: Context, result: dict) -> None:
    """Stop recording for real, in this order: terminate the contract, stop the process, delete
    the logon task.

    Called before anything is removed and it never raises. The order matters: the terminated contract
    is what a collector that misses the pipe honours, the pipe request is what stops it now, and the
    deleted task is what keeps it from coming back at the next logon.
    """
    cpaths = collector_paths()
    if cpaths is None:
        result.update(collector_stop=COLLECTOR_MISSING, collector_ipc=COLLECTOR_MISSING,
                      collector_task=COLLECTOR_MISSING)
        return
    root = Path(cpaths.root)
    if not _scheduler_is_real():
        result.update(collector_stop="skipped_dev", collector_ipc="skipped_dev", collector_task="skipped_dev")
        return
    ok, detail = collector_call("contract", "terminate", root, reason="uninstall")
    result["collector_stop"] = "terminated" if ok else detail
    result["collector_ipc"] = collector_ask_stop(cpaths)
    _, detail = collector_call("tasks", "delete", root)
    result["collector_task"] = detail
    log.write(ctx.cpaths.log, "setup", "collector_removed", stop=result["collector_stop"],
              ipc=result["collector_ipc"], task=detail)


# ---- install ------------------------------------------------------------------------------------

def configure_backends(ctx: Context) -> None:
    """Point the recharge window and the uploader at this build's backends."""
    from cdkr import config as cconfig

    p = ctx.product
    if p.recharge_base_url:
        cpaths = cconfig.Paths(ctx.cdkr_root)
        cfg = cconfig.load(cpaths)
        if cfg.base_url != p.recharge_base_url:
            cfg.base_url = p.recharge_base_url
            cconfig.save(cpaths, cfg)
    if p.sync_base_url:
        sp = SyncPaths(ctx.rpaths)
        try:
            url = sapi.check_base_url(p.sync_base_url)
        except sapi.InsecureUrl:
            ctx.notes.append("sync_url_insecure")
            return
        dev = vault.load(sp.device)
        if dev and dev.get("base_url") and dev["base_url"] != url:
            ctx.notes.append("sync_device_elsewhere")  # the panel asks the user to revoke first
            return
        cfg = sconfig.load(sp)
        if cfg.base_url != url:
            cfg.base_url = url
            sconfig.save(sp, cfg)
            st = sstate.load(sp)
            if sstate.is_on(st) and sstate.needs_new_consent(st, url):
                from ctr import timeutil

                sstate.pause(sp, timeutil.now())  # a new backend needs a new consent
                ctx.notes.append("sync_paused_new_backend")


def _set_enabled(exe: Path, plugin_id: str, enabled: bool) -> bool:
    """Through app-server's config/value/write — the interface the App's plugin switch uses."""
    from ctr.appserver import AppServer

    server = None
    try:
        server = AppServer(exe, str(Path.home()))
        server.initialize()
        server.request("config/value/write", {"keyPath": f"plugins.{plugin_id}.enabled", "value": enabled,
                                              "mergeStrategy": "replace"}, timeout=60)
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        if server:
            server.close()


def install(ctx: Context, probe: bool = True) -> dict:
    """Register (or refresh) the plugin with Codex. Also the repair behind 「重新检测」.

    The collector is wired on every outcome, including "no Codex" and "incompatible": it reads
    Codex's own files and does not depend on the plugin, so a missing kernel must not leave the
    machine without one — and a collector problem must never fail the install.
    """
    result = _install_plugin(ctx, probe)
    collector_install(ctx, result)
    return result


def _install_plugin(ctx: Context, probe: bool) -> dict:
    """The plugin side of `install`: the steps that always ran, through Codex's own commands."""
    p = ctx.product
    result = {"step": "install", "version": p.version, "status": FAILED, "legacy": [], "notes": ctx.notes}
    if ctx.marketplace_dir is None or not (ctx.marketplace_dir / ".agents").exists():
        result.update(message=f"找不到 {p.name} 的安装目录，请重新运行安装程序。")
        return result
    local.update_json(ctx.cpaths.state, install_dir=str(ctx.install_dir), installed_version=p.version)
    configure_backends(ctx)
    found = kernel.find_app_kernel()
    c = compat.evaluate(p, None) if not found else None
    if found:
        exe, version = found
        problems = compat.probe(exe) if probe and version else []
        c = compat.evaluate(p, version, compat.app_version(), str(exe), problems)
    result["compat"] = c.to_dict()
    if c.status == compat.NO_CODEX:
        result.update(status=compat.NO_CODEX, message=c.message)
        return result
    if c.status == compat.INCOMPATIBLE:
        if ctx.state().installed:  # e.g. Codex was downgraded: stop the hooks instead of failing in them
            _run(exe, ["plugin", "remove", p.plugin_id])
        result.update(status=compat.INCOMPATIBLE, message=c.message)
        return result
    saved = codexcfg.backup(ctx.home, ctx.cpaths.backup, "install")
    result["backup"] = str(saved) if saved else None
    result["legacy"] = [pid for pid in codexcfg.installed_plugins(ctx.home, LEGACY_MARKETPLACE) if pid in LEGACY_PLUGINS]

    st = ctx.state()
    # `codex plugin add` turns a plugin the user disabled back on (measured): remember the choice.
    user_disabled = st.installed and not st.enabled
    ours = str(ctx.marketplace_dir)
    if st.marketplace_root and _norm(st.marketplace_root) != _norm(ours):
        _run(exe, ["plugin", "marketplace", "remove", p.marketplace])  # an older install elsewhere
        st = ctx.state()
    if not st.marketplace_root:
        ok, text = _run(exe, ["plugin", "marketplace", "add", ours])
        if not ok:
            result.update(message="添加插件市场失败：" + text)
            return result
    ok, text = _run(exe, ["plugin", "add", p.plugin_id])
    if not ok:
        result.update(message="安装插件失败：" + text)
        return result
    hooks = _hooks(exe, ctx)
    hookless = not p.declares_hooks()  # the shipped plugin has none: nothing to wait for
    if user_disabled:
        if ctx.state().enabled and not _set_enabled(exe, p.plugin_id, False):
            result["notes"].append("could_not_keep_disabled")
        result["disabled_by_user"] = True
    elif hooks is not None and not hooks and not hookless:
        _run(exe, ["plugin", "remove", p.plugin_id])
        c = compat.evaluate(p, c.kernel_version, c.app_version, c.kernel_path, ["插件 Hooks 未加载"])
        result.update(status=compat.INCOMPATIBLE, compat=c.to_dict(), message=c.message)
        return result
    trusted = sum(1 for h in hooks or [] if h.get("trustStatus") in ("trusted", "managed"))
    result["hooks"] = {"total": len(hooks or []), "trusted": trusted, "checked": hooks is not None}
    if not user_disabled and sstate.wants_worker(sstate.load(SyncPaths(ctx.rpaths))):
        try:
            launcher.kick(ctx.rpaths, force=True)
        except OSError:
            pass
    message = c.message
    if user_disabled:
        message += f"（你在 Codex 中禁用了「{p.name}」，更新后保持禁用。）"
    result.update(status=c.status, message=message)
    log.write(ctx.cpaths.log, "setup", "install", trusted=trusted)
    return result


def _norm(path: str) -> str:
    import os
    return os.path.normcase(os.path.normpath(path))


def remove_legacy(ctx: Context) -> dict:
    """Uninstall the development plugins (P1/P2/P3 .cmd installs): with both installed, every
    session would be recorded twice and /充值 would open two windows. Their uploader notices the
    removal by itself (hostgate) and exits."""
    result = {"step": "remove_legacy", "removed": [], "status": OK}
    found = kernel.find_app_kernel()
    if not found:
        result["status"] = compat.NO_CODEX
        return result
    exe = found[0]
    for pid in codexcfg.installed_plugins(ctx.home, LEGACY_MARKETPLACE):
        if pid in LEGACY_PLUGINS:
            ok, _ = _run(exe, ["plugin", "remove", pid])
            if ok:
                result["removed"].append(pid)
    if not codexcfg.installed_plugins(ctx.home, LEGACY_MARKETPLACE):
        _run(exe, ["plugin", "marketplace", "remove", LEGACY_MARKETPLACE])
    for pid in result["removed"]:  # only the trust entries Codex left behind; tables stay Codex's
        codexcfg.clean(ctx.home, pid, None, ctx.cpaths.backup)
    if any(pid in LEGACY_PLUGINS for pid in codexcfg.installed_plugins(ctx.home, LEGACY_MARKETPLACE)):
        result["status"] = FAILED  # one would not go: say so rather than half-clean it
    log.write(ctx.cpaths.log, "setup", "remove_legacy", n=len(result["removed"]))
    return result


# ---- stop / uninstall ---------------------------------------------------------------------------

def _process_roots(ctx: Context) -> list[Path]:
    roots = [ctx.cache_dir, ctx.product.root]
    if ctx.install_dir:
        roots.append(ctx.install_dir)
    return roots


def stop_processes(ctx: Context) -> list[str]:
    launcher.wait_until_stopped(SyncPaths(ctx.rpaths), 5.0)
    return procs.stop(_process_roots(ctx))


def prepare_upgrade(ctx: Context) -> dict:
    """Before new files replace the old ones: stop the long-lived processes (pythonw: control panel,
    recharge window, uploader, update checker). Hook processes (python.exe) run from Codex's cache,
    which is refreshed in place, and finish within seconds — they are left alone."""
    stopped = procs.stop(_process_roots(ctx), names={"pythonw.exe"})
    log.write(ctx.cpaths.log, "setup", "prepare_upgrade", n=len(stopped))
    return {"step": "prepare_upgrade", "status": OK, "stopped": stopped}


def _purge(ctx: Context, keep_revocation: bool) -> list[str]:
    """Delete the local data. With a revocation that could not be sent, keep exactly what sending
    it later needs (sync state, device token, backend address): reinstalling sends it."""
    kept = []
    for d in (ctx.cdkr_root, ctx.cpaths.root):
        shutil.rmtree(d, ignore_errors=True)
    if not keep_revocation:
        shutil.rmtree(ctx.rpaths.root, ignore_errors=True)
        return kept
    sync = SyncPaths(ctx.rpaths)
    for child in list(ctx.rpaths.root.iterdir()) if ctx.rpaths.root.exists() else []:
        if child != sync.root:
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    for child in list(sync.root.iterdir()) if sync.root.exists() else []:
        if child not in (sync.state, sync.device, sync.config, sync.state_lock):
            shutil.rmtree(child, ignore_errors=True) if child.is_dir() else child.unlink(missing_ok=True)
    kept.append(str(sync.root))
    return kept


def uninstall(ctx: Context, delete_remote: bool = False, purge_local: bool = False) -> dict:
    from .authz import Authz

    p = ctx.product
    result = {"step": "uninstall", "status": OK, "remote": None, "kept": [], "purged": purge_local}
    collector_uninstall(ctx, result)  # first: stopping recording must not depend on later steps
    authz = Authz(p, ctx.rpaths, ctx.cpaths)
    sp = SyncPaths(ctx.rpaths)
    try:
        authz.pause()
        if delete_remote or (purge_local and vault.load(sp.device)):
            # Deleting local data would also delete the device token, after which the uploaded
            # data could never be deleted: revoke the device (and delete if asked) first.
            result["remote"] = authz.revoke(delete_remote=delete_remote, delete_local=False)
    except Exception as exc:  # noqa: BLE001 - uninstall goes on regardless
        result["authz_error"] = type(exc).__name__
    result["stopped"] = stop_processes(ctx)

    found = kernel.find_app_kernel()
    if found:
        exe = found[0]
        if ctx.state().installed:
            ok, text = _run(exe, ["plugin", "remove", p.plugin_id])
            if not ok:
                result["plugin_remove"] = text
        others = [pid for pid in codexcfg.installed_plugins(ctx.home, p.marketplace) if pid != p.plugin_id]
        if ctx.state().marketplace_root and not others:
            _run(exe, ["plugin", "marketplace", "remove", p.marketplace])
    else:
        result["kernel"] = "missing"  # clean the config directly below
    cleaned = codexcfg.clean(ctx.home, p.plugin_id, p.marketplace, ctx.cpaths.backup)
    result["config"] = {"removed": len(cleaned.removed), "verified": cleaned.verified, "error": cleaned.error,
                        "backup": cleaned.backup}
    doc = codexcfg.read(ctx.home)
    result["config"]["leftovers"] = codexcfg.leftovers(doc, p.plugin_id, p.marketplace) if doc is not None else None
    for d in (ctx.cache_dir, ctx.plugin_data_dir):
        shutil.rmtree(d, ignore_errors=True)
        if d.exists():
            result.setdefault("left_dirs", []).append(str(d))
    if cleaned.verified:  # config copies may hold secrets; once the config is restored they go
        shutil.rmtree(ctx.cpaths.backup, ignore_errors=True)
        result["config"]["backup"] = None

    unsent = bool(result["remote"]) and not result["remote"].get("sent")
    if purge_local:
        result["kept"] = _purge(ctx, keep_revocation=unsent)
    else:
        result["kept"] = [str(d) for d in (ctx.rpaths.root, ctx.cdkr_root) if d.exists()]
        log.write(ctx.cpaths.log, "setup", "uninstall", verified=int(cleaned.verified))
    if not cleaned.verified or result.get("left_dirs") or result.get("authz_error"):
        result["status"] = FAILED
    return result
