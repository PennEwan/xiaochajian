using System;
using System.Collections.Generic;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;

namespace CodexNativeAssist
{
    // One rollout line → csr.event/v1 events. A C# port of tools/session-reader/csr/parser.py, so the
    // plugin and the session reader agree on what one event is (same kinds, same event_key).
    // Canonical sources only: prompts and replies come from event_msg/item_completed, never also from
    // response_item/message; duplicates, policy omissions and unknown types are counted by name.
    sealed class ParseContext
    {
        public string SessionId, CodexVersion, ThreadModel, TurnId, LastTurnModel, Cwd, Originator;
        public List<string[]> TurnModels = new List<string[]>(); // [turn_id, model], newest last, max 64
        public long LastOrdinal = -1;
        public int BadSinceLast;
        public List<string> SensitiveCalls = new List<string>();

        public string ModelOf(string turnId)
        {
            if (turnId == null) return null;
            for (int i = TurnModels.Count - 1; i >= 0; i--) if (TurnModels[i][0] == turnId) return TurnModels[i][1];
            return null;
        }

        public string ToJson()
        {
            var tm = new List<object>();
            foreach (var p in TurnModels) tm.Add(new List<object> { p[0], p[1] });
            return Json.Stringify(Json.D("session_id", SessionId, "codex_version", CodexVersion, "thread_model", ThreadModel,
                "turn_id", TurnId, "last_turn_model", LastTurnModel, "cwd", Cwd, "originator", Originator, "turn_models", tm,
                "last_ordinal", LastOrdinal, "bad_since_last", BadSinceLast, "sensitive_calls", SensitiveCalls));
        }

        public static ParseContext FromJson(string json, string fallbackSession)
        {
            var c = new ParseContext { SessionId = fallbackSession };
            var d = Json.ParseObject(json ?? "");
            if (d == null) return c;
            c.SessionId = Json.Str(d, "session_id") ?? fallbackSession;
            c.CodexVersion = Json.Str(d, "codex_version");
            c.ThreadModel = Json.Str(d, "thread_model");
            c.TurnId = Json.Str(d, "turn_id");
            c.LastTurnModel = Json.Str(d, "last_turn_model");
            c.Cwd = Json.Str(d, "cwd");
            c.Originator = Json.Str(d, "originator");
            foreach (var p in Json.List(d, "turn_models"))
            {
                var pair = p as object[];
                if (pair != null && pair.Length == 2) c.TurnModels.Add(new[] { pair[0] as string, pair[1] as string });
            }
            c.LastOrdinal = Json.Long(d, "last_ordinal", -1);
            c.BadSinceLast = (int)Json.Long(d, "bad_since_last", 0);
            foreach (var s in Json.List(d, "sensitive_calls")) if (s is string) c.SensitiveCalls.Add((string)s);
            return c;
        }
    }

    sealed class Issue
    {
        public string Category, Code, Detail;
        public long Offset;
        public long? Ordinal;
        public long Count = 1;
    }

    sealed class Event
    {
        public string Key, SessionId, TurnId, Ts, Kind, Model, CallId, Body;
        public long? Ordinal;
        public int Idx;
        public long Offset;
    }

    sealed class Parsed
    {
        public List<Event> Events = new List<Event>();
        public List<Issue> Issues = new List<Issue>();
        public Dictionary<string, long> Skipped = new Dictionary<string, long>();
        public string Ts; // the record's timestamp (null if missing or unparsable)

        public void Skip(string what) { long n; Skipped.TryGetValue(what, out n); Skipped[what] = n + 1; }
    }

    static class RolloutParser
    {
        public const string Schema = "csr.event/v1";
        const int MaxField = 256 * 1024;
        static readonly HashSet<string> TurnScoped = new HashSet<string> { "user_prompt", "assistant_message",
            "reasoning_summary", "tool_call", "tool_result", "tool_execution", "token_usage", "turn_error" };
        const string NoTurn = "记录无 turn_id，且当时没有进行中的回合";
        const string NoTs = "记录缺少 timestamp";
        const string NoSummary = "模型未返回公开推理摘要（加密推理内容不解密）";
        const string SensitivePlaceholder = "[REDACTED:sensitive_file]";
        static readonly Regex SensitiveFile = new Regex(
            @"(?i)(?:^|[\\/\s""'=:(])(auth\.json|\.env(?:\.[\w.-]+)?|id_(?:rsa|ed25519|ecdsa|dsa)[\w.-]*|[\w.-]+\.(?:pem|pfx|p12)" +
            @"|cap_sid|credentials(?:\.json)?|\.netrc|\.git-credentials|\.npmrc|\.pypirc)(?=$|[\s""'\\/;|&),])", RegexOptions.Compiled);

        sealed class Rec
        {
            public long? Ordinal;
            public string Ts, Name, FileName;
            public long Offset;
        }

        public static Parsed ParseLine(byte[] raw, int len, long offset, ParseContext ctx, string fileName)
        {
            var o = new Parsed();
            string line;
            try { line = new UTF8Encoding(false, true).GetString(raw, 0, len); }
            catch (DecoderFallbackException)
            {
                ctx.BadSinceLast++;
                o.Issues.Add(new Issue { Category = "parse", Code = "bad_encoding", Detail = "不是 UTF-8，行长 " + len + " 字节", Offset = offset });
                return o;
            }
            object parsedObj;
            try { parsedObj = Json.Parse(line); }
            catch (Exception)
            {
                ctx.BadSinceLast++;
                o.Issues.Add(new Issue { Category = "parse", Code = "bad_json", Detail = "JSON 解析失败（行长 " + len + " 字节）", Offset = offset });
                return o;
            }
            var rec = parsedObj as Dictionary<string, object>;
            if (rec == null)
            {
                ctx.BadSinceLast++;
                o.Issues.Add(new Issue { Category = "parse", Code = "not_object", Detail = "顶层不是对象", Offset = offset });
                return o;
            }
            long? ordinal = null;
            object ordObj;
            if (rec.TryGetValue("ordinal", out ordObj) && (ordObj is int || ordObj is long)) ordinal = Convert.ToInt64(ordObj);
            if (ordinal.HasValue) TrackOrdinal(ordinal.Value, offset, ctx, o);

            string rtype = Json.Str(rec, "type");
            if (rtype == null)
            {
                o.Issues.Add(new Issue { Category = "parse", Code = "missing_type", Detail = "记录缺少字符串类型的 type", Offset = offset, Ordinal = ordinal });
                return o;
            }
            var p = Json.Obj(rec, "payload");
            if (p == null)
            {
                o.Issues.Add(new Issue { Category = "parse", Code = "bad_payload", Detail = rtype + " 的 payload 不是对象", Offset = offset, Ordinal = ordinal });
                return o;
            }
            string ts = Json.Str(rec, "timestamp");
            o.Ts = ts;
            string sub = Json.Str(p, "type");
            var r = new Rec { Ordinal = ordinal, Ts = ts, Offset = offset, Name = sub != null ? rtype + "/" + sub : rtype, FileName = fileName };

            switch (rtype)
            {
                case "session_meta": SessionMeta(p, r, ctx, o); break;
                case "turn_context": TurnContext(p, r, ctx, o); break;
                case "event_msg": EventMsg(sub, p, r, ctx, o); break;
                case "response_item": ResponseItem(sub, p, r, ctx, o); break;
                case "token_usage_record":
                    Emit(o, ctx, r, "token_usage", Json.D("response_id", Get(p, "response_id"), "usage", Get(p, "usage"),
                        "turn_token_usage", Get(p, "turn_token_usage"), "thread_token_usage", Get(p, "thread_token_usage")),
                        Json.Str(p, "turn_id"), null, null, null, false);
                    break;
                case "world_state": o.Skip("omitted:world_state"); break;
                default: o.Skip("unrecognized:" + r.Name); break;
            }
            return o;
        }

        static object Get(Dictionary<string, object> d, string k)
        {
            object v;
            return d != null && d.TryGetValue(k, out v) ? v : null;
        }

        static object Sub(object v, string k) { return Get(v as Dictionary<string, object>, k); }

        static void TrackOrdinal(long ordinal, long offset, ParseContext ctx, Parsed o)
        {
            if (ctx.LastOrdinal == long.MinValue) // reading started mid-file (first consent): nothing to compare with
            {
                ctx.LastOrdinal = ordinal;
                ctx.BadSinceLast = 0;
                return;
            }
            long expected = ctx.LastOrdinal + 1;
            if (ordinal > expected)
            {
                long gap = ordinal - expected - ctx.BadSinceLast; // unreadable lines are already parse failures
                if (gap > 0)
                    o.Issues.Add(new Issue { Category = "missing", Code = "ordinal_gap", Offset = offset, Ordinal = ordinal, Count = gap,
                        Detail = "缺少 " + gap + " 条记录（ordinal " + expected + "–" + (ordinal - 1) + " 之间）" });
            }
            else if (ordinal < expected)
            {
                o.Issues.Add(new Issue { Category = "order", Code = "ordinal_repeat", Offset = offset, Ordinal = ordinal,
                    Detail = "ordinal " + ordinal + " 不大于上一条 " + ctx.LastOrdinal + "（重复或乱序）" });
            }
            ctx.LastOrdinal = Math.Max(ctx.LastOrdinal, ordinal);
            ctx.BadSinceLast = 0;
        }

        static void Emit(Parsed o, ParseContext ctx, Rec r, string kind, Dictionary<string, object> data, string turnId,
                         string itemId, string callId, List<object> missing, bool sensitive)
        {
            missing = missing ?? new List<object>();
            bool inferred = false;
            if (string.IsNullOrEmpty(turnId))
            {
                turnId = null;
                if (TurnScoped.Contains(kind))
                {
                    if (ctx.TurnId != null) { turnId = ctx.TurnId; inferred = true; }
                    else missing.Add(Json.D("field", "turn_id", "reason", NoTurn));
                }
            }
            if (r.Ts == null) missing.Add(Json.D("field", "ts", "reason", NoTs));
            int redactions = 0;
            var counts = new Dictionary<string, object>();
            object clean = RedactObj(data, ref redactions);
            if (redactions > 0) counts["total"] = redactions;
            if (sensitive) counts["sensitive_file"] = 1;
            var truncated = new List<object>();
            clean = Truncate(clean, "data", truncated);
            int idx = o.Events.Count;
            string keyPos = r.Ordinal.HasValue ? r.Ordinal.Value.ToString() : "@" + r.Offset;
            string model = null;
            if (kind != "session_meta") model = (turnId != null ? ctx.ModelOf(turnId) : null) ?? ctx.ThreadModel;
            string key = ctx.SessionId + ":" + keyPos + ":" + idx;
            var body = Json.D("schema", Schema, "event_key", key, "session_id", ctx.SessionId, "turn_id", turnId,
                "turn_id_inferred", inferred, "ordinal", r.Ordinal, "ts", r.Ts, "kind", kind, "model", model,
                "item_id", itemId, "call_id", callId, "data", clean, "missing", missing,
                "source", Json.D("file", r.FileName, "offset", r.Offset, "record", r.Name, "codex_version", ctx.CodexVersion),
                "redactions", counts, "truncated", truncated);
            o.Events.Add(new Event { Key = key, SessionId = ctx.SessionId, TurnId = turnId, Ts = r.Ts, Kind = kind, Model = model,
                CallId = callId, Ordinal = r.Ordinal, Idx = idx, Offset = r.Offset, Body = Json.Stringify(body) });
        }

        static object RedactObj(object value, ref int n)
        {
            var s = value as string;
            if (s != null) { int c; s = Redact.Text(s, out c); n += c; return s; }
            var d = value as Dictionary<string, object>;
            if (d != null)
            {
                var o = new Dictionary<string, object>();
                foreach (var kv in d)
                {
                    var sv = kv.Value as string;
                    if (sv != null && sv.Length > 0 && Redact.IsSecretKey(kv.Key))
                    {
                        if (!sv.StartsWith("[REDACTED")) n++;
                        o[kv.Key] = "[REDACTED:key]";
                    }
                    else o[kv.Key] = RedactObj(kv.Value, ref n);
                }
                return o;
            }
            var arr = value as System.Collections.IEnumerable;
            if (arr != null)
            {
                var list = new List<object>();
                foreach (var item in arr) list.Add(RedactObj(item, ref n));
                return list;
            }
            return value;
        }

        static object Truncate(object value, string path, List<object> notes)
        {
            var s = value as string;
            if (s != null)
            {
                if (s.Length <= MaxField) return s;
                using (var sha = SHA256.Create())
                    notes.Add(Json.D("path", path, "original_length", s.Length,
                        "sha256", BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(s))).Replace("-", "").ToLowerInvariant()));
                return s.Substring(0, MaxField);
            }
            var d = value as Dictionary<string, object>;
            if (d != null)
            {
                var o = new Dictionary<string, object>();
                foreach (var kv in d) o[kv.Key] = Truncate(kv.Value, path + "." + kv.Key, notes);
                return o;
            }
            var list = value as List<object>;
            if (list != null)
            {
                var o = new List<object>();
                for (int i = 0; i < list.Count; i++) o.Add(Truncate(list[i], path + "[" + i + "]", notes));
                return o;
            }
            return value;
        }

        public static bool MentionsSensitiveFile(object v)
        {
            if (v == null) return false;
            string text = v as string ?? Json.Stringify(v);
            return SensitiveFile.IsMatch(text);
        }

        static void SessionMeta(Dictionary<string, object> p, Rec r, ParseContext ctx, Parsed o)
        {
            string sid = Json.Str(p, "id") ?? Json.Str(p, "session_id");
            if (!string.IsNullOrEmpty(sid) && sid != ctx.SessionId)
            {
                if (ctx.SessionId != null)
                    o.Issues.Add(new Issue { Category = "file", Code = "session_id_mismatch", Offset = r.Offset, Ordinal = r.Ordinal,
                        Detail = "文件名会话 ID " + ctx.SessionId + " 与 session_meta.id " + sid + " 不一致，以后者为准" });
                ctx.SessionId = sid;
            }
            if (Json.Str(p, "cli_version") != null) ctx.CodexVersion = Json.Str(p, "cli_version");
            if (Json.Str(p, "cwd") != null) ctx.Cwd = Json.Str(p, "cwd");
            if (Json.Str(p, "originator") != null) ctx.Originator = Json.Str(p, "originator");
            if (p.ContainsKey("creator_user_id") || p.ContainsKey("creator_account_id")) o.Skip("omitted:session_meta.creator_ids");
            string bi = Get(p, "base_instructions") as string;
            Emit(o, ctx, r, "session_meta", Json.D("session_id", sid, "cli_version", Get(p, "cli_version"), "originator", Get(p, "originator"),
                "source", Get(p, "source"), "thread_source", Get(p, "thread_source"), "cwd", Get(p, "cwd"),
                "model_provider", Get(p, "model_provider"), "context_window", Get(p, "context_window"),
                "history_mode", Get(p, "history_mode"), "base_instructions_length", bi != null ? (object)bi.Length : null),
                null, null, null, null, false);
        }

        static void ModelConfig(Parsed o, ParseContext ctx, Rec r, string scope, string turnId, Dictionary<string, object> data)
        {
            var d = new Dictionary<string, object> { { "scope", scope } };
            foreach (var kv in data) d[kv.Key] = kv.Value;
            var missing = new List<object>();
            foreach (var k in new[] { "model", "reasoning_effort" })
                if (Get(d, k) == null) missing.Add(Json.D("field", k, "reason", r.Name + " 未提供该字段"));
            Emit(o, ctx, r, "model_config", d, turnId, null, null, missing, false);
        }

        static void TurnContext(Dictionary<string, object> p, Rec r, ParseContext ctx, Parsed o)
        {
            string turnId = Json.Str(p, "turn_id"), model = Json.Str(p, "model");
            if (!string.IsNullOrEmpty(turnId))
            {
                ctx.TurnId = turnId;
                if (model != null)
                {
                    ctx.TurnModels.RemoveAll(x => x[0] == turnId);
                    ctx.TurnModels.Add(new[] { turnId, model });
                    while (ctx.TurnModels.Count > 64) ctx.TurnModels.RemoveAt(0);
                }
            }
            if (model != null) ctx.ThreadModel = model;
            if (Json.Str(p, "cwd") != null && ctx.Cwd == null) ctx.Cwd = Json.Str(p, "cwd");
            ModelConfig(o, ctx, r, "turn", turnId, Json.D("model", model, "reasoning_effort", Get(p, "effort"),
                "reasoning_summary", Get(p, "summary"), "approval_policy", Get(p, "approval_policy"),
                "approvals_reviewer", Get(p, "approvals_reviewer"), "sandbox_policy", Sub(Get(p, "sandbox_policy"), "type"),
                "network_access", Sub(Get(p, "sandbox_policy"), "network_access"),
                "collaboration_mode", Sub(Get(p, "collaboration_mode"), "mode"),
                "permission_profile", Sub(Get(p, "permission_profile"), "type"), "cwd", Get(p, "cwd")));
            if (model != null && ctx.LastTurnModel != null && model != ctx.LastTurnModel)
                Emit(o, ctx, r, "model_changed", Json.D("from", ctx.LastTurnModel, "to", model), turnId, null, null, null, false);
            if (model != null) ctx.LastTurnModel = model;
        }

        static void EndTurn(ParseContext ctx, string turnId)
        {
            if (turnId != null && ctx.TurnId == turnId) ctx.TurnId = null;
        }

        static void EventMsg(string sub, Dictionary<string, object> p, Rec r, ParseContext ctx, Parsed o)
        {
            string turnId = Json.Str(p, "turn_id");
            switch (sub)
            {
                case "thread_settings_applied":
                    var s = Json.Obj(p, "thread_settings") ?? new Dictionary<string, object>();
                    if (Json.Str(s, "model") != null) ctx.ThreadModel = Json.Str(s, "model");
                    ModelConfig(o, ctx, r, "thread", null, Json.D("model", Get(s, "model"), "reasoning_effort", Get(s, "reasoning_effort"),
                        "reasoning_summary", Get(s, "reasoning_summary"), "model_provider", Get(s, "model_provider_id"),
                        "service_tier", Get(s, "service_tier"), "approval_policy", Get(s, "approval_policy"),
                        "approvals_reviewer", Get(s, "approvals_reviewer"), "personality", Get(s, "personality"),
                        "collaboration_mode", Sub(Get(s, "collaboration_mode"), "mode"), "cwd", Get(s, "cwd")));
                    break;
                case "task_started":
                    if (!string.IsNullOrEmpty(turnId)) ctx.TurnId = turnId;
                    var attr = Get(p, "turn_attribution");
                    Emit(o, ctx, r, "turn_started", Json.D("started_at", Get(p, "started_at"), "model_context_window", Get(p, "model_context_window"),
                        "collaboration_mode_kind", Get(p, "collaboration_mode_kind"), "turn_trigger", Sub(attr, "turn_trigger"),
                        "parent_turn_id", Sub(attr, "parent_turn_id")), turnId, null, null, null, false);
                    break;
                case "task_complete":
                    var err = Get(p, "error");
                    var missing = new List<object>();
                    if (Get(p, "time_to_first_token_ms") == null)
                        missing.Add(Json.D("field", "time_to_first_token_ms", "reason", "记录未提供（出错或无输出的回合常见）"));
                    Emit(o, ctx, r, "turn_completed", Json.D("status", err != null ? "error" : "completed", "started_at", Get(p, "started_at"),
                        "completed_at", Get(p, "completed_at"), "duration_ms", Get(p, "duration_ms"),
                        "time_to_first_token_ms", Get(p, "time_to_first_token_ms"), "last_agent_message", Get(p, "last_agent_message")),
                        turnId, null, null, missing, false);
                    if (err != null)
                        Emit(o, ctx, r, "turn_error", Json.D("origin", "task_complete", "message", Sub(err, "message"),
                            "codex_error_info", Sub(err, "codex_error_info")), turnId, null, null, null, false);
                    EndTurn(ctx, turnId);
                    break;
                case "turn_aborted":
                    Emit(o, ctx, r, "turn_aborted", Json.D("reason", Get(p, "reason"), "started_at", Get(p, "started_at"),
                        "completed_at", Get(p, "completed_at"), "duration_ms", Get(p, "duration_ms")), turnId, null, null, null, false);
                    EndTurn(ctx, turnId);
                    break;
                case "error":
                    Emit(o, ctx, r, "turn_error", Json.D("origin", "error", "message", Get(p, "message"),
                        "codex_error_info", Get(p, "codex_error_info")), turnId, null, null, null, false);
                    break;
                case "token_count":
                    o.Skip("duplicate:event_msg/token_count");
                    if (Get(p, "rate_limits") != null) o.Skip("omitted:rate_limits");
                    break;
                case "item_completed":
                    Item(p, r, ctx, o);
                    break;
                default:
                    o.Skip("unrecognized:" + r.Name);
                    break;
            }
        }

        static long? DurationMs(object v)
        {
            var d = v as Dictionary<string, object>;
            if (d == null || !(Get(d, "secs") is int || Get(d, "secs") is long)) return null;
            return Convert.ToInt64(d["secs"]) * 1000 + Convert.ToInt64(Get(d, "nanos") ?? 0) / 1000000;
        }

        static string TextParts(object content, List<object> other)
        {
            var s = content as string;
            if (s != null) return s;
            var texts = new List<string>();
            var arr = content as System.Collections.IEnumerable;
            if (arr != null)
                foreach (var part in arr)
                {
                    var pd = part as Dictionary<string, object>;
                    if (pd == null) continue;
                    if (Get(pd, "text") is string) texts.Add((string)pd["text"]);
                    else other.Add(Convert.ToString(Get(pd, "type")));
                }
            return texts.Count > 0 ? string.Join("\n", texts) : null;
        }

        static string Outcome(object exitCode, object status, object error, object isError)
        {
            if (exitCode is int || exitCode is long) return Convert.ToInt64(exitCode) == 0 ? "success" : "error";
            if ((error != null && !(error is bool && !(bool)error)) || (isError is bool && (bool)isError)) return "error";
            var st = status as string;
            if (st != null)
            {
                st = st.ToLowerInvariant();
                if (new[] { "failed", "error", "declined", "rejected", "cancelled", "canceled", "timed_out" }.Contains(st)) return "error";
                if (new[] { "completed", "success", "succeeded", "ok" }.Contains(st)) return "success";
            }
            return "unknown";
        }

        static string OutputOutcome(object output)
        {
            var s = output as string;
            if (s == null) return "unknown";
            string head = s.TrimStart();
            head = head.Substring(0, Math.Min(200, head.Length)).ToLowerInvariant();
            foreach (var prefix in new[] { "exit code:", "process exited with code" })
            {
                if (!head.StartsWith(prefix)) continue;
                var parts = head.Substring(prefix.Length).Split(new[] { ' ', '\n', '\r', '\t' }, StringSplitOptions.RemoveEmptyEntries);
                long code;
                if (parts.Length > 0 && long.TryParse(parts[0], out code)) return code == 0 ? "success" : "error";
            }
            return "unknown";
        }

        static void Item(Dictionary<string, object> p, Rec r, ParseContext ctx, Parsed o)
        {
            var it = Json.Obj(p, "item") ?? new Dictionary<string, object>();
            string itype = Json.Str(it, "type");
            r.Name = "event_msg/item_completed:" + itype;
            string turnId = Json.Str(p, "turn_id"), itemId = Json.Str(it, "id");
            object startedMs = Get(p, "started_at_ms"), completedMs = Get(p, "completed_at_ms");
            switch (itype)
            {
                case "UserMessage":
                case "AgentMessage":
                {
                    var other = new List<object>();
                    string text = TextParts(Get(it, "content"), other);
                    var missing = new List<object>();
                    if (text == null) missing.Add(Json.D("field", "text", "reason", "消息没有文本部分" + (other.Count > 0 ? "（含非文本输入，不展开）" : "")));
                    var data = itype == "AgentMessage"
                        ? Json.D("text", text, "phase", Get(it, "phase"), "non_text_parts", other, "started_at_ms", startedMs, "completed_at_ms", completedMs)
                        : Json.D("text", text, "non_text_parts", other, "started_at_ms", startedMs, "completed_at_ms", completedMs);
                    Emit(o, ctx, r, itype == "UserMessage" ? "user_prompt" : "assistant_message", data, turnId, itemId, null, missing, false);
                    break;
                }
                case "Reasoning":
                {
                    object summary = Get(it, "summary_text");
                    string text;
                    var sl = summary as System.Collections.IEnumerable;
                    if (summary is string) text = (string)summary;
                    else if (sl != null) text = string.Join("\n", sl.Cast<object>().OfType<string>());
                    else text = null;
                    if (string.IsNullOrEmpty(text)) text = null;
                    if (Get(it, "raw_content") != null && !(Get(it, "raw_content") is object[] && ((object[])Get(it, "raw_content")).Length == 0))
                        o.Skip("omitted:reasoning_raw_content");
                    var missing = new List<object>();
                    if (text == null) missing.Add(Json.D("field", "summary", "reason", NoSummary));
                    Emit(o, ctx, r, "reasoning_summary", Json.D("summary", text, "started_at_ms", startedMs, "completed_at_ms", completedMs),
                        turnId, itemId, null, missing, false);
                    break;
                }
                case "CommandExecution":
                {
                    object command = Get(it, "command"), output = Get(it, "aggregated_output");
                    bool sensitive = false;
                    if (MentionsSensitiveFile(command)) { output = SensitivePlaceholder; sensitive = true; }
                    Emit(o, ctx, r, "tool_execution", Json.D("tool_name", "CommandExecution", "command", command, "cwd", Get(it, "cwd"),
                        "exit_code", Get(it, "exit_code"), "status", Get(it, "status"), "duration_ms", DurationMs(Get(it, "duration")),
                        "source", Get(it, "source"), "output", output, "outcome", Outcome(Get(it, "exit_code"), Get(it, "status"), null, null),
                        "started_at_ms", startedMs, "completed_at_ms", completedMs), turnId, itemId, null, null, sensitive);
                    break;
                }
                case "McpToolCall":
                {
                    object args = Get(it, "arguments"), result = Get(it, "result");
                    bool sensitive = false;
                    if (MentionsSensitiveFile(args)) { result = SensitivePlaceholder; sensitive = true; }
                    Emit(o, ctx, r, "tool_execution", Json.D("tool_name", "mcp:" + Get(it, "server") + "/" + Get(it, "tool"),
                        "server", Get(it, "server"), "tool", Get(it, "tool"), "arguments", args, "result", result, "error", Get(it, "error"),
                        "status", Get(it, "status"), "duration_ms", DurationMs(Get(it, "duration")),
                        "outcome", Outcome(null, Get(it, "status"), Get(it, "error"), Sub(Get(it, "result"), "isError")),
                        "started_at_ms", startedMs, "completed_at_ms", completedMs), turnId, itemId, null, null, sensitive);
                    break;
                }
                case "FileChange":
                    Emit(o, ctx, r, "tool_execution", Json.D("tool_name", "FileChange", "changes", Get(it, "changes"), "status", Get(it, "status"),
                        "outcome", Outcome(null, Get(it, "status"), null, null), "started_at_ms", startedMs, "completed_at_ms", completedMs),
                        turnId, itemId, null, null, false);
                    break;
                default:
                    o.Skip("unrecognized:" + r.Name);
                    break;
            }
        }

        static void ResponseItem(string sub, Dictionary<string, object> p, Rec r, ParseContext ctx, Parsed o)
        {
            string callId = Json.Str(p, "call_id");
            switch (sub)
            {
                case "function_call":
                case "custom_tool_call":
                case "local_shell_call":
                case "web_search_call":
                {
                    Dictionary<string, object> data;
                    object probe;
                    if (sub == "function_call")
                    {
                        object args = Get(p, "arguments");
                        if (args is string) { try { args = Json.Parse((string)args); } catch (Exception) { } }
                        data = Json.D("tool_name", Get(p, "name"), "arguments", args);
                        probe = args;
                    }
                    else if (sub == "custom_tool_call")
                    {
                        data = Json.D("tool_name", Get(p, "name"), "input", Get(p, "input"));
                        probe = Get(p, "input");
                    }
                    else
                    {
                        callId = callId ?? Json.Str(p, "id");
                        data = Json.D("tool_name", sub.Substring(0, sub.Length - "_call".Length), "action", Get(p, "action"));
                        probe = Get(p, "action");
                    }
                    data["status"] = Get(p, "status");
                    if (MentionsSensitiveFile(probe) && callId != null && !ctx.SensitiveCalls.Contains(callId))
                    {
                        ctx.SensitiveCalls.Add(callId);
                        while (ctx.SensitiveCalls.Count > 200) ctx.SensitiveCalls.RemoveAt(0);
                    }
                    var missing = new List<object>();
                    if (callId == null) missing.Add(Json.D("field", "call_id", "reason", r.Name + " 未提供 call_id，无法关联结果"));
                    Emit(o, ctx, r, "tool_call", data, null, null, callId, missing, false);
                    break;
                }
                case "function_call_output":
                case "custom_tool_call_output":
                {
                    object output = Get(p, "output");
                    string outcome = OutputOutcome(output);
                    bool sensitive = false;
                    if (callId != null && ctx.SensitiveCalls.Contains(callId)) { output = SensitivePlaceholder; sensitive = true; }
                    Emit(o, ctx, r, "tool_result", Json.D("output", output, "outcome", outcome), null, null, callId, null, sensitive);
                    break;
                }
                case "message": o.Skip("duplicate:response_item/message"); break;
                case "reasoning":
                    o.Skip("duplicate:response_item/reasoning");
                    if (Get(p, "encrypted_content") != null) o.Skip("omitted:encrypted_reasoning");
                    break;
                case "additional_tools": o.Skip("omitted:response_item/additional_tools"); break;
                default: o.Skip("unrecognized:" + r.Name); break;
            }
        }
    }
}
