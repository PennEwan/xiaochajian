using System;
using System.Collections.Generic;

namespace CodexNativeAssist
{
    // Test harness for RechargeFlow (see tests/test_native_assist.py). Input JSON:
    //   {"account": {"id","email","plan"} | null, "bind": true,
    //    "cdks": ["..."], "retry_ms": [..], "poll_ms": n, "poll_timeout_ms": n, "timeout_ms": n}
    static class FlowTest
    {
        public static Dictionary<string, object> Run(string input)
        {
            var spec = Json.ParseObject(input) ?? new Dictionary<string, object>();
            var acct = Json.Obj(spec, "account");
            var flow = new RechargeFlow();
            flow.ReadAccount = delegate
            {
                if (acct == null) return null; // Load() then shows the read_failed message
                return new Account { Id = Json.Str(acct, "id"), Email = Json.Str(acct, "email"), Plan = Json.Str(acct, "plan") };
            };
            var retry = Json.List(spec, "retry_ms");
            if (retry.Count > 0) flow.RetryDelaysMs = retry.ConvertAll(x => Convert.ToInt32(x)).ToArray();
            flow.PollIntervalMs = (int)Json.Long(spec, "poll_ms", flow.PollIntervalMs);
            flow.PollTimeoutMs = (int)Json.Long(spec, "poll_timeout_ms", flow.PollTimeoutMs);
            flow.TimeoutMs = (int)Json.Long(spec, "timeout_ms", flow.TimeoutMs);

            View view = flow.Load();
            var views = new List<object> { Json.D("state", view.State, "message", view.Message) };
            if (Json.Bool(spec, "bind", true) && (view.State == RechargeFlow.NeedBind || view.State == RechargeFlow.AccountChanged))
            {
                view = flow.Bind();
                views.Add(Json.D("state", view.State, "message", view.Message));
            }
            var outcomes = new List<object>();
            var progress = new List<object>();
            foreach (var c in Json.List(spec, "cdks"))
            {
                string target = acct == null ? null : (Json.Str(acct, "email") ?? Json.Str(acct, "id"));
                var o = flow.Redeem((string)c, target, msg => progress.Add(msg));
                outcomes.Add(Json.D("kind", o.Kind, "message", o.Message, "product", o.Product));
            }
            return Json.D("views", views, "outcomes", outcomes, "progress", progress, "bound", Vault.Load() != null);
        }
    }
}
