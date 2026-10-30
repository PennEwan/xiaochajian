using System;
using System.Collections;
using System.Collections.Generic;
using System.Text;
using System.Web.Script.Serialization;

namespace CodexNativeAssist
{
    // Thin wrapper over JavaScriptSerializer (part of .NET Framework). Objects come back as
    // Dictionary<string, object>, arrays as object[] / ArrayList, numbers as int/long/decimal.
    static class Json
    {
        static JavaScriptSerializer New()
        {
            var s = new JavaScriptSerializer();
            s.MaxJsonLength = int.MaxValue;
            s.RecursionLimit = 256;
            return s;
        }

        public static object Parse(string text)
        {
            return New().DeserializeObject(text);
        }

        public static Dictionary<string, object> ParseObject(string text)
        {
            try { return Parse(text) as Dictionary<string, object>; }
            catch (Exception) { return null; }
        }

        public static string Stringify(object value)
        {
            // JavaScriptSerializer escapes < > & ' as \uXXXX; still valid JSON.
            return New().Serialize(value);
        }

        public static Dictionary<string, object> Obj(Dictionary<string, object> d, string key)
        {
            object v;
            return d != null && d.TryGetValue(key, out v) ? v as Dictionary<string, object> : null;
        }

        public static string Str(Dictionary<string, object> d, string key)
        {
            object v;
            return d != null && d.TryGetValue(key, out v) ? v as string : null;
        }

        public static bool Bool(Dictionary<string, object> d, string key, bool fallback)
        {
            object v;
            if (d != null && d.TryGetValue(key, out v) && v is bool) return (bool)v;
            return fallback;
        }

        public static long Long(Dictionary<string, object> d, string key, long fallback)
        {
            object v;
            if (d == null || !d.TryGetValue(key, out v) || v == null) return fallback;
            if (v is int) return (int)v;
            if (v is long) return (long)v;
            if (v is decimal) return (long)(decimal)v;
            if (v is double) return (long)(double)v;
            return fallback;
        }

        public static List<object> List(Dictionary<string, object> d, string key)
        {
            object v;
            if (d == null || !d.TryGetValue(key, out v) || v == null) return new List<object>();
            var arr = v as IEnumerable;
            if (arr == null || v is string || v is IDictionary) return new List<object>();
            var list = new List<object>();
            foreach (var item in arr) list.Add(item);
            return list;
        }

        public static Dictionary<string, object> D(params object[] kv)
        {
            var d = new Dictionary<string, object>();
            for (int i = 0; i + 1 < kv.Length; i += 2) d[(string)kv[i]] = kv[i + 1];
            return d;
        }
    }

    static class Cli
    {
        public static void WriteLine(string text)
        {
            // Works for a GUI-subsystem exe when stdout is redirected (pipes, files).
            var bytes = new UTF8Encoding(false).GetBytes(text + "\n");
            using (var o = Console.OpenStandardOutput())
            {
                o.Write(bytes, 0, bytes.Length);
                o.Flush();
            }
        }

        public static int Config(string[] args)
        {
            for (int i = 1; i < args.Length; i++)
            {
                if (args[i] == "--base-url" && i + 1 < args.Length)
                {
                    string error;
                    string url = RechargeApi.CheckBaseUrl(args[i + 1], out error);
                    if (url == null) { WriteLine("{\"ok\":false,\"error\":" + Json.Stringify(error) + "}"); return 2; }
                    Settings.SaveUserBaseUrl(url);
                    WriteLine("{\"ok\":true,\"base_url\":" + Json.Stringify(url) + "}");
                    return 0;
                }
                if (args[i] == "--reset")
                {
                    Settings.SaveUserBaseUrl(null);
                    WriteLine("{\"ok\":true}");
                    return 0;
                }
                if (args[i] == "--sync-url" && i + 1 < args.Length)
                {
                    string error;
                    string url = JsonHttp.CheckBaseUrl(args[i + 1], "上传服务地址", out error);
                    if (url == null) { WriteLine("{\"ok\":false,\"error\":" + Json.Stringify(error) + "}"); return 2; }
                    Settings.SaveUserSyncUrl(url);
                    using (var db = Store.Open())
                        db.Write(delegate { db.Exec("UPDATE sync SET state = 'idle', last_error = NULL WHERE id = 1"); });
                    WriteLine("{\"ok\":true,\"sync_base_url\":" + Json.Stringify(url) + "}");
                    return 0;
                }
                if (args[i] == "--sync-reset")
                {
                    Settings.SaveUserSyncUrl(null);
                    WriteLine("{\"ok\":true}");
                    return 0;
                }
            }
            WriteLine(Json.Stringify(Json.D("base_url", Settings.RechargeBaseUrl(), "mock", Settings.IsMock(),
                "sync_base_url", Settings.SyncBaseUrl())));
            return 0;
        }
    }
}
