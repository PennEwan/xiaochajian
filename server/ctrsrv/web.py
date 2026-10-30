"""Read-only HTML views for `/admin/ui`: devices, one device, one session.

Server-rendered, no JavaScript, no external assets — the page works from a bare `curl` and from a
browser without a build step. Everything goes through the same admin credential as `/admin/*`;
the browser form of it is a `?token=` query that is turned into a cookie on the first hit.
"""
from __future__ import annotations

import html
import json
from urllib.parse import quote


def _href_id(value: str) -> str:
    """Event ids contain `#` and `:`; a raw `#` would be read as a fragment by the browser."""
    return quote(str(value), safe="")

CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.55 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 0; padding: 24px;
       background: #0f1115; color: #e6e8ee; }
h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
h2 { font-size: 15px; margin: 26px 0 8px; color: #9aa4b2; font-weight: 600; }
a { color: #6cb6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
.sub { color: #8b95a5; margin: 0 0 18px; }
table { border-collapse: collapse; width: 100%; margin: 0 0 8px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #232833; vertical-align: top; }
th { color: #8b95a5; font-weight: 500; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
tr:hover td { background: #161a22; }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; }
.tag { display: inline-block; padding: 1px 7px; border-radius: 10px; font-size: 11.5px;
       background: #1d2430; color: #9fb0c7; margin-right: 4px; }
.tag.on { background: #14361f; color: #7ee2a8; }
.tag.off { background: #3a1c22; color: #ff9aa8; }
.kL0 { color: #8b95a5; } .kL1 { color: #9fb0c7; } .kL2 { color: #d6c07a; } .kL3 { color: #7ee2a8; }
pre { background: #12151c; border: 1px solid #232833; border-radius: 8px; padding: 14px;
      overflow: auto; font-size: 12.5px; }
.empty { color: #8b95a5; padding: 14px 0; }
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def layout(title: str, body: str) -> bytes:
    doc = (f"<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
           f"<title>{esc(title)}</title><style>{CSS}</style></head><body>{body}</body></html>")
    return doc.encode("utf-8")


def badge(state: str | None) -> str:
    on = (state or "") == "active"
    return f'<span class="tag {"on" if on else "off"}">{esc(state or "?")}</span>'


def _table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return '<div class="empty">还没有数据。</div>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


# --- pages -------------------------------------------------------------------------------------
def devices_page(stats: dict, devices: list[dict]) -> bytes:
    rows = []
    for d in devices:
        rows.append([
            f'<a class="mono" href="/admin/ui/devices/{esc(d["device_id"])}">{esc(d["device_id"])}</a>',
            badge(d.get("state")),
            esc(d.get("event_count", 0)),
            esc(d.get("session_count", 0)),
            f'<span class="mono">{esc((d.get("last_seen_at") or "—"))}</span>',
            f'<span class="mono">{esc(d.get("client") or "—")}</span>',
            "是" if d.get("uninstalled_at") else "—",
        ])
    tiers = stats.get("tiers") or {}
    body = [
        "<h1>采集后台</h1>",
        f'<p class="sub">{esc(stats.get("generated_at"))} · 生成于服务端</p>',
        '<p>' + "".join(f'<span class="tag">{esc(k)} {esc(v)}</span>' for k, v in [
            ("设备", stats.get("devices", 0)), ("在录", stats.get("active_devices", 0)),
            ("事件", stats.get("events", 0)), ("会话", stats.get("sessions", 0)),
            ("充值", stats.get("redemptions", 0))]) + "</p>",
        "<h2>按档位</h2>",
        '<p>' + ("".join(f'<span class="tag k{k}">{esc(k)} {esc(v)}</span>' for k, v in tiers.items())
                    or '<span class="empty">还没有事件</span>') + "</p>",
        "<h2>设备</h2>",
        _table(["设备", "状态", "事件", "会话", "最近活跃", "客户端", "已卸载"], rows),
    ]
    return layout("采集后台", "".join(body))


def device_page(device: dict, stats: dict, sessions: list[dict]) -> bytes:
    rows = []
    for s in sessions:
        kinds = s.get("kinds") or {}
        summary = "、".join(f"{k}×{v}" for k, v in list(kinds.items())[:6])
        rows.append([
            f'<a class="mono" href="/admin/ui/devices/{esc(device["device_id"])}/sessions/{esc(s["session_id"])}">'
            f'{esc(s["session_id"])}</a>',
            esc(s.get("events", 0)),
            " ".join(f'<span class="tag k{t}">{esc(t)} {esc(n)}</span>' for t, n in (s.get("tiers") or {}).items()),
            f'<span class="mono">{esc(s.get("first_ts") or "—")} → {esc(s.get("last_ts") or "—")}</span>',
            f'<span class="sub">{esc(summary)}</span>',
        ])
    body = [
        f'<h1>设备 {esc(device["device_id"])}</h1>',
        f'<p class="sub">{badge(device.get("state"))} 创建于 {esc(device.get("created_at"))}'
        f' · 最近活跃 {esc(device.get("last_seen_at") or "—")}'
        f'{" · 已卸载 " + esc(device.get("uninstalled_at")) if device.get("uninstalled_at") else ""}</p>',
        '<p>' + "".join(f'<span class="tag k{k}">{esc(k)} {esc(v)}</span>' for k, v in
                         (stats.get("tiers") or {}).items()) + "</p>"
        if stats.get("tiers") else "",
        "<h2>会话</h2>",
        _table(["会话", "记录", "档位", "时间范围", "内容概览"], rows),
        '<p class="sub"><a href="/admin/ui">← 返回设备列表</a></p>',
    ]
    return layout(f"设备 {device['device_id']}", "".join(body))


def session_page(device_id: str, session_id: str, events: list[dict]) -> bytes:
    rows = []
    for e in events:
        try:
            data = json.loads(e["body"]).get("data")
        except (KeyError, ValueError):
            data = None
        preview = _preview(data)
        rows.append([
            f'<span class="mono">{esc(e.get("received_at"))}</span>',
            f'<span class="k{esc(e.get("tier") or "L0")}">{esc(e.get("tier") or "-")}</span>',
            f'<span class="mono">{esc(e.get("kind") or "-")}</span>',
            f'<span class="mono">{esc(e.get("seq"))}</span>',
            esc(preview),
            f'<a href="/admin/ui/devices/{esc(device_id)}/events/{_href_id(e.get("event_id"))}">JSON</a>',
        ])
    body = [
        f'<h1>会话 {esc(session_id)}</h1>',
        f'<p class="sub">设备 {esc(device_id)} · {len(events)} 条记录（同一事件会按档位各有一条）</p>',
        _table(["到达时间", "档位", "类型", "seq", "内容", "原文"], rows),
        f'<p class="sub"><a href="/admin/ui/devices/{esc(device_id)}">← 返回设备</a></p>',
    ]
    return layout(f"会话 {session_id}", "".join(body))


def event_page(device_id: str, event: dict) -> bytes:
    try:
        pretty = json.dumps(json.loads(event["body"]), ensure_ascii=False, indent=2)
    except (KeyError, ValueError):
        pretty = str(event.get("body"))
    body = [
        f'<h1>{esc(event.get("kind") or "事件")}</h1>',
        f'<p class="sub">{esc(event.get("device_id"))} · {esc(event.get("session_id"))}'
        f' · {esc(event.get("tier"))} · seq {esc(event.get("seq"))}'
        f' · 到达 {esc(event.get("received_at"))}</p>',
        f"<pre>{esc(pretty)}</pre>",
        f'<p class="sub"><a href="/admin/ui/devices/{esc(device_id)}/sessions/'
        f'{_href_id(event.get("session_id"))}">← 返回会话</a></p>',
    ]
    return layout(str(event.get("kind") or "事件"), "".join(body))


def _preview(data, limit: int = 220) -> str:
    """A readable one-liner from whatever shape the event's `data` has."""
    if isinstance(data, dict):
        for key in ("text", "message", "output", "summary_text", "command", "last_agent_message"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip().replace("\r\n", "\n")
                return text[:limit] + ("…" if len(text) > limit else "")
        if data.get("summary_text"):
            return str(data["summary_text"])[:limit]
        return json.dumps(data, ensure_ascii=False)[:limit]
    if isinstance(data, str):
        return data[:limit]
    return ""
