using System;
using System.Collections.Generic;
using System.Text;

namespace CodexNativeAssist
{
    // A fixed, model-free entry point in the Codex App: an "MCP app" sidebar entry.
    //
    // The App (26.1013) turns a plugin MCP tool whose _meta carries a ui:// resource and
    // "openai/ui".entrypoints = [{ type: "global", … }] into a sidebar app ("…" menu, pinnable to the rail).
    // Opening it renders the ui:// page; the page's button calls open_recharge_window through the MCP Apps
    // bridge (app-server mcpServer/tool/call) — no model turn, no prompt, no approval card.
    // visibility ["app"] keeps the launcher tool out of the model's tool list.
    // The quickAction is only drawn in the legacy (non-rail) sidebar, which the current App does not show;
    // it is kept because it costs nothing. See docs/native-recharge-entry-spike.md.
    static class AppEntry
    {
        public const string ResourceUri = "ui://native-assist/recharge.html";
        public const string MimeType = "text/html;profile=mcp-app";
        public const string LauncherTool = "recharge_launcher";

        // Orange rounded square with a white "¥" (same colours as the plugin icon).
        const string IconSvg =
            "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect x='4' y='4' width='56' height='56' rx='14' fill='#F59A23'/>" +
            "<text x='32' y='44' font-family='Segoe UI,Arial' font-size='34' font-weight='700' fill='#fff' text-anchor='middle'>¥</text></svg>";

        public static string IconDataUri
        {
            get { return "data:image/svg+xml;base64," + Convert.ToBase64String(Encoding.UTF8.GetBytes(IconSvg)); }
        }

        public static List<object> Icons()
        {
            return new List<object> { Json.D("src", IconDataUri, "mimeType", "image/svg+xml", "sizes", new List<object> { "any" }) };
        }

        public static Dictionary<string, object> LauncherToolDefinition()
        {
            return Json.D("name", LauncherTool,
                "title", "CDK 充值",
                "description", "Codex 侧边栏中的「CDK 充值」入口。由用户在侧边栏点击时调用，不需要模型调用。",
                "inputSchema", Json.D("type", "object", "properties", new Dictionary<string, object>(), "additionalProperties", false),
                "annotations", Json.D("title", "CDK 充值", "readOnlyHint", true, "destructiveHint", false, "idempotentHint", true, "openWorldHint", false),
                "icons", Icons(),
                "_meta", Json.D(
                    "ui", Json.D("resourceUri", ResourceUri, "visibility", new List<object> { "app" }),
                    "openai/outputTemplate", ResourceUri,
                    "openai/ui", Json.D("entrypoints", new List<object>
                    {
                        Json.D("type", "global", "quickAction", Json.D(
                            "title", "打开 CDK 充值窗口",
                            "icons", Icons(),
                            "target", Json.D("type", "tool", "name", "open_recharge_window", "arguments", Json.D("wait", false)))),
                    })));
        }

        public static Tools.Result CallLauncher()
        {
            return Tools.Result.Ok("CDK 充值入口：点击「打开充值窗口」，在弹出的本地窗口中输入 CDK。", Json.D("entry", "recharge"));
        }

        public static List<object> ResourceList()
        {
            return new List<object> { Json.D("uri", ResourceUri, "name", "recharge", "title", "CDK 充值", "mimeType", MimeType) };
        }

        public static Dictionary<string, object> Read(string uri)
        {
            if (uri != ResourceUri) return null;
            return Json.D("contents", new List<object>
            {
                Json.D("uri", ResourceUri, "mimeType", MimeType, "text", Html,
                    "_meta", Json.D("ui", Json.D("prefersBorder", true))),
            });
        }

        // The page only launches the native window; the CDK is never typed here (it never enters Codex).
        const string Html = @"<!doctype html>
<html lang=""zh-CN""><head><meta charset=""utf-8"">
<style>
 body{font-family:""Microsoft YaHei UI"",""Segoe UI"",sans-serif;margin:0;padding:20px;color:#17191f}
 h1{font-size:18px;margin:0 0 6px} p{margin:0 0 14px;color:#666;font-size:13px}
 button{background:#0060df;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer}
 button:disabled{background:#9db4d8} #s{margin-top:12px;font-size:13px;color:#555;white-space:pre-wrap}
</style></head><body>
<h1>CDK 充值</h1>
<p>点击按钮打开本地充值窗口。CDK 只在本地窗口中输入，不会进入 Codex 对话。</p>
<button id=""b"">打开充值窗口</button><div id=""s""></div>
<script>
(function(){
 var next=1, pending={}, s=document.getElementById('s'), b=document.getElementById('b');
 function log(t){ s.textContent=t; }
 function rpc(method, params){
   var id=next++;
   window.parent.postMessage({jsonrpc:'2.0', id:id, method:method, params:params||{}}, '*');
   return new Promise(function(res, rej){
     pending[id]={res:res, rej:rej};
     setTimeout(function(){ if(pending[id]){ delete pending[id]; rej(new Error('timeout')); } }, 20000);
   });
 }
 window.addEventListener('message', function(e){
   var m=e.data; if(!m || m.jsonrpc!=='2.0' || m.id==null || !pending[m.id]) return;
   var p=pending[m.id]; delete pending[m.id]; if(m.error) p.rej(new Error(m.error.message||'error')); else p.res(m.result);
 });
 var ready = rpc('ui/initialize', {appInfo:{name:'native-assist-recharge', version:'0.3.4'}, appCapabilities:{}, protocolVersion:'2025-06-18'})
   .then(function(){ window.parent.postMessage({jsonrpc:'2.0', method:'ui/notifications/initialized', params:{}}, '*'); })
   .catch(function(e){ log('初始化：'+e.message); });
 b.onclick=function(){
   b.disabled=true; log('正在打开…');
   var call = (window.openai && window.openai.callTool)
     ? window.openai.callTool('open_recharge_window', {wait:false})
     : ready.then(function(){ return rpc('tools/call', {name:'open_recharge_window', arguments:{wait:false}}); });
   Promise.resolve(call).then(function(r){
     var t=(r && r.content && r.content[0] && r.content[0].text) || '已打开充值窗口';
     log(t); b.disabled=false;
   }, function(e){ log('打开失败：'+e.message); b.disabled=false; });
 };
})();
</script></body></html>";
    }
}
