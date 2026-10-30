using System;
using System.Collections.Generic;
using System.IO;
using System.Security.Cryptography;
using System.Text;

namespace CodexNativeAssist
{
    // Uploads recorded events to the sync backend (docs/sync-api.md §8) and deletes each one from the local
    // store as soon as the backend has confirmed it (accepted, duplicate or rejected), so the store only
    // holds what is still on its way. Runs on the collector leader right after each pass.
    //
    // Recording consent covers the upload (no separate switch; users are told). Nothing is sent while
    // recording is off or the plugin is disabled, and nothing at all without a configured address — events
    // then simply stay local. An event whose fate is unknown (timeout, incomplete answer) stays local and is
    // sent again; the backend deduplicates by event_id.
    sealed class Uploader
    {
        const int MaxEvents = 200, MaxBytes = 1024 * 1024, BatchesPerRun = 5, TimeoutMs = 15000;
        DateTime _nextTry = DateTime.MinValue, _lastReclaim = DateTime.MinValue;
        int _backoffSec, _limit = MaxEvents;

        public void Run(Db db)
        {
            string raw = Settings.SyncBaseUrl(), error;
            string baseUrl = string.IsNullOrEmpty(raw) ? null : JsonHttp.CheckBaseUrl(raw, "上传服务地址", out error);
            if (baseUrl == null)
            {
                SetState(db, string.IsNullOrEmpty(raw) ? "not_configured" : "bad_url", null);
                return;
            }
            if (Store.Long(db.One("SELECT COUNT(*) AS n FROM sync WHERE id = 1 AND state = 'revoked'"), "n") > 0) return;
            if (DateTime.UtcNow < _nextTry) return;
            bool deleted = false;
            try
            {
                for (int b = 0; b < BatchesPerRun; b++)
                {
                    // Re-checked before every batch: once recording is off, nothing more leaves the machine.
                    var consent = Consent.Load(db);
                    if (!consent.Enabled) return;
                    if (!Collector.IgnorePluginDisabled && Collector.PluginDisabledInCodex()) return;
                    var rows = NextBatch(db);
                    if (rows.Count == 0) break;
                    var device = Device(db, baseUrl, consent);
                    deleted |= Send(db, baseUrl, device, rows);
                }
                _backoffSec = 0;
                long pending = Store.Long(db.One("SELECT COUNT(*) AS n FROM events"), "n");
                SetState(db, pending > 0 ? "uploading" : "idle", null);
            }
            catch (ApiException ex)
            {
                Failed(db, ex);
            }
            catch (Exception ex)
            {
                Backoff(db, "error", ex.GetType().Name, null);
            }
            finally
            {
                if (deleted) Reclaim(db);
            }
        }

        // Oldest first (insertion order = per-session order), at most MaxEvents / MaxBytes; a single event
        // larger than MaxBytes goes alone.
        List<Dictionary<string, object>> NextBatch(Db db)
        {
            var rows = new List<Dictionary<string, object>>();
            long bytes = 0;
            foreach (var r in db.Query("SELECT event_key, ordinal, idx, body FROM events ORDER BY rowid LIMIT ?", (long)_limit))
            {
                long size = Encoding.UTF8.GetByteCount(Store.Str(r, "body") ?? "");
                if (rows.Count > 0 && bytes + size > MaxBytes) break;
                rows.Add(r);
                bytes += size;
            }
            return rows;
        }

        // Returns true when it deleted something locally.
        bool Send(Db db, string baseUrl, Dictionary<string, object> device, List<Dictionary<string, object>> rows)
        {
            var events = new List<object>();
            var sent = new List<string>();
            foreach (var r in rows)
            {
                var e = Json.ParseObject(Store.Str(r, "body")) ?? new Dictionary<string, object>();
                string key = Store.Str(r, "event_key");
                e["event_id"] = key;
                e["seq"] = Store.Long(r, "ordinal") * 1000 + Store.Long(r, "idx");
                events.Add(e);
                sent.Add(key);
            }
            var batch = Json.D("schema", "assist.sync.batch/v1", "batch_id", Guid.NewGuid().ToString("N"),
                "client", "codex-native-assist/" + Program.Version, "events", events);
            Dictionary<string, object> res;
            try
            {
                res = JsonHttp.Call(baseUrl, TimeoutMs, "X-Ctr-Client", "POST", "/v1/events/batch",
                    Json.Str(device, "device_token"), Json.Stringify(batch));
            }
            catch (ApiException ex)
            {
                bool whole = ex.Kind == "http" && (ex.Status == 413 || (ex.Status == 400 && ex.Code == "invalid_batch"));
                if (!whole) throw;
                if (rows.Count > 1)
                {
                    _limit = Math.Max(1, rows.Count / 2); // find the event the backend objects to
                    return false;
                }
                Finish(db, new List<string>(), 0, 0, sent); // a single event the backend will never take: drop it
                return true;
            }
            _limit = MaxEvents;
            var done = new List<string>();
            var rejected = new List<string>();
            long accepted = 0, duplicates = 0;
            var wanted = new HashSet<string>(sent);
            foreach (var o in Json.List(res, "results"))
            {
                var item = o as Dictionary<string, object>;
                string id = Json.Str(item, "event_id"), status = Json.Str(item, "status");
                if (id == null || !wanted.Remove(id)) continue;
                if (status == "accepted") { accepted++; done.Add(id); }
                else if (status == "duplicate") { duplicates++; done.Add(id); }
                else if (status == "rejected") rejected.Add(id);
                else wanted.Add(id); // unknown status: keep it and send again
            }
            Finish(db, done, accepted, duplicates, rejected);
            return done.Count + rejected.Count > 0;
        }

        static void Finish(Db db, List<string> done, long accepted, long duplicates, List<string> rejected)
        {
            db.Write(delegate
            {
                foreach (var id in done) db.Exec("DELETE FROM events WHERE event_key = ?", id);
                foreach (var id in rejected) db.Exec("DELETE FROM events WHERE event_key = ?", id);
                db.Exec("UPDATE sync SET uploaded = uploaded + ?, duplicates = duplicates + ?, rejected = rejected + ?, " +
                        "last_upload_at = ?, updated_at = ? WHERE id = 1",
                    accepted + duplicates, duplicates, (long)rejected.Count, Store.Now(), Store.Now());
            });
            if (rejected.Count > 0) Log.Lifecycle("sync_rejected", Json.D("count", rejected.Count));
        }

        // The device token for this backend; registers on first use or when the address changed.
        static Dictionary<string, object> Device(Db db, string baseUrl, Consent consent)
        {
            var d = SyncVault.Load();
            if (d != null && Json.Str(d, "base_url") == baseUrl && !string.IsNullOrEmpty(Json.Str(d, "device_token"))) return d;
            var res = JsonHttp.Call(baseUrl, TimeoutMs, "X-Ctr-Client", "POST", "/v1/devices", null, Json.Stringify(Json.D(
                "client", "codex-native-assist/" + Program.Version, "platform", "windows",
                "consent", Json.D("version", consent.Version, "accepted_at", consent.ConsentedAt))));
            string token = Json.Str(res, "device_token"), id = Json.Str(res, "device_id");
            if (string.IsNullOrEmpty(token) || !token.StartsWith("ctrd_"))
                throw new ApiException("bad_response", "device_token", 0, null, null, null);
            d = Json.D("base_url", baseUrl, "device_id", id, "device_token", token, "created_at", Json.Str(res, "created_at"));
            SyncVault.Save(d);
            db.Write(delegate { db.Exec("UPDATE sync SET device_id = ?, updated_at = ? WHERE id = 1", id, Store.Now()); });
            Log.Lifecycle("sync_registered", Json.D("device_id", id));
            return d;
        }

        void Failed(Db db, ApiException ex)
        {
            if (ex.Kind == "http" && ex.Status == 401)
            {
                SyncVault.Clear(); // device_invalid: register again on the next run
                Backoff(db, "retrying", ex.Code, 2);
            }
            else if (ex.Kind == "http" && ex.Status == 403)
            {
                SyncVault.Clear(); // device_revoked: the backend does not want this device any more
                SetState(db, "revoked", ex.Code);
                Log.Lifecycle("sync_revoked", null);
            }
            else if (ex.Kind == "http" && ex.Status == 429)
                Backoff(db, "retrying", ex.Code, Math.Max(2, Math.Min(300, ex.RetryAfter ?? 5)));
            else
                Backoff(db, "retrying", ex.Kind == "http" ? ex.Code : ex.Kind + ":" + ex.Detail, null);
        }

        void Backoff(Db db, string state, string error, int? seconds)
        {
            _backoffSec = seconds ?? Math.Min(300, _backoffSec == 0 ? 2 : _backoffSec * 2);
            _nextTry = DateTime.UtcNow.AddSeconds(_backoffSec);
            SetState(db, state, error);
        }

        static void SetState(Db db, string state, string error)
        {
            var row = db.One("SELECT state, last_error FROM sync WHERE id = 1");
            if (row != null && Store.Str(row, "state") == state && Store.Str(row, "last_error") == error) return;
            db.Write(delegate { db.Exec("UPDATE sync SET state = ?, last_error = ?, updated_at = ? WHERE id = 1", state, error, Store.Now()); });
        }

        // Give deleted pages back to the file system: incremental vacuum once the file allows it, and a
        // one-time VACUUM to switch an older store (created without auto_vacuum) over. At most once a minute.
        void Reclaim(Db db)
        {
            if ((DateTime.UtcNow - _lastReclaim).TotalSeconds < 60) return;
            _lastReclaim = DateTime.UtcNow;
            try
            {
                long mode = Store.Long(db.One("PRAGMA auto_vacuum"), "auto_vacuum");
                long free = Store.Long(db.One("PRAGMA freelist_count"), "freelist_count");
                if (mode == 2) db.Exec("PRAGMA incremental_vacuum");
                else if (free >= 256) { db.Exec("PRAGMA auto_vacuum=INCREMENTAL"); db.Exec("VACUUM"); }
                db.Exec("PRAGMA wal_checkpoint(TRUNCATE)");
            }
            catch (Exception ex) { Log.Lifecycle("sync_reclaim_failed", Json.D("error", ex.GetType().Name)); }
        }

        public static Dictionary<string, object> Stats(Db db)
        {
            var s = db.One("SELECT * FROM sync WHERE id = 1") ?? new Dictionary<string, object>();
            string raw = Settings.SyncBaseUrl(), error;
            bool configured = !string.IsNullOrEmpty(raw) && JsonHttp.CheckBaseUrl(raw, "", out error) != null;
            return Json.D("configured", configured, "state", Store.Str(s, "state"), "device_id", Store.Str(s, "device_id"),
                "uploaded", Store.Long(s, "uploaded"), "duplicates", Store.Long(s, "duplicates"), "rejected", Store.Long(s, "rejected"),
                "last_upload_at", Store.Str(s, "last_upload_at"), "last_error", Store.Str(s, "last_error"));
        }
    }

    // sync.bin: DPAPI (current user) — the device token never sits on disk in clear text.
    static class SyncVault
    {
        static readonly byte[] Entropy = Encoding.UTF8.GetBytes("codex-native-assist/sync/v1");
        static string FilePath { get { return Paths.In("sync.bin"); } }

        public static Dictionary<string, object> Load()
        {
            try
            {
                if (!File.Exists(FilePath)) return null;
                byte[] plain = ProtectedData.Unprotect(File.ReadAllBytes(FilePath), Entropy, DataProtectionScope.CurrentUser);
                return Json.ParseObject(Encoding.UTF8.GetString(plain));
            }
            catch (Exception) { return null; } // tampered or from another user: register again
        }

        public static void Save(Dictionary<string, object> device)
        {
            byte[] data = ProtectedData.Protect(Encoding.UTF8.GetBytes(Json.Stringify(device)), Entropy, DataProtectionScope.CurrentUser);
            string tmp = FilePath + ".tmp";
            File.WriteAllBytes(tmp, data);
            if (File.Exists(FilePath)) File.Replace(tmp, FilePath, null); else File.Move(tmp, FilePath);
        }

        public static void Clear()
        {
            try { File.Delete(FilePath); } catch (Exception) { }
        }
    }
}
