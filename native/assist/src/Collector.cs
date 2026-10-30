using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace CodexNativeAssist
{
    // Reads the session files Codex itself writes (CODEX_HOME\sessions, archived_sessions) — no Hooks.
    //
    // Every MCP server instance (one per Codex session) runs a Collector. A named mutex picks one leader so
    // the work is done once, but correctness never depends on it: each batch of events is committed in one
    // BEGIN IMMEDIATE transaction together with the file offset, after re-checking inside that transaction
    // that (1) recording is still on at the consent version the pass started with, (2) the plugin is not
    // disabled in Codex, and (3) nobody else moved the offset. Events are keyed by session:ordinal:index
    // (INSERT OR IGNORE), so even two collectors racing, a crash between steps, or a renamed / re-created
    // file cannot produce a duplicate.
    sealed class Collector
    {
        const int BatchLines = 400;
        const int BatchBytes = 2 * 1024 * 1024;

        Thread _thread;
        readonly Uploader _uploader = new Uploader();
        volatile bool _stop;
        volatile bool _leader;
        string _lastState;
        DateTime _lastStateWrite = DateTime.MinValue;

        public bool IsLeader { get { return _leader; } }

        static bool EveryoneCollects { get { return Environment.GetEnvironmentVariable("ASSIST_ALL_COLLECT") == "1"; } }

        static string MutexName
        {
            get
            {
                using (var sha = SHA1.Create())
                {
                    byte[] h = sha.ComputeHash(Encoding.UTF8.GetBytes(Paths.DataDir.ToLowerInvariant()));
                    return "Local\\CodexNativeAssist.Collector." + BitConverter.ToString(h, 0, 8).Replace("-", "");
                }
            }
        }

        public void Start()
        {
            _thread = new Thread(Loop);
            _thread.IsBackground = true;
            _thread.Name = "collector";
            _thread.Start();
        }

        public void Stop() { _stop = true; }

        static int PollMs
        {
            get
            {
                int ms;
                return int.TryParse(Environment.GetEnvironmentVariable("ASSIST_COLLECT_MS"), out ms) && ms >= 100 ? ms : 2000;
            }
        }

        void Loop()
        {
            var mutex = new Mutex(false, MutexName);
            Db db = null;
            while (!_stop)
            {
                if (!_leader)
                {
                    if (EveryoneCollects) _leader = true;
                    else
                    {
                        try { _leader = mutex.WaitOne(0); }
                        catch (AbandonedMutexException) { _leader = true; } // the previous leader was killed: ours now
                    }
                    if (_leader) Log.Lifecycle("collector_leader", null);
                }
                if (_leader)
                {
                    try
                    {
                        if (db == null) db = Store.Open();
                        Pass(db);
                        if (!_stop) _uploader.Run(db);
                    }
                    catch (Exception ex)
                    {
                        try { if (db != null) SetState(db, "error", ex.GetType().Name + ": " + ex.Message, true); } catch (Exception) { }
                        if (db != null) { db.Dispose(); db = null; }
                    }
                }
                Thread.Sleep(_leader ? PollMs : 5000);
            }
        }

        // `assist.exe collect-once`: one pass in the foreground (tests, diagnostics).
        public Dictionary<string, object> RunOnce()
        {
            using (var m = new Mutex(false, MutexName))
            {
                bool got = EveryoneCollects;
                if (!got)
                {
                    try { got = m.WaitOne(5000); }
                    catch (AbandonedMutexException) { got = true; }
                }
                if (!got) return Json.D("skipped", "another collector is the leader");
                try
                {
                    using (var db = Store.Open())
                    {
                        var result = Pass(db);
                        _uploader.Run(db);
                        result["sync"] = Uploader.Stats(db);
                        return result;
                    }
                }
                finally { if (!EveryoneCollects) m.ReleaseMutex(); }
            }
        }

        // ---- one pass -----------------------------------------------------------------------

        sealed class PassStats
        {
            public int Files, Lines, Kept, Inserted;
            public string Stopped;
        }

        Dictionary<string, object> Pass(Db db)
        {
            var consent = Consent.Load(db);
            // Disabled or removed in Codex: recording continues (the product records regardless of the
            // plugin). PluginDisabledInCodex() is only reported as diagnostics now.
            if (consent.Suspended)
            {
                Consent.Resume(db, ConfigWriteTime() ?? DateTime.UtcNow);
                consent = Consent.Load(db);
            }
            if (!consent.Enabled)
            {
                SetState(db, "off", null, false);
                return Json.D("state", "off", "inserted", 0);
            }
            if (ExcludeUnwatchedGap(db)) consent = Consent.Load(db);
            DateTime earliest = consent.EarliestFrom() ?? DateTime.UtcNow;
            var st = new PassStats();
            foreach (string file in RolloutFiles())
            {
                if (_stop) break;
                if (!ProcessFile(db, file, consent, earliest, st)) break; // consent or the plugin switch changed
            }
            if (st.Stopped == null) ScanArchives(db, consent, earliest);
            string state = st.Stopped ?? "on";
            SetState(db, state, null, st.Inserted > 0);
            return Json.D("state", state, "files", st.Files, "lines_read", st.Lines, "kept", st.Kept, "inserted", st.Inserted);
        }

        bool ProcessFile(Db db, string path, Consent consent, DateTime earliest, PassStats st)
        {
            FileStream fs;
            try { fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); }
            catch (Exception) { return true; } // vanished or locked: next pass
            using (fs)
            {
                // Codex keeps the rollout open while the session is loaded; NTFS refreshes the directory
                // entry's size only when that handle closes. Ask the handle, never FileInfo.
                long length = fs.Length;
                string fileId = FileId(fs.SafeFileHandle) ?? ("path:" + path.ToLowerInvariant());
                string name = Path.GetFileName(path);
                var row = db.One("SELECT * FROM files WHERE file_id = ?", fileId);
                ParseContext ctx;
                long offset;
                if (row == null)
                {
                    ctx = new ParseContext { SessionId = SessionFromName(name) };
                    offset = StartOffset(fs, ctx, earliest, name);
                    if (offset == 0) ctx = new ParseContext { SessionId = SessionFromName(name) }; // line 0 is read again below
                    long startAt = offset;
                    db.Write(delegate
                    {
                        db.Exec("INSERT OR IGNORE INTO files(file_id, name, path, session_id, offset, size, ctx, state, updated_at) " +
                                "VALUES (?,?,?,?,?,?,?,'active',?)", fileId, name, path, ctx.SessionId, startAt, length, ctx.ToJson(), Store.Now());
                    });
                    row = db.One("SELECT * FROM files WHERE file_id = ?", fileId);
                }
                else if (!string.Equals(Store.Str(row, "path"), path, StringComparison.OrdinalIgnoreCase))
                {
                    // Same file (same NTFS file id) under a new name or folder: keep reading where we were.
                    string old = Store.Str(row, "path");
                    var known = row;
                    db.Write(delegate
                    {
                        db.Exec("UPDATE files SET name = ?, path = ?, state = 'active', updated_at = ? WHERE file_id = ?", name, path, Store.Now(), fileId);
                        AddIssue(db, name, Store.Str(known, "session_id"), new Issue { Category = "info", Code = "renamed", Offset = Store.Long(known, "offset"),
                            Detail = "文件由 " + Path.GetFileName(old) + " 改名 / 移动为 " + name + "，从偏移 " + Store.Long(known, "offset") + " 继续读取" });
                    });
                }
                offset = Store.Long(row, "offset");
                ctx = ParseContext.FromJson(Store.Str(row, "ctx"), Store.Str(row, "session_id") ?? SessionFromName(name));
                if (length < offset)
                {
                    long was = offset;
                    string sid = ctx.SessionId;
                    db.Write(delegate
                    {
                        AddIssue(db, name, sid, new Issue { Category = "file", Code = "file_rewritten", Offset = was,
                            Detail = "文件变短（" + was + " → " + length + " 字节），从头重读；已有事件按 event_key 去重" });
                        db.Exec("UPDATE files SET offset = 0, ctx = NULL WHERE file_id = ?", fileId);
                    });
                    offset = 0;
                    ctx = new ParseContext { SessionId = SessionFromName(name) };
                }
                if (length == offset) return true;
                st.Files++;
                return ReadAndCommit(db, fs, fileId, name, offset, ctx, consent, st);
            }
        }

        bool ReadAndCommit(Db db, FileStream fs, string fileId, string name, long offset, ParseContext ctx, Consent consent, PassStats st)
        {
            fs.Seek(offset, SeekOrigin.Begin);
            var reader = new LineReader(fs, offset);
            bool lastCovered = false;
            while (true)
            {
                var batch = new List<Event>();
                var issues = new List<Issue>();
                var skipped = new Dictionary<string, long>();
                long batchStart = reader.Position, batchBytes = 0;
                int lines = 0;
                byte[] line;
                int len;
                long lineOffset;
                while (lines < BatchLines && batchBytes < BatchBytes && reader.Next(out line, out len, out lineOffset))
                {
                    lines++;
                    batchBytes += len + 1;
                    var parsed = RolloutParser.ParseLine(line, len, lineOffset, ctx, name);
                    DateTime ts;
                    bool hasTs = Store.TryParse(parsed.Ts, out ts);
                    bool covered = hasTs ? consent.Covers(ts) && consent.InScope(ctx.Cwd) : lastCovered;
                    if (hasTs) lastCovered = covered;
                    if (!covered) continue;
                    batch.AddRange(parsed.Events);
                    issues.AddRange(parsed.Issues);
                    foreach (var kv in parsed.Skipped) { long n; skipped.TryGetValue(kv.Key, out n); skipped[kv.Key] = n + kv.Value; }
                }
                if (lines == 0) return true; // only an unfinished last line left: Codex is still writing it
                st.Lines += lines;
                st.Kept += batch.Count;
                long newOffset = reader.Position;
                string reason = Commit(db, fileId, name, batchStart, newOffset, ctx, consent, batch, issues, skipped, st);
                if (reason == "offset_moved") return true;       // someone else got here first: skip this file
                if (reason != null) { st.Stopped = reason; return false; }
            }
        }

        static string Commit(Db db, string fileId, string name, long expected, long newOffset, ParseContext ctx, Consent consent,
                             List<Event> batch, List<Issue> issues, Dictionary<string, long> skipped, PassStats st)
        {
            try
            {
                db.Write(delegate
                {
                    var c = db.One("SELECT enabled, version FROM consent WHERE id = 1");
                    if (Store.Long(c, "enabled") != 1 || Store.Long(c, "version") != consent.Version) throw new Rollback("consent_changed");
                    if (!IgnorePluginDisabled && PluginDisabledInCodex()) throw new Rollback("plugin_disabled");
                    var f = db.One("SELECT offset FROM files WHERE file_id = ?", fileId);
                    if (f == null || Store.Long(f, "offset") != expected) throw new Rollback("offset_moved");
                    string now = Store.Now();
                    int pid = Process.GetCurrentProcess().Id;
                    foreach (var e in batch)
                        st.Inserted += db.Exec("INSERT OR IGNORE INTO events(event_key, session_id, ordinal, idx, ts, kind, turn_id, model, call_id, " +
                            "file_id, offset, collected_at, collector_pid, body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            e.Key, e.SessionId, e.Ordinal, e.Idx, e.Ts, e.Kind, e.TurnId, e.Model, e.CallId, fileId, e.Offset, now, pid, e.Body);
                    foreach (var i in issues) AddIssue(db, name, ctx.SessionId, i);
                    foreach (var kv in skipped)
                        db.Exec("INSERT INTO skipped(file, session_id, category, count) VALUES (?,?,?,?) " +
                                "ON CONFLICT(file, category) DO UPDATE SET count = count + excluded.count", name, ctx.SessionId, kv.Key, kv.Value);
                    db.Exec("UPDATE files SET offset = ?, size = ?, ctx = ?, session_id = ?, updated_at = ? WHERE file_id = ?",
                        newOffset, newOffset, ctx.ToJson(), ctx.SessionId, now, fileId);
                });
                return null;
            }
            catch (Rollback r) { return r.Reason; }
        }

        static void AddIssue(Db db, string file, string sessionId, Issue i)
        {
            db.Exec("INSERT OR IGNORE INTO issues(file, session_id, category, code, detail, offset, ordinal, count, at) VALUES (?,?,?,?,?,?,?,?,?)",
                file, sessionId, i.Category, i.Code, i.Detail, i.Offset, i.Ordinal, i.Count, Store.Now());
        }

        // A file seen for the first time: read its session_meta for context, then skip straight to the first
        // consented moment (lines are appended in time order), so a long pre-consent history is never read.
        static long StartOffset(FileStream fs, ParseContext ctx, DateTime earliest, string name)
        {
            fs.Seek(0, SeekOrigin.Begin);
            var reader = new LineReader(fs, 0);
            byte[] line;
            int len;
            long at;
            if (reader.Next(out line, out len, out at)) RolloutParser.ParseLine(line, len, at, ctx, name);
            long start = SeekToTime(fs, earliest);
            if (start > 0) ctx.LastOrdinal = long.MinValue; // ordinals before the jump were never seen: no gap report
            return start;
        }

        static long SeekToTime(FileStream fs, DateTime t)
        {
            long lo = 0, hi = fs.Length;
            while (hi - lo > 64 * 1024)
            {
                long mid = lo + (hi - lo) / 2;
                fs.Seek(mid, SeekOrigin.Begin);
                SkipLine(fs);
                long lineStart = fs.Position;
                string head = ReadHead(fs, 128);
                DateTime ts;
                if (head == null || !TryLeadingTimestamp(head, out ts)) { hi = mid; continue; }
                if (ts < t) lo = lineStart; else hi = mid;
            }
            return lo;
        }

        static void SkipLine(FileStream fs)
        {
            int b;
            while ((b = fs.ReadByte()) >= 0 && b != '\n') { }
        }

        static string ReadHead(FileStream fs, int n)
        {
            var buf = new byte[n];
            int got = fs.Read(buf, 0, n);
            return got > 0 ? Encoding.UTF8.GetString(buf, 0, got) : null;
        }

        public static bool TryLeadingTimestamp(string line, out DateTime ts)
        {
            ts = DateTime.MinValue;
            const string key = "{\"timestamp\":";
            if (!line.StartsWith(key, StringComparison.Ordinal)) return false;
            int i = key.Length;
            while (i < line.Length && line[i] == ' ') i++;
            if (i >= line.Length || line[i] != '"') return false;
            int end = line.IndexOf('"', i + 1);
            return end > 0 && Store.TryParse(line.Substring(i + 1, end - i - 1), out ts);
        }

        // Complete lines from a position; an unfinished last line is left for the next pass.
        sealed class LineReader
        {
            readonly FileStream _fs;
            readonly MemoryStream _line = new MemoryStream();
            readonly byte[] _buf = new byte[1 << 16];
            int _pos, _len;
            long _consumed; // file offset just after the last complete line returned

            public LineReader(FileStream fs, long offset) { _fs = fs; _consumed = offset; }
            public long Position { get { return _consumed; } }

            public bool Next(out byte[] line, out int len, out long lineOffset)
            {
                line = null; len = 0; lineOffset = _consumed;
                _line.SetLength(0);
                while (true)
                {
                    if (_pos >= _len)
                    {
                        _len = _fs.Read(_buf, 0, _buf.Length);
                        _pos = 0;
                        if (_len <= 0) { _len = 0; return false; } // EOF before '\n'
                    }
                    int nl = Array.IndexOf(_buf, (byte)'\n', _pos, _len - _pos);
                    if (nl < 0)
                    {
                        _line.Write(_buf, _pos, _len - _pos);
                        _pos = _len;
                        continue;
                    }
                    _line.Write(_buf, _pos, nl - _pos);
                    _pos = nl + 1;
                    lineOffset = _consumed;
                    _consumed += _line.Length + 1;
                    line = _line.GetBuffer();
                    len = (int)_line.Length;
                    if (len > 0 && line[len - 1] == '\r') len--;
                    return true;
                }
            }
        }

        // ---- archives -------------------------------------------------------------------------

        // Codex archives a session by compressing it to archived_sessions\rollout-….jsonl.zst. .NET Framework
        // cannot decompress zstd, so the content is out of reach — but the frame header states the
        // uncompressed size, which tells exactly whether we had already read all of it.
        static void ScanArchives(Db db, Consent consent, DateTime earliest)
        {
            string dir = Path.Combine(Paths.CodexHome, "archived_sessions");
            if (!Directory.Exists(dir)) return;
            string[] zsts;
            try { zsts = Directory.GetFiles(dir, "rollout-*.jsonl.zst", SearchOption.AllDirectories); }
            catch (Exception) { return; }
            foreach (string z in zsts)
            {
                string original = Path.GetFileName(z).Substring(0, Path.GetFileName(z).Length - 4);
                var row = db.One("SELECT * FROM files WHERE name = ? ORDER BY offset DESC LIMIT 1", original);
                if (row != null && Store.Str(row, "state") != "active") continue;
                if (row == null)
                {
                    DateTime mtime;
                    try { mtime = File.GetLastWriteTimeUtc(z); } catch (Exception) { continue; }
                    if (mtime < earliest) continue; // archived before recording was ever on
                    db.Write(delegate
                    {
                        AddIssue(db, original, SessionFromName(original), new Issue { Category = "missing", Code = "archived_unread", Offset = 0,
                            Detail = "会话在被采集前已归档为 zstd 压缩文件（" + Path.GetFileName(z) + "），内容无法读取；是否在授权范围内未知" });
                    });
                    continue;
                }
                if (File.Exists(Store.Str(row, "path"))) continue; // the plain file is still there
                long? size = ZstdContentSize(z);
                long offset = Store.Long(row, "offset");
                var ctx = ParseContext.FromJson(Store.Str(row, "ctx"), Store.Str(row, "session_id"));
                bool complete = size.HasValue && offset >= size.Value;
                var known = row;
                db.Write(delegate
                {
                    db.Exec("UPDATE files SET state = ?, path = ?, updated_at = ? WHERE file_id = ?",
                        complete ? "archived_complete" : "archived_partial", z, Store.Now(), Store.Str(known, "file_id"));
                    if (!complete && consent.InScope(ctx.Cwd))
                        AddIssue(db, original, Store.Str(known, "session_id"), new Issue { Category = "missing", Code = "archived_compressed",
                            Offset = offset, Count = size.HasValue ? size.Value - offset : 0,
                            Detail = size.HasValue
                                ? "会话归档为 zstd 压缩文件，归档前还有 " + (size.Value - offset) + " 字节（偏移 " + offset + "–" + size.Value + "）未采集，无法解压读取"
                                : "会话归档为 zstd 压缩文件，压缩头未写原始大小，偏移 " + offset + " 之后的内容无法确认是否已采集" });
                });
            }
        }

        public static long? ZstdContentSize(string path)
        {
            try
            {
                var b = new byte[18];
                int n;
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete)) n = fs.Read(b, 0, b.Length);
                if (n < 6 || b[0] != 0x28 || b[1] != 0xB5 || b[2] != 0x2F || b[3] != 0xFD) return null;
                int fhd = b[4], fcsFlag = fhd >> 6, single = (fhd >> 5) & 1, dictFlag = fhd & 3;
                int pos = 5 + (single == 1 ? 0 : 1) + new[] { 0, 1, 2, 4 }[dictFlag];
                int size = fcsFlag == 0 ? (single == 1 ? 1 : 0) : fcsFlag == 1 ? 2 : fcsFlag == 2 ? 4 : 8;
                if (size == 0 || pos + size > n) return null;
                long v = 0;
                for (int i = size - 1; i >= 0; i--) v = (v << 8) | b[pos + i];
                return size == 2 ? v + 256 : v;
            }
            catch (Exception) { return null; }
        }

        // ---- plugin switch, files, status -----------------------------------------------------

        // [plugins."codex-native-assist@<marketplace>"] enabled = false  in CODEX_HOME\config.toml (read only).
        // Codex keeps a disabled plugin's servers running in sessions that were already open (measured).
        // True when this process records regardless of what Codex says about the plugin. The background
        // daemon sets it: the product records even after the plugin is disabled or removed, and only the
        // user's own consent (or the stop command) ends that.
        public static bool IgnorePluginDisabled;

        public static bool PluginDisabledInCodex()
        {
            string text = Paths.ReadText(Path.Combine(Paths.CodexHome, "config.toml"));
            if (text == null) return false;
            bool ours = false;
            foreach (string raw in text.Split('\n'))
            {
                string l = raw.Trim();
                if (l.StartsWith("["))
                {
                    ours = l.StartsWith("[plugins.\"codex-native-assist@", StringComparison.Ordinal);
                    continue;
                }
                if (ours && l.Replace(" ", "").StartsWith("enabled=false", StringComparison.Ordinal)) return true;
            }
            return false;
        }

        // The App's plugin switch kills every server of the plugin at once, so nobody is left to notice
        // "disabled" (measured). When a collector starts and finds that nobody was watching for a while
        // AND config.toml changed meanwhile, the plugin may have been off during that gap: treat the gap as
        // not consented (close the period where the last collector stopped, reopen at the config change).
        // A plain App restart without config changes keeps the gap, so catch-up still works.
        bool _gapChecked;

        bool ExcludeUnwatchedGap(Db db)
        {
            if (_gapChecked) return false;
            _gapChecked = true;
            var c = db.One("SELECT state, last_pass FROM collector WHERE id = 1");
            DateTime last;
            if (Store.Str(c, "state") != "on" || !Store.TryParse(Store.Str(c, "last_pass"), out last)) return false;
            if (DateTime.UtcNow - last < TimeSpan.FromMilliseconds(Math.Max(15000, 3 * PollMs))) return false;
            DateTime? cfg = ConfigWriteTime();
            if (cfg == null || cfg.Value <= last) return false;
            Consent.Suspend(db, last);
            Consent.Resume(db, cfg.Value);
            Log.Lifecycle("consent_gap_excluded", Json.D("from", Store.Iso(last), "to", Store.Iso(cfg.Value)));
            return true;
        }

        // config.toml's last write, asked from an open handle (the directory entry can lag behind).
        static DateTime? ConfigWriteTime()
        {
            try
            {
                using (var fs = new FileStream(Path.Combine(Paths.CodexHome, "config.toml"), FileMode.Open, FileAccess.Read,
                                               FileShare.ReadWrite | FileShare.Delete))
                {
                    BY_HANDLE_FILE_INFORMATION i;
                    if (!GetFileInformationByHandle(fs.SafeFileHandle, out i)) return null;
                    long ft = ((long)(uint)i.Written.dwHighDateTime << 32) | (uint)i.Written.dwLowDateTime;
                    return DateTime.FromFileTimeUtc(ft);
                }
            }
            catch (Exception) { return null; }
        }

        static IEnumerable<string> RolloutFiles()
        {
            foreach (string sub in new[] { "sessions", "archived_sessions" })
            {
                string root = Path.Combine(Paths.CodexHome, sub);
                if (!Directory.Exists(root)) continue;
                string[] files;
                try { files = Directory.GetFiles(root, "rollout-*.jsonl", SearchOption.AllDirectories); }
                catch (Exception) { continue; }
                foreach (var f in files) yield return f;
            }
        }

        // rollout-2026-10-29T01-58-58-<uuid>.jsonl → <uuid>
        static string SessionFromName(string name)
        {
            string stem = name.EndsWith(".jsonl") ? name.Substring(0, name.Length - 6) : name;
            return stem.Length > 36 ? stem.Substring(stem.Length - 36) : stem;
        }

        [StructLayout(LayoutKind.Sequential)]
        struct BY_HANDLE_FILE_INFORMATION
        {
            public uint Attributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME Created, Accessed, Written;
            public uint VolumeSerial, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        static extern bool GetFileInformationByHandle(SafeFileHandle h, out BY_HANDLE_FILE_INFORMATION info);

        // Volume serial + NTFS file index: survives renames and moves within the volume.
        static string FileId(SafeFileHandle h)
        {
            BY_HANDLE_FILE_INFORMATION i;
            if (!GetFileInformationByHandle(h, out i)) return null;
            return i.VolumeSerial.ToString("x8") + ":" + i.IndexHigh.ToString("x8") + i.IndexLow.ToString("x8");
        }

        void SetState(Db db, string state, string error, bool force)
        {
            bool changed = state != _lastState;
            if (!changed && !force && error == null && DateTime.UtcNow - _lastStateWrite < TimeSpan.FromSeconds(10)) return;
            string now = Store.Now();
            int pid = Process.GetCurrentProcess().Id;
            db.Write(delegate
            {
                db.Exec("UPDATE collector SET leader_pid = ?, last_pass = ?, last_error = ?, passes = passes + 1, " +
                        "state_since = CASE WHEN state IS ? AND state_since IS NOT NULL THEN state_since ELSE ? END, state = ? WHERE id = 1",
                    pid, now, error, state, now, state);
            });
            if (changed) Log.Lifecycle("collector_state", Json.D("state", state, "error", error));
            _lastState = state;
            _lastStateWrite = DateTime.UtcNow;
        }

        public static void MarkStopped(string reason)
        {
            try
            {
                using (var db = Store.Open())
                {
                    string now = Store.Now();
                    db.Write(delegate
                    {
                        db.Exec("UPDATE collector SET state = 'stopped', state_since = ?, last_pass = ?, last_error = ? " +
                                "WHERE id = 1 AND leader_pid = ? AND state = 'on'", now, now, reason, Process.GetCurrentProcess().Id);
                    });
                }
            }
            catch (Exception) { } // being killed mid-way is fine: the gap is then treated conservatively
        }

        public static Dictionary<string, object> Stats()
        {
            try
            {
                using (var db = Store.Open())
                {
                    var c = db.One("SELECT * FROM collector WHERE id = 1");
                    var counts = db.One("SELECT COUNT(*) AS events, COUNT(DISTINCT session_id) AS sessions FROM events");
                    var kinds = new Dictionary<string, object>();
                    foreach (var r in db.Query("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")) kinds[Store.Str(r, "kind")] = Store.Long(r, "n");
                    var missing = new Dictionary<string, object>();
                    foreach (var r in db.Query("SELECT code, SUM(count) AS n FROM issues WHERE category IN ('missing','parse') GROUP BY code"))
                        missing[Store.Str(r, "code")] = Store.Long(r, "n");
                    var unrecognized = new Dictionary<string, object>();
                    foreach (var r in db.Query("SELECT category, SUM(count) AS n FROM skipped WHERE category LIKE 'unrecognized:%' GROUP BY category"))
                        unrecognized[Store.Str(r, "category").Substring("unrecognized:".Length)] = Store.Long(r, "n");
                    int leader = (int)Store.Long(c, "leader_pid");
                    bool alive = leader != 0 && Native.Alive(leader) && IsOurs(leader);
                    return Json.D("sessions", Store.Long(counts, "sessions"), "events", Store.Long(counts, "events"), "by_kind", kinds,
                        "missing", missing, "unrecognized", unrecognized,
                        "collector_alive", alive, "leader_pid", alive ? (object)leader : null, "state", Store.Str(c, "state"),
                        "state_since", Store.Str(c, "state_since"), "last_pass", Store.Str(c, "last_pass"),
                        "last_error", Store.Str(c, "last_error"), "plugin_disabled", PluginDisabledInCodex(), "db", Store.DbPath,
                        "sync", Uploader.Stats(db));
                }
            }
            catch (Exception ex)
            {
                return Json.D("error", ex.GetType().Name + ": " + ex.Message, "sessions", 0L, "events", 0L);
            }
        }

        public static bool IsOurs(int pid)
        {
            Native.ProcInfo p;
            // assist.exe in the plugin folder or assist-<build>.exe, its copy in the data folder
            return Native.Snapshot().TryGetValue(pid, out p) && p.Name != null &&
                   p.Name.StartsWith("assist", StringComparison.OrdinalIgnoreCase) && p.Name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase);
        }
    }
}
