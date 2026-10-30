using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace CodexNativeAssist
{
    // Reads the signed-in ChatGPT account from Codex's own login file, entirely on this machine.
    //
    // Why this and not the browser: the user only has to be logged in to Codex. The access token in
    // %CODEX_HOME%\auth.json is a JWT, and its claims already carry the account id, the plan and the email,
    // so nothing has to be fetched from ChatGPT, no page is opened and no extension is installed. The token
    // itself is never copied, logged or sent anywhere — only the three account fields leave this method.
    static class LocalAccount
    {
        // Claims of a JWT payload, or null.
        internal static Dictionary<string, object> DecodeJwt(string jwt)
        {
            string[] parts = (jwt ?? "").Split('.');
            if (parts.Length < 2) return null;
            string payload = parts[1].Replace('-', '+').Replace('_', '/');
            switch (payload.Length % 4)
            {
                case 2: payload += "=="; break;
                case 3: payload += "="; break;
            }
            return Json.ParseObject(Encoding.UTF8.GetString(Convert.FromBase64String(payload)));
        }

        public static Account Read(out string error)
        {
            error = null;
            try
            {
                string path = Path.Combine(Paths.CodexHome, "auth.json");
                if (!File.Exists(path)) { error = "no_auth_file"; return null; }
                var doc = Json.ParseObject(File.ReadAllText(path, Encoding.UTF8));
                var tokens = Json.Obj(doc, "tokens");
                string jwt = Json.Str(tokens, "access_token");
                if (string.IsNullOrEmpty(jwt)) jwt = Json.Str(doc, "access_token");
                if (string.IsNullOrEmpty(jwt)) { error = "no_access_token"; return null; }

                var claims = DecodeJwt(jwt);
                if (claims == null) { error = "bad_claims"; return null; }

                var auth = Json.Obj(claims, "https://api.openai.com/auth");
                var profile = Json.Obj(claims, "https://api.openai.com/profile");
                string email = Json.Str(profile, "email");
                string id = Json.Str(auth, "chatgpt_account_id");
                string plan = Json.Str(auth, "chatgpt_plan_type");
                if (string.IsNullOrEmpty(email) && string.IsNullOrEmpty(id)) { error = "no_account_fields"; return null; }

                // Expired token: the account is still the right one, but say so honestly.
                long exp = Json.Long(claims, "exp", 0);
                if (exp > 0 && exp < (long)(DateTime.UtcNow - new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc)).TotalSeconds) error = "token_expired";
                // Logging must never be able to fail the read.
                try { Log.Lifecycle("local_account", Json.D("event", "read", "plan", plan, "has_email", !string.IsNullOrEmpty(email))); } catch (Exception) { }
                return new Account { Id = id, Email = email, Plan = plan };
            }
            catch (Exception ex)
            {
                error = ex.GetType().Name;
                return null;
            }
        }
    }
}
