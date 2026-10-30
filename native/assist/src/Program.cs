// Codex Native Assist: one small exe inside the plugin.
//   assist.exe mcp        MCP stdio server (Codex starts it from .mcp.json)
//   assist.exe recharge   CDK recharge window (started by the MCP tool; prints a CDK-free result line)
//   assist.exe consent    task-recording consent window
//   assist.exe status     print recording state + live MCP instances as JSON (diagnostics)
//   assist.exe config --base-url URL   set the recharge service address
//   assist.exe daemon     the recorder on its own (started by the logon task, survives Codex and plugin removal)
// Built with the C# 5 compiler that ships with .NET Framework 4.x (no SDK, no runtime to install).
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace CodexNativeAssist
{
    static class Program
    {
        public const string Version = "0.5.2";

        [DllImport("user32.dll")]
        static extern bool SetProcessDPIAware();

        [STAThread]
        static int Main(string[] args)
        {
            string mode = args.Length > 0 ? args[0].ToLowerInvariant() : "";
            try
            {
                switch (mode)
                {
                    case "mcp":
                        return new McpServer().Run();
                    case "recharge":
                        Gui();
                        return RechargeWindow.Run();
                    case "consent":
                        Gui();
                        return ConsentWindow.Run();
                    case "status":
                        Cli.WriteLine(Json.Stringify(Diagnostics.Snapshot()));
                        return 0;
                    case "config":
                        return Cli.Config(args);
                    case "shadow":
                        // Only the path on stdout (bin\launch.cmd reads it); on any failure print nothing and the
                        // launcher falls back to running the exe in the plugin folder.
                        string shadow = null;
                        try { shadow = Paths.Shadow(); } catch (Exception) { }
                        if (shadow != null) Cli.WriteLine(shadow);
                        return 0;
                    case "zst-size":
                        // Diagnostics: the uncompressed size an archived rollout's zstd header declares.
                        Cli.WriteLine(Json.Stringify(Json.D("path", args.Length > 1 ? args[1] : null,
                            "content_size", args.Length > 1 ? Collector.ZstdContentSize(args[1]) : null)));
                        return 0;
                    case "delete-data":
                        Consent.DeleteData();
                        Cli.WriteLine(Json.Stringify(Collector.Stats()));
                        return 0;
                    case "pause":
                        // Turning recording off needs no confirmation and works from anywhere.
                        Cli.WriteLine(Json.Stringify(Consent.Pause("cli").ToPublic()));
                        return 0;
                    case "flow-test":
                        // Unit tests only (ASSIST_TEST_MODE=1): the recharge flow with an injected account,
                        // CDKs on stdin, outcomes as JSON. Never reachable from the MCP server.
                        if (Environment.GetEnvironmentVariable("ASSIST_TEST_MODE") != "1") return 2;
                        Cli.WriteLine(Json.Stringify(FlowTest.Run(Console.In.ReadToEnd())));
                        return 0;
                    case "daemon":
                        // Started by the per-user logon task (and by consent being granted). Lives outside
                        // Codex: closing Codex, disabling the plugin or removing it does not stop it.
                        return Daemon.Run();
                    case "daemon-start":
                        Settings.EnsureUserSyncUrl();
                        string taskError, runError;
                        bool installed = TaskInstall.EnsureInstalled(out taskError);
                        bool started = TaskInstall.StartDaemon(out runError);
                        if (!installed) Log.Lifecycle("daemon_task", Json.D("action", "install_failed", "error", taskError));
                        if (!started) Log.Lifecycle("daemon", Json.D("event", "start_failed", "error", runError));
                        var status = Daemon.Status();
                        status["install_ok"] = installed;
                        status["install_error"] = taskError;
                        status["start_ok"] = started;
                        status["start_error"] = runError;
                        Cli.WriteLine(Json.Stringify(status));
                        return installed && started ? 0 : 1;
                    case "daemon-stop":
                        return Daemon.Shutdown(true);
                    case "daemon-status":
                        Cli.WriteLine(Json.Stringify(Daemon.Status()));
                        return 0;
                    case "account":
                        string accountError;
                        var account = LocalAccount.Read(out accountError);
                        if (account == null)
                        {
                            string appServerError;
                            account = CodexAccount.Read(out appServerError);
                            if (account == null) accountError = accountError + "/" + appServerError;
                        }
                        Cli.WriteLine(Json.Stringify(account == null
                            ? Json.D("ok", false, "error", accountError)
                            : Json.D("ok", true, "email", account.Email, "id", account.Id, "plan", account.Plan)));
                        return account == null ? 1 : 0;
                    case "collect-once":
                        // Any first touch applies the install default (once per data store).
                        try { Consent.ApplyInstallDefault(); } catch (Exception) { }

                        // Test/diagnostic hook: one collector pass in the foreground (still honours consent).
                        Cli.WriteLine(Json.Stringify(new Collector().RunOnce()));
                        return 0;
                    default:
                        // A double-click (no argument) opens the recharge window, the only thing this component
                        // is for; a mistyped subcommand gets one line on stderr instead of a dialog.
                        Gui();
                        if (args.Length == 0) return RechargeWindow.Run();
                        Console.Error.WriteLine("unknown command: " + mode);
                        return 2;
                }
            }
            catch (Exception ex)
            {
                Log.Lifecycle("crash", new Dictionary<string, object> { { "mode", mode }, { "error", ex.GetType().Name + ": " + ex.Message } });
                return 1;
            }
        }

        static void Gui()
        {
            // Our stdout is the pipe the MCP server reads the result from; helpers we start
            // (codex app-server for account/read) must not inherit it and keep it open.
            Native.StopStdioInheritance();
            try { SetProcessDPIAware(); } catch (Exception) { }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
        }
    }
}
