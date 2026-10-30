using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

namespace CodexNativeAssist
{
    // %LOCALAPPDATA%\CodexNativeAssist\assist.sqlite3 — the one place every process (each session's MCP
    // server, the windows, `assist.exe status`) coordinates through. WAL lets readers run alongside the
    // single writer; every write is a BEGIN IMMEDIATE transaction (Db.Write).
    static class Store
    {
        public const int SchemaVersion = 5;

        const string Ddl = @"
CREATE TABLE IF NOT EXISTS consent (
  id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL, scope TEXT NOT NULL, folders TEXT NOT NULL,
  version INTEGER NOT NULL, text_version TEXT, consented_at TEXT, updated_at TEXT, changed_by TEXT,
  suspended INTEGER NOT NULL DEFAULT 0, debug_default_at TEXT);
CREATE TABLE IF NOT EXISTS consent_periods (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS files (
  file_id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, session_id TEXT, offset INTEGER NOT NULL,
  size INTEGER NOT NULL, ctx TEXT, state TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS files_by_name ON files(name);
CREATE TABLE IF NOT EXISTS events (
  event_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, ordinal INTEGER, idx INTEGER NOT NULL, ts TEXT,
  kind TEXT NOT NULL, turn_id TEXT, model TEXT, call_id TEXT, file_id TEXT, offset INTEGER,
  collected_at TEXT NOT NULL, collector_pid INTEGER, body TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_by_session ON events(session_id, ordinal, idx);
CREATE INDEX IF NOT EXISTS events_by_collected ON events(collected_at);
CREATE TABLE IF NOT EXISTS issues (
  id INTEGER PRIMARY KEY AUTOINCREMENT, file TEXT NOT NULL, session_id TEXT, category TEXT NOT NULL,
  code TEXT NOT NULL, detail TEXT NOT NULL, offset INTEGER NOT NULL, ordinal INTEGER, count INTEGER NOT NULL,
  at TEXT NOT NULL, UNIQUE(file, offset, code));
CREATE TABLE IF NOT EXISTS skipped (
  file TEXT NOT NULL, session_id TEXT, category TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(file, category));
CREATE TABLE IF NOT EXISTS collector (
  id INTEGER PRIMARY KEY CHECK (id = 1), leader_pid INTEGER, state TEXT, last_pass TEXT, last_error TEXT,
  state_since TEXT, passes INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO consent(id, enabled, scope, folders, version) VALUES (1, 0, 'all', '[]', 0);
INSERT OR IGNORE INTO collector(id, state, passes) VALUES (1, 'idle', 0);
CREATE TABLE IF NOT EXISTS sync (
  id INTEGER PRIMARY KEY CHECK (id = 1), state TEXT, device_id TEXT, uploaded INTEGER NOT NULL DEFAULT 0,
  duplicates INTEGER NOT NULL DEFAULT 0, rejected INTEGER NOT NULL DEFAULT 0, last_upload_at TEXT,
  last_error TEXT, updated_at TEXT);
INSERT OR IGNORE INTO sync(id, state) VALUES (1, 'idle');
";

        public static string DbPath { get { return Paths.In("assist.sqlite3"); } }

        public static Db Open()
        {
            var db = new Db(DbPath, 5000);
            try
            {
                db.Exec("PRAGMA auto_vacuum=INCREMENTAL"); // takes effect for a new file; Uploader converts old ones
                db.Exec("PRAGMA journal_mode=WAL");
                db.Exec("PRAGMA synchronous=NORMAL");
                var v = db.One("PRAGMA user_version");
                if (v == null || Convert.ToInt64(v["user_version"]) < SchemaVersion)
                {
                    db.Write(delegate
                    {
                        foreach (string stmt in Ddl.Split(new[] { ";\r\n", ";\n" }, StringSplitOptions.RemoveEmptyEntries))
                            if (stmt.Trim().Length > 0) db.Exec(stmt.Trim().TrimEnd(';'));
                        var cols = new HashSet<string>();
                        foreach (var col in db.Query("PRAGMA table_info(consent)")) cols.Add(Str(col, "name"));
                        if (!cols.Contains("suspended")) db.Exec("ALTER TABLE consent ADD COLUMN suspended INTEGER NOT NULL DEFAULT 0");
                        if (!cols.Contains("debug_default_at")) db.Exec("ALTER TABLE consent ADD COLUMN debug_default_at TEXT");
                        db.Exec("PRAGMA user_version=" + SchemaVersion);
                    });
                }
                return db;
            }
            catch
            {
                db.Dispose();
                throw;
            }
        }

        public static string Now() { return Iso(DateTime.UtcNow); }
        public static string Iso(DateTime utc) { return utc.ToString("yyyy-MM-ddTHH:mm:ss.fffZ", CultureInfo.InvariantCulture); }

        public static bool TryParse(string s, out DateTime utc)
        {
            utc = DateTime.MinValue;
            if (string.IsNullOrEmpty(s)) return false;
            return DateTime.TryParse(s, CultureInfo.InvariantCulture,
                DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out utc);
        }

        public static long Long(Dictionary<string, object> row, string col)
        {
            object v;
            return row != null && row.TryGetValue(col, out v) && v != null ? Convert.ToInt64(v) : 0;
        }

        public static string Str(Dictionary<string, object> row, string col)
        {
            object v;
            return row != null && row.TryGetValue(col, out v) ? v as string : null;
        }
    }

    // Raised inside Db.Write to roll the transaction back on purpose (e.g. consent changed meanwhile).
    sealed class Rollback : Exception
    {
        public readonly string Reason;
        public Rollback(string reason) : base(reason) { Reason = reason; }
    }
}
