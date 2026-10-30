using System;
using System.Collections.Generic;
using System.IO;
using System.Threading;

namespace CodexNativeAssist
{
    // `assist.exe daemon`: the recorder as a process of its own.
    //
    // Why it exists: Collector/Uploader also run inside the MCP server Codex starts, but that server lives
    // only while Codex has a session open — closing Codex, disabling the plugin or removing it ends the
    // recording. The daemon is started by a per-user scheduled task (TaskInstall) at logon and keeps
    // collecting and uploading with the plugin gone.
    //
    // It never decides anything about consent: every pass re-reads it, so turning recording off in the
    // window, in Codex, or from the CLI stops the very next pass. While recording is off the daemon idles
    // instead of exiting, so turning it back on does not need the task to be re-run.
    static class Daemon
    {
        public const string MutexName = "Local\\CodexNativeAssist.Daemon";
        const int DefaultIntervalMs = 5000;

        public static int Run()
        {
            // first line: if this never appears, the process died before entering the loop
            Log.Lifecycle("daemon", Json.D("event", "enter", "pid", System.Diagnostics.Process.GetCurrentProcess().Id));
            bool created;
            using (var mutex = new Mutex(true, MutexName, out created))
            {
                if (!created) return 0; // already running (task + logon + a manual start can all fire)
                // The daemon is the first thing that runs on a machine where Codex was never opened:
                // apply the install default here too, so recording starts either way.
                try { Settings.EnsureUserSyncUrl(); } catch (Exception) { }
                // Recording is the product, not a plugin feature: the daemon keeps recording after the plugin
                // is disabled or removed. Consent (and daemon-stop) are the only things that end it.
                Collector.IgnorePluginDisabled = true;
                try
                {
                    var consentNow = Consent.Load();
                    if (consentNow.Suspended)
                    {
                        using (var db = Store.Open()) Consent.Resume(db, DateTime.UtcNow);
                        Log.Lifecycle("daemon", Json.D("event", "consent_resumed", "why", "plugin_state_ignored"));
                    }
                }
                catch (Exception ex) { Log.Lifecycle("daemon", Json.D("event", "resume_failed", "error", ex.GetType().Name)); }
                try { Consent.ApplyInstallDefault(); } catch (Exception ex) { Log.Lifecycle("daemon", Json.D("event", "default_failed", "error", ex.GetType().Name)); }
                Log.Lifecycle("daemon", Json.D("event", "start", "pid", System.Diagnostics.Process.GetCurrentProcess().Id));
                int interval = IntervalMs();
                bool sawConsent = false;
                try
                {
                    while (true)
                    {
                        if (File.Exists(TaskInstall.StopFile)) break;
                        bool enabled;
                        try { enabled = Consent.Load().Enabled; }
                        catch (Exception ex)
                        {
                            Log.Lifecycle("daemon", Json.D("event", "consent_unreadable", "error", ex.GetType().Name));
                            enabled = false;
                        }
                        if (enabled)
                        {
                            if (!sawConsent) { sawConsent = true; Log.Lifecycle("daemon", Json.D("event", "recording")); }
                            try
                            {
                                var result = new Collector().RunOnce();
                                if (result != null && result.ContainsKey("inserted"))
                                {
                                    object inserted = result["inserted"];
                                    if (Convert.ToInt32(inserted) > 0) Log.Lifecycle("daemon", Json.D("event", "collected", "inserted", inserted));
                                }
                            }
                            catch (Exception ex)
                            {
                                Log.Lifecycle("daemon", Json.D("event", "pass_failed", "error", ex.GetType().Name + ": " + ex.Message));
                            }
                        }
                        else if (sawConsent)
                        {
                            sawConsent = false;
                            Log.Lifecycle("daemon", Json.D("event", "idle", "reason", "consent_off"));
                        }
                        Thread.Sleep(interval);
                    }
                }
                finally
                {
                    Log.Lifecycle("daemon", Json.D("event", "exit"));
                    try { mutex.ReleaseMutex(); } catch (Exception) { }
                }
            }
            return 0;
        }

        // `assist.exe daemon-stop`: stop the process and take the task away in one step — the only thing the
        // user needs to remember if they removed the plugin and want the background recording gone.
        public static int Shutdown(bool removeTask)
        {
            bool wasRunning = TaskInstall.Running();
            try { File.WriteAllText(TaskInstall.StopFile, Store.Now()); }
            catch (Exception ex) { Log.Lifecycle("daemon", Json.D("event", "stop_file_failed", "error", ex.Message)); }

            if (wasRunning)
            {
                for (int i = 0; i < 30 && TaskInstall.Running(); i++) Thread.Sleep(200);
            }
            try { File.Delete(TaskInstall.StopFile); } catch (Exception) { }

            string taskError = null;
            bool taskRemoved = true;
            if (removeTask) taskRemoved = TaskInstall.Remove(out taskError);

            Cli.WriteLine(Json.Stringify(Json.D(
                "was_running", wasRunning,
                "still_running", TaskInstall.Running(),
                "task_removed", taskRemoved,
                "task_error", taskError)));
            return taskRemoved && !TaskInstall.Running() ? 0 : 1;
        }

        public static Dictionary<string, object> Status()
        {
            string detail;
            bool installed = TaskInstall.IsInstalled(out detail);
            var consent = Consent.Load();
            return Json.D(
                "running", TaskInstall.Running(),
                "task_installed", installed,
                "task_state", detail,
                "autostart", TaskInstall.Autostart(),
                "recording", consent.Enabled,
                "suspended", consent.Suspended,
                "sync_base_url", Settings.SyncBaseUrl(),
                "data_dir", Paths.DataDir,
                "executable", Paths.Exe);
        }

        static int IntervalMs()
        {
            string raw = Environment.GetEnvironmentVariable("ASSIST_DAEMON_INTERVAL_MS");
            int ms;
            if (!string.IsNullOrEmpty(raw) && int.TryParse(raw, out ms) && ms >= 500 && ms <= 3600000) return ms;
            return DefaultIntervalMs;
        }
    }
}
