using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

namespace CodexNativeAssist
{
    // MCP over stdio: one JSON-RPC message per line (UTF-8, no BOM). Logs never go to stdout.
    sealed class McpServer
    {
        static readonly string[] KnownProtocols = { "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05" };

        const string Instructions =
            "CDK 充值：Codex 侧边栏里的「CDK 充值」入口打开一个本地小窗口，用户在窗口里输入充值账号和 CDK。" +
            "本服务器不向模型提供任何工具：不要在对话中索要、复述或记录 CDK；用户已经把 CDK 发在对话里时，" +
            "提醒对方改在侧边栏窗口里输入。";

        readonly Stream _out = Console.OpenStandardOutput();
        readonly object _outGate = new object();
        readonly DateTime _started = DateTime.UtcNow;
        readonly Collector _collector = new Collector();
        int _requests;
        string _client = "";
        volatile bool _exiting;

        public int Run()
        {
            Native.StopStdioInheritance();
            // Never hold the plugin folder as working directory: Codex could not delete it on uninstall.
            try { Environment.CurrentDirectory = Paths.DataDir; } catch (Exception) { }
            var procs = Native.Snapshot();
            int pid = Process.GetCurrentProcess().Id;
            Native.ProcInfo me, parent = null;
            int ppid = procs.TryGetValue(pid, out me) ? me.ParentPid : 0;
            if (ppid != 0) procs.TryGetValue(ppid, out parent);
            Native.ProcInfo grand = null;
            if (parent != null) procs.TryGetValue(parent.ParentPid, out grand);

            var envNames = new List<string>();
            foreach (System.Collections.DictionaryEntry kv in Environment.GetEnvironmentVariables()) envNames.Add((string)kv.Key);
            envNames.Sort(StringComparer.OrdinalIgnoreCase);

            Log.Lifecycle("start", Json.D(
                "ppid", ppid, "parent", parent != null ? parent.Name : null,
                "grandparent", grand != null ? grand.Name : null,
                "cwd", Environment.CurrentDirectory, "exe", Paths.Exe, "version", Program.Version,
                "in_job", Native.InJob(),
                "env_names", envNames,
                "PLUGIN_ROOT", Environment.GetEnvironmentVariable("PLUGIN_ROOT"),
                "PLUGIN_DATA", Environment.GetEnvironmentVariable("PLUGIN_DATA"),
                "CODEX_HOME", Environment.GetEnvironmentVariable("CODEX_HOME")));
            Instances.Register(ppid, parent != null ? parent.Name : null);
            Paths.CleanTemp();
            try { Consent.ApplyInstallDefault(); }
            catch (Exception ex) { Log.Lifecycle("install_default_failed", Json.D("error", ex.GetType().Name)); }

            StartBackground("heartbeat", HeartbeatLoop);
            if (ppid != 0) WatchParent(ppid);
            _collector.Start();

            var reader = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false));
            while (true)
            {
                string line;
                try { line = reader.ReadLine(); }
                catch (IOException) { line = null; }
                if (line == null) break;
                if (line.Trim().Length == 0) continue;
                Dictionary<string, object> msg = Json.ParseObject(line);
                if (msg == null)
                {
                    Send(Json.D("jsonrpc", "2.0", "id", null, "error", Json.D("code", -32700, "message", "Parse error")));
                    continue;
                }
                Handle(msg);
            }
            Exit("stdin_eof");
            return 0;
        }

        void Handle(Dictionary<string, object> msg)
        {
            string method = Json.Str(msg, "method");
            bool isRequest = msg.ContainsKey("id") && method != null;
            object id = isRequest ? msg["id"] : null;
            var prm = Json.Obj(msg, "params") ?? new Dictionary<string, object>();
            if (method == null) return; // a response to something we never send
            Interlocked.Increment(ref _requests);
            if (method != "ping" && method != "tools/call")
                Log.Lifecycle("rpc", Json.D("method", method));

            switch (method)
            {
                case "initialize":
                    var client = Json.Obj(prm, "clientInfo");
                    _client = (Json.Str(client, "name") ?? "") + " " + (Json.Str(client, "version") ?? "");
                    string requested = Json.Str(prm, "protocolVersion");
                    string version = Array.IndexOf(KnownProtocols, requested) >= 0 ? requested : "2025-06-18";
                    var caps = Json.Obj(prm, "capabilities");
                    Log.Lifecycle("initialize", Json.D("client", _client, "protocol", requested,
                        "client_capabilities", caps != null ? new List<string>(caps.Keys) : new List<string>()));
                    Instances.Update(_client);
                    Reply(id, Json.D(
                        "protocolVersion", version,
                        "capabilities", Json.D("tools", Json.D("listChanged", false), "resources", Json.D("listChanged", false)),
                        "serverInfo", Json.D("name", "codex-native-assist", "title", "Codex 原生助手", "version", Program.Version,
                            "icons", AppEntry.Icons()),
                        "instructions", Instructions));
                    return;
                case "notifications/initialized":
                case "notifications/cancelled":
                    return;
                case "ping":
                    if (isRequest) Reply(id, new Dictionary<string, object>());
                    return;
                case "tools/list":
                    Reply(id, Json.D("tools", Tools.List()));
                    return;
                // The App asks every server for these at startup even when they are not advertised.
                case "resources/list":
                    Reply(id, Json.D("resources", AppEntry.ResourceList()));
                    return;
                case "resources/read":
                    var res = AppEntry.Read(Json.Str(prm, "uri"));
                    Log.Lifecycle("resource_read", Json.D("uri", Json.Str(prm, "uri"), "found", res != null));
                    if (res != null) Reply(id, res);
                    else Send(Json.D("jsonrpc", "2.0", "id", id, "error", Json.D("code", -32002, "message", "Resource not found")));
                    return;
                case "resources/templates/list":
                    Reply(id, Json.D("resourceTemplates", new List<object>()));
                    return;
                case "prompts/list":
                    Reply(id, Json.D("prompts", new List<object>()));
                    return;
                case "tools/call":
                    string name = Json.Str(prm, "name");
                    var args = Json.Obj(prm, "arguments") ?? new Dictionary<string, object>();
                    var meta = Json.Obj(prm, "_meta");
                    // Who called: a model turn carries the turn's metadata, a direct UI call does not (keys only).
                    Log.Lifecycle("tool_request", Json.D("tool", name, "meta_keys", meta != null ? new List<string>(meta.Keys) : new List<string>()));
                    // Tools may block (a window waiting for the user): never on the stdin loop.
                    ThreadPool.QueueUserWorkItem(delegate { CallTool(id, name, args); });
                    return;
                default:
                    if (isRequest)
                        Send(Json.D("jsonrpc", "2.0", "id", id, "error", Json.D("code", -32601, "message", "Method not found: " + method)));
                    return;
            }
        }

        void CallTool(object id, string name, Dictionary<string, object> args)
        {
            var sw = Stopwatch.StartNew();
            Log.Lifecycle("tool_call", Json.D("tool", name));
            Tools.Result result;
            try { result = Tools.Call(name, args, this); }
            catch (Exception ex) { result = Tools.Result.Error("工具执行失败：" + ex.GetType().Name); }
            Log.Lifecycle("tool_done", Json.D("tool", name, "ms", sw.ElapsedMilliseconds, "is_error", result.IsError));
            var content = new List<object> { Json.D("type", "text", "text", result.Text) };
            var body = Json.D("content", content, "isError", result.IsError);
            if (result.Structured != null) body["structuredContent"] = result.Structured;
            Reply(id, body);
        }

        public Dictionary<string, object> Self()
        {
            return Json.D("pid", Process.GetCurrentProcess().Id, "client", _client.Trim(),
                "uptime_sec", (long)(DateTime.UtcNow - _started).TotalSeconds, "requests", _requests,
                "collector_leader", _collector.IsLeader);
        }

        void Reply(object id, object result)
        {
            Send(Json.D("jsonrpc", "2.0", "id", id, "result", result));
        }

        void Send(Dictionary<string, object> msg)
        {
            var bytes = new UTF8Encoding(false).GetBytes(Json.Stringify(msg) + "\n");
            lock (_outGate)
            {
                try
                {
                    _out.Write(bytes, 0, bytes.Length);
                    _out.Flush();
                }
                catch (IOException) { Exit("stdout_closed"); }
            }
        }

        void HeartbeatLoop()
        {
            int seconds = 30;
            int.TryParse(Environment.GetEnvironmentVariable("ASSIST_HEARTBEAT_SEC") ?? "30", out seconds);
            if (seconds < 1) seconds = 30;
            while (!_exiting)
            {
                Thread.Sleep(seconds * 1000);
                Log.Lifecycle("heartbeat", Json.D("uptime_sec", (long)(DateTime.UtcNow - _started).TotalSeconds,
                    "requests", _requests, "collector_leader", _collector.IsLeader));
                Instances.Update(null);
            }
        }

        void WatchParent(int ppid)
        {
            IntPtr h = Native.OpenProcess(Native.SYNCHRONIZE, false, ppid);
            if (h == IntPtr.Zero) return;
            StartBackground("parent-watch", delegate
            {
                Native.WaitForSingleObject(h, 0xFFFFFFFF);
                // Orphaned: Codex went away without closing our stdin. Do not linger — except when the
                // lifecycle probe asks us to stay, to see whether Codex itself cleans up its servers.
                if (Environment.GetEnvironmentVariable("ASSIST_NO_ORPHAN_EXIT") == "1")
                {
                    Log.Lifecycle("parent_exited", Json.D("staying", true));
                    return;
                }
                Exit("parent_exited");
            });
        }

        void Exit(string reason)
        {
            lock (this)
            {
                if (_exiting) return;
                _exiting = true;
            }
            Log.Lifecycle("exit", Json.D("reason", reason, "uptime_sec", (long)(DateTime.UtcNow - _started).TotalSeconds,
                "requests", _requests));
            _collector.Stop();
            // Codex closing our stdin is an orderly shutdown (App quit, kernel restart). Say so, so the next
            // collector may catch up on what happened meanwhile; a silent kill (the App's plugin switch)
            // leaves the state "on" and the gap is treated as not consented.
            if (_collector.IsLeader) Collector.MarkStopped(reason);
            Instances.Unregister();
            Environment.Exit(0);
        }

        static void StartBackground(string name, ThreadStart body)
        {
            var t = new Thread(body);
            t.IsBackground = true;
            t.Name = name;
            t.Start();
        }
    }

    // run\instances\<pid>.json: which MCP server processes are alive right now (for diagnostics).
    static class Instances
    {
        static string FileFor(int pid) { return Paths.In("run", "instances", pid + ".json"); }
        static Dictionary<string, object> _me;

        public static void Register(int ppid, string parent)
        {
            _me = Json.D("pid", Process.GetCurrentProcess().Id, "ppid", ppid, "parent", parent,
                "started", Log.Now(), "last_seen", Log.Now(), "client", "");
            Write();
        }

        public static void Update(string client)
        {
            if (_me == null) return;
            if (client != null) _me["client"] = client.Trim();
            _me["last_seen"] = Log.Now();
            Write();
        }

        static void Write()
        {
            try { Paths.WriteAtomic(FileFor(Process.GetCurrentProcess().Id), Json.Stringify(_me)); }
            catch (Exception) { }
        }

        public static void Unregister()
        {
            try { File.Delete(FileFor(Process.GetCurrentProcess().Id)); } catch (Exception) { }
        }

        public static List<object> Live()
        {
            var live = new List<object>();
            string dir = Paths.In("run", "instances");
            if (!Directory.Exists(dir)) return live;
            foreach (string f in Directory.GetFiles(dir, "*.json"))
            {
                var d = Json.ParseObject(Paths.ReadText(f) ?? "");
                if (d == null) continue;
                int pid = (int)Json.Long(d, "pid", 0);
                if (pid != 0 && Native.Alive(pid) && Collector.IsOurs(pid)) live.Add(d);
                else { try { File.Delete(f); } catch (Exception) { } }
            }
            return live;
        }
    }
}
