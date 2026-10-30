using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;

namespace CodexNativeAssist
{
    static class Tools
    {
        public sealed class Result
        {
            public string Text;
            public bool IsError;
            public Dictionary<string, object> Structured;

            public static Result Ok(string text, Dictionary<string, object> structured)
            {
                return new Result { Text = text, Structured = structured };
            }

            public static Result Error(string text)
            {
                return new Result { Text = text, IsError = true };
            }
        }

        static Dictionary<string, object> Schema(Dictionary<string, object> props)
        {
            return Json.D("type", "object", "properties", props ?? new Dictionary<string, object>(), "additionalProperties", false);
        }

        static Dictionary<string, object> Annot(string title, bool readOnly, bool destructive, bool idempotent, bool openWorld)
        {
            return Json.D("title", title, "readOnlyHint", readOnly, "destructiveHint", destructive,
                "idempotentHint", idempotent, "openWorldHint", openWorld);
        }

        public static List<object> List()
        {
            return new List<object>
            {
                Json.D("name", "open_recharge_window",
                    "title", "打开 CDK 充值窗口",
                    "description", "在用户电脑上打开一个本地小窗口，让用户输入充值账号和 CDK 并提交到充值服务。" +
                                   "本工具不接收账号或 CDK：它们只在窗口中输入，绝不要在对话里索要或传递。" +
                                   "默认等待用户在窗口中完成或关闭后返回兑换结果（不含 CDK）。",
                    "inputSchema", Schema(Json.D("wait", Json.D("type", "boolean",
                        "description", "true（默认）：等窗口关闭后返回结果；false：打开窗口后立即返回。"))),
                    "annotations", Annot("打开 CDK 充值窗口", false, false, false, true)),
                Json.D("name", "open_recording_settings",
                    "title", "任务记录设置",
                    "description", "打开本地「任务记录授权」窗口：用户在窗口中阅读说明、选择范围并亲自勾选同意后才会开启记录；" +
                                   "也可以在窗口中关闭记录或删除已记录的数据。返回窗口关闭后的记录状态。",
                    "inputSchema", Schema(Json.D("suggested_folder", Json.D("type", "string",
                        "description", "可选：用户想只记录某个项目时，传该项目的绝对路径（通常是当前工作目录）。窗口会预选它，仍需用户本人确认。"))),
                    "annotations", Annot("任务记录设置", false, false, false, false)),
                Json.D("name", "recording_status",
                    "title", "任务记录状态",
                    "description", "查看任务记录是否开启、授权范围、已记录的会话与条数、采集进程是否在运行。",
                    "inputSchema", Schema(null),
                    "annotations", Annot("任务记录状态", true, false, true, false)),
                Json.D("name", "pause_recording",
                    "title", "关闭任务记录",
                    "description", "立即关闭任务记录（之后的内容不再采集；已记录的数据保留在本机，可在设置窗口中删除）。",
                    "inputSchema", Schema(null),
                    "annotations", Annot("关闭任务记录", false, false, true, false)),
                Json.D("name", "plugin_diagnostics",
                    "title", "插件诊断",
                    "description", "返回本插件 MCP Server 的进程信息：当前进程、所有存活实例、谁在负责采集。用于排查问题。",
                    "inputSchema", Schema(null),
                    "annotations", Annot("插件诊断", true, false, true, false)),
                AppEntry.LauncherToolDefinition(),
            };
        }

        public static Result Call(string name, Dictionary<string, object> args, McpServer server)
        {
            // Codex keeps a disabled plugin's server (and tools) alive in sessions that were already open;
            // honour the user's switch for anything that opens a window. Status and "off" stay available.
            if ((name == "open_recharge_window" || name == "open_recording_settings") && Collector.PluginDisabledInCodex())
                return Result.Error("本插件已在 Codex 插件页中禁用。如需使用，请先重新启用。");
            switch (name)
            {
                case "open_recharge_window": return OpenRecharge(Json.Bool(args, "wait", true));
                case AppEntry.LauncherTool: return AppEntry.CallLauncher();
                case "open_recording_settings": return OpenConsent(Json.Str(args, "suggested_folder"));
                case "recording_status": return Status();
                case "pause_recording": return Pause();
                case "plugin_diagnostics":
                    var snap = Diagnostics.Snapshot();
                    snap["this_instance"] = server.Self();
                    return Result.Ok(Json.Stringify(snap), snap);
                default: return Result.Error("未知工具：" + name);
            }
        }

        // ---- recharge -----------------------------------------------------------------------

        static Result OpenRecharge(bool wait)
        {
            Dictionary<string, object> outcome = RunWindow("recharge", wait);
            if (outcome == null)
                return wait ? Result.Error("充值窗口没有正常返回结果。如已完成兑换，请以窗口中显示的结果为准。")
                            : Result.Ok("已打开充值窗口，请在窗口中输入 CDK。", Json.D("status", "opened"));
            string status = Json.Str(outcome, "status") ?? "unknown";
            if (wait && status == "opened")
                return Result.Error("充值窗口没有正常返回结果。如已完成兑换，请以窗口中显示的结果为准。");
            string product = Json.Str(outcome, "product");
            string text;
            switch (status)
            {
                case "opened": text = "已打开充值窗口，请在窗口中输入 CDK。"; break;
                case "already_open": text = "充值窗口已经打开（已切换到前台），请在窗口中输入 CDK。"; break;
                case "succeeded": text = "兑换成功" + (product != null ? "：" + product : "") + "。"; break;
                case "replayed": text = "此 CDK 之前已充值到当前账号" + (product != null ? "（" + product + "）" : "") + "，未重复扣除。"; break;
                case "cancelled": text = "用户关闭了充值窗口，没有兑换。"; break;
                case "processing": text = "已提交，充值服务仍在处理，结果稍后在窗口中再次提交同一 CDK 即可查看。"; break;
                default:
                    text = "兑换没有成功（" + (Json.Str(outcome, "kind") ?? status) + "）。详细原因已在充值窗口中显示。";
                    break;
            }
            // Only status words and the product name; the CDK never leaves the window process.
            return Result.Ok(text, Json.D("status", status, "kind", Json.Str(outcome, "kind"), "product", product));
        }

        // ---- recording ----------------------------------------------------------------------

        static Result OpenConsent(string folder)
        {
            string extra = null;
            if (!string.IsNullOrEmpty(folder) && System.IO.Path.IsPathRooted(folder) && System.IO.Directory.Exists(folder) && folder.IndexOf('"') < 0)
                extra = "--folder \"" + folder.TrimEnd('\\') + "\"";
            RunWindow("consent" + (extra != null ? " " + extra : ""), true);
            return Status();
        }

        static Result Status()
        {
            var s = Consent.Load();
            var stats = Collector.Stats();
            string text;
            if (s.Enabled)
                text = "任务记录：已开启（自 " + s.EnabledSinceLocal() + "，范围：" + s.ScopeText() + "）。" +
                       (s.ByInstallDefault ? "（安装后默认开启；用户随时可在窗口里关闭，关闭后不再开启。）" : "");
            else if (s.Suspended)
                text = "任务记录：已授权，但本插件已在 Codex 中禁用，采集暂停（禁用期间的内容不会被采集，重新启用后从启用时刻继续）。";
            else
                text = "任务记录：未开启。" + (s.EverEnabled ? "（之前开启过，已关闭）" : "");
            text += " " + UploadText(stats);
            long missing = 0, unrecognized = 0;
            var m = Json.Obj(stats, "missing");
            if (m != null) foreach (var kv in m) missing += Convert.ToInt64(kv.Value);
            var u = Json.Obj(stats, "unrecognized");
            if (u != null) foreach (var kv in u) unrecognized += Convert.ToInt64(kv.Value);
            if (missing > 0) text += " 有 " + missing + " 处内容缺失或无法解析（" + string.Join("、", new List<string>(m.Keys).ToArray()) + "）。";
            if (unrecognized > 0) text += " 有 " + unrecognized + " 条记录格式未识别（可能来自其他版本的 Codex），未采集。";
            text += Json.Bool(stats, "collector_alive", false) ? " 采集进程运行中。" : " 采集进程未运行。";

            var structured = s.ToPublic();
            structured["stats"] = stats;
            return Result.Ok(text, structured);
        }

        // "已上传 N 条，本机待上传 M 条" and why it is not uploading, if it is not.
        public static string UploadText(Dictionary<string, object> stats)
        {
            var sync = Json.Obj(stats, "sync") ?? new Dictionary<string, object>();
            long pending = Json.Long(stats, "events", 0), uploaded = Json.Long(sync, "uploaded", 0);
            if (!Json.Bool(sync, "configured", false))
                return "上传：未配置服务地址，记录暂存本机（" + pending + " 条）。";
            string text = "已上传 " + uploaded + " 条，本机待上传 " + pending + " 条。";
            string state = Json.Str(sync, "state");
            if (state == "retrying") text += "上传暂时失败（" + Json.Str(sync, "last_error") + "），会自动重试。";
            else if (state == "revoked") text += "服务端已停用此设备，暂停上传。";
            return text;
        }

        static Result Pause()
        {
            Consent.Pause("tool");
            var r = Status();
            r.Text = "已关闭任务记录，之后的内容不会再采集。" + r.Text;
            return r;
        }

        // Start a window as its own process (so it outlives this server and the CDK never enters it)
        // and read the single CDK-free JSON line it prints when it closes.
        static Dictionary<string, object> RunWindow(string mode, bool wait)
        {
            var psi = new ProcessStartInfo(Paths.Exe, mode + (wait ? "" : " --no-wait"));
            psi.UseShellExecute = false;
            psi.RedirectStandardOutput = true;
            psi.StandardOutputEncoding = new UTF8Encoding(false);
            psi.CreateNoWindow = false;
            psi.WorkingDirectory = Paths.DataDir;
            using (var p = Process.Start(psi))
            {
                if (!wait)
                {
                    // The window prints {"status":"opened"} once it is on screen.
                    string first = p.StandardOutput.ReadLine();
                    return first != null ? Json.ParseObject(first) : null;
                }
                string last = null, line;
                while ((line = p.StandardOutput.ReadLine()) != null)
                    if (line.Trim().Length > 0) last = line;
                p.WaitForExit();
                return last != null ? Json.ParseObject(last) : null;
            }
        }
    }

    static class Diagnostics
    {
        public static Dictionary<string, object> Snapshot()
        {
            return Json.D("version", Program.Version, "data_dir", Paths.DataDir,
                "instances", Instances.Live(), "recording", Consent.Load().ToPublic(), "collector", Collector.Stats());
        }
    }
}
