"""The control panel with real Tk widgets and a fake back end (same approach as the recharge window
tests): what it says in each state, and what the buttons do."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from companion import codexcfg, compat, product
from companion.authz import OFF, ON, PAUSED, AuthzError, Status
from companion.status import Snapshot

try:
    import tkinter as tk
    tk.Tk().destroy()
    HAVE_TK = True
except Exception:  # noqa: BLE001
    HAVE_TK = False

P = product.Product(root=Path("."), version="1.0.0")


def snap(state=OFF, trusted=12, enabled=True, installed=True, compat_status="ok", update_rel=None,
         **authz):
    c = compat.Compat(compat_status, "msg-" + compat_status, "C:\\codex.exe", "0.163.0-alpha.10.2", "26.1013.4894.0")
    return Snapshot(
        product=P, compat=c,
        plugin=codexcfg.PluginState(installed=installed, enabled=enabled, hooks_total=12, hooks_trusted=trusted),
        hooks={"trusted": trusted, "untrusted": 12 - trusted} if trusted < 12 else {"trusted": 12},
        authz=Status(state=state, recording=state == ON, uploading=state == ON, consented=state != OFF, **authz),
        update_mode="notify", update_info={}, update_available=update_rel)


class FakeActions:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def collect(self):
        return self.snapshot

    def consent_text(self):
        return "授权说明全文"

    def enable(self, accepted):
        self.calls.append(("enable", accepted))
        self.snapshot = snap(ON)

    def pause(self):
        self.calls.append(("pause",))
        self.snapshot = snap(PAUSED)

    def revoke(self, delete_remote, delete_local):
        self.calls.append(("revoke", delete_remote, delete_local))
        self.snapshot = snap(OFF)
        return {"sent": True, "deleted": "ok" if delete_remote else None}

    def open_recharge(self):
        self.calls.append(("recharge",))

    def recheck(self):
        self.calls.append(("recheck",))
        return self.snapshot

    def check_update(self):
        self.calls.append(("check_update",))
        return {"error": None}

    def set_update_mode(self, value):
        self.calls.append(("mode", value))

    def uninstall(self):
        self.calls.append(("uninstall",))
        return True


def sync_runner(fn, done):
    try:
        done(True, fn())
    except Exception as exc:  # noqa: BLE001
        done(False, exc)


@unittest.skipUnless(HAVE_TK, "needs Tk")
class PanelTest(unittest.TestCase):
    def make(self, snapshot):
        from companion.panel import Panel

        self.actions = FakeActions(snapshot)
        self.panel = Panel(self.actions, run_async=sync_runner)
        self.addCleanup(self.panel.close)
        self.panel.refresh()
        return self.panel

    def text(self, item):
        return self.panel.canvas.itemcget(item, "text")

    def row_values(self):
        return {self.text(lab): self.text(val) for _, lab, val in self.panel.rows}

    def test_first_run_lists_the_next_step(self):
        p = self.make(snap(trusted=0))
        self.assertEqual(self.text(p.headline), "还差 1 步完成设置")
        self.assertIn("全部信任", self.text(p.hint))
        self.assertEqual(self.row_values()["Hook 信任"], "已信任 0/12")
        self.assertEqual(p.primary["text"], "开启任务记录")

    def test_all_ready(self):
        p = self.make(snap(ON, pending_events=3, uploaded=40))
        self.assertEqual(self.text(p.headline), "一切就绪")
        rows = self.row_values()
        self.assertEqual(rows["任务记录"], "● 记录并上传中")
        self.assertIn("待上传 3 条", rows["上传"])
        self.assertEqual(p.primary["text"], "暂停任务记录")

    def test_enable_shows_the_consent_text_first(self):
        p = self.make(snap(OFF))
        p.primary.invoke()
        self.assertEqual(self.actions.calls, [])
        self.assertIsNotNone(p.dialog)
        p.dialog_accept()
        self.assertEqual(self.actions.calls, [("enable", True)])
        self.assertEqual(p.primary["text"], "暂停任务记录")

    def test_cancelling_the_consent_changes_nothing(self):
        p = self.make(snap(OFF))
        p.primary.invoke()
        p.dialog_cancel()
        self.assertEqual(self.actions.calls, [])

    def test_pause_and_resume(self):
        p = self.make(snap(ON))
        p.primary.invoke()
        self.assertEqual(self.actions.calls, [("pause",)])
        self.assertEqual(p.primary["text"], "恢复任务记录")
        p.primary.invoke()
        self.assertEqual(self.actions.calls[-1], ("enable", False))

    def test_revoke_with_remote_deletion(self):
        p = self.make(snap(ON, uploaded=5))
        p.revoke_button.invoke()
        p.revoke_vars[0].set(True)
        p.dialog_accept()
        self.assertEqual(self.actions.calls, [("revoke", True, False)])
        self.assertIn("后台已删除", self.text(p.message))

    def test_errors_are_shown(self):
        p = self.make(snap(OFF))
        self.actions.enable = mock.Mock(side_effect=AuthzError("插件已在 Codex 中禁用：…"))
        p.primary.invoke()
        p.dialog_accept()
        self.assertIn("已在 Codex 中禁用", self.text(p.message))

    def test_disabled_plugin_and_host_block(self):
        p = self.make(snap(ON, enabled=False, host_blocked="plugin_disabled"))
        rows = self.row_values()
        self.assertEqual(rows["插件"], "已在 Codex 中禁用")
        self.assertIn("没有在记录", rows["任务记录"])

    def test_config_wins_over_a_stale_uploader_status(self):
        # The uploader exited idle before the user disabled the plugin: it never wrote host_blocked.
        s = snap(ON, enabled=False)
        s.plugin.reason = "plugin_disabled"
        self.make(s)
        self.assertIn("插件已在 Codex 中禁用", self.row_values()["任务记录"])

    def test_degraded_after_the_plugin_was_switched_off(self):
        p = self.make(snap(OFF, installed=False, compat_status="incompatible"))
        self.assertEqual(self.text(p.headline), "Codex 版本不兼容，已降级运行")
        self.assertEqual(self.text(p.hint), "msg-incompatible")
        self.assertEqual(p.primary["state"], "disabled")
        p.recharge.invoke()  # recharge stays available
        self.assertEqual(self.actions.calls, [("recharge",)])

    def test_incompatible_while_still_registered_offers_the_way_out(self):
        p = self.make(snap(ON, compat_status="incompatible"))
        self.assertEqual(self.text(p.headline), "Codex 版本不兼容")
        self.assertIn("点「重新检测」将停用插件", self.text(p.hint))
        self.assertEqual(p.primary["state"], "normal")  # pausing is always possible
        p.primary.invoke()
        self.assertEqual(self.actions.calls, [("pause",)])

    def test_recheck_repairs(self):
        p = self.make(snap(OFF, installed=False))
        p.refresh_button.invoke()
        self.assertEqual(self.actions.calls, [("recheck",)])

    def test_update_available(self):
        rel = SimpleNamespace(version="1.0.1", requires_retrust=False, notes="")
        p = self.make(snap(ON, update_rel=rel))
        self.assertEqual(p.update_button["text"], "更新到 1.0.1")
        self.assertEqual(self.row_values()["更新"], "有新版本 1.0.1")

    def test_card_grows_with_content(self):
        p = self.make(snap(OFF))
        short = p._height
        p._say("很长的提示。" * 40, "info")
        self.assertGreater(p._height, short)


if __name__ == "__main__":
    unittest.main()
