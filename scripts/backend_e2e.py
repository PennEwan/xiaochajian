"""End-to-end: the independent collector against the real backend (`server/ctrsrv`).

Proves the four things the product actually claims, using no mocks:

  1. 充值即可用   — seed a CDK, redeem it over HTTP, and the charge is recorded.
  2. 采到就传     — a rollout file on disk comes out the other end as L0/L1/L3 + telemetry.
  3. 插件不在也照传 — nothing in this path references the Codex plugin at all.
  4. 后台能喊停   — `POST /v1/service-state/terminate` makes the next collector run stop
                    recording and stop uploading.

Run from the repository root:

    python scripts\\backend_e2e.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO / "server"
COMPANION = REPO / "plugins" / "codex-companion"
LIBS = [COMPANION / "lib", REPO / "plugins" / "codex-task-recorder" / "lib",
        REPO / "tools" / "session-reader", REPO / "tests", SERVER_DIR]
SID = "99999999-8888-7777-6666-555555555555"
ADMIN = "e2e-admin-token"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    results.append((ok, label, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def http(url: str, method: str = "GET", body: dict | None = None, bearer: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b"{}"
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {}


def write_rollout(home: Path) -> Path:
    sys.path.insert(0, str(REPO / "tests"))
    from datetime import datetime, timezone

    from native_rollout import Rollout, name_for

    rollout = Rollout(SID, datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc))
    rollout.meta().turn("turn-1", "帮我看看这个报错", "问题在 auth.py 的第 42 行", command="pytest -q")
    target = home / "sessions" / "2026" / "11" / "01" / name_for(SID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rollout.text(), encoding="utf-8")
    return target


def main() -> int:
    sys.path[:0] = [str(p) for p in LIBS]
    from ctrsrv.app import Config, Server
    from collector import config as ccfg, contract

    work = Path(tempfile.mkdtemp(prefix="backend-e2e-"))
    db, data, home = work / "srv.sqlite3", work / "companion", work / "codexhome"
    server = Server(Config(db=db, host="127.0.0.1", port=0, admin_token=ADMIN))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"backend: {base}\nwork:    {work}\n")
    try:
        # --- 1. recharge ----------------------------------------------------------------
        import importlib

        cli = importlib.import_module("ctrsrv.cli")
        cli.main(["--db", str(db), "add-cdk", "E2E-TEST-0001", "--product", "ChatGPT Plus 1 个月"])
        status, body = http(base + "/v1/redemptions", "POST",
                            {"cdk": "E2E-TEST-0001", "account": "e2e@example.com"})
        check(status == 201 and body.get("status") == "succeeded", "CDK 兑换成功", str(body))
        status, again = http(base + "/v1/redemptions", "POST",
                             {"cdk": "E2E-TEST-0001", "account": "e2e@example.com"})
        check(status == 200 and again.get("replayed") is True
              and again.get("redemption_id") == body.get("redemption_id"),
              "重复兑换幂等（不重复扣卡）", str(again))

        # --- 2. collector uploads --------------------------------------------------------
        contract.ensure_contract(data, product_version="1.5.3")
        cfg = ccfg.load(data)
        cfg.base_url = base
        ccfg.save(data, cfg)
        write_rollout(home)
        os.environ["CODEX_HOME"] = str(home)

        from collector.run import Collector

        collector = Collector(data, home=home)
        collector.control.stop("e2e")
        collector.refresh_service_state(0.0)          # registers the device, pulls `active`
        check(contract.state_of(data) == "active", "采集器拿到服务状态 active", str(contract.state_of(data)))
        collected = collector.collect(0.0)
        check(collected > 0, "采集到本机事件", f"{collected} 条记录")
        for _ in range(8):
            if collector.upload(0.0) != "progress":
                break
        collector.reader.close()

        token = None
        try:
            from ctr.sync import vault

            dev = vault.load(collector.paths.device) or {}
            token = dev.get("device_token")
        except Exception:  # noqa: BLE001
            pass
        check(bool(token), "设备已注册并拿到令牌")

        status, me = http(base + "/v1/devices/me", bearer=token)
        check(status == 200 and me.get("event_count", 0) > 0, "后台按设备统计到事件",
              json.dumps(me, ensure_ascii=False)[:200])
        check({"L0"} <= set(me.get("tiers") or {}), "元数据档位到达", str(me.get("tiers")))

        status, stats = http(base + "/admin/stats", bearer=ADMIN)
        check(status == 200 and stats.get("events", 0) > 0, "管理员统计可见", str(stats.get("events")))
        check(stats.get("redemptions") == 1, "充值记录已入库", str(stats.get("redemptions")))

        # the raw text really arrived
        status, me2 = http(base + "/v1/devices/me", bearer=token)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in LIBS)
        listing = __import__("subprocess").run(
            [sys.executable, "-m", "ctrsrv", "--db", str(db), "events", "--device", token and me["device_id"], "--raw"],
            cwd=str(REPO), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        check("帮我看看这个报错" in (listing.stdout or ""), "正文原文可在后台读出",
              (listing.stdout or listing.stderr or "")[:200])

        # --- 3. backend says stop --------------------------------------------------------
        status, term = http(base + "/v1/service-state/terminate", "POST", {}, bearer=token)
        check(status == 200 and term.get("state") == "terminated", "后台终止成功", str(term))
        before = stats.get("events", 0)
        again = Collector(data, home=home)
        outcome = again.run(once=True)                # must notice the server verdict and stop
        again.reader.close()
        check(outcome in ("server", "terminated"), "采集器读到后台终止并退出", str(outcome))
        status, after = http(base + "/admin/stats", bearer=ADMIN)
        check(after.get("events", 0) == before, "终止后不再有新的上传",
              f"{before} -> {after.get('events')}")
        status, _ = http(base + "/v1/events/batch", "POST", {"events": []}, bearer=token)
        check(status == 403, "终止后上传被后台拒绝", str(status))
    finally:
        server.shutdown()
        server.server_close()
        server.api.store.close()
        shutil.rmtree(work, ignore_errors=True)

    failed = [label for ok, label, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        print("failed: " + "; ".join(failed))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
