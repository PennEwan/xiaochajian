# 浏览器扩展怎么"替用户装好"

结论先说：**扩展必须由浏览器安装，外部程序不能静默塞进去**。Chrome 从 137 起禁用了 `--load-extension`，
Edge 也校验扩展完整性；改 `Secure Preferences`、往 `Extensions` 目录里复制都会被判定为篡改并禁用。
能真正做到"用户只点一下"的只有三条路，按推荐顺序：

| 路线 | 用户要做什么 | 我们要做什么 | 代价 |
|---|---|---|---|
| **A. 商店上架**（推荐） | 点一次「添加到 Edge / Chrome」 | 提交一次扩展包 | Edge Add-ons 免费；Chrome Web Store 一次性 $5 |
| **B. 企业策略强制安装** | 什么都不用做 | 上架后写一条注册表值（含扩展 ID） | 需管理员；只适合企业/托管机 |
| **C. 开发者模式加载**（现在这条） | 打开 `edge://extensions` → 开发者模式 → 加载已解压 → 选目录 | 给目录路径 | 零成本，但用户要动手 |

## A. 商店上架

提交物已经打好了：

```
dist\extension-store-0.4.3.zip      ← 直接上传这个（manifest 在 zip 根目录）
```

两个商店都用这个包：

- **Edge Add-ons**（免费）：<https://partner.microsoft.com/dashboard/microsoftedge/>
- **Chrome Web Store**（$5 一次性）：<https://chrome.google.com/webstore/devconsole>

提交时按这个填：

| 字段 | 内容 |
|---|---|
| 名称 | Codex 原生助手 · 登录信息读取 |
| 简介 | 为 Codex 原生助手的充值窗口读取当前 ChatGPT 登录信息，并在本机回传一次结果。 |
| 类别 | Productivity |
| 权限说明 | `host_permissions` 只含 `chatgpt.com` / `chat.openai.com` / `127.0.0.1`；无 `permissions`，不读标签页内容、不注入脚本到其它站点 |
| 数据用途 | 读取的登录内容只回传到本机 `127.0.0.1:43991` 的一次性请求，不上传第三方 |
| 图标 | `icon16/32/48/128.png`（已生成） |

上架后拿到扩展 ID（形如 `abcdefghijklmnop…`），用户点一次商店链接就装好、且自动更新。

## B. 企业策略强制安装（无需用户操作）

上架后用扩展 ID 写策略。Edge 支持用户级策略键（通常不需要管理员），Chrome 一般要 HKLM：

```powershell
# Edge（当前用户）
$key = 'HKCU:\Software\Policies\Microsoft\Edge\ExtensionInstallForcelist'
New-Item -Path $key -Force | Out-Null
New-ItemProperty -Path $key -Name '1' -Value '<EXTENSION_ID>;https://edge.microsoft.com/extensionwebstorebase/v1/crx' -PropertyType String -Force

# Chrome（需管理员）
$key = 'HKLM:\Software\Policies\Google\Chrome\ExtensionInstallForcelist'
New-Item -Path $key -Force | Out-Null
New-ItemProperty -Path $key -Name '1' -Value '<EXTENSION_ID>;https://clients2.google.com/service/update2/crx' -PropertyType String -Force
```

用户下次启动浏览器时自动安装、不可卸载、自动更新。撤销就删掉这条值。
（自托管 CRX 也行，但需要签名的 `.crx` + 一个 https 的 `update.xml`，比上架麻烦，不推荐。）

## C. 开发者模式（现在这条，零成本）

1. `edge://extensions` 或 `chrome://extensions`
2. 打开「开发人员模式」
3. 「加载解压缩的扩展」→ 选
   `…\plugins\codex-native-assist\browser-extension`
4. 保持一个已登录的 `chatgpt.com` 标签页开着

## 但先说清楚：这个扩展现在不是必需品

客户端已经能从 Codex 自己的登录文件里读到账号和令牌（`~/.codex/auth.json`），所以：

| 场景 | 要不要扩展 |
|---|---|
| 充值只要账号（邮箱 / 账号 ID） | **不要**，本地读，0.6 秒 |
| 服务端要 Codex 那个 access_token | **不要**，勾选「把登录令牌随兑换请求发送」即可 |
| 服务端**必须**要网页端那个 session（只有浏览器能拿到的 cookie 会话） | 要，走上面 A/B/C 之一 |

换句话说：只有当你的后端坚持要**网页 session**时，装扩展这件事才值得为它付上架成本。
