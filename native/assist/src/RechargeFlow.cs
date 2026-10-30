using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Threading;

namespace CodexNativeAssist
{
    // The recharge flow behind the window. The user supplies the account and CDK in the window;
    // both are sent directly to the recharge backend. No Codex account lookup or binding occurs.
    sealed class View
    {
        public string State, Message, Email, Plan, BoundEmail;
    }

    sealed class Outcome
    {
        public string Kind, Message, Product;
        public bool Ok { get { return Kind == "succeeded" || Kind == "replayed"; } }
        public Outcome(string kind, string message) { Kind = kind; Message = message; }
    }

    sealed class RechargeFlow
    {
        public const string NotConfigured = "not_configured", NoAccount = "no_account", NeedBind = "need_bind",
            AccountChanged = "account_changed", Ready = "ready";

        static readonly HashSet<string> KnownCodes = new HashSet<string> { "invalid_account", "rate_limited", "cdk_invalid",
            "cdk_already_redeemed", "cdk_expired", "binding_invalid", "account_mismatch", "fulfillment_failed", "not_found", "server_error" };
        static readonly Dictionary<string, string> PlanNames = new Dictionary<string, string>
        {
            { "free", "Free" }, { "go", "Go" }, { "plus", "Plus" }, { "pro", "Pro" }, { "team", "Team" },
            { "business", "Business" }, { "enterprise", "Enterprise" }, { "edu", "Edu" },
        };

        public Func<Account> ReadAccount;
        public Action<int> Sleep = Thread.Sleep;
        public int[] RetryDelaysMs = { 2000, 5000, 10000 };
        public int PollIntervalMs = 3000, PollTimeoutMs = 60000, TimeoutMs = 15000;

        public Account Account;
        Dictionary<string, object> _binding;
        string _accountError;

        // No account lookup: the user types the account in the window (RechargeFlow.Load says so).
        // Account stays null unless a caller injects one (tests), which only enables the binding path.

        string BaseUrl { get { return Settings.RechargeBaseUrl(); } }

        public static string PlanName(string plan)
        {
            if (string.IsNullOrEmpty(plan)) return null;
            string n;
            return PlanNames.TryGetValue(plan, out n) ? n : plan;
        }

        public static string Mask(string cdk)
        {
            if (cdk.Length >= 12) return cdk.Substring(0, 4) + "…" + cdk.Substring(cdk.Length - 4);
            if (cdk.Length >= 8) return cdk.Substring(0, 2) + "…" + cdk.Substring(cdk.Length - 2);
            return cdk.Substring(0, Math.Min(2, cdk.Length)) + "…";
        }

        public static string Normalize(string cdk)
        {
            string v = new string((cdk ?? "").Where(c => !char.IsWhiteSpace(c)).ToArray());
            if (v.Length >= 4 && v.Length <= 128 && v.All(c => c >= 33 && c <= 126)) return v;
            return null;
        }

        public static string NormalizeAccount(string account)
        {
            string v = (account ?? "").Trim();
            if (v.Length == 0 || v.Length > 256 || v.Any(c => c < 32 || c == 127)) return null;
            return v;
        }

        // ---- account and binding --------------------------------------------------------------

        public View Load()
        {
            string error;
            if (RechargeApi.CheckBaseUrl(BaseUrl, out error) == null)
                return new View { State = NotConfigured, Message = string.IsNullOrEmpty(BaseUrl)
                    ? "尚未配置充值服务地址。请让管理员运行：assist.exe config --base-url <充值服务地址>" : error };
            Log.Op("ready", "ok", null, null);
            return new View { State = Ready,
                Message = "请输入充值账号，再输入 CDK，点击「确认提交」发送给充值服务。账号和 CDK 不会进入 Codex 对话。" };
        }

        public View Bind()
        {
            if (Account == null) return Load();
            var sw = Stopwatch.StartNew();
            string token;
            try
            {
                var result = new RechargeApi(BaseUrl, TimeoutMs).Bind(Account.Id, Account.Email);
                token = Json.Str(result, "binding_token");
                if (string.IsNullOrEmpty(token)) throw new ApiException("bad_response", "no_token", 200, null, null, null);
            }
            catch (ApiException ex)
            {
                Log.Op("bind", ex.Kind == "http" ? ex.Code : ex.Kind, ex.Status == 0 ? (int?)null : ex.Status, sw.ElapsedMilliseconds);
                return new View { State = NeedBind, Message = BindError(ex), Email = Account.Email, Plan = PlanName(Account.Plan) };
            }
            catch (ArgumentException ex)
            {
                return new View { State = NeedBind, Message = ex.Message, Email = Account.Email, Plan = PlanName(Account.Plan) };
            }
            _binding = Json.D("binding_token", token, "chatgpt_account_id", Account.Id, "email", Account.Email,
                "base_url", BaseUrl, "bound_at", DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"));
            Vault.Save(_binding);
            Log.Op("bind", "ok", null, sw.ElapsedMilliseconds);
            return new View { State = Ready, Message = "绑定成功。输入 CDK 后点「确认兑换」。", Email = Account.Email, Plan = PlanName(Account.Plan) };
        }

        static string BindError(ApiException ex)
        {
            if (ex.Kind == "not_sent") return "网络连接失败，没能绑定。请检查网络后重试。";
            if (ex.Kind == "unknown") return "网络中断，绑定没有完成，请重试。";
            if (ex.Kind == "http" && ex.Status == 429) return RateMessage(ex);
            if (ex.Kind == "http" && ex.Status == 400) return "充值服务不接受这个账号信息，请联系客服。";
            return "充值服务暂时不可用，请稍后重试。";
        }

        // ---- redemption --------------------------------------------------------------------------

        public Outcome Redeem(string cdk, string account, Action<string> progress)
        {
            progress = progress ?? (s => { });
            string value = Normalize(cdk);
            if (value == null) return new Outcome("invalid_format", "CDK 格式不对：请完整复制 CDK（4–128 位字母、数字或符号，不含中文）。");
            string target = NormalizeAccount(account);
            if (target == null) return new Outcome("not_ready", "请输入要充值的账号。");
            RechargeApi client;
            try { client = new RechargeApi(BaseUrl, TimeoutMs); }
            catch (ArgumentException ex) { return new Outcome("server_error", ex.Message); }
            string shown = Mask(value);

            Dictionary<string, object> record = null;
            bool uncertain = false;
            for (int attempt = 0; attempt <= RetryDelaysMs.Length; attempt++)
            {
                if (attempt > 0)
                {
                    progress("网络不稳定，正在用同一请求重试（第 " + attempt + " 次），不会重复扣除…");
                    Sleep(RetryDelaysMs[attempt - 1]);
                }
                var sw = Stopwatch.StartNew();
                try
                {
                    record = client.Redeem(value, target);
                    Log.Op("redeem", StatusId(record), null, sw.ElapsedMilliseconds);
                    break;
                }
                catch (ApiException ex)
                {
                    if (ex.Kind == "not_sent")
                    {
                        Log.Op("redeem", "not_sent", null, sw.ElapsedMilliseconds);
                        if (!uncertain) return new Outcome("not_sent", "网络连接失败，兑换请求没有发出，CDK 未使用。请检查网络后重试。");
                    }
                    else if (ex.Kind == "unknown")
                    {
                        Log.Op("redeem", "unknown", null, sw.ElapsedMilliseconds);
                        uncertain = true;
                    }
                    else if (ex.Kind == "http")
                    {
                        Log.Op("redeem", KnownCodes.Contains(ex.Code) ? ex.Code : "http_error", ex.Status, sw.ElapsedMilliseconds);
                        return HttpOutcome(ex, shown, uncertain);
                    }
                    else
                    {
                        Log.Op("redeem", "bad_response", ex.Status, sw.ElapsedMilliseconds);
                        return new Outcome("bad_response", "充值服务返回了无法识别的结果。请稍后再次提交同一 CDK 查看结果，不会重复扣除。");
                    }
                }
            }
            if (record == null)
                return new Outcome("unknown", "网络中断，充值结果没有确认。联网后再次提交同一 CDK 即可确认结果，不会重复扣除。");

            if (Json.Str(record, "status") == "processing")
            {
                progress("已提交，充值服务正在处理…");
                Outcome pending;
                record = Poll(client, record, out pending);
                if (record == null) return pending;
            }
            return Final(record, shown, !uncertain);
        }

        Dictionary<string, object> Poll(RechargeApi client, Dictionary<string, object> record, out Outcome pending)
        {
            pending = null;
            string rid = Json.Str(record, "redemption_id") ?? "";
            var deadline = DateTime.UtcNow.AddMilliseconds(PollTimeoutMs);
            while (rid.Length > 0 && DateTime.UtcNow < deadline)
            {
                Sleep(PollIntervalMs);
                var sw = Stopwatch.StartNew();
                Dictionary<string, object> latest;
                try { latest = client.GetRedemption(rid); }
                catch (ApiException ex)
                {
                    Log.Op("poll", ex.Kind == "http" ? "http_error" : "error", ex.Status == 0 ? (int?)null : ex.Status, sw.ElapsedMilliseconds);
                    continue;
                }
                Log.Op("poll", StatusId(latest), null, sw.ElapsedMilliseconds);
                if (Json.Str(latest, "status") != "processing")
                {
                    latest["replayed"] = record.ContainsKey("replayed") ? record["replayed"] : false;
                    return latest;
                }
            }
            pending = new Outcome("processing", "已提交，充值服务仍在处理。稍后再次提交同一 CDK 可查看结果，不会重复扣除。");
            return null;
        }

        static Outcome Final(Dictionary<string, object> record, string shown, bool replayIsDuplicate)
        {
            string status = Json.Str(record, "status");
            if (status == "failed")
            {
                string reason = Json.Str(Json.Obj(record, "error"), "message") ?? "开通没有成功";
                return new Outcome("fulfillment_failed", "充值失败：" + reason + "。CDK 未被扣除，可稍后重试。");
            }
            if (status != "succeeded")
                return new Outcome("bad_response", "充值服务返回了无法识别的结果。请稍后再次提交同一 CDK 查看结果，不会重复扣除。");
            string product = Json.Str(record, "product") ?? "充值";
            if (Json.Bool(record, "replayed", false) && replayIsDuplicate)
                return new Outcome("replayed", "重复兑换：CDK " + shown + " 已于 " + LocalTime(Json.Str(record, "redeemed_at")) +
                    " 充值到当前账号（" + product + "），未重复扣除。") { Product = product };
            return new Outcome("succeeded", "充值成功：" + product + "。CDK " + shown + " 已核销。") { Product = product };
        }

        Outcome HttpOutcome(ApiException ex, string shown, bool uncertain)
        {
            if (ex.Code == "invalid_account") return new Outcome("invalid_account", "充值服务不接受这个账号信息，请联系客服。");
            if (ex.Code == "cdk_invalid" || (ex.Status == 400 && !ex.Code.StartsWith("invalid_")))
                return new Outcome("cdk_invalid", "CDK " + shown + " 无效，请检查是否输错。");
            if (ex.Code == "cdk_already_redeemed") return new Outcome("cdk_used", "CDK " + shown + " 已被其他账号使用。如有疑问请联系发卡方。");
            if (ex.Code == "cdk_expired") return new Outcome("cdk_expired", "CDK " + shown + " 已过期。");
            if (ex.Code == "fulfillment_failed")
                return new Outcome("fulfillment_failed", "充值失败：" + (ex.UserMessage ?? "开通没有成功") + "。CDK 未被扣除，可稍后重试。");
            if (uncertain) return new Outcome("unknown", "网络中断，充值结果没有确认。稍后再次提交同一 CDK 即可确认结果，不会重复扣除。");
            if (ex.Status == 429) return new Outcome("rate_limited", RateMessage(ex));
            return new Outcome("server_error", "充值服务暂时不可用（" + ex.Status + "），请稍后重试。");
        }

        public string PlanNote()
        {
            return "";
        }

        static string StatusId(Dictionary<string, object> r)
        {
            string s = Json.Str(r, "status");
            return s == "succeeded" || s == "processing" || s == "failed" ? s : "other";
        }

        static string RateMessage(ApiException ex)
        {
            return "操作太频繁，请" + (ex.RetryAfter.HasValue ? " " + ex.RetryAfter + " 秒" : "一会儿") + "后再试。";
        }

        static string LocalTime(string iso)
        {
            DateTime t;
            if (!DateTime.TryParseExact(iso ?? "", "yyyy-MM-ddTHH:mm:ssZ", CultureInfo.InvariantCulture,
                    DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out t)) return "之前";
            return t.ToLocalTime().ToString("yyyy-MM-dd HH:mm");
        }
    }
}
