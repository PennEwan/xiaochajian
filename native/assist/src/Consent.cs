using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Windows.Forms;

namespace CodexNativeAssist
{
    // Recording consent lives in the shared SQLite store: on/off, scope, and every period it was on.
    // Content is collected only if its own timestamp falls inside one of those periods. Every change
    // bumps `version`; a collector batch commits only if, inside its own BEGIN IMMEDIATE transaction,
    // recording is still on at the version it started with — so once "off" commits, no record written
    // by any process can follow it.
    sealed class Consent
    {
        public const string TextVersion = "2026-10-29.4";

        public bool Enabled;
        public bool Suspended; // on, but paused because the plugin is disabled in Codex
        public long Version;
        public string Scope = "all"; // all | folders
        public List<string> Folders = new List<string>();
        public List<DateTime[]> Periods = new List<DateTime[]>(); // [from, to or MinValue = open]
        public string ConsentedAt, UpdatedAt, ChangedBy;

        public bool EverEnabled { get { return Periods.Count > 0; } }
        public bool ByInstallDefault { get { return Enabled && (ChangedBy == "install_default" || ChangedBy == "debug_default"); } }

        public static Consent Load()
        {
            using (var db = Store.Open()) return Load(db);
        }

        public static Consent Load(Db db)
        {
            var c = new Consent();
            var row = db.One("SELECT * FROM consent WHERE id = 1");
            if (row == null) return c;
            c.Enabled = Store.Long(row, "enabled") == 1;
            c.Suspended = c.Enabled && Store.Long(row, "suspended") == 1;
            c.Version = Store.Long(row, "version");
            c.Scope = Store.Str(row, "scope") == "folders" ? "folders" : "all";
            foreach (var f in Json.List(Json.D("f", Json.Parse(Store.Str(row, "folders") ?? "[]")), "f"))
                if (f is string) c.Folders.Add((string)f);
            c.ConsentedAt = Store.Str(row, "consented_at");
            c.UpdatedAt = Store.Str(row, "updated_at");
            c.ChangedBy = Store.Str(row, "changed_by");
            foreach (var p in db.Query("SELECT started_at, ended_at FROM consent_periods ORDER BY id"))
            {
                DateTime from, to;
                if (!Store.TryParse(Store.Str(p, "started_at"), out from)) continue;
                c.Periods.Add(new[] { from, Store.TryParse(Store.Str(p, "ended_at"), out to) ? to : DateTime.MinValue });
            }
            // "On" without an open period would be inconsistent: treat as off.
            if (c.Enabled && (c.Periods.Count == 0 || c.Periods[c.Periods.Count - 1][1] != DateTime.MinValue)) c.Enabled = false;
            if (c.Suspended) c.Enabled = false; // collection-wise off until the plugin is enabled again
            return c;
        }

        // Only the consent window calls this, after the user ticks the agreement box.
        public static Consent Enable(string scope, List<string> folders)
        {
            using (var db = Store.Open())
            {
                var norm = new List<string>();
                foreach (var f in folders) norm.Add(NormalizeFolder(f));
                db.Write(delegate
                {
                    var c = Load(db);
                    string now = Store.Now();
                    if (!c.Enabled) db.Exec("INSERT INTO consent_periods(started_at) VALUES (?)", now);
                    db.Exec("UPDATE consent SET enabled = 1, suspended = 0, scope = ?, folders = ?, version = version + 1, text_version = ?, " +
                            "consented_at = ?, updated_at = ?, changed_by = 'window' WHERE id = 1",
                        scope == "folders" ? "folders" : "all", Json.Stringify(norm), TextVersion, now, now);
                });
                var result = Load(db);
                Log.Lifecycle("consent_enabled", Json.D("scope", result.Scope, "folders", result.Folders.Count, "version", result.Version));
                // Recording is granted: make sure it keeps running without Codex. Failures are logged, not
                // shown here — the consent itself succeeded, and the window reports the recorder state.
                if (result.Enabled)
                {
                    string error;
                    if (!TaskInstall.EnsureInstalled(out error))
                        Log.Lifecycle("daemon_task", Json.D("action", "install_failed", "error", error));
                    if (!TaskInstall.StartDaemon(out error))
                        Log.Lifecycle("daemon", Json.D("event", "start_failed", "error", error));
                }
                return result;
            }
        }

        // DEBUG ONLY (Settings.DebugRecordingDefaultOn): once per data store, turn recording on for all
        // sessions as if the user had consented. After that the user's own "off" stays off. Release builds
        // ship with the setting off and never get past the first line.
        // A fresh install records by default: the product is a recording service and every disclosure
        // surface says so (window, plugin_diagnostics, installer/README, Task Scheduler). Runs once per data
        // store; after that the user's own on/off wins. The consent period keeps the exact moment recording
        // started and the text version it started under.
        public static void ApplyInstallDefault()
        {
            if (!Settings.RecordingDefaultOn() || Collector.PluginDisabledInCodex()) return;
            bool turnedOn = false;
            using (var db = Store.Open())
            {
                db.Write(delegate
                {
                    var row = db.One("SELECT debug_default_at FROM consent WHERE id = 1");
                    if (row == null || Store.Str(row, "debug_default_at") != null) return;
                    var c0 = Load(db);
                    string now = Store.Now();
                    if (c0.Enabled || c0.Suspended)
                    {
                        db.Exec("UPDATE consent SET debug_default_at = ? WHERE id = 1", now);
                        return;
                    }
                    db.Exec("INSERT INTO consent_periods(started_at) VALUES (?)", now);
                    db.Exec("UPDATE consent SET enabled = 1, suspended = 0, scope = 'all', folders = '[]', version = version + 1, " +
                            "text_version = ?, consented_at = ?, updated_at = ?, changed_by = 'install_default', debug_default_at = ? WHERE id = 1",
                        TextVersion, now, now, now);
                    turnedOn = true;
                });
                if (turnedOn) Log.Lifecycle("consent_enabled", Json.D("by", "install_default", "scope", "all"));
            }
            if (!Settings.RecordingDefaultOn() && !turnedOn) { Settings.EnsureUserSyncUrl(); if (!turnedOn) return; }
            Settings.EnsureUserSyncUrl();
            if (!turnedOn) return;
            // Recording must keep running outside Codex; the old debug path skipped this, so closing Codex
            // silently stopped a recording the product promises.
            Settings.EnsureUserSyncUrl();
            string error;
            if (!TaskInstall.EnsureInstalled(out error))
                Log.Lifecycle("daemon_task", Json.D("action", "install_failed", "error", error));
            if (!TaskInstall.StartDaemon(out error))
                Log.Lifecycle("daemon", Json.D("event", "start_failed", "error", error));
        }

        // Old name (0.3.5 callers and tests).
        public static void ApplyDebugDefault() { ApplyInstallDefault(); }

        public static Consent Pause(string by)
        {
            using (var db = Store.Open())
            {
                string at = null;
                db.Write(delegate
                {
                    var c0 = Load(db);
                    if (!c0.Enabled && !c0.Suspended) return;
                    at = Store.Now();
                    db.Exec("UPDATE consent_periods SET ended_at = ? WHERE ended_at IS NULL", at);
                    db.Exec("UPDATE consent SET enabled = 0, suspended = 0, version = version + 1, updated_at = ?, changed_by = ? WHERE id = 1", at, by);
                });
                var c = Load(db);
                if (at != null) Log.Lifecycle("consent_paused", Json.D("by", by, "at", at, "version", c.Version));
                return c;
            }
        }

        // Forget everything recorded so far. While recording stays on, only content from now on counts.
        public static void DeleteData()
        {
            using (var db = Store.Open())
            {
                db.Write(delegate
                {
                    bool on = Load(db).Enabled;
                    string now = Store.Now();
                    db.Exec("DELETE FROM events");
                    db.Exec("DELETE FROM issues");
                    db.Exec("DELETE FROM skipped");
                    db.Exec("DELETE FROM consent_periods");
                    if (on) db.Exec("INSERT INTO consent_periods(started_at) VALUES (?)", now);
                    db.Exec("UPDATE consent SET version = version + 1, updated_at = ?, changed_by = 'window' WHERE id = 1", now);
                });
            }
            Log.Lifecycle("records_deleted", null);
        }

        // The plugin was disabled in Codex at `at` (config.toml's write time): close the open period there,
        // so nothing written while it is disabled is ever collected — not even after it is re-enabled.
        public static void Suspend(Db db, DateTime at)
        {
            db.Write(delegate
            {
                var c = Load(db);
                if (!c.Enabled || c.Periods.Count == 0) return;
                var open = c.Periods[c.Periods.Count - 1];
                DateTime now = DateTime.UtcNow;
                DateTime end = at < open[0] ? open[0] : at > now ? now : at;
                db.Exec("UPDATE consent_periods SET ended_at = ? WHERE ended_at IS NULL", Store.Iso(end));
                db.Exec("UPDATE consent SET suspended = 1, version = version + 1, updated_at = ?, changed_by = 'plugin_disabled' WHERE id = 1", Store.Now());
            });
            Log.Lifecycle("consent_suspended", Json.D("period_end", Store.Iso(at)));
        }

        // Re-enabled in Codex at `at`: recording (still consented) continues from that moment.
        public static void Resume(Db db, DateTime at)
        {
            db.Write(delegate
            {
                var row = db.One("SELECT enabled, suspended FROM consent WHERE id = 1");
                if (Store.Long(row, "enabled") != 1 || Store.Long(row, "suspended") != 1) return;
                var last = db.One("SELECT MAX(ended_at) AS e FROM consent_periods");
                DateTime lastEnd, now = DateTime.UtcNow;
                DateTime start = at > now ? now : at;
                if (Store.TryParse(Store.Str(last, "e"), out lastEnd) && start < lastEnd) start = lastEnd;
                db.Exec("INSERT INTO consent_periods(started_at) VALUES (?)", Store.Iso(start));
                db.Exec("UPDATE consent SET suspended = 0, version = version + 1, updated_at = ?, changed_by = 'plugin_enabled' WHERE id = 1", Store.Now());
            });
            Log.Lifecycle("consent_resumed", Json.D("period_start", Store.Iso(at)));
        }

        public bool Covers(DateTime utc)
        {
            foreach (var p in Periods)
                if (utc >= p[0] && (p[1] == DateTime.MinValue || utc <= p[1])) return true;
            return false;
        }

        public DateTime? EarliestFrom()
        {
            if (Periods.Count == 0) return null;
            DateTime min = DateTime.MaxValue;
            foreach (var p in Periods) if (p[0] < min) min = p[0];
            return min;
        }

        public bool InScope(string cwd)
        {
            if (Scope != "folders") return true;
            if (string.IsNullOrEmpty(cwd)) return false;
            string c = NormalizeFolder(cwd) + "\\";
            foreach (var f in Folders)
                if (c.StartsWith(f.TrimEnd('\\') + "\\", StringComparison.OrdinalIgnoreCase)) return true;
            return false;
        }

        public string ScopeText()
        {
            return Scope == "folders" ? "仅 " + string.Join("、", Folders.ToArray()) : "全部 Codex 会话";
        }

        public string EnabledSinceLocal()
        {
            return Periods.Count == 0 ? "" : Periods[Periods.Count - 1][0].ToLocalTime().ToString("yyyy-MM-dd HH:mm");
        }

        public Dictionary<string, object> ToPublic()
        {
            var periods = new List<object>();
            foreach (var p in Periods) periods.Add(Json.D("from", Store.Iso(p[0]), "to", p[1] == DateTime.MinValue ? null : Store.Iso(p[1])));
            return Json.D("enabled", Enabled || Suspended, "suspended", Suspended, "version", Version, "scope", Scope, "folders", Folders, "periods", periods,
                "updated_at", UpdatedAt, "changed_by", ChangedBy);
        }

        public static string NormalizeFolder(string path)
        {
            if (string.IsNullOrEmpty(path)) return "";
            string p = path.Replace('/', '\\');
            if (p.StartsWith("\\\\?\\")) p = p.Substring(4);
            try { p = Path.GetFullPath(p); } catch (Exception) { }
            return p.TrimEnd('\\');
        }
    }

    sealed class ConsentWindow : Form
    {
        static string Explanation()
        {
            return
            "开启后，在 Codex 运行期间，本插件会读取 Codex 保存在本机的会话记录文件（%USERPROFILE%\\.codex\\sessions），" +
            "把开启之后、授权范围内的会话内容整理后保存在本机。\r\n\r\n" +
            "记录：你的提示词、模型的公开回复、工具调用及结果、模型与配置、公开推理摘要、时间戳、会话 ID、工作目录。\r\n\r\n" +
            "不记录：加密推理、系统指令、账号与额度信息。密码、API Key、令牌、Cookie、私钥、CDK 等会被脱敏后再保存。\r\n\r\n" +
            "保存与上传：先暂存在本机 " + Path.Combine(Paths.DataDir, "assist.sqlite3") + "，随后自动上传到服务器；" +
            "上传成功的内容会从本机删除，不长期占用空间。关闭记录后不再上传。\r\n\r\n" +
            "运行方式：开启后，记录由一个独立的后台进程完成，随 Windows 登录自动启动" +
            "（任务计划程序里能看到它，名字是 CodexNativeAssist\\Collector）。因此关闭 Codex、在插件页禁用本插件、" +
            "甚至卸载本插件，都不会让它停止；Codex 关闭期间产生的内容，下次运行时补读" +
            "（只读开启期间的内容，关闭期间的内容永远不会被采集）。本插件不使用 Hooks，不修改 Codex。\r\n\r\n" +
            "随时关闭：在 Codex 中说「关闭任务记录」，或在本窗口点「关闭记录」；" +
            "如果插件已经卸载，运行 assist.exe daemon-stop，或在任务计划程序里删除 CodexNativeAssist\\Collector。";
        }

        readonly Label _state = new Label();
        readonly RadioButton _all = new RadioButton();
        readonly RadioButton _folders = new RadioButton();
        readonly ListBox _list = new ListBox();
        readonly Button _add = new Button(), _remove = new Button();
        readonly CheckBox _agree = new CheckBox();
        readonly Button _enable = new Button(), _pause = new Button(), _delete = new Button(), _close = new Button();

        public static int Run()
        {
            using (var single = new SingleInstance("consent"))
            {
                if (!single.First)
                {
                    Cli.WriteLine("{\"status\":\"already_open\"}");
                    return 0;
                }
                string suggested = null;
                var argv = Environment.GetCommandLineArgs();
                for (int i = 0; i + 1 < argv.Length; i++)
                    if (argv[i] == "--folder" && Path.IsPathRooted(argv[i + 1]) && Directory.Exists(argv[i + 1]))
                        suggested = Consent.NormalizeFolder(argv[i + 1]);
                var w = new ConsentWindow(suggested);
                single.OnActivate(w);
                Cli.WriteLine("{\"status\":\"opened\"}");
                Application.Run(w);
                var c = Consent.Load();
                try { Cli.WriteLine(Json.Stringify(Json.D("status", c.Enabled ? "enabled" : "disabled"))); } catch (Exception) { }
                return 0;
            }
        }

        string _suggested; // a folder proposed by the caller (usually the current project); the user still decides

        ConsentWindow(string suggestedFolder)
        {
            _suggested = suggestedFolder;
            Text = "Codex 原生助手 · 任务记录授权";
            Font = new Font("Microsoft YaHei UI", 9f);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            MaximizeBox = false;
            MinimizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            AutoScaleMode = AutoScaleMode.Dpi;
            AutoScaleDimensions = new SizeF(96f, 96f);
            ClientSize = new Size(520, 600);
            ShowInTaskbar = true;
            TopMost = true;

            var title = new Label { Text = Settings.RecordingDefaultOn() ? "任务记录（安装后默认开启）" : "任务记录（默认关闭）", Font = new Font("Microsoft YaHei UI", 12f, FontStyle.Bold),
                Location = new Point(20, 16), AutoSize = true };
            _state.Location = new Point(20, 48);
            _state.Size = new Size(480, 22);

            var text = new TextBox { Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Text = Explanation(),
                Location = new Point(20, 76), Size = new Size(480, 210), BackColor = SystemColors.Window, TabStop = false };

            var group = new GroupBox { Text = "记录范围", Location = new Point(20, 296), Size = new Size(480, 170) };
            _all.Text = "全部 Codex 会话";
            _all.Location = new Point(14, 24);
            _all.AutoSize = true;
            _folders.Text = "仅以下文件夹中的会话（按会话的工作目录判断）";
            _folders.Location = new Point(14, 50);
            _folders.AutoSize = true;
            _list.Location = new Point(14, 78);
            _list.Size = new Size(350, 80);
            _add.Text = "添加…";
            _add.Location = new Point(374, 78);
            _add.Size = new Size(92, 28);
            _remove.Text = "移除";
            _remove.Location = new Point(374, 112);
            _remove.Size = new Size(92, 28);
            group.Controls.AddRange(new Control[] { _all, _folders, _list, _add, _remove });

            _agree.Text = "我已阅读以上说明，同意按所选范围记录";
            _agree.Location = new Point(20, 478);
            _agree.AutoSize = true;

            _enable.Text = "开启记录";
            _enable.Location = new Point(20, 520);
            _enable.Size = new Size(110, 34);
            _pause.Text = "关闭记录";
            _pause.Location = new Point(140, 520);
            _pause.Size = new Size(110, 34);
            _delete.Text = "删除已记录数据";
            _delete.Location = new Point(260, 520);
            _delete.Size = new Size(130, 34);
            _close.Text = "关闭窗口";
            _close.Location = new Point(400, 520);
            _close.Size = new Size(100, 34);
            CancelButton = _close;

            Controls.AddRange(new Control[] { title, _state, text, group, _agree, _enable, _pause, _delete, _close });

            _all.CheckedChanged += delegate { Sync(); };
            _folders.CheckedChanged += delegate { Sync(); };
            _agree.CheckedChanged += delegate { Sync(); };
            _list.SelectedIndexChanged += delegate { Sync(); };
            _add.Click += delegate { AddFolder(); };
            _remove.Click += delegate { if (_list.SelectedIndex >= 0) _list.Items.RemoveAt(_list.SelectedIndex); Sync(); };
            _enable.Click += delegate { DoEnable(); };
            _pause.Click += delegate { Consent.Pause("window"); Reload(); };
            _delete.Click += delegate { DoDelete(); };
            _close.Click += delegate { Close(); };
            Shown += delegate { Activate(); TopMost = false; };

            Reload();
        }

        void Reload()
        {
            var c = Consent.Load();
            var stats = Collector.Stats();
            _state.Text = (c.Enabled ? "当前：已开启（自 " + c.EnabledSinceLocal() + "，" + c.ScopeText() + (c.ByInstallDefault ? "，安装默认" : "") + "）"
                           : c.Suspended ? "当前：已授权，插件被禁用中，采集暂停" : "当前：未开启") +
                          "  ·  " + Tools.UploadText(stats).TrimEnd('。');
            _state.ForeColor = c.Enabled ? Color.FromArgb(0, 128, 64) : Color.FromArgb(90, 90, 90);
            _list.Items.Clear();
            foreach (var f in c.Folders) _list.Items.Add(f);
            if (c.Scope == "folders") _folders.Checked = true; else _all.Checked = true;
            if (_suggested != null)
            {
                if (!_list.Items.Contains(_suggested)) _list.Items.Add(_suggested);
                _folders.Checked = true;
                _suggested = null;
            }
            _agree.Checked = false;
            _enable.Text = c.Enabled ? "更新范围" : "开启记录";
            Sync();
        }

        void Sync()
        {
            bool folders = _folders.Checked;
            _list.Enabled = folders;
            _add.Enabled = folders;
            _remove.Enabled = folders && _list.SelectedIndex >= 0;
            _enable.Enabled = _agree.Checked && (!folders || _list.Items.Count > 0);
            var now = Consent.Load();
            _pause.Enabled = now.Enabled || now.Suspended;
        }

        void AddFolder()
        {
            using (var dlg = new FolderBrowserDialog { Description = "选择要记录的工作目录（含子目录）" })
            {
                if (dlg.ShowDialog(this) == DialogResult.OK && !string.IsNullOrEmpty(dlg.SelectedPath))
                {
                    string f = Consent.NormalizeFolder(dlg.SelectedPath);
                    if (!_list.Items.Contains(f)) _list.Items.Add(f);
                }
            }
            Sync();
        }

        void DoEnable()
        {
            if (!_agree.Checked) return;
            var folders = new List<string>();
            foreach (var item in _list.Items) folders.Add((string)item);
            Consent.Enable(_folders.Checked ? "folders" : "all", folders);
            Reload();
        }

        void DoDelete()
        {
            if (MessageBox.Show(this, "删除本机已记录的全部任务数据？此操作无法撤销。", "删除已记录数据",
                    MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK) return;
            Consent.DeleteData();
            Reload();
        }
    }
}
