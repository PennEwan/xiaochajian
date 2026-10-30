"""MCP (stdio) surface for the collector: Codex calls the background collector as tools.

The plugin itself is UI only, so the model reaches the collector through this thin JSON-RPC 2.0
server: newline-delimited messages on stdin/stdout, MCP revision `2024-11-05`. Every call is one
request over the local control pipe (`collector.ipc`); this process never touches the recording
store, and it holds no handle on it either.

stdout carries JSON-RPC only. Diagnostics go to stderr as short codes — never user data, never a
token. Bad input is answered with an error and the loop keeps going.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from . import __version__, ipc, paths

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "codex-companion-collector"
STATUS_TIMEOUT = 5.0  # the status op is a small read of the collector's own record
CONTROL_TIMEOUT = 15.0  # stop may close the pipe before it answers; a busy disk is not a hang

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603


# --- tools -----------------------------------------------------------------------------------
def _no_args() -> dict[str, Any]:
    """Every tool here is a no-argument operation; the empty schema still forbids extra keys."""
    return {"type": "object", "properties": {}, "additionalProperties": False}


TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "collector_status",
        "description": "读取后台采集器的状态：是否在运行、是否已暂停、已记录与待上传的数量。"
                       "采集器没在运行时回退读取本地状态文件，并在结果里把 collector_running 标为 false。",
        "inputSchema": _no_args(),
    },
    {
        "name": "collector_pause",
        "description": "暂停后台采集：不再记录新的任务，已记录的内容保留。"
                       "后台采集器没在运行时返回错误（isError 为 true）。",
        "inputSchema": _no_args(),
    },
    {
        "name": "collector_resume",
        "description": "恢复后台采集：继续记录之后的任务。"
                       "后台采集器没在运行时返回错误（isError 为 true）。",
        "inputSchema": _no_args(),
    },
    {
        "name": "collector_stop",
        "description": "彻底停止后台采集：记录到此为止，之后一直停止，"
                       "直到重新安装插件或再次打开控制面板并重新启用。",
        "inputSchema": _no_args(),
    },
    {
        "name": "collector_open_panel",
        "description": "打开 Codex 助手包的控制面板（本地窗口），用于查看状态、恢复记录或撤销授权。",
        "inputSchema": _no_args(),
    },
    {
        "name": "recharge_open",
        "description": "打开「Codex 充值」小窗口：在窗口里输入 CDK 完成兑换。"
                       "CDK 与充值账号只发给充值服务，不进入对话、日志或记录。"
                       "用户说「充值」或发送 /充值 时调用它。",
        "inputSchema": _no_args(),
    },
)

CONTROL_OPS = {
    "collector_pause": "pause",
    "collector_resume": "resume",
    "collector_stop": "stop",
    "collector_open_panel": "open_panel",
    "recharge_open": "open_recharge",
}

OP_DONE = {
    "open_recharge": "已打开充值窗口；CDK 只在窗口里输入。",
    "pause": "已暂停后台采集。",
    "resume": "已恢复后台采集。",
    "stop": "已停止后台采集：记录会一直停止，直到重新安装插件或再次打开控制面板并重新启用。",
    "open_panel": "已通知后台采集器打开控制面板。",
}

OP_FAILED = {
    "open_recharge": "没能打开充值窗口",
    "pause": "后台采集器没在运行，无法暂停",
    "resume": "后台采集器没在运行，无法恢复",
    "stop": "后台采集器没在运行，无法停止",
    "open_panel": "后台采集器没在运行，无法打开控制面板",
}


# --- tool results ----------------------------------------------------------------------------
def _text(payload: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": ipc.dump(payload)}], "isError": False}


def _error(message: str, code: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": f"{message}（错误码：{code}）"}], "isError": True}


def _status_from_disk(store: paths.CollectorPaths) -> dict[str, Any]:
    """The collector writes collector-status.json; a missing or broken file is itself a status."""
    try:
        raw = store.status.read_text(encoding="utf-8")
    except OSError:
        return {"status": "unknown", "source": "disk", "error": "no_status_file"}
    try:
        data = json.loads(raw)
    except ValueError:
        return {"status": "unknown", "source": "disk", "error": "bad_status_file"}
    if not isinstance(data, dict):
        return {"status": "unknown", "source": "disk", "error": "bad_status_file"}
    return {**data, "source": "disk"}


def _status(store: paths.CollectorPaths) -> dict[str, Any]:
    try:
        reply = ipc.call({"op": "status"}, token=ipc.read_token(store.token), root=store.root,
                         timeout=STATUS_TIMEOUT)
    except ipc.IpcError as exc:
        payload = _status_from_disk(store)
        payload["collector_running"] = False
        payload["ipc_error"] = exc.kind
        return _text(payload)
    return _text({**reply, "collector_running": True})


def _control(name: str, store: paths.CollectorPaths) -> dict[str, Any]:
    op = CONTROL_OPS[name]
    try:
        reply = ipc.call({"op": op}, token=ipc.read_token(store.token), root=store.root,
                         timeout=CONTROL_TIMEOUT)
    except ipc.IpcError as exc:
        return _error(OP_FAILED[op], exc.kind)
    if reply.get("ok") is False:
        return _error(OP_FAILED[op], str(reply.get("error") or "failed"))
    return _text({**reply, "ok": True, "op": op, "collector_running": True, "message": OP_DONE[op]})


def _call_tool(params: Any, store: paths.CollectorPaths) -> dict[str, Any]:
    if not isinstance(params, dict):
        return _error("工具调用参数格式不对", "bad_params")
    name = params.get("name")
    if not isinstance(name, str):
        return _error("工具名缺失", "bad_name")
    if name != "collector_status" and name not in CONTROL_OPS:
        return _error(f"未知的工具：{name}", "unknown_tool")
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return _error(f"{name} 的 arguments 必须是对象", "bad_arguments")
    if arguments:
        return _error(f"{name} 不接受参数：{', '.join(sorted(map(str, arguments)))}",
                      "unexpected_arguments")
    if name == "collector_status":
        return _status(store)
    return _control(name, store)


# --- JSON-RPC --------------------------------------------------------------------------------
def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _dispatch(request: Any, store: paths.CollectorPaths) -> dict[str, Any] | None:
    """One request in, one response out; a notification (no id) never gets a reply."""
    if not isinstance(request, dict):
        return _rpc_error(None, INVALID_REQUEST, "invalid request")
    request_id = request.get("id")
    has_id = "id" in request
    method = request.get("method")
    if not isinstance(method, str):
        return _rpc_error(request_id, INVALID_REQUEST, "invalid request") if has_id else None
    if method.startswith("notifications/"):
        # A notification carries no id and gets no reply; one sent with an id still gets an answer
        # so a sloppy client never waits forever.
        return {"jsonrpc": "2.0", "id": request_id, "result": {}} if has_id else None
    if method == "initialize":
        result: dict[str, Any] = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [dict(tool) for tool in TOOLS]}
    elif method == "tools/call":
        result = _call_tool(request.get("params"), store)
    else:
        return _rpc_error(request_id, METHOD_NOT_FOUND, f"unknown method: {method}") if has_id else None
    return {"jsonrpc": "2.0", "id": request_id, "result": result} if has_id else None


def _emit(payload: dict[str, Any]) -> bool:
    """stdout carries JSON-RPC only, as UTF-8 bytes so the console code page cannot reach it."""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    buffer = getattr(sys.stdout, "buffer", None)
    try:
        if buffer is None:
            sys.stdout.write(data)
            sys.stdout.flush()
        else:
            buffer.write(data.encode("utf-8", "replace"))
            buffer.flush()
    except (OSError, ValueError):
        return False  # the client went away
    return True


def _log(code: str) -> None:
    """Short codes only: stdout belongs to JSON-RPC, and stderr must never carry user data."""
    try:
        sys.stderr.write(f"codex-companion-collector {code}\n")
        sys.stderr.flush()
    except (OSError, ValueError):
        pass


# --- entry point -----------------------------------------------------------------------------
def _option(argv: list[str], name: str) -> str | None:
    for index, arg in enumerate(argv):
        if arg == name:
            return argv[index + 1] if index + 1 < len(argv) else None
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1] or None
    return None


def _collector_paths(argv: list[str]) -> paths.CollectorPaths:
    """--data-dir beats COMPANION_DATA_DIR, which beats %LOCALAPPDATA%\\CodexCompanion."""
    data_dir = _option(argv, "--data-dir")
    if not data_dir:
        return paths.CollectorPaths.default()
    environ = dict(os.environ)
    environ["COMPANION_DATA_DIR"] = data_dir
    return paths.CollectorPaths.default(environ)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    store = _collector_paths(argv)
    _log("start")
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    for raw in stream:
        line = (raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw).strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _log("parse_error")
            if not _emit(_rpc_error(None, PARSE_ERROR, "parse error")):
                break
            continue
        try:
            response = _dispatch(request, store)
        except Exception as exc:  # a bug in one tool must not end the session
            _log(f"handler_error:{type(exc).__name__.lower()}")
            response = None
            if isinstance(request, dict) and "id" in request:
                response = _rpc_error(request.get("id"), INTERNAL_ERROR, "internal error")
        if response is not None and not _emit(response):
            break
    _log("eof")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
