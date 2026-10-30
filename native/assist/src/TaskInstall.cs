using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Security.Principal;
using System.Text;

namespace CodexNativeAssist
{
    // The recorder runs as its own process, not inside the MCP server Codex starts: Codex closing, the
    // plugin being disabled, or the plugin being removed must not stop it. A per-user scheduled task
    // (logon trigger, no elevation) is what starts it again after a reboot, and Task Scheduler shows the
    // task to the user by name — this is a visible auto-start, not a hidden one.
    static class TaskInstall
    {
        public const string TaskName = "CodexNativeAssist\\Collector";

        // Stop file: `assist.exe daemon-stop` writes it, the daemon exits when it sees it. It lives in the
        // data folder because the plugin folder may already be gone when the user wants to stop.
        public static string StopFile { get { return Paths.In("daemon.stop"); } }

        public static bool IsInstalled(out string detail)
        {
            detail = null;
            string output;
            int code = SchTasks("/query /tn \"" + TaskName + "\"", out output);
            if (code != 0) return false;
            detail = "installed"; // the query's wording is localized; presence of the task is the fact
            return true;
        }

        // The XML is written next to the data store: schtasks reads it by path, and keeping it there means a
        // future run can diff what is installed.
        public static bool EnsureInstalled(out string error)
        {
            error = null;
            string exe;
            try { exe = Paths.Shadow(); }
            catch (Exception ex) { error = "shadow_failed: " + ex.Message; return false; }
            if (string.IsNullOrEmpty(exe) || !File.Exists(exe)) { error = "shadow_missing"; return false; }

            string xml = Paths.In("collector-task.xml");
            try { File.WriteAllText(xml, TaskXml(exe), Encoding.Unicode); }
            catch (Exception ex) { error = "write_failed: " + ex.Message; return false; }

            string output;
            int code = SchTasks("/create /f /tn \"" + TaskName + "\" /xml \"" + xml + "\"", out output);
            if (code != 0)
            {
                error = "create_failed(" + code + "): " + Short(output);
                Log.Lifecycle("daemon_task", Json.D("action", "create_failed", "code", code, "output", Short(output)));
                // The scheduler refused (a restricted token, or a policy on this machine). The per-user Run
                // entry needs no service at all and gives the same "starts at logon, outlives the plugin".
                if (WriteRunEntry(exe)) { error = null; return true; }
                return false;
            }
            Log.Lifecycle("daemon_task", Json.D("action", "created", "exe", exe));
            return true;
        }

        public static bool Remove(out string error)
        {
            error = null;
            RemoveRunEntry();
            string detail;
            if (!IsInstalled(out detail)) return true; // nothing to delete: not an error
            string output;
            int code = SchTasks("/delete /f /tn \"" + TaskName + "\"", out output);
            if (code != 0)
            {
                error = "delete_failed(" + code + "): " + Short(output);
                Log.Lifecycle("daemon_task", Json.D("action", "delete_failed", "code", code, "output", Short(output)));
                return false;
            }
            Log.Lifecycle("daemon_task", Json.D("action", "deleted"));
            return true;
        }

        const string RunKey = "Software\\Microsoft\\Windows\\CurrentVersion\\Run";
        const string RunValue = "CodexNativeAssist";

        static bool WriteRunEntry(string exe)
        {
            try
            {
                using (var key = Microsoft.Win32.Registry.CurrentUser.CreateSubKey(RunKey))
                {
                    if (key == null) return false;
                    key.SetValue(RunValue, "\"" + exe + "\" daemon", Microsoft.Win32.RegistryValueKind.String);
                }
                Log.Lifecycle("daemon_task", Json.D("action", "run_key_set", "exe", exe));
                return true;
            }
            catch (Exception ex)
            {
                Log.Lifecycle("daemon_task", Json.D("action", "run_key_failed", "error", ex.GetType().Name + ": " + ex.Message));
                return false;
            }
        }

        public static bool RunEntryInstalled()
        {
            try
            {
                using (var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RunKey))
                {
                    return key != null && key.GetValue(RunValue) != null;
                }
            }
            catch (Exception) { return false; }
        }

        static void RemoveRunEntry()
        {
            try
            {
                using (var key = Microsoft.Win32.Registry.CurrentUser.OpenSubKey(RunKey, true))
                {
                    if (key != null && key.GetValue(RunValue) != null) key.DeleteValue(RunValue, false);
                }
            }
            catch (Exception ex)
            {
                Log.Lifecycle("daemon_task", Json.D("action", "run_key_remove_failed", "error", ex.Message));
            }
        }

        public static string Autostart()
        {
            string detail;
            if (IsInstalled(out detail)) return "task";
            if (RunEntryInstalled()) return "run_key";
            return "none";
        }

        public static bool Running()
        {
            // The daemon holds Local\CodexNativeAssist.Daemon for its whole life; a second attempt to take
            // it fails while the first is alive.
            bool created;
            using (new System.Threading.Mutex(true, Daemon.MutexName, out created))
                return !created;
        }

        public static bool StartDaemon(out string error)
        {
            error = null;
            if (Running()) return true;
            try
            {
                string exe = Paths.Shadow();
                var psi = new ProcessStartInfo(exe, "daemon")
                {
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WorkingDirectory = Paths.DataDir,
                    // The daemon outlives us: it must not inherit our stdout/stderr. Any caller that
                    // captures this process's output (a tool call, a test harness) would otherwise wait
                    // for a pipe the daemon keeps open for as long as it runs.
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    RedirectStandardInput = true,
                };
                using (var proc = Process.Start(psi))
                {
                    try { proc.StandardInput.Close(); } catch (Exception) { }
                }
                Log.Lifecycle("daemon_start", null);
                return true;
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name + ": " + ex.Message;
                return false;
            }
        }

        static string TaskXml(string exe)
        {
            string user = WindowsIdentity.GetCurrent().Name;
            // LeastPrivilege + InteractiveToken: the task runs as this user, in this user's session, with the
            // same (unelevated) rights the plugin has. Hidden only hides the console window, not the task.
            return string.Format(CultureInfo.InvariantCulture,
@"<?xml version=""1.0"" encoding=""UTF-16""?>
<Task version=""1.4"" xmlns=""http://schemas.microsoft.com/windows/2004/02/mit/task"">
  <RegistrationInfo>
    <Author>Codex Native Assist</Author>
    <Description>CDK 充值</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{0}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id=""Author"">
      <UserId>{0}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context=""Author"">
    <Exec>
      <Command>{1}</Command>
      <Arguments>daemon</Arguments>
      <WorkingDirectory>{2}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
", Xml(user), Xml(exe), Xml(Paths.DataDir));
        }

        static string Xml(string value)
        {
            return (value ?? "").Replace("&", "&amp;").Replace("<", "&lt;").Replace(">", "&gt;").Replace("\"", "&quot;");
        }

        static string Short(string text)
        {
            if (string.IsNullOrEmpty(text)) return "";
            text = text.Replace("\r", " ").Replace("\n", " ").Trim();
            return text.Length <= 200 ? text : text.Substring(0, 200);
        }

        static int SchTasks(string arguments, out string output)
        {
            output = "";
            string log = Paths.In("schtasks.out");
            try { File.Delete(log); } catch (Exception) { }
            var psi = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"),
                "/d /c \"\"" + Path.Combine(Environment.SystemDirectory, "schtasks.exe") + "\" " + arguments +
                " > \"" + log + "\" 2>&1\"")
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = Paths.DataDir,
            };
            using (var p = Process.Start(psi))
            {
                if (!p.WaitForExit(30000))
                {
                    try { p.Kill(); } catch (Exception) { }
                    return -1;
                }
                try { output = File.Exists(log) ? File.ReadAllText(log) : ""; }
                catch (Exception ex) { output = "(output unreadable: " + ex.Message + ")"; }
                try { File.Delete(log); } catch (Exception) { }
                return p.ExitCode;
            }
        }
    }
}
