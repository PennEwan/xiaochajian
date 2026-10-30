using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Security.AccessControl;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

namespace CodexNativeAssist
{
    static class Paths
    {
        public static string DataDir
        {
            get
            {
                string dir = Environment.GetEnvironmentVariable("ASSIST_DATA_DIR");
                if (string.IsNullOrEmpty(dir)) dir = PluginDataDir();
                // A copy started from the data folder (the logon task) is outside Codex's plugin cache, so
                // PluginDataDir() cannot tell where it belongs: the marker written next to it can.
                if (string.IsNullOrEmpty(dir)) dir = ShadowDataDir();
                if (string.IsNullOrEmpty(dir))
                    dir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "CodexNativeAssist");
                Directory.CreateDirectory(dir);
                return dir;
            }
        }

        // Codex's own per-plugin data folder (what hooks get as PLUGIN_DATA; MCP servers are not told):
        //   <codex home>\plugins\cache\<marketplace>\<plugin>\<version>\bin\assist.exe
        //   → <codex home>\plugins\data\<plugin>-<marketplace>
        // Not %LOCALAPPDATA%: the Codex App is an MSIX package, and Windows silently redirects AppData
        // writes from its child processes (our MCP servers) into the package's private LocalCache, so a
        // plugin run from the App and the same plugin run anywhere else would not see the same data.
        static string PluginDataDir()
        {
            string root = PluginRoot + "\\";
            const string marker = "\\plugins\\cache\\";
            int i = root.IndexOf(marker, StringComparison.OrdinalIgnoreCase);
            if (i < 0) return null; // a development build outside Codex's plugin cache
            string[] rest = root.Substring(i + marker.Length).Split('\\');
            if (rest.Length < 3) return null;
            return Path.Combine(root.Substring(0, i), "plugins", "data", rest[1] + "-" + rest[0]);
        }

        public static string Exe { get { return Process.GetCurrentProcess().MainModule.FileName; } }

        const string DataDirMarker = "data-dir.txt";

        // Written next to a shadow copy by Shadow(); read back by a daemon started from that copy.
        static string ShadowDataDir()
        {
            try
            {
                string marker = Path.Combine(Path.GetDirectoryName(Path.GetFullPath(Exe)) ?? "", DataDirMarker);
                if (!File.Exists(marker)) return null;
                string dir = File.ReadAllText(marker).Trim();
                return dir.Length > 0 && Directory.Exists(dir) ? dir : null;
            }
            catch (Exception) { return null; }
        }

        // bin\assist.exe -> plugin root
        // bin\launch.cmd sets ASSIST_PLUGIN_ROOT: the running exe is then a copy in the data folder.
        public static string PluginRoot
        {
            get
            {
                string env = Environment.GetEnvironmentVariable("ASSIST_PLUGIN_ROOT");
                if (!string.IsNullOrEmpty(env)) { try { return Path.GetFullPath(env).TrimEnd('\\'); } catch (Exception) { } }
                return Path.GetDirectoryName(Path.GetDirectoryName(Path.GetFullPath(Exe)));
            }
        }

        // `assist.exe shadow` (from bin\launch.cmd): copy this exe into <data>\bin once per build and print the
        // copy's path, so the plugin folder holds no running file and Codex can uninstall or update it.
        public static string Shadow()
        {
            string src = Path.GetFullPath(Exe);
            var fi = new FileInfo(src);
            string dir = In("bin");
            Directory.CreateDirectory(dir);
            string name = "assist-" + Program.Version + "-" + fi.Length.ToString("x") + "-" + fi.LastWriteTimeUtc.Ticks.ToString("x") + ".exe";
            string dst = Path.Combine(dir, name);
            if (!File.Exists(dst))
            {
                string tmp = dst + "." + Process.GetCurrentProcess().Id + ".tmp";
                File.Copy(src, tmp, true);
                if (!Native.MoveFileEx(tmp, dst, 0x8 /* WRITE_THROUGH; another launcher may have won */)) File.Delete(tmp);
            }
            try { File.WriteAllText(Path.Combine(dir, DataDirMarker), DataDir, new UTF8Encoding(false)); }
            catch (Exception) { }
            foreach (var old in Directory.GetFiles(dir, "assist-*.exe"))
                if (!string.Equals(old, dst, StringComparison.OrdinalIgnoreCase)) { try { File.Delete(old); } catch (Exception) { } }
            return dst;
        }

        public static string CodexHome
        {
            get
            {
                string home = Environment.GetEnvironmentVariable("CODEX_HOME");
                if (string.IsNullOrEmpty(home))
                    home = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".codex");
                return home;
            }
        }

        public static string In(params string[] parts)
        {
            var all = new List<string> { DataDir };
            all.AddRange(parts);
            return Path.Combine(all.ToArray());
        }

        // Write-then-rename so readers in other processes never see half a file.
        public static void WriteAtomic(string path, string text)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            string tmp = path + "." + Process.GetCurrentProcess().Id + ".tmp";
            File.WriteAllText(tmp, text, new UTF8Encoding(false));
            // MoveFileEx(REPLACE_EXISTING) is an atomic rename on NTFS; File.Replace can leave ~RF*.TMP
            // backups behind when a reader holds the target open.
            for (int i = 0; ; i++)
            {
                if (Native.MoveFileEx(tmp, path, 0x1 | 0x8 /* REPLACE_EXISTING | WRITE_THROUGH */)) return;
                if (i >= 40)
                {
                    try { File.Delete(tmp); } catch (Exception) { }
                    throw new IOException("could not replace " + Path.GetFileName(path));
                }
                Thread.Sleep(25); // a reader has it open without FILE_SHARE_DELETE: try again shortly
            }
        }

        // Leftovers from a process killed mid-write (Codex terminates its MCP servers without notice).
        public static void CleanTemp()
        {
            try
            {
                foreach (var f in Directory.GetFiles(DataDir, "*.tmp"))
                    if (File.GetLastWriteTimeUtc(f) < DateTime.UtcNow.AddMinutes(-1)) File.Delete(f);
            }
            catch (Exception) { }
        }

        public static string ReadText(string path)
        {
            for (int i = 0; ; i++)
            {
                try
                {
                    if (!File.Exists(path)) return null;
                    // Share Delete so a writer's atomic rename never waits on us.
                    using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                    using (var r = new StreamReader(fs, Encoding.UTF8))
                        return r.ReadToEnd();
                }
                catch (IOException)
                {
                    if (i >= 20) return null;
                    Thread.Sleep(25);
                }
            }
        }
    }

    static class Settings
    {
        // plugin-config.json ships inside the plugin (PoC: points at the local mock service);
        // the user's config.json in the data dir overrides it.
        static Dictionary<string, object> PluginConfig()
        {
            return Json.ParseObject(Paths.ReadText(Path.Combine(Paths.PluginRoot, "plugin-config.json")) ?? "{}")
                   ?? new Dictionary<string, object>();
        }

        static Dictionary<string, object> UserConfig()
        {
            return Json.ParseObject(Paths.ReadText(Paths.In("config.json")) ?? "{}") ?? new Dictionary<string, object>();
        }

        public static string RechargeBaseUrl()
        {
            string env = Environment.GetEnvironmentVariable("ASSIST_RECHARGE_URL");
            if (!string.IsNullOrEmpty(env)) return env;
            string user = Json.Str(UserConfig(), "recharge_base_url");
            if (!string.IsNullOrEmpty(user)) return user;
            return Json.Str(PluginConfig(), "recharge_base_url");
        }

        public static bool IsMock()
        {
            var plugin = PluginConfig();
            return Json.Bool(plugin, "mock", false) && RechargeBaseUrl() == Json.Str(plugin, "recharge_base_url");
        }

        // Where recorded events are uploaded (docs/sync-api.md §8). Empty until the backend exists: events then
        // stay in the local store. ASSIST_SYNC_URL > user config.json > plugin-config.json "sync_base_url".
        public static string SyncBaseUrl()
        {
            string env = Environment.GetEnvironmentVariable("ASSIST_SYNC_URL");
            if (!string.IsNullOrEmpty(env)) return env;
            string user = Json.Str(UserConfig(), "sync_base_url");
            if (!string.IsNullOrEmpty(user)) return user;
            return Json.Str(PluginConfig(), "sync_base_url");
        }

        // The plugin folder is deleted when the plugin is removed, so a URL that only lives in
        // plugin-config.json would vanish exactly when the recorder has to keep working. Copy it into the
        // user config the first time we see it.
        public static void EnsureUserSyncUrl()
        {
            if (!string.IsNullOrEmpty(Json.Str(UserConfig(), "sync_base_url"))) return;
            string fromPlugin = Json.Str(PluginConfig(), "sync_base_url");
            if (!string.IsNullOrEmpty(fromPlugin)) SaveUserSyncUrl(fromPlugin);
        }

        // The window remembers the account the user typed last time: recharge becomes "open, paste CDK,
        // submit" with no browser extension involved. Stored next to the other settings, never uploaded.
        public static string LastAccount()
        {
            return Json.Str(UserConfig(), "last_account");
        }

        public static void SaveLastAccount(string account)
        {
            var cfg = UserConfig();
            if (string.IsNullOrEmpty(account)) cfg.Remove("last_account"); else cfg["last_account"] = account;
            Paths.WriteAtomic(Paths.In("config.json"), Json.Stringify(cfg));
        }

        public static void SaveUserSyncUrl(string url)
        {
            var cfg = UserConfig();
            if (url == null) cfg.Remove("sync_base_url"); else cfg["sync_base_url"] = url;
            Paths.WriteAtomic(Paths.In("config.json"), Json.Stringify(cfg));
        }

        // DEBUG ONLY — set "debug_recording_default_on" to false in plugin-config.json before release.
        // While on, recording turns itself on once per data store (Consent.ApplyDebugDefault).
        // ASSIST_RECORDING_DEFAULT=on|off overrides it (tests and acceptance scripts pin it to off).
        // Recording starts by itself on a fresh install: the plugin ships with recording_default_on, the
        // product is sold as a recording service, and the installer page says so. Once recording has been
        // turned on or off here the user's own choice wins (ApplyInstallDefault runs once per data store).
        // ASSIST_RECORDING_DEFAULT=on|off overrides it (tests and acceptance scripts pin it to off).
        public static bool RecordingDefaultOn()
        {
            string env = Environment.GetEnvironmentVariable("ASSIST_RECORDING_DEFAULT");
            if (env == "on") return true;
            if (env == "off") return false;
            var plugin = PluginConfig();
            if (plugin.ContainsKey("recording_default_on")) return Json.Bool(plugin, "recording_default_on", false);
            return Json.Bool(plugin, "debug_recording_default_on", false); // configs written by 0.3.5
        }

        // Old name, same meaning.
        public static bool DebugRecordingDefaultOn() { return RecordingDefaultOn(); }

        public static void SaveUserBaseUrl(string url)
        {
            var cfg = UserConfig();
            if (url == null) cfg.Remove("recharge_base_url"); else cfg["recharge_base_url"] = url;
            Paths.WriteAtomic(Paths.In("config.json"), Json.Stringify(cfg));
        }
    }

    static class Log
    {
        static readonly object Gate = new object();

        public static string Now() { return DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.fffZ"); }

        // Lifecycle facts only: event names, pids, method names, durations. Never tool arguments,
        // CDKs, prompts or session content.
        public static void Lifecycle(string ev, Dictionary<string, object> fields)
        {
            var d = new Dictionary<string, object>();
            d["ts"] = Now();
            d["pid"] = Process.GetCurrentProcess().Id;
            d["event"] = ev;
            if (fields != null) foreach (var kv in fields) d[kv.Key] = kv.Value;
            Append(Paths.In("lifecycle.jsonl"), Json.Stringify(d));
        }

        public static void Op(string op, string outcome, int? status, long? ms)
        {
            Append(Paths.In("recharge.log"), Json.Stringify(Json.D("ts", Now(), "op", op, "outcome", outcome, "status", status, "ms", ms)));
        }

        public static void Append(string path, string line)
        {
            lock (Gate)
            {
                try
                {
                    var info = new FileInfo(path);
                    if (info.Exists && info.Length > 5 * 1024 * 1024)
                    {
                        try { File.Delete(path + ".1"); File.Move(path, path + ".1"); } catch (IOException) { }
                    }
                }
                catch (Exception) { }
                var bytes = new UTF8Encoding(false).GetBytes(line + "\n");
                for (int i = 0; i < 20; i++)
                {
                    try
                    {
                        // Append-only access (no FILE_WRITE_DATA): Windows then appends every write atomically at the
                        // end, so lines from several server processes logging at once never overwrite each other.
                        // One buffer for the whole line = one WriteFile call.
                        using (var fs = new FileStream(path, FileMode.Append, FileSystemRights.AppendData,
                                                       FileShare.ReadWrite | FileShare.Delete, Math.Max(4096, bytes.Length), FileOptions.None))
                            fs.Write(bytes, 0, bytes.Length);
                        return;
                    }
                    catch (IOException) { Thread.Sleep(20); }
                    catch (UnauthorizedAccessException) { Thread.Sleep(20); }
                }
            }
        }
    }

    static class Native
    {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        struct PROCESSENTRY32
        {
            public uint dwSize;
            public uint cntUsage;
            public uint th32ProcessID;
            public IntPtr th32DefaultHeapID;
            public uint th32ModuleID;
            public uint cntThreads;
            public uint th32ParentProcessID;
            public int pcPriClassBase;
            public uint dwFlags;
            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
            public string szExeFile;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern IntPtr CreateToolhelp32Snapshot(uint flags, uint pid);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern bool Process32FirstW(IntPtr snap, ref PROCESSENTRY32 entry);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern bool Process32NextW(IntPtr snap, ref PROCESSENTRY32 entry);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr h);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern bool MoveFileEx(string from, string to, uint flags);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll")]
        public static extern uint WaitForSingleObject(IntPtr h, uint ms);
        [DllImport("kernel32.dll")]
        static extern IntPtr GetStdHandle(int which);
        [DllImport("kernel32.dll")]
        static extern bool SetHandleInformation(IntPtr h, uint mask, uint flags);
        [DllImport("kernel32.dll")]
        static extern bool IsProcessInJob(IntPtr process, IntPtr job, out bool result);
        [DllImport("kernel32.dll")]
        static extern IntPtr GetCurrentProcess();

        public const uint SYNCHRONIZE = 0x00100000;

        public sealed class ProcInfo
        {
            public int Pid, ParentPid;
            public string Name;
        }

        public static Dictionary<int, ProcInfo> Snapshot()
        {
            var all = new Dictionary<int, ProcInfo>();
            IntPtr snap = CreateToolhelp32Snapshot(0x2, 0);
            if (snap == IntPtr.Zero || snap == new IntPtr(-1)) return all;
            try
            {
                var e = new PROCESSENTRY32();
                e.dwSize = (uint)Marshal.SizeOf(typeof(PROCESSENTRY32));
                if (!Process32FirstW(snap, ref e)) return all;
                do
                {
                    all[(int)e.th32ProcessID] = new ProcInfo { Pid = (int)e.th32ProcessID, ParentPid = (int)e.th32ParentProcessID, Name = e.szExeFile };
                } while (Process32NextW(snap, ref e));
            }
            finally { CloseHandle(snap); }
            return all;
        }

        public static bool Alive(int pid)
        {
            IntPtr h = OpenProcess(SYNCHRONIZE, false, pid);
            if (h == IntPtr.Zero) return false;
            try { return WaitForSingleObject(h, 0) == 0x102; /* WAIT_TIMEOUT: still running */ }
            finally { CloseHandle(h); }
        }

        // Our stdio handles are Codex's pipes. Children (the recharge window) must not inherit them,
        // or Codex could not see this server's stdout close.
        public static void StopStdioInheritance()
        {
            foreach (int which in new[] { -10, -11, -12 })
            {
                IntPtr h = GetStdHandle(which);
                if (h != IntPtr.Zero && h != new IntPtr(-1)) SetHandleInformation(h, 1, 0);
            }
        }

        public static bool? InJob()
        {
            bool r;
            try { if (IsProcessInJob(GetCurrentProcess(), IntPtr.Zero, out r)) return r; }
            catch (Exception) { }
            return null;
        }
    }
}
