# 用户全流程（本机版）：装 → 信任 → 充值 → 用 → 看数据 → 停

面向"我现在就是用户"的场景，全部在本机跑通，不依赖任何外网服务。约 15 分钟。
命令都在 `D:\codex-task-recorder` 下执行（PowerShell）。

---

## 0. 先把"服务端"起起来（两个窗口，别关）

现成的安装包指向 `127.0.0.1`，所以本机得先有服务。开一个窗口放**充值服务**：

```powershell
cd D:\codex-task-recorder
python scripts\mock_recharge_server.py
```

再开一个窗口放**真正的后台**（记录上传 + 看数据）：

```powershell
cd D:\codex-task-recorder
$env:PYTHONPATH="server"
python -m ctrsrv --db data\ctrsrv.sqlite3 initdb
python -m ctrsrv --db data\ctrsrv.sqlite3 add-cdk TEST-OK01 --product "Plus 1 个月"
python -m ctrsrv --db data\ctrsrv.sqlite3 serve --port 8080 --admin-token demo
```

看到 `ctrsrv 1.0.0 listening on http://127.0.0.1:8080` 就成了。
（`initdb` / `add-cdk` 只需要做一次。）

---

## 1. 打一个"指向本机后台"的安装包

仓库里现成的 2.0.1 指向的是旧 mock（18788）。花一分钟重打一个指向 8080 的：

```powershell
python packaging\build.py --version 2.0.2 --profile prod `
  --recharge-url http://127.0.0.1:18787 `
  --sync-url http://127.0.0.1:8080 `
  --update-url http://127.0.0.1:18789/manifest.json
```

产物：`dist\CodexCompanion-Setup-2.0.2.exe`，同时留下 `build\stage-2.0.2\`。

---

## 2. 安装

**正常情况**：双击 `dist\CodexCompanion-Setup-2.0.2.exe` → 向导里那一页「本产品为会话记录服务」→ 下一步 → 安装。

**如果 exe 被拦截**（这台沙箱会话会拦，返回 ERROR_CANCELLED），用等价的命令行安装：

```powershell
python scripts\install_local.py --stage build\stage-2.0.2
```

两种方式做的事完全一样。期望看到：

```
[ OK ] 文件已就位
  status             untested
  terms_version      4
  collector_task     created
[ OK ] 插件注册完成（status=untested）。
```

- `untested` = 你的 Codex（内核 0.165）比我验证过的 0.163 新，只是提示，不是错误。
- `collector_task created` = 计划任务 `CodexCompanion\Collector` 已注册（登录时自动起）。

---

## 3. 在 Codex 里信任（一次）

1. **完全退出** Codex（托盘里也退出），再打开。
2. 插件 → Codex 助手包 → **信任 `companion_collector`**（只有一个服务器，没有 12 个 Hook）。
3. 开始菜单「Codex 助手包」→「重新检测」→ 清单里「已在 Codex 中信任插件的 MCP 服务器」变成完成。

---

## 4. 充值

在 Codex 任意对话里发送 **`/充值`** → 模型调用 `recharge_open` → 弹出小窗口：

1. 第一次：确认充值到当前账号；
2. 输入 CDK：**`TEST-OK01`**（模拟充值服务预置的可用码，`TEST-OK02`… 也行）；
3. 确认兑换 → 提示成功。

没弹窗？开始菜单「Codex 助手包 → Codex 充值」直接开，效果一样。

---

## 5. 正常用两轮 Codex

随便问两句，最好让它跑一次命令（例如「列一下当前目录」），这样工具调用、token 用量、错误码这些都有。

后台组件不需要你点任何开关：它读的是 Codex 自己写在 `%USERPROFILE%\.codex\sessions\` 下的会话文件。

---

## 6. 看数据到没到

**命令行（最直观，按对话还原）**

```powershell
python -m ctrsrv --db data\ctrsrv.sqlite3 view --latest
```

期望：`会话 <uuid>（设备 dev_…，N 条记录）` 下面一串 `[时间] L3 user_prompt … 你问的那句话`、`L3 assistant_message …`、`L1 tool_call …`。

**网页**

```
http://127.0.0.1:8080/admin/ui?token=demo
```

设备 → 会话 → 单条 JSON，一路点下去就能看到原文。

**只看数字**

```powershell
python -m ctrsrv --db data\ctrsrv.sqlite3 stats
```

期望：`events` 大于 0，`tiers` 里有 `L0/L1/L3`，`devices` 是 1。

---

## 7. 验证"插件没了还在采"

1. 在 Codex 插件页**禁用**「Codex 助手包」；
2. 再用 Codex 聊两轮；
3. 再跑一次 `view --latest` → 应该看到**新会话**，条数继续涨。

这就是这个产品跟普通插件最大的区别：插件只是界面，记录由独立组件承担（任务计划程序里能看到它）。

---

## 8. 想停 / 想卸

```powershell
$py = "$env:LOCALAPPDATA\Programs\CodexCompanion\marketplace\plugins\codex-companion\runtime\python.exe"
& $py -m collector stop      # 立刻停：写终止时间、进程退出；后台不再收到任何东西
& $py -m collector resume    # 你自己停的，可以恢复（服务端终止的不行）
```

卸载：「设置 → 应用 → Codex 助手包 → 卸载」。卸载顺序是：写终止 → 停进程 → 删计划任务 → 移插件 → 还原 Codex 配置 → 删文件。

命令行装的（`install_local.py`）没有卸载器：删安装目录 + `schtasks /delete /tn CodexCompanion\Collector /f` 即可。

---

## 每一步"不对劲"时看哪里

| 现象 | 先看什么 |
|---|---|
| 装完面板说「后台组件未在运行」 | `schtasks /query /tn CodexCompanion\Collector`；没有就跑 `install_local.py` 再看 `collector_task` |
| `view --latest` 说后台还没有事件 | 采集器的上报地址：`%LOCALAPPDATA%\CodexCompanion\collector-config.json` 里 `base_url` 是不是 `http://127.0.0.1:8080` |
| 一直「待上传 N 条」不降 | 后台窗口是不是关了；`collector.log` 里 `last_error` |
| `/充值` 没弹窗 | 没信任 MCP 服务器，或模型没调工具——从开始菜单开 |
| 兑换说 CDK 无效 | 模拟充值服务只认 `TEST-OK01`…`TEST-OK05` |
| 面板说 Codex 未经验证 | 正常提示（你的是 0.165，验证过的是 0.163） |
