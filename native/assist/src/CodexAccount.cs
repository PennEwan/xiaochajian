using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

namespace CodexNativeAssist
{
    // Reads the ChatGPT account Codex itself is signed in to, through Codex's own app-server
    // (`account/read`). No browser, no page, no extension: the window can fill the recharge account the
    // moment the user clicks the link, and nothing leaves the machine — the reply carries email, plan and
    // account id only, never a token.
    static class CodexAccount
    {
        const int TimeoutMs = 15000;

        public static Account Read(out string error)
        {
            error = null;
            string exe = FindExe();
            if (exe == null) { error = "codex_not_found"; return null; }
            Process proc = null;
            try
            {
                var psi = new ProcessStartInfo(exe, "app-server")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    StandardOutputEncoding = Encoding.UTF8,
                    StandardErrorEncoding = Encoding.UTF8,
                };
                proc = Process.Start(psi);
                if (proc == null) { error = "app_server_failed"; return null; }

                var lines = StartReader(proc);
                Send(proc, "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"clientInfo\":{\"name\":\"codex-native-assist\",\"version\":\"" + Program.Version + "\"}}}");
                if (WaitFor(lines, "\"id\":1", TimeoutMs) == null) { error = "init_timeout"; return null; }
                Send(proc, "{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}");
                Send(proc, "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"account/read\",\"params\":{\"refreshToken\":false}}");
                string line = WaitFor(lines, "\"id\":2", TimeoutMs);
                if (line == null) { error = "read_timeout"; return null; }

                var doc = Json.ParseObject(line);
                var result = Json.Obj(doc, "result");
                if (result == null) { error = "no_result"; return null; }
                var acct = Json.Obj(result, "account");
                if (acct == null) { error = "not_logged_in"; return null; }
                string email = Json.Str(acct, "email");
                string id = Json.Str(Json.Obj(result, "workspaceRouting"), "chatgptAccountId");
                string plan = Json.Str(acct, "planType");
                if (string.IsNullOrEmpty(email) && string.IsNullOrEmpty(id)) { error = "no_account_fields"; return null; }
                Log.Lifecycle("codex_account", Json.D("event", "read", "plan", plan, "has_email", !string.IsNullOrEmpty(email)));
                return new Account { Id = id, Email = email, Plan = plan };
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name;
                return null;
            }
            finally
            {
                try { if (proc != null && !proc.HasExited) proc.Kill(); } catch (Exception) { }
                try { if (proc != null) proc.Dispose(); } catch (Exception) { }
            }
        }

        static void Send(Process proc, string line)
        {
            proc.StandardInput.WriteLine(line);
            proc.StandardInput.Flush();
        }

        // One reader thread fills a queue; callers wait on the queue with a deadline. Overlapping
        // ReadLineAsync calls on one stream consume each other's data, which made the first version time out.
        static BlockingCollection<string> StartReader(Process proc)
        {
            var queue = new BlockingCollection<string>();
            var thread = new Thread(delegate()
            {
                try
                {
                    string line;
                    while ((line = proc.StandardOutput.ReadLine()) != null) queue.Add(line);
                }
                catch (Exception) { }
                finally { try { queue.CompleteAdding(); } catch (Exception) { } }
            });
            thread.IsBackground = true;
            thread.Name = "app-server-stdout";
            thread.Start();
            return queue;
        }

        static string WaitFor(BlockingCollection<string> queue, string marker, int timeoutMs)
        {
            var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            while (DateTime.UtcNow < deadline)
            {
                int remaining = Math.Max(50, (int)(deadline - DateTime.UtcNow).TotalMilliseconds);
                string line;
                if (!queue.TryTake(out line, remaining)) return null;
                if (line != null && line.IndexOf(marker, StringComparison.Ordinal) >= 0) return line;
            }
            return null;
        }

        // %LOCALAPPDATA%\OpenAI\Codex\bin\<build>\codex.exe, newest build first.
        static string FindExe()
        {
            string env = Environment.GetEnvironmentVariable("CODEX_CLI_PATH");
            if (!string.IsNullOrEmpty(env) && File.Exists(env)) return env;
            try
            {
                string bin = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                    "OpenAI", "Codex", "bin");
                if (Directory.Exists(bin))
                {
                    var dirs = new List<string>(Directory.GetDirectories(bin));
                    dirs.Sort(StringComparer.OrdinalIgnoreCase);
                    dirs.Reverse();
                    foreach (var dir in dirs)
                    {
                        string exe = Path.Combine(dir, "codex.exe");
                        if (File.Exists(exe)) return exe;
                    }
                }
            }
            catch (Exception) { }
            return null;
        }
    }
}
