"""P4 acceptance: the real installer against the Codex App kernel, in a throwaway CODEX_HOME.

    python packaging\\build.py --version 1.0.0 --profile mock
    python packaging\\build.py --version 1.0.1 --profile mock
    python scripts\\p4_acceptance.py

Everything Codex-side happens in a throwaway CODEX_HOME (D:\\tmp\\p4acc by default) and throwaway
data directories; the bundle itself is installed for real into %LOCALAPPDATA%\\Programs\\CodexCompanion
(and uninstalled again at the end). The mock backends from scripts\\ serve recharge (18787), sync
(18788) and updates (18789) — the ports the `mock` build profile points at.

Not covered here, needs the user in the desktop App (docs/p4-manual-test.md): clicking 「全部信任」
(simulated by writing the hash hooks/list reports — in the throwaway home only), the App's UI.
The throwaway home has no Codex login, so model turns end in an auth error after the hooks ran:
the hook -> record -> upload pipeline is real, the model's answers are not (P3's live test covered
those). The recharge redemption reads the real Codex account (account/read, read-only).

Results: .testdata/p4-acceptance/results.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins" / "codex-task-recorder" / "lib"))

from ctr import kernel  # noqa: E402
from ctr.appserver import AppServer  # noqa: E402

OUT = REPO / ".testdata" / "p4-acceptance"
DIST = REPO / "dist"
PID = "codex-companion@codex-companion"
APP_ID = "{8C5D3A3E-6B7A-4F0E-9D4C-2B1E7A6F9C10}_is1"
INSTALL_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "CodexCompanion"
START_MENU = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "CDK 充值"
APP_PY = INSTALL_DIR / "marketplace" / "plugins" / "codex-companion" / "runtime" / "python.exe"
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
INITIAL_CONFIG = '''model = "mock-model"
model_provider = "mock"
model_reasoning_effort = "low"

[model_providers.mock]
name = "Mock"
base_url = "http://127.0.0.1:18790/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0

[features]
goals = true

[projects.'d:\\codexrouter']
trust_level = "trusted"
'''

RESULTS: dict = {"scenarios": {}}


def record(scenario: str, check: str, ok: bool, evidence=None) -> bool:
    RESULTS["scenarios"].setdefault(scenario, []).append({"check": check, "ok": bool(ok), "evidence": evidence})
    print(f"  [{'PASS' if ok else 'FAIL'}] {scenario}: {check}" + (f" — {evidence}" if evidence is not None else ""),
          flush=True)
    return bool(ok)


def wait(pred, timeout: float, step: float = 0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = pred()
        if value:
            return value
        time.sleep(step)
    return pred()


class Mock:
    def __init__(self, script: str, port: int, *extra: str):
        self.script, self.port, self.extra, self.proc = script, port, extra, None
        self.url = f"http://127.0.0.1:{port}"

    def start(self) -> "Mock":
        log = open(OUT / f"{Path(self.script).stem}.log", "a", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(REPO / "scripts" / self.script), "--port", str(self.port),
                                      *self.extra], stdout=log, stderr=subprocess.STDOUT)
        if not wait(lambda: self.alive(), 15, 0.2):
            raise RuntimeError(f"{self.script} did not start")
        return self

    def alive(self) -> bool:
        try:
            DIRECT.open(self.url + "/_mock/ping", timeout=2)
        except urllib.error.HTTPError:
            return True
        except OSError:
            return False
        return True

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)
            self.proc = None

    def get(self, path: str) -> dict:
        with DIRECT.open(self.url + path, timeout=5) as resp:
            return json.loads(resp.read())

    def post(self, path: str, body) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with DIRECT.open(req, timeout=5) as resp:
            return json.loads(resp.read())


class Env:
    def __init__(self, work: Path):
        self.work = work
        self.home = work / "home"
        self.bed = work / "bed"
        self.vars = {"CODEX_HOME": str(self.home), "COMPANION_DATA_DIR": str(work / "companion"),
                     "CTR_DATA_DIR": str(work / "recorder"), "CDKR_HOME": str(work / "cdkr")}

    @property
    def config(self) -> Path:
        return self.home / "config.toml"

    def companion(self, *args: str, env: dict | None = None, timeout: int = 300) -> tuple[int, str]:
        proc = subprocess.run([str(APP_PY), "-m", "companion", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env or os.environ, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        return proc.returncode, proc.stdout + proc.stderr

    def doc(self) -> dict:
        return tomllib.loads(self.config.read_text(encoding="utf-8"))


def kill_strays(work: Path) -> list[str]:
    """Processes still using the throwaway home — e.g. the `git fetch` of Codex's curated plugin
    marketplace that app-server starts in a fresh CODEX_HOME and that outlives it."""
    needle = str(work).replace("'", "''")
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine.Contains('"
              + needle + "') -and $_.ProcessId -ne $PID } | ForEach-Object { $_.Name; "
              "Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    return proc.stdout.split()


def remove_tree(path: Path) -> None:
    """rmtree that also removes read-only files (git objects of the marketplace clone Codex makes)."""
    import stat

    def clear_and_retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        shutil.rmtree(path, onexc=clear_and_retry)


def run_installer(exe: Path, log: Path, extra_env: dict | None = None) -> int:
    extra_env = dict(extra_env or {})
    args = extra_env.pop("__ARGS", "").split()
    proc = subprocess.run([str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={log}", *args],
                          env={**os.environ, **extra_env}, timeout=900)
    return proc.returncode


def run_uninstaller() -> bool:
    unins = INSTALL_DIR / "unins000.exe"
    if not unins.exists():
        return False
    subprocess.run([str(unins), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300)
    # The uninstaller re-launches itself from %TEMP% and returns at once: wait for its work.
    return bool(wait(lambda: not (INSTALL_DIR / "unins000.exe").exists() and not registry_key(), 180, 1))


def registry_key() -> bool:
    proc = subprocess.run(["reg", "query", rf"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"],
                          capture_output=True)
    return proc.returncode == 0


def our_processes(env: Env) -> list:
    sys.path.insert(0, str(REPO / "plugins" / "codex-companion" / "lib"))
    from companion import procs

    return procs.find([INSTALL_DIR, env.home / "plugins" / "cache" / "codex-companion"])


class Codex:
    """Our own app-server on the App's kernel (same protocol as the desktop App)."""

    def __init__(self, env: Env, exe: Path):
        self.env, self.exe = env, exe
        self.s: AppServer | None = None
        self.thread: str | None = None

    def start(self) -> "Codex":
        self.s = AppServer(self.exe, str(self.env.bed), OUT / "appserver.jsonl")
        self.s.initialize()
        self.thread = None
        return self

    def stop(self) -> None:
        if self.s:
            self.s.close()
            self.s = None

    def hooks(self) -> list[dict]:
        return self.s.plugin_hooks(str(self.env.bed), PID)

    def trust_all(self) -> int:
        """Simulates the user's 「全部信任」 click — throwaway CODEX_HOME only."""
        hooks = self.hooks()
        with open(self.env.config, "a", encoding="utf-8") as f:
            for h in hooks:
                if h["trustStatus"] != "trusted":
                    f.write(f'\n[hooks.state."{h["key"]}"]\ntrusted_hash = "{h["currentHash"]}"\n')
        self.stop()
        self.start()
        return Counter(h["trustStatus"] for h in self.hooks()).get("trusted", 0)

    def set_plugin_enabled(self, enabled: bool) -> dict:
        return self.s.request("config/value/write", {"keyPath": f"plugins.{PID}.enabled", "value": enabled,
                                                     "mergeStrategy": "replace"})

    def turn(self, text: str) -> dict:
        if self.thread is None:
            self.thread = self.s.request("thread/start", {"cwd": str(self.env.bed), "approvalPolicy": "never",
                                                          "sandbox": "danger-full-access"})["thread"]["id"]
        mark = self.s.mark()
        res = self.s.request("turn/start", {"threadId": self.thread,
                                            "input": [{"type": "text", "text": text, "text_elements": []}]})
        turn_id = res["turn"]["id"]
        done = self.s.wait_for(lambda m: m.get("method") == "turn/completed"
                               and m["params"]["turn"]["id"] == turn_id, timeout=180, after=mark)
        hooks = [m["params"]["run"] for m in self.s.notifications[mark:]
                 if m.get("method") == "hook/completed" and "codex-companion" in (m["params"]["run"].get("sourcePath") or "")]
        RESULTS.setdefault("hook_ms", []).extend(
            (h.get("eventName"), h.get("status"), h.get("durationMs")) for h in hooks)
        return {"turn_id": turn_id, "status": done["params"]["turn"].get("status"),
                # app-server names events in camelCase (userPromptSubmit); keep the hooks.json spelling
                "hooks": [(h.get("eventName", "")[:1].upper() + h.get("eventName", "")[1:], h.get("status"),
                           h.get("durationMs")) for h in hooks],
                "entries": [e for h in hooks for e in h.get("entries") or []]}


def local_events(env: Env, turn_id: str | None = None) -> list[dict]:
    out = []
    for f in (env.work / "recorder" / "sessions").glob("*/*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if turn_id is None or e.get("turn_id") == turn_id:
                out.append(e)
    return out


def wait_remote(sync: Mock, codex: "Codex", turn_ids: list[str], timeout: float) -> bool:
    """Wait until the backend has events of every turn. Keeps using Codex meanwhile, as a user
    would: the uploader is started by hooks at most every 30 s."""
    deadline, nudged = time.time() + timeout, time.time()
    while time.time() < deadline:
        if all(remote_events(sync, t) for t in turn_ids):
            return True
        if time.time() - nudged > 31:
            codex.turn("p4 acceptance: keep using codex")
            nudged = time.time()
        time.sleep(1)
    return all(remote_events(sync, t) for t in turn_ids)


def remote_events(sync: Mock, turn_id: str | None = None) -> list[dict]:
    return [e for e in sync.get("/_mock/events")["events"] if turn_id is None or e.get("turn_id") == turn_id]


def sync_status(env: Env) -> dict:
    try:
        return json.loads((env.work / "recorder" / "sync" / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def worker_running(env: Env) -> bool:
    sys.path.insert(0, str(REPO / "plugins" / "codex-task-recorder" / "lib"))
    from ctr import store

    return store.is_held(env.work / "recorder" / "sync" / "worker.lock")


def product_version(path: Path) -> str | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def redeem_script(cdk: str) -> str:
    return ("import json\nfrom cdkr import account, config, flow\n"
            "paths = config.Paths.default(); cfg = config.load(paths)\n"
            "r = flow.Recharge(paths, cfg, account.read_account)\n"
            "v = r.load()\n"
            "if v.state in (flow.NEED_BIND, flow.ACCOUNT_CHANGED): v = r.bind()\n"
            f"o = r.redeem({cdk!r})\n"
            "print(json.dumps({'view': v.state, 'kind': o.kind, 'ok': o.ok, 'message': o.message}, ensure_ascii=False))\n")


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=r"D:\tmp\p4acc")
    ap.add_argument("--keep", action="store_true", help="leave the bundle installed at the end")
    args = ap.parse_args()
    setup_100, setup_101 = DIST / "CodexCompanion-Setup-1.0.0.exe", DIST / "CodexCompanion-Setup-1.0.1.exe"
    for f in (setup_100, setup_101):
        if not f.exists():
            print(f"missing {f}: build it first (see the docstring)")
            return 2
    if INSTALL_DIR.exists():
        print(f"{INSTALL_DIR} exists: uninstall CDK 充值 first")
        return 2
    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("*"):
        f.unlink()
    env = Env(Path(args.work))
    kill_strays(env.work)
    time.sleep(1)
    remove_tree(env.work)
    env.home.mkdir(parents=True)
    env.bed.mkdir()
    env.config.write_text(INITIAL_CONFIG, encoding="utf-8")
    os.environ.update(env.vars)  # app-server, its hooks and every child see the throwaway dirs
    os.environ.update(NO_PROXY="127.0.0.1,localhost", PYTHONIOENCODING="utf-8")
    exe, version = kernel.find_app_kernel()
    RESULTS.update(kernel=version, started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    print("kernel:", exe, version)
    recharge = Mock("mock_recharge_server.py", 18787).start()
    sync = Mock("mock_sync_server.py", 18788, "--store", str(OUT / "sync-store.json")).start()
    upd = Mock("mock_update_server.py", 18789).start()
    model = Mock("mock_model_server.py", 18790, "--tool-call").start()
    codex = Codex(env, exe)
    try:
        scenarios(env, codex, recharge, sync, upd, setup_100, setup_101, args.keep)
    finally:
        codex.stop()
        for m in (recharge, sync, upd, model):
            m.stop()
        RESULTS["strays_killed"] = kill_strays(env.work)
        RESULTS["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        failed = [(s, c["check"]) for s, checks in RESULTS["scenarios"].items() for c in checks if not c["ok"]]
        RESULTS["failed"] = failed
        (OUT / "results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n{sum(len(c) for c in RESULTS['scenarios'].values()) - len(failed)} passed, {len(failed)} failed")
        print(OUT / "results.json")
    return 1 if RESULTS["failed"] else 0


def scenarios(env, codex, recharge, sync, upd, setup_100, setup_101, keep) -> None:
    S = "1 全新安装"
    before = env.config.read_text(encoding="utf-8")
    t0 = time.time()
    code = run_installer(setup_100, OUT / "install-1.0.0.log")
    record(S, "静默安装退出码 0", code == 0, f"{code}，{time.time() - t0:.0f} s")
    record(S, "安装目录与私有运行时", APP_PY.exists())
    record(S, "「应用和功能」卸载登记", registry_key())
    record(S, "开始菜单快捷方式", sorted(p.name for p in START_MENU.glob("*.lnk")),
           sorted(p.name for p in START_MENU.glob("*.lnk")))
    doc = env.doc()
    record(S, "Codex 配置：市场与插件（由 Codex 自己的命令写入）",
           "codex-companion" in doc.get("marketplaces", {}) and doc.get("plugins", {}).get(PID, {}).get("enabled"))
    record(S, "没有写入任何信任项", not [k for k in doc.get("hooks", {}).get("state", {}) if k.startswith(PID)])
    codex.start()
    trust = Counter(h["trustStatus"] for h in codex.hooks())
    record(S, "Codex 加载 12 个 Hook，待用户信任", trust == Counter({"untrusted": 12}), dict(trust))
    record(S, "Hook 命令使用插件自带的 Python",
           all("\\runtime\\python.exe" in (h.get("command") or "") for h in codex.hooks()))
    record(S, "充值与同步后台地址已写入", json.loads((env.work / "cdkr" / "config.json").read_text())["base_url"]
           == recharge.url and json.loads((env.work / "recorder" / "sync" / "config.json").read_text())["base_url"]
           == sync.url)
    record(S, "模拟用户点「全部信任」后 12/12 trusted", codex.trust_all() == 12)

    S = "2 充值成功"
    turn = codex.turn("/充值")
    record(S, "/充值 被 Hook 拦截，未发给模型", ("UserPromptSubmit", "blocked") in [(e, s) for e, s, _ in turn["hooks"]]
           and turn["status"] == "completed", turn["hooks"])
    window = wait(lambda: [p for p in our_processes(env) if p[1].lower().endswith("pythonw.exe")], 10)
    record(S, "充值窗口进程已启动（插件自带运行时）", bool(window), window)
    sys.path.insert(0, str(REPO / "plugins" / "codex-companion" / "lib"))
    from companion import procs
    for pid, _ in window or []:
        procs.terminate(pid)
    real_home_env = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}  # account/read: the real login
    outcome = None
    for cdk in ("TEST-OK01", "TEST-OK02", "TEST-OK03", "TEST-OK04"):
        proc = subprocess.run([str(APP_PY), "-c", redeem_script(cdk)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=real_home_env, timeout=120)
        try:
            outcome = json.loads(proc.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            outcome = {"error": proc.stderr[-500:]}
        if outcome.get("kind") not in ("replayed", "cdk_used"):  # a CDK this account used before: next
            break
    record(S, "真实账号（account/read）绑定后兑换成功（模拟后台）", outcome and outcome.get("kind") == "succeeded",
           outcome and outcome.get("message"))
    record(S, "CDK 没有进入本机记录与日志", not any("TEST-OK" in p.read_text(encoding="utf-8", errors="replace")
                                              for p in env.work.rglob("*") if p.is_file() and p.suffix in (".jsonl", ".log")))

    S = "3 授权开启与关闭"
    code, out = env.companion("authz", "enable", "--i-consent")
    record(S, "同意后开启（记录 + 上传）", code == 0 and '"state": "on"' in out, out.strip()[-160:])
    turn = codex.turn("p4 acceptance: hello")
    notice = [e.get("text") for e in turn["entries"] if "CDK 充值正在记录" in (e.get("text") or "")]
    record(S, "Codex 中显示记录提示", bool(notice), notice[:1])
    got = wait(lambda: [e for e in remote_events(sync, turn["turn_id"]) if e["kind"] == "turn_coverage"], 40, 1)
    kinds = Counter(e["kind"] for e in remote_events(sync, turn["turn_id"]))
    record(S, "回合事件上传到后台（提示、工具调用与结果、回复、回合结束、覆盖）",
           bool(got) and all(kinds.get(k) for k in ("user_prompt", "tool_call", "tool_result", "assistant_message",
                                                    "turn_end", "turn_coverage")), dict(kinds))
    record(S, "回合状态 completed（模拟模型，真实执行了一条命令）", turn["status"] == "completed", turn["status"])
    code, out = env.companion("authz", "pause")
    turn = codex.turn("p4 acceptance: while paused")
    time.sleep(4)
    record(S, "暂停后：本机不记录", not local_events(env, turn["turn_id"]))
    record(S, "暂停后：后台无新事件", not remote_events(sync, turn["turn_id"]))
    code, out = env.companion("authz", "resume")
    turn = codex.turn("p4 acceptance: resumed")
    record(S, "恢复后继续上传（不需重新同意）", bool(wait(lambda: remote_events(sync, turn["turn_id"]), 30, 1)))

    S = "4 插件关闭、重启"
    sync.stop()  # queue something that the uploader will still want to send
    queued = codex.turn("p4 acceptance: queued before disabling")
    record(S, "后台不可用时内容留在本机", bool(wait(lambda: local_events(env, queued["turn_id"]), 15, 1)))
    codex.set_plugin_enabled(False)
    sync.start()
    codex.stop()
    codex.start()
    record(S, "在 Codex 中禁用插件后 Hook 不再加载", codex.hooks() == [])
    stopped = wait(lambda: not worker_running(env), 15, 0.5)
    record(S, "上传进程自行停止", stopped, sync_status(env).get("host_blocked"))
    time.sleep(3)
    record(S, "禁用期间不上传已排队内容", not remote_events(sync, queued["turn_id"]))
    turn = codex.turn("p4 acceptance: while disabled")
    time.sleep(3)
    record(S, "禁用期间不记录", not local_events(env, turn["turn_id"]))
    code, out = env.companion("authz", "resume")
    record(S, "禁用时面板 / 命令行拒绝开启并说明原因", code != 0 and "禁用" in out, out.strip()[-120:])
    codex.stop()  # an automatic update while the plugin is off (Codex's `plugin add` would switch it on)
    code = run_installer(setup_100, OUT / "update-while-disabled.log", {"__ARGS": "/UPDATE=auto"})
    log_text = (OUT / "update-while-disabled.log").read_text(encoding="utf-8", errors="replace")
    record(S, "插件禁用期间自动更新：仍保持禁用、不误判为不兼容",
           # setup install exits 0 only for ok / untested (2 for incompatible or failed)
           code == 0 and env.doc()["plugins"][PID]["enabled"] is False
           and "setup install" in log_text and "-> started=1 exit=0" in log_text, env.doc()["plugins"][PID])
    codex.start()
    record(S, "更新后禁用的插件仍不加载 Hook", codex.hooks() == [])
    codex.set_plugin_enabled(True)
    codex.stop()
    codex.start()
    record(S, "重新启用后 Hook 恢复且仍为 trusted",
           Counter(h["trustStatus"] for h in codex.hooks()) == Counter({"trusted": 12}))
    turn = codex.turn("p4 acceptance: re-enabled")
    record(S, "重新启用后排队内容与新内容都上传（继续使用 Codex 即恢复）",
           wait_remote(sync, codex, [queued["turn_id"], turn["turn_id"]], 100))

    S = "5 Codex 重启"
    worker_before = worker_running(env)
    codex.stop()
    codex.start()
    record(S, "重启后 Hook 仍为 trusted", Counter(h["trustStatus"] for h in codex.hooks()) == Counter({"trusted": 12}))
    turn = codex.turn("p4 acceptance: after restart")
    record(S, "重启后继续记录并上传", bool(wait(lambda: remote_events(sync, turn["turn_id"]), 30, 1)),
           {"worker_alive_across_restart": worker_before})

    S = "6 网络异常"
    sync.stop()
    turn = codex.turn("p4 acceptance: offline")
    wait(lambda: sync_status(env).get("last_error"), 20, 1)
    record(S, "断网：内容留在本机，状态显示未发出", bool(local_events(env, turn["turn_id"])),
           (sync_status(env).get("last_error") or {}).get("kind"))
    sync.start()
    sync.post("/_mock/fail-next", {"mode": "drop_after_store", "count": 1})
    turn2 = codex.turn("p4 acceptance: back online")  # the hook starts the uploader again if it gave up
    ok = wait_remote(sync, codex, [turn["turn_id"], turn2["turn_id"]], 180)
    ids = [e["event_id"] for e in remote_events(sync)]
    record(S, "恢复后补传，响应丢失也不重复", bool(ok) and len(ids) == len(set(ids)),
           {"remote": len(ids), "unique": len(set(ids))})
    upd.stop()
    code, out = env.companion("update", "check")
    record(S, "更新服务不可用：检查失败但不报错退出", code == 1 and '"error": "network"' in out)
    upd.start()
    recharge.stop()
    proc = subprocess.run([str(APP_PY), "-c", redeem_script("TEST-OK05")], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env={k: v for k, v in os.environ.items()
                                                                     if k != "CODEX_HOME"}, timeout=120)
    out = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else proc.stderr[-300:]
    record(S, "充值后台不可用：提示请求未发出、CDK 未使用", "not_sent" in out and "未使用" in out, out[-160:])
    recharge.start()

    S = "7 版本不兼容"
    fake = env.work / "old-codex.cmd"
    fake.write_text(f'@"{sys.executable}" "{REPO / "tests" / "fake_codex.py"}" %*\n', encoding="ascii")
    old = {**os.environ, "CTR_CODEX_BIN": str(fake), "FAKE_CODEX_VERSION": "0.150.0"}
    code, out = env.companion("setup", "install", "--result", str(OUT / "incompatible.json"), env=old)
    res = json.loads((OUT / "incompatible.json").read_text(encoding="utf-8"))
    record(S, "旧内核被判为不兼容并给出降级提示", res["status"] == "incompatible", res.get("message"))
    record(S, "不兼容时插件被停用（Hook 不再运行）", PID not in env.doc().get("plugins", {}))
    code, out = env.companion("compat", env=old)
    record(S, "控制面板 / 命令行显示同样的降级提示", code == 2 and "不可用" in out)
    newer = {**old, "FAKE_CODEX_VERSION": "0.170.0"}
    code, out = env.companion("compat", env=newer)
    record(S, "未验证的新版本：警告但可用", code == 0 and "未经验证" in out)
    code, out = env.companion("setup", "install")
    codex.stop()
    codex.start()
    record(S, "换回兼容内核后重新检测即恢复", Counter(h["trustStatus"] for h in codex.hooks()) == Counter({"trusted": 12}))

    S = "8 更新"
    upd.post("/_mock/release", {"version": "1.0.1", "path": str(setup_101), "tamper": True})
    env.companion("update", "check")
    code, out = env.companion("update", "download")
    record(S, "被篡改的安装包校验失败、不会执行", code == 1 and "hash_mismatch" in out)
    upd.post("/_mock/release", {"version": "1.0.1", "path": str(setup_101)})
    code, out = env.companion("update", "check")
    record(S, "发现新版本 1.0.1", code == 0 and '"version": "1.0.1"' in out)
    code, out = env.companion("update", "install", "--silent")
    plugin_product = INSTALL_DIR / "marketplace" / "plugins" / "codex-companion" / "product.json"
    cache_product = env.home / "plugins" / "cache" / "codex-companion" / "codex-companion" / "1.0.0" / "product.json"
    ok = wait(lambda: product_version(plugin_product) == "1.0.1" and product_version(cache_product) == "1.0.1", 300, 2)
    record(S, "用户确认后静默安装：安装目录与插件缓存（原地刷新）都是 1.0.1", bool(ok),
           {"install": product_version(plugin_product), "cache": product_version(cache_product)})
    time.sleep(5)
    codex.stop()
    codex.start()
    record(S, "更新后 Hook 仍为 trusted（不需重新信任）",
           Counter(h["trustStatus"] for h in codex.hooks()) == Counter({"trusted": 12}))
    turn = codex.turn("p4 acceptance: after update")
    record(S, "更新后授权保持，继续记录并上传", bool(wait(lambda: remote_events(sync, turn["turn_id"]), 30, 1)))
    for pid, image in our_processes(env):  # the update reopened the panel
        if image.lower().endswith("pythonw.exe"):
            procs.terminate(pid)

    S = "9 插件卸载与恢复"
    codex.stop()
    ok = run_uninstaller()
    record(S, "静默卸载完成", ok)
    after = env.config.read_text(encoding="utf-8")
    record(S, "Codex 配置还原为安装前（解析后完全一致）", tomllib.loads(after) == tomllib.loads(before),
           "逐字节一致" if after.strip() == before.strip() else after[-600:])
    record(S, "无残留进程", our_processes(env) == [])
    record(S, "无残留目录（安装目录、插件缓存）", not INSTALL_DIR.exists()
           and not (env.home / "plugins" / "cache" / "codex-companion").exists())
    record(S, "无残留登记与开始菜单", not registry_key() and not START_MENU.exists())
    record(S, "未选择删除时本机数据保留", (env.work / "recorder" / "sessions").exists())
    record(S, "卸载后不再采集（上传进程未运行）", not worker_running(env))
    code = run_installer(setup_100, OUT / "reinstall.log")
    codex.start()
    trust = Counter(h["trustStatus"] for h in codex.hooks())
    record(S, "重新安装即恢复（需要重新信任）", code == 0 and trust == Counter({"untrusted": 12}), dict(trust))
    if not keep:
        codex.stop()
        env.companion("setup", "uninstall", "--purge-local", "--delete-remote")
        run_uninstaller()
        record(S, "清理：卸载并删除数据后配置仍与安装前一致",
               tomllib.loads(env.config.read_text(encoding="utf-8")) == tomllib.loads(before))


if __name__ == "__main__":
    raise SystemExit(main())
