using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace CodexNativeAssist
{
    sealed class BrowserSessionResult
    {
        public bool Ok;
        public string Content;
        public string Error;   // not_logged_in | empty | bridge_unavailable | timeout | busy | http_NNN
    }

    // Bridge for the browser extension: the recharge window cannot read chatgpt.com itself (it has no
    // cookie jar), so it asks the user's browser through a fixed loopback port.
    //
    //   GET  /v1/browser-session/pending   → the open request ({request_id, nonce}) or 204 when idle
    //   POST /v1/browser-session/result    → {request_id, nonce, status, body}; the response text
    //   OPTIONS *                          → CORS + Chrome's private-network preflight
    //
    // Only 127.0.0.1, a random nonce per request, exactly one in-flight request, and the payload is only
    // ever passed back to the window that asked (it is never written to disk or logged).
    sealed class BrowserSessionBridge : IDisposable
    {
        public const int Port = 43991;
        const int ResultTimeoutMs = 30000;
        const int MaxWaitSeconds = 30;   // long-poll budget for one GET /pending

        readonly TcpListener _listener;
        readonly Thread _thread;
        readonly object _gate = new object();
        readonly ManualResetEventSlim _done = new ManualResetEventSlim(false);
        volatile bool _stop;

        string _requestId, _nonce;          // the request currently waiting for the browser
        BrowserSessionResult _result;

        public BrowserSessionBridge()
        {
            try
            {
                _listener = new TcpListener(IPAddress.Loopback, Port);
                _listener.Start();
            }
            catch (Exception ex)
            {
                Log.Lifecycle("browser_bridge", Json.D("event", "listen_failed", "error", ex.GetType().Name));
                _listener = null;
                return;
            }
            _thread = new Thread(Loop) { IsBackground = true, Name = "browser-session-bridge" };
            _thread.Start();
            Log.Lifecycle("browser_bridge", Json.D("event", "listening", "port", Port));
        }

        // Called by the window when the user clicks the session link: blocks until the extension answers.
        public BrowserSessionResult Request(int timeoutMs = ResultTimeoutMs)
        {
            if (_listener == null) return new BrowserSessionResult { Error = "bridge_unavailable" };
            string id = Guid.NewGuid().ToString("N");
            string nonce = Convert.ToBase64String(Guid.NewGuid().ToByteArray()).Replace("=", "");
            lock (_gate)
            {
                if (_requestId != null) return new BrowserSessionResult { Error = "busy" };
                _requestId = id;
                _nonce = nonce;
                _result = null;
            }
            _done.Reset();
            Log.Lifecycle("browser_bridge", Json.D("event", "request", "id", id));
            if (!_done.Wait(timeoutMs))
            {
                lock (_gate) { _requestId = null; _nonce = null; }
                Log.Lifecycle("browser_bridge", Json.D("event", "timeout", "id", id));
                return new BrowserSessionResult { Error = "timeout" };
            }
            lock (_gate)
            {
                var result = _result ?? new BrowserSessionResult { Error = "timeout" };
                _requestId = null;
                _nonce = null;
                _result = null;
                Log.Lifecycle("browser_bridge", Json.D("event", "answered", "ok", result.Ok, "error", result.Error));
                return result;
            }
        }

        void Loop()
        {
            while (!_stop)
            {
                TcpClient client;
                try { client = _listener.AcceptTcpClient(); }
                catch (Exception) { return; }
                TcpClient accepted = client;
                ThreadPool.QueueUserWorkItem(delegate
                {
                    try { Handle(accepted); }
                    catch (Exception) { }
                    finally { try { accepted.Close(); } catch (Exception) { } }
                });
            }
        }

        void Handle(TcpClient client)
        {
            client.ReceiveTimeout = 10000;
            using (var stream = client.GetStream())
            {
                string method, path, body, rawQuery;
                if (!ReadRequest(stream, out method, out path, out body, out rawQuery)) return;

                if (method == "OPTIONS") { Respond(stream, 204, null); return; }

                if (method == "GET" && path == "/v1/browser-session/pending")
                {
                    int waitSec = WaitSeconds(rawQuery);
                    string id = null, nonce = null;
                    var deadline = DateTime.UtcNow.AddSeconds(waitSec);
                    while (true)
                    {
                        lock (_gate) { id = _requestId; nonce = _nonce; }
                        if (id != null) break;
                        if (DateTime.UtcNow >= deadline) break;
                        Thread.Sleep(120);
                    }
                    if (id == null) { Respond(stream, 204, null); return; }
                    Respond(stream, 200, Json.Stringify(Json.D("request_id", id, "nonce", nonce)));
                    return;
                }

                if (method == "POST" && path == "/v1/browser-session/result")
                {
                    var doc = Json.ParseObject(body ?? "");
                    if (doc == null) { Respond(stream, 400, "{\"error\":\"bad_json\"}"); return; }
                    string status = Json.Str(doc, "status");
                    int code;
                    int.TryParse(status ?? "", out code);
                    string text = Json.Str(doc, "body") ?? "";
                    lock (_gate)
                    {
                        if (_requestId == null || Json.Str(doc, "nonce") != _nonce || Json.Str(doc, "request_id") != _requestId)
                        {
                            Respond(stream, 409, "{\"error\":\"no_such_request\"}");
                            return;
                        }
                        BrowserSessionResult result;
                        if (code == 0) result = new BrowserSessionResult { Error = "not_logged_in" };
                        else if (code != 200) result = new BrowserSessionResult { Error = "http_" + code };
                        else if (string.IsNullOrWhiteSpace(text)) result = new BrowserSessionResult { Error = "empty" };
                        else result = new BrowserSessionResult { Ok = true, Content = text };
                        _result = result;
                        _done.Set();
                    }
                    Respond(stream, 200, "{}");
                    return;
                }

                Respond(stream, 404, "{\"error\":\"not_found\"}");
            }
        }

        // Minimal HTTP/1.1: request line, headers, optional Content-Length body. Enough for one fetch().
        static bool ReadRequest(Stream stream, out string method, out string path, out string body, out string rawQuery)
        {
            method = null; path = null; body = null; rawQuery = "";
            var headerBytes = new List<byte>();
            var one = new byte[1];
            while (headerBytes.Count < 32768)
            {
                int n = stream.Read(one, 0, 1);
                if (n <= 0) return false;
                headerBytes.Add(one[0]);
                int c = headerBytes.Count;
                if (c >= 4 && headerBytes[c - 4] == 13 && headerBytes[c - 3] == 10 && headerBytes[c - 2] == 13 && headerBytes[c - 1] == 10)
                    break;
            }
            string head = Encoding.ASCII.GetString(headerBytes.ToArray());
            string[] lines = head.Split(new[] { "\r\n" }, StringSplitOptions.None);
            if (lines.Length == 0) return false;
            string[] first = lines[0].Split(' ');
            if (first.Length < 2) return false;
            method = first[0].ToUpperInvariant();
            path = first[1];
            int q = path.IndexOf('?');
            if (q >= 0) { rawQuery = path.Substring(q + 1); path = path.Substring(0, q); }
            else rawQuery = "";

            int length = 0;
            for (int i = 1; i < lines.Length; i++)
            {
                int colon = lines[i].IndexOf(':');
                if (colon <= 0) continue;
                if (string.Equals(lines[i].Substring(0, colon).Trim(), "Content-Length", StringComparison.OrdinalIgnoreCase))
                    int.TryParse(lines[i].Substring(colon + 1).Trim(), out length);
            }
            if (length > 0)
            {
                if (length > 4 * 1024 * 1024) return false;
                var buffer = new byte[length];
                int read = 0;
                while (read < length)
                {
                    int n = stream.Read(buffer, read, length - read);
                    if (n <= 0) break;
                    read += n;
                }
                body = Encoding.UTF8.GetString(buffer, 0, read);
            }
            return true;
        }

        static void Respond(Stream stream, int status, string json)
        {
            byte[] payload = Encoding.UTF8.GetBytes(json ?? "");
            var head = new StringBuilder();
            head.Append("HTTP/1.1 ").Append(status).Append(' ').Append(Reason(status)).Append("\r\n");
            head.Append("Access-Control-Allow-Origin: *\r\n");
            head.Append("Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n");
            head.Append("Access-Control-Allow-Headers: Content-Type, Accept\r\n");
            // Chrome asks before a public page may talk to loopback; say yes explicitly.
            head.Append("Access-Control-Allow-Private-Network: true\r\n");
            head.Append("Access-Control-Max-Age: 600\r\n");
            head.Append("Cache-Control: no-store\r\n");
            if (status != 204) head.Append("Content-Type: application/json; charset=utf-8\r\n");
            head.Append("Content-Length: ").Append(payload.Length).Append("\r\n");
            head.Append("Connection: close\r\n\r\n");
            byte[] header = Encoding.ASCII.GetBytes(head.ToString());
            stream.Write(header, 0, header.Length);
            if (payload.Length > 0) stream.Write(payload, 0, payload.Length);
            stream.Flush();
        }

        static int WaitSeconds(string query)
        {
            if (string.IsNullOrEmpty(query)) return 0;
            foreach (var pair in query.Split('&'))
            {
                if (!pair.StartsWith("wait=", StringComparison.OrdinalIgnoreCase)) continue;
                int seconds;
                if (int.TryParse(pair.Substring(5), out seconds) && seconds > 0)
                    return Math.Min(seconds, MaxWaitSeconds);
            }
            return 0;
        }

        static string Reason(int status)
        {
            switch (status)
            {
                case 200: return "OK";
                case 204: return "No Content";
                case 400: return "Bad Request";
                case 404: return "Not Found";
                case 409: return "Conflict";
                default: return "Status";
            }
        }

        public void Dispose()
        {
            _stop = true;
            try { if (_listener != null) _listener.Stop(); } catch (Exception) { }
            try { if (_thread != null) _thread.Join(500); } catch (Exception) { }
            try { _done.Dispose(); } catch (Exception) { }
        }
    }
}
