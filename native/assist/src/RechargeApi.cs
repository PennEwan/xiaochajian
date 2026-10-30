using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Text;

namespace CodexNativeAssist
{
    // Client for docs/recharge-api.md. Failure kinds tell the caller what is known about the request:
    //   NotSent  — it certainly never reached the backend (refused, DNS, TLS handshake);
    //   Unknown  — it may have been processed (timeout or connection lost after sending);
    //   Http     — the backend answered with an error code;  BadResponse — unreadable answer.
    // No exception message ever carries the CDK, the binding token or a request body.
    sealed class ApiException : Exception
    {
        public readonly string Kind;      // not_sent / unknown / http / bad_response
        public readonly string Detail;    // refused, dns, timeout, redirect, …
        public readonly int Status;
        public readonly string Code;
        public readonly string UserMessage;
        public readonly int? RetryAfter;

        public ApiException(string kind, string detail, int status, string code, string message, int? retryAfter)
            : base(kind + ":" + detail)
        {
            Kind = kind; Detail = detail; Status = status; Code = code; UserMessage = message; RetryAfter = retryAfter;
        }
    }

    sealed class RechargeApi
    {
        readonly string _base;
        readonly int _timeoutMs;

        public RechargeApi(string baseUrl, int timeoutMs)
        {
            string error;
            _base = CheckBaseUrl(baseUrl, out error);
            if (_base == null) throw new ArgumentException(error);
            _timeoutMs = timeoutMs;
        }

        public static string CheckBaseUrl(string url, out string error)
        {
            return JsonHttp.CheckBaseUrl(url, "充值服务地址", out error);
        }

        public Dictionary<string, object> Bind(string accountId, string email)
        {
            return Call("POST", "/v1/bindings", null, Json.D("chatgpt_account_id", accountId, "email", email));
        }

        public Dictionary<string, object> Redeem(string token, string cdk, string accountId)
        {
            return Call("POST", "/v1/redemptions", token, Json.D("cdk", cdk, "chatgpt_account_id", accountId));
        }

        public Dictionary<string, object> Redeem(string cdk, string account)
        {
            return Call("POST", "/v1/redemptions", null, Json.D("cdk", cdk, "account", account));
        }

        public Dictionary<string, object> GetRedemption(string token, string id)
        {
            return Call("GET", "/v1/redemptions/" + Uri.EscapeDataString(id), token, null);
        }

        public Dictionary<string, object> GetRedemption(string id)
        {
            return Call("GET", "/v1/redemptions/" + Uri.EscapeDataString(id), null, null);
        }

        Dictionary<string, object> Call(string method, string path, string token, Dictionary<string, object> body)
        {
            return JsonHttp.Call(_base, _timeoutMs, "X-Cdkr-Client", method, path, token, body == null ? null : Json.Stringify(body));
        }
    }

    // JSON over HTTP(S) for the plugin's backends (recharge, sync). Never follows redirects, never puts a
    // request body or token into an exception.
    static class JsonHttp
    {
        static JsonHttp()
        {
            try { ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12 | (SecurityProtocolType)12288; }
            catch (NotSupportedException) { ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12; }
            ServicePointManager.Expect100Continue = false;
        }

        // https:// anywhere, plain http:// only for this machine (local mock backends).
        public static string CheckBaseUrl(string url, string label, out string error)
        {
            error = null;
            Uri u;
            if (string.IsNullOrEmpty(url) || !Uri.TryCreate(url, UriKind.Absolute, out u))
            {
                error = label + "格式不对";
                return null;
            }
            if (u.Scheme == "https" && u.Host.Length > 0) return url.TrimEnd('/');
            if (u.Scheme == "http" && u.IsLoopback) return url.TrimEnd('/');
            error = label + "必须是 https://（本机调试地址除外）";
            return null;
        }

        public static Dictionary<string, object> Call(string baseUrl, int timeoutMs, string clientHeader, string method, string path,
                                                      string token, string jsonBody)
        {
            var req = (HttpWebRequest)WebRequest.Create(baseUrl + path);
            req.Method = method;
            req.AllowAutoRedirect = false; // would carry Authorization to any Location
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            req.Accept = "application/json";
            req.Headers[clientHeader] = "codex-native-assist/" + Program.Version;
            req.KeepAlive = false;
            if (new Uri(baseUrl).IsLoopback) req.Proxy = null; // a local (mock) backend is always direct
            if (token != null) req.Headers["Authorization"] = "Bearer " + token;

            if (jsonBody != null)
            {
                byte[] data = new UTF8Encoding(false).GetBytes(jsonBody);
                req.ContentType = "application/json; charset=utf-8";
                req.ContentLength = data.Length;
                req.AllowWriteStreamBuffering = false;
                Stream s;
                try { s = req.GetRequestStream(); } // connect + headers: failures here mean "not sent"
                catch (WebException ex) { throw NotSentOrUnknown(ex); }
                try { using (s) s.Write(data, 0, data.Length); }
                catch (Exception) { throw new ApiException("unknown", "send_failed", 0, null, null, null); }
            }

            HttpWebResponse resp;
            try { resp = (HttpWebResponse)req.GetResponse(); }
            catch (WebException ex)
            {
                var er = ex.Response as HttpWebResponse;
                if (er != null) using (er) return Classify(method, er);
                if (jsonBody == null) throw NotSentOrUnknown(ex);
                throw new ApiException("unknown", ex.Status == WebExceptionStatus.Timeout ? "timeout" : "connection_lost", 0, null, null, null);
            }
            using (resp) return Classify(method, resp);
        }

        static ApiException NotSentOrUnknown(WebException ex)
        {
            switch (ex.Status)
            {
                case WebExceptionStatus.ConnectFailure: return new ApiException("not_sent", "refused", 0, null, null, null);
                case WebExceptionStatus.NameResolutionFailure:
                case WebExceptionStatus.ProxyNameResolutionFailure: return new ApiException("not_sent", "dns", 0, null, null, null);
                case WebExceptionStatus.SecureChannelFailure:
                case WebExceptionStatus.TrustFailure: return new ApiException("not_sent", "tls", 0, null, null, null);
                case WebExceptionStatus.Timeout: return new ApiException("not_sent", "timeout", 0, null, null, null);
                default: return new ApiException("unknown", "connection_lost", 0, null, null, null);
            }
        }

        static Dictionary<string, object> Classify(string method, HttpWebResponse resp)
        {
            int status = (int)resp.StatusCode;
            string text;
            try { using (var r = new StreamReader(resp.GetResponseStream(), Encoding.UTF8)) text = r.ReadToEnd(); }
            catch (Exception) { text = ""; }
            if (status >= 300 && status < 400)
            {
                // A redirected POST may already have been processed; a redirected GET is just unusable.
                if (method == "POST") throw new ApiException("unknown", "redirect", status, null, null, null);
                throw new ApiException("bad_response", "redirect", status, null, null, null);
            }
            var obj = Json.ParseObject(text);
            if (status >= 200 && status < 300)
            {
                if (obj == null) throw new ApiException("bad_response", "unparsable", status, null, null, null);
                return obj;
            }
            var err = Json.Obj(obj, "error");
            string code = Json.Str(err, "code");
            if (string.IsNullOrEmpty(code)) code = status == 429 ? "rate_limited" : status >= 500 ? "server_error" : "http_error";
            int ra;
            int? retry = int.TryParse((resp.Headers["Retry-After"] ?? "").Trim(), out ra) ? (int?)ra : null;
            throw new ApiException("http", code, status, code, Json.Str(err, "message"), retry);
        }
    }
}
