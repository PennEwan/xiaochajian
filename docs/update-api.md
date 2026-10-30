# Codex 助手包 · 更新服务接口约定（v1）

- 适用：Codex 助手包（P4）客户端 `lib/companion/update.py`
- 状态：后台尚未实现；本机模拟后台 `scripts/mock_update_server.py`（默认端口 18789）
- 地址：构建时写入 `product.json` 的 `update_manifest_url`（`packaging/build.py --update-url`）；未配置时客户端从不检查更新

## 1. 原则

- 客户端只发 `GET`，只带 `User-Agent: CodexCompanion/<当前版本>`；**不带**设备 ID、账号、令牌或任何本机信息。
- 必须 `https://`（本机回环地址 `127.0.0.1` / `localhost` 仅供开发）；**不跟随重定向**（3xx 视为失败）。
- 安装包下载后先核对 `size` 与 `sha256`，两者都一致才会运行；不一致的文件立即删除。
- 何时安装由用户决定（第 4 节），服务端不能强制。

## 2. 获取清单

```
GET {update_manifest_url}
200 application/json
```

```json
{
  "product": "codex-companion",
  "channel": "stable",
  "latest": {
    "version": "1.0.1",
    "url": "https://downloads.example.com/codex-companion/CodexCompanion-Setup-1.0.1.exe",
    "sha256": "a83f856727d696e4abe82feb520b48f385ff4628a35607abbd76754ba450e134",
    "size": 12400000,
    "published_at": "2026-10-29T00:00:00Z",
    "min_kernel": "0.163.0",
    "requires_retrust": false,
    "notes": "修复……"
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `product` | 是 | 固定为 `codex-companion`，否则清单无效 |
| `latest` | 是 | 没有可用版本时为 `null` |
| `latest.version` | 是 | `主.次[.修订[.构建]]`，纯数字；高于当前版本才算新版本 |
| `latest.url` | 是 | 安装包地址，必须 `https://` |
| `latest.sha256` | 是 | 安装包 SHA-256，64 位小写十六进制 |
| `latest.size` | 是 | 安装包字节数，1 B – 200 MiB |
| `latest.min_kernel` | 否 | 新版本需要的最低 Codex App 内核；自动安装时内核低于它则不装 |
| `latest.requires_retrust` | 否 | 新版本修改了 `hooks/hooks.json`，安装后用户须在 Codex 中重新「全部信任」；为 `true` 时**永不自动安装** |
| `latest.notes` | 否 | 更新说明（≤ 2000 字），显示在安装确认框里 |

清单大小上限 64 KiB。格式不符、字段缺失或取值越界 → 客户端记为 `bad_manifest`，不提示更新。

## 3. 下载安装包

```
GET {latest.url}
200 application/octet-stream
```

客户端边下载边计算 SHA-256；字节数超过 `size` 立即中止（`size_mismatch`），下载完成后长度或哈希不符（`size_mismatch` / `hash_mismatch`）→ 删除，不运行。

## 4. 客户端行为

| 更新方式（控制面板） | 检查 | 安装 |
|---|---|---|
| 提示我（默认） | 每 24 小时最多一次（由 Hook 拉起独立的后台检查进程，Hook 本身不联网）；打开面板时 | 面板显示「更新到 x.y」，Codex 中每天最多提示一次；**用户点击确认后**下载、校验、运行安装程序 |
| 自动安装（用户主动开启） | 同上 | 后台下载并校验后静默安装（`/SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE=1`）；`requires_retrust` 或内核低于 `min_kernel` 的版本仍等用户点击 |
| 不检查 | 从不联网 | — |

安装程序升级时先停止本产品的进程（控制面板、充值窗口、上传进程、更新检查），原地替换文件，再用 Codex 自己的命令刷新插件缓存；插件清单版本号在 1.x 内不变，Hook 定义不变时不需要重新信任。用户的授权、充值绑定与本机记录保持不变。

## 5. 错误处理

| 情况 | 客户端记录（`update.json` 的 `error`） | 用户可见 |
|---|---|---|
| 网络不通 / 超时 | `network` | 面板「更新：检查失败，稍后自动重试」；不弹窗、不影响 Codex |
| HTTP 非 200（含 3xx） | `http_<状态码>` | 同上 |
| 清单无效 | `bad_manifest` | 同上 |
| 下载校验失败 | `hash_mismatch` / `size_mismatch` | 面板显示下载失败；文件已删除 |

## 6. 模拟后台（`scripts/mock_update_server.py`）

- `GET /manifest.json`、`GET /files/<安装包文件名>`
- `POST /_mock/release {"version","path","requires_retrust","min_kernel","notes","tamper"}`：设置要发布的安装包（`tamper: true` 时下载内容被篡改一个字节，用于验证哈希校验）；请求体为 `null` 表示没有新版本
- `POST /_mock/fail-next {"status": 503, "count": 1}` 或 `{"mode": "redirect"}`
- `GET /_mock/requests`：收到的请求与 User-Agent
