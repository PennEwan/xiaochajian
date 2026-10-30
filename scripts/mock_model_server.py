"""Mock model endpoint (OpenAI Responses API, streaming) for the P4 acceptance test.

A throwaway CODEX_HOME points a custom model provider at it, so turns complete without a Codex
login or model quota and every lifecycle hook still fires. Optionally the first reply is a
shell tool call, so PreToolUse / PostToolUse fire too.

    [model_providers.mock]
    name = "Mock"
    base_url = "http://127.0.0.1:18790/v1"
    wire_api = "responses"

    python scripts\\mock_model_server.py [--port 18790] [--tool-call]
    GET /_mock/requests     how many requests, and the tool names Codex offered
    PUT /_mock/call-tool    {"match": "plugin_diagnostics", "args": {}}: unanswered requests call that tool ({} = stop)
"""
from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHELL_TOOLS = ("shell", "shell_command", "exec_command", "local_shell")


def _sse(events: list[dict]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode("utf-8")


def _usage() -> dict:
    return {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 6,
            "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 18}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_PUT(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.loads(raw or b"{}")
        self.server.call_tool = body.get("match") or None
        self.server.call_args = body.get("args") or {}
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        body = json.dumps({"requests": self.server.requests}).encode("utf-8")
        self.send_response(200 if self.path == "/_mock/requests" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            req = {}
        tools = []
        for t in req.get("tools") or []:
            if not isinstance(t, dict):
                continue
            tools.append(t.get("name") or t.get("type"))
            # Namespaced tool groups (MCP servers) nest their functions.
            for sub in t.get("tools") or []:
                if isinstance(sub, dict) and sub.get("name"):
                    tools.append(f"{t.get('name')}.{sub['name']}")
        items = req.get("input") or []
        # Only the current turn counts: a tool output earlier in the thread must not stop a new call.
        last_user = max((n for n, i in enumerate(items) if isinstance(i, dict) and i.get("role") == "user"), default=-1)
        answered = any(isinstance(i, dict) and i.get("type") in ("function_call_output", "local_shell_call_output",
                                                                  "custom_tool_call_output") for i in items[last_user + 1:])
        with self.server.lock:
            n = len(self.server.requests) + 1
            self.server.requests.append({"n": n, "tools": tools, "answered_tool": answered})
        rid = f"resp_{n}"
        events = [{"type": "response.created", "response": {"id": rid}}]
        shell = next((t for t in tools if t in SHELL_TOOLS), None)
        match = self.server.call_tool
        target = next((t for t in tools if match and t and match in t), None) if not answered else None
        if target:
            ns, _, fn = target.rpartition(".")
            item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": fn or target,
                    "arguments": json.dumps(self.server.call_args)}
            if ns:
                item["namespace"] = ns
            events.append({"type": "response.output_item.done", "item": item})
        elif self.server.tool_call and shell and not answered:
            args = {"exec_command": {"cmd": "echo p4-acceptance"},
                    "shell_command": {"command": "echo p4-acceptance"}}.get(
                shell, {"command": ["cmd.exe", "/c", "echo p4-acceptance"]})
            events.append({"type": "response.output_item.done", "item": {
                "type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": shell,
                "arguments": json.dumps(args)}})
        else:
            events.append({"type": "response.output_item.done", "item": {
                "type": "message", "role": "assistant", "id": f"msg_{n}",
                "content": [{"type": "output_text", "text": "mock reply: done"}]}})
        events.append({"type": "response.completed", "response": {"id": rid, "usage": _usage()}})
        body = _sse(events)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str = "127.0.0.1", port: int = 18790, tool_call: bool = False,
                call_tool: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.requests, server.lock, server.tool_call = [], threading.Lock(), tool_call
    server.call_tool, server.call_args = call_tool, {}
    return server


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18790)
    ap.add_argument("--tool-call", action="store_true")
    ap.add_argument("--call-tool", help="call the first offered tool whose name contains this text")
    args = ap.parse_args()
    server = make_server("127.0.0.1", args.port, args.tool_call, args.call_tool)
    print(f"mock model on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
