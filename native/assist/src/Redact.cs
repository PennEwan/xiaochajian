using System;
using System.Linq;
using System.Text.RegularExpressions;

namespace CodexNativeAssist
{
    // Port of the recorder's redaction rules (plugins/codex-task-recorder/lib/ctr/redact.py):
    // a false positive costs a few characters, a false negative leaks a secret.
    static class Redact
    {
        static readonly Tuple<string, Regex>[] ValuePatterns =
        {
            P("private_key", @"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
            P("anthropic_key", @"\bsk-ant-[A-Za-z0-9_\-]{16,}"),
            P("openai_key", @"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}"),
            P("github_token", @"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})"),
            P("aws_access_key", @"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
            P("google_api_key", @"\bAIza[0-9A-Za-z_\-]{35}"),
            P("slack_token", @"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
            P("jwt", @"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
            P("session_key", @"\bsess-[A-Za-z0-9]{20,}"),
            P("app_token", @"\b(?:cdkb|ctrd)_[A-Za-z0-9_\-]{8,}"),
        };

        static readonly Regex CdkContext = new Regex(
            @"((?<![A-Za-z])(?i:cdk(?:ey)?|redeem[ _-]?code|activation[ _-]?code)(?![A-Za-z])|兑换码|激活码|卡密|充值码)" +
            @"(码?[""'”」』]?\s*(?:是|为)?\s*[:：=]?\s*[""'“「『(（]?\s*)" +
            @"([A-Za-z0-9][A-Za-z0-9_-]{3,127})", RegexOptions.Compiled);
        const string CdkMarkers = ":：=是为\"'“「『(（";
        static readonly Regex CodeLike = new Regex(@"^[A-Z0-9_-]+$", RegexOptions.Compiled);
        static readonly Regex GroupedCode = new Regex(@"(?<![A-Za-z0-9-])[A-Z0-9]{4,6}(?:-[A-Z0-9]{4,6}){2,5}(?![A-Za-z0-9-])", RegexOptions.Compiled);
        static readonly Regex AuthHeader = new Regex(@"(?i)\b(authorization|proxy-authorization|set-cookie|cookie|x-api-key)(\s*:\s*)([^\r\n""']+)", RegexOptions.Compiled);
        static readonly Regex Bearer = new Regex(@"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{16,}=*", RegexOptions.Compiled);
        static readonly Regex UrlCredentials = new Regex(@"([A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:)([^/\s@]+)(@)", RegexOptions.Compiled);
        static readonly Regex Assignment = new Regex(
            @"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key" +
            @"|private[_-]?key|client[_-]?secret|credential)[A-Za-z0-9_.-]*)" +
            @"(\s*[:=]\s*)(""[^""\r\n]*""|'[^'\r\n]*'|[^\s""',;()\[\]{}]+)", RegexOptions.Compiled);
        static readonly string[] NotASecret = { "str", "int", "bool", "none", "null", "true", "false", "string",
            "required", "optional", "any", "bytes", "float", "undefined" };

        static readonly string[] KeySubstrings = { "password", "passwd", "secret", "api_key", "apikey", "api-key",
            "access_key", "accesskey", "private_key", "privatekey", "authorization", "cookie", "credential",
            "session_key", "bearer", "兑换码", "激活码", "卡密", "充值码" };
        static readonly string[] KeyExact = { "pwd", "token", "auth", "auth_token", "access_token", "refresh_token",
            "id_token", "session_token", "sessionid", "session_cookie" };
        static readonly string[] KeyNormalised = { "cdk", "redeemcode", "activationcode" };

        static Tuple<string, Regex> P(string kind, string pattern)
        {
            return Tuple.Create(kind, new Regex(pattern, RegexOptions.Compiled));
        }

        public static bool IsSecretKey(string key)
        {
            string k = key.ToLowerInvariant();
            if (KeyExact.Contains(k)) return true;
            if (KeySubstrings.Any(s => k.Contains(s))) return true;
            string n = Regex.Replace(k, @"[\s_\-]", "");
            return KeyNormalised.Any(s => n.Contains(s));
        }

        public static string Text(string text, out int count)
        {
            int n = 0;
            if (text.TrimStart().StartsWith("/充值") || text.TrimStart().StartsWith("／充值"))
            {
                count = 1;
                return "[REDACTED:recharge_command]";
            }
            foreach (var p in ValuePatterns)
            {
                string kind = p.Item1;
                text = p.Item2.Replace(text, m => { n++; return "[REDACTED:" + kind + "]"; });
            }
            text = CdkContext.Replace(text, m =>
            {
                string value = m.Groups[3].Value;
                bool explicitMarker = m.Groups[2].Value.Any(c => CdkMarkers.IndexOf(c) >= 0);
                bool upperCode = CodeLike.IsMatch(value) && value.Any(c => char.IsDigit(c) || c == '-');
                bool mixed = value.Length >= 6 && value.Any(char.IsDigit) && value.Any(char.IsLetter);
                if (!(explicitMarker || upperCode || mixed)) return m.Value;
                n++;
                return m.Groups[1].Value + m.Groups[2].Value + "[REDACTED:cdk]";
            });
            text = GroupedCode.Replace(text, m =>
            {
                string code = m.Value;
                if (!(code.Any(char.IsDigit) && code.Any(char.IsLetter))) return code;
                n++;
                return "[REDACTED:cdk_like]";
            });
            text = AuthHeader.Replace(text, m =>
            {
                if (m.Groups[3].Value.StartsWith("[REDACTED")) return m.Value;
                n++;
                return m.Groups[1].Value + m.Groups[2].Value + "[REDACTED:auth_header]";
            });
            text = Bearer.Replace(text, m => { n++; return "Bearer [REDACTED:bearer]"; });
            text = UrlCredentials.Replace(text, m =>
            {
                if (m.Groups[2].Value.StartsWith("[REDACTED")) return m.Value;
                n++;
                return m.Groups[1].Value + "[REDACTED:url_password]" + m.Groups[3].Value;
            });
            text = Assignment.Replace(text, m =>
            {
                string v = m.Groups[3].Value.Trim('"', '\'');
                if (v.Length < 4 || v.StartsWith("[REDACTED") || v.StartsWith("$") || v.StartsWith("%") ||
                    v.All(char.IsDigit) || NotASecret.Contains(v.ToLowerInvariant())) return m.Value;
                n++;
                return m.Groups[1].Value + m.Groups[2].Value + "[REDACTED:assignment]";
            });
            count = n;
            return text;
        }
    }
}
