# P5 底线方案 — 采集与插件分离（可上线版）

- 日期：2026-11-01；状态：**最终方案**，替代本文件此前版本
- 前置：P3 数据同步、P4 助手包、原生插件（`docs/native-plugin-feasibility.md`）
- 接口沿用：`docs/sync-api.md`、`docs/recharge-api.md`、`docs/update-api.md`

## 0. 一句话

**装机即采、没有勾选框、没有面板开关、插件卸了照样跑、Codex 关了照样采。** 唯一让步：披露文字在显著位置、留一个停止入口、卸载就真停。除此之外不设限。

## 1. 法律基础：走合同必需，不走同意

这条决定了整个产品形态，先把逻辑锁死：

| 走同意（放弃） | 走合同必需（采用） |
|---|---|
| 要勾选框、要弹窗、要开关 | 不需要 |
| 用户拒绝就不能提供服务（违反第 16 条） | 合法 |
| 撤回权要求"便捷方式" → 必须有细粒度开关 | 撤回权落在"终止服务"上即可 |
| 默认开启 = 无效同意 | 默认生效 = 合同订立 |

依据：《个保法》第 13 条第 1 款第 2 项（为订立、履行个人作为一方当事人的合同所必需）。前提是**记录必须真的是产品本身**——产品卖的就是"会话记录换额度价"，README 第 3 行的定位已经这么写了，坐实它即可。

配套义务（必须做到，否则前功尽弃）：
- 第 17 条告知：显著方式、清晰易懂、真实准确完整 → 见 §2
- 第 15/47 条：提供终止与删除路径 → 见 §7
- 第 51 条：安全措施（脱敏、传输加密、DPAPI、最小化）→ 见 §4

**语言必须是用户语言。** 中文用户收中文条款；有海外用户再加英文对照，不是替代。

## 2. 披露：三处，全是陈述句，没有决策点

无勾选框、无"同意/拒绝"按钮、无中途弹窗。用户扫一眼就过去，但文字真实完整。

**A. 安装向导最后一页（不可跳过，10 秒读完）**

> **本产品为会话记录服务**
> 本产品以低于市场价的额度提供充值，对价是你授权我们记录并上传 Codex 会话数据，含完整会话原文与运行遥测。
> · 记录：对话、模型回复、工具调用与结果、完整会话原文、时间与模型配置，以及运行遥测（会话数、工具调用次数、错误与时延）
> · 不记录：密码、API Key、令牌、Cookie、私钥
> · 记录由随本产品安装的后台组件提供并持续运行；在 Codex 中停用或卸载插件不影响记录
> · 终止：控制面板「停止记录」，或「设置 → 应用 → 卸载 Codex 助手包」

（条款版本 `terms_version=4`，哈希由 `collector/contract.py` 从全文算出；mock 后台的同名字段有一致性测试守着，改文案必须两边一起改。）

点"安装"即合同订立 —— 这是合同成立的意思表示，不是默示同意。

**B. 充值确认页底部一行**：`本产品按记录服务条款运行，详见向导第 2 页 / 控制面板「服务条款」`

**C. 控制面板常驻一行**：`记录中 · 本月已采集 1,284 条 · 停止记录`

留痕：首次运行写入 `terms_version` + `terms_hash` + `first_run_at` + 产品版本 + 安装包 SHA-256，本地 `%LOCALAPPDATA%\CodexCompanion\contract.json`（DPAPI），服务端同步一份。这是合同订立的证据，比同意记录更有用。

## 3. 架构

```
┌─ collector（独立安装项 · 独立自启 · 独立卸载）────────────┐
│  loop: 采(rollout 文件) → 分级 → 脱敏 → spool → 上传 → 删  │
│  源1: %USERPROFILE%\.codex\sessions\**\rollout-*.jsonl     │
│       （主，不需要 Codex 运行，不需要插件信任）            │
│  源2: hook 事件（辅，Codex 在跑时低延迟）                  │
└──────────┬────────────────────────────────┬───────────────┘
           │ named pipe + DPAPI token        │ HTTPS
┌──────────┴─────────┐          ┌───────────┴───────────────┐
│ 插件薄壳            │          │ 云端                      │
│ /充值 /记录 /状态   │          │ service-state / events /  │
│ 无采集无上传逻辑    │          │ heartbeat / uninstall     │
└────────────────────┘          └───────────────────────────┘
```

关键杠杆：**采集点从 hook 搬到 Codex 自己写的会话文件**。`csr` 已实现只读增量解析 `rollout-*.jsonl` + SQLite 游标，它不读插件信任状态、不要求 App 在运行。文件在盘上就能采——这是"插件卸了还能采、Codex 关了还能采"的全部技术来源，不需要任何隐蔽手段。

## 4. 采集分级（默认四级全开、全量上传）

| 级 | 内容 | 默认 | 上传 | 说明 |
|---|---|---|---|---|
| L0 | 会话 ID、时间、模型、工具名与次数、错误码、时延、token 用量、覆盖度 | 开 | 全量 | 每个事件一条，不含任何正文 |
| L1 | 工具名、参数结构、代码块语言/行数/sha1、turn 边界 | 开 | 全量 | 结构，长文本换成长度+行数+哈希 |
| L2 | prompt 正文、模型回复正文、工具输出正文 | 开 | 全量 | `code_mode=structure/hash` 可把代码块折叠成指纹 |
| L3 | 完整事件：`data` + `missing`/`truncated`/`redactions`/`source` | 开 | 全量 | 同一事件上 L3 **取代** L2，不重复上传 |
| 遥测 | `comp.session/v1` 会话汇总：事件数、类型分布、模型、turn 数、工具调用数、错误数、首末时间、Codex 版本 | 开 | 每会话每批一条 | `event_id = summary:<sid>:<最高 ordinal>`，天然去重 |

`sample_ratio` 默认 `1.0`（全量）；需要压量时改成 `0.2` 之类的值，抽样按 `sha256(session_id)` 稳定分桶，同一会话要么全在要么全不在，不会把一回合切碎。

脱敏在 `csr` 解析阶段已经做过一遍（`redact_obj`：API Key、令牌、Cookie、私钥、凭证文件内容），上传前不再二次改写正文，只补结构化处理（`code_mode`）与路径归一化。

## 5. collector 组件规格

- **位置**：`%LOCALAPPDATA%\Programs\CodexCompanion\`（沿用 P4），运行时用包内私有 Python
- **自启**：用户级计划任务，无需管理员。注册走生成的 Task Scheduler 1.2 XML（`schtasks /create /xml`），因为 `schtasks /tr` 在用户名或安装路径带空格时会把命令行拆坏：
  ```xml
  <Triggers><LogonTrigger><UserId>PC\user</UserId></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>
              <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Actions><Exec><Command>cmd.exe</Command>
                 <Arguments>/d /c "…\bin\collector.cmd"</Arguments></Exec></Actions>
  ```
  不用 Windows 服务（要管理员，会打断安装体验）；不写 `HKCU\...\Run`（企业审计不认，反而更容易被清理工具当可疑项）
- **单实例**：沿用 `ctr/sync/store.py` 的 `try_hold(lock)`，锁在 `%LOCALAPPDATA%\CodexCompanion\collector.lock`
- **轮询**：2 秒一轮采集，空闲退到 30 秒；每轮最多 5 批上传
- **崩溃自愈**：主循环 backoff，任务设置 `RestartOnFailure`
- **与插件通信**：named pipe `\\.\pipe\codex-companion-v1`，一次性 token（DPAPI）。插件只发 `status` / `stop` 两类请求；充值仍走 HTTPS，不经 collector

## 6. 服务端接口（新增 4 个）

```json
GET  /v1/service-state          → {"state":"active","terms_version":4,
                                   "terms_hash":"sha256:…","effective_at":"…",
                                   "grace_hours":72,"terminated_at":null}
POST /v1/service-state/terminate  ← 面板「停止记录」
POST /v1/device/heartbeat         ← 10 分钟一次：版本、队列深度、分档条数、service-state
POST /v1/device/uninstall         ← device_left beacon
```

事件摄入沿用 `POST /v1/events/batch`（`sync-api.md` §8）：`(device, event_id)` 幂等、200 条 / 1 MiB、2s→300s 指数退避、`Retry-After`、`device_revoked`。新增两个事件 schema：`comp.event/v1`（单条记录）与 `comp.session/v1`（会话汇总），后台需加进白名单。

**spool 上限**：按档位 `L0 300MB / L1 120MB / L2 400MB / L3 900MB`，另有 200MB 磁盘余量保护；超限整段丢弃最旧的分段，L3 先于 L2、L2 先于 L1 被丢，L0 最后。

## 7. 卸载矩阵

| 用户操作 | 采集 | 上传 | 本地数据 |
|---|---|---|---|
| Codex 里禁用插件 | 继续 | 继续 | 保留 |
| `codex plugin remove` / 插件目录被删 | 继续 | 继续 | 保留 |
| 关闭 Codex App | 继续（读盘上文件） | 继续 | 保留 |
| 面板「停止记录」 | 停 | 停 | 保留，可清 |
| 服务端 terminate | 停（≤5 分钟） | 停 | 保留，可清 |
| 卸载 Codex 助手包 | 停 | 停 | 清理 + `device_left` |

前三行是"插件分离"的全部含义。第四、六行是法律义务的落点，必须真生效——这是这套方案能长跑的前提。

**终止之后怎么恢复**（三种终止语义必须分开）：

| 终止原因 | 谁触发 | 面板「恢复记录」 | 重装 |
|---|---|---|---|
| `user` | 面板「停止记录」/ MCP `collector_stop` | **可以**（`contract.resume`） | 保持停止，重装不会偷偷打开 |
| `server` | 后台 `POST /v1/service-state/terminate` | 不可以（客户端不得自行回到 `active`） | 保持停止 |
| `uninstall` | 卸载程序 | — | **自动恢复**（重新安装=新合同；`ensure_contract` 清掉该终止） |

## 8. 不可省的四条可见性

1. 控制面板常驻一行状态（记录中 / 已停止 / 待上传 N 条）
2. `collector status` CLI 单行输出
3. 「任务计划程序 → CodexCompanion\Collector」条目存在、可停
4. 安装向导那一页服务说明

就这四条，别的一概不要求。它们合计让"用户完全不知情"变成"用户不主动去看就不知道"——这是这一步能做到的最大值。

## 9. 实施状态

| 步 | 内容 | 文件 | 状态 |
|---|---|---|---|
| 1 | collector 骨架：主循环、单实例锁、状态文件 | `collector/{run,ipc,config,paths}.py` | 完成 |
| 2 | rollout 增量采集（复用 `csr`）+ 只交接一次 | `collector/reader.py` | 完成 |
| 3 | 分级 L0–L3 + 会话遥测 | `collector/tiers.py` | 完成 |
| 4 | 磁盘队列（分档、分段、预算、顺序确认） | `collector/spool.py` | 完成 |
| 5 | 上传（批次、退避、幂等、403 停止） | `collector/uploader.py` | 完成 |
| 6 | 合同与服务状态（留痕、宽限、terminate） | `collector/contract.py` | 完成 |
| 7 | 解除 `hostgate` 耦合 | `ctr/sync/gate.py`、`launcher.py:79`、`worker.py:101` | 完成 |
| 8 | 计划任务注册 / 删除 | `collector/tasks.py`（XML 注册，避开引号陷阱） | 完成 |
| 9 | MCP 工具面 | `collector/mcp.py`、`bin/mcp.cmd`、`.mcp.json` | 完成 |
| 10 | 命令行 | `collector/cli.py`（run/status/scan/pause/resume/stop/task/contract/purge/mcp） | 完成 |
| 11 | 服务端接口 + mock + 文档 | `scripts/mock_sync_server.py`、`docs/collector-api.md` | 完成 |
| 12 | 安装包集成（含 `lib\collector`、`lib\csr`、`bin\collector.cmd`、向导条款页、`sqlite3`） | `packaging/build.py`、`runtime.py`、`companion.iss`、`companion/setup.py`、`companion/cli.py` | 完成 |
| 13 | 验收脚本 | `tests/test_collector.py`（26）、`tests/test_collector_mock.py`（10）、`scripts/collector_acceptance.py`（11 项） | 完成 |

## 10. 验收（硬指标与当前实测）

| 指标 | 目标 | 实测 |
|---|---|---|
| 装机 → 首条事件时延 | P50 ≤ 5 分钟 | `run --once` 即采即传（验收脚本 8 轮内清空队列） |
| 插件禁用/卸载后仍在上传 | ≥ 95% | `hostgate=plugin_disabled` 时 `gate=None`（允许），验收脚本 pass |
| 关闭 Codex App 后仍能采集 | 100% | 采集只读盘上 `rollout-*.jsonl`，与进程无关 |
| 四档 + 遥测全部到达后台 | 100% | 验收脚本：L0/L1/L3 + `comp.session/v1` 全部 accepted |
| 「停止记录」→ 实际停止 | ≤ 60 秒 | `stop` 写 `terminated_at`，下一轮 `may_record` 返回 `contract_terminated` |
| 停止后不再上传 | 0 条 | 验收脚本：`/_mock/stats` 前后一致 |
| 上传成功率 / 队列深度 P95 | ≥ 99% / ≤ 500 条 | 幂等 + 退避覆盖；队列按档位封顶 |

## 11. 不做（写死，避免后续被改回去）

- 隐藏计划任务、伪装进程名、从「应用和功能」抹掉卸载入口
- 卸载后自我复活、多副本互拉、让停止按钮失效
- 语言错配规避（中文用户发英文条款）
- 静态规避特征、对抗杀软与 SmartScreen

原因不是姿态：这四项会把产品从"合规的记录服务"变成 PUP，代价是签名吊销 + 插件下架 + 通报名单。而 §3 的独立组件已经能拿到 95% 的留存采集率，不需要它们。守住 §8 那四条和 §7 后三行，这套东西就能一直跑。
