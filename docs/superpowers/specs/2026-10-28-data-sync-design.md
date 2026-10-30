# Codex 任务记录 · 数据同步（P3）— 设计说明

- 日期：2026-10-28；状态：用户已选定方案 A，按本文实施
- 前置：P2 本地记录器（`2026-10-28-codex-task-recorder-design.md`，以下称"记录器"）；P1 充值插件（`2026-10-28-cdk-recharge-design.md`）
- 接口约定：`docs/sync-api.md`

## 1. 目标与约束

把记录器扩展为：用户**单独开启并同意**后，把已记录的 Codex 任务事件持续上传到后台。

用户约束：
1. 单独开启、明确同意；未授权不上传任何任务内容。
2. 与充值功能独立：不依赖充值窗口、充值插件、充值绑定。
3. 正常使用 Codex 时自动采集与上传。
4. 本地持久化队列，断网可恢复；不重复、不丢失、不错序。
5. 统一事件结构，关联 session / turn / model / tool。
6. 排除密码、密钥、CDK、session 凭证等。
7. 可暂停、撤销授权、删除数据。
8. 不阻塞 Codex，不明显增加延迟。
9. 复用 P2 采集；不改官方二进制，不注入进程，不全局监听键盘。

用户决定（2026-10-28）：
- 后台 **尚不存在** → 先定接口约定 + 本地模拟后台（与 P1 相同做法）。
- 数据归属：**匿名设备**（随机设备 ID + 设备令牌，后台不知道 ChatGPT 账号）。
- 方案 **A**：会话 JSONL 即队列 + 按需拉起的后台上传进程。

假设（已向用户说明，未被更正）：
- 同步是记录器插件的一部分，不另建插件；同步只上传记录器记下的内容，所以要求记录开启。
- "关闭同步后不再采集新内容" = 不再为上传采集或发送新内容；本地记录由 P2 自己的开关控制。
- 只上传开启同步**之后**的内容；开启前的本地历史不补传。

## 2. 方案概览

```
Codex Hook ──► ctr.hook（P2，扩展）──► sessions\<日>\<会话>.jsonl   ← 唯一数据源（队列）
                    │  追加后 kick()
                    ▼
             sync\worker.lock 未被持有？→ 分离拉起 pythonw -m ctr.sync
                                                │
                       每个会话一个上传游标 ◄────┤ 读游标之后的完整行 → 授权时段过滤
                       sync\cursors\*.json      │ → 精简 + 二次脱敏 → 分批 POST
                                                ▼
                                         后台 /v1/events/batch（按 event_id 幂等）
```

- Hook 进程**不联网**。只多做三件事：给事件编号（`seq`）、标注当前模型（`model`）、检查上传进程是否在运行（不在就拉起）。
- 上传进程单实例（文件锁），空闲 10 分钟退出；下一次 Hook 或 CLI 再拉起。
- 本地数据只有一份：上传游标只记"传到哪"，不复制事件。

## 3. 事件结构 `ctr.event/v2`

v2 = v1（记录器设计 6.4）+ 两个字段，在记录时写入：

| 字段 | 含义 |
|---|---|
| `seq` | 会话内从 1 递增的整数，在会话锁内按写入顺序分配（`store.SessionHandle.append`）。即"采集顺序"。 |
| `model` | 事件发生时的模型。Hook 事件取 Hook 输入的 `model`；transcript 事件取 transcript 流中最近一次 `turn_context` / `thread_settings_applied` 的模型；派生事件取所在批次的当前模型；未知为 null。 |

关联键：`session_id` → `turn_id` → `tool_use_id`（Hook `exec-…` / 模型级 `call_…`）/ `item_id`；`agent_id`；`model`。模型切换另有 `model_changed` 事件（P2 已有）。

排序：同一会话按 `seq` 排序即采集顺序；内容发生时间用 `source_ts`（无则 `recorded_at`）。

旧数据：v1 事件没有 `seq`，不会被上传（只会出现在开启同步之前，本来也不在授权时段内）；如在授权时段内遇到 v1 事件（插件缓存未更新），跳过并计为 `old_schema`，状态页提示运行 `ctr install`。

### 3.1 回合覆盖事件 `turn_coverage`（验收 6：明确标记缺失）

记录器在回合结束时派生一条 `turn_coverage`，逐项说明本回合捕获了什么、缺什么、为什么缺：

- 触发：标准会话在 transcript 的 `task_complete` / `turn_aborted`；后台会话（无 transcript）在 Hook 的 `Stop` / `Interrupt`。每个回合只发一次。
- 统计：会话游标里按 `turn_id` 累计各类事件（种类、完整度、模型），发出后删除；最多保留 50 个未结束回合。
- 每项状态：`captured`（已捕获）/ `partial`（部分）/ `unavailable`（来源明确没有提供）/ `not_captured`（应有但未见到）/ `none`（本回合本来就没有，如未调用工具）。

| 项目 | 判定 |
|---|---|
| `user_prompt` | 有 user_prompt → captured；否则 not_captured |
| `assistant_messages` | 有 transcript 的完整消息 → captured；只有 Stop 的最后一条 → partial；没有：回合完成 → not_captured，失败/中断 → none |
| `reasoning_summary` | 有内容 → captured；只有 unavailable 占位 → unavailable；transcript 中完全没有推理条目 → 同样 unavailable（注明"模型未返回推理或摘要"；回合结束时已读完整个回合，没有就是来源没给） |
| `model` | 本回合事件出现过模型 → captured（列出全部取值）；否则 not_captured |
| `tool_calls` | 调用/结果计数；0 → none；附 `outcome_unknown` 数 |
| `token_usage` | 有 → captured；回合完成但没有 → not_captured；失败/中断 → none |
| `timestamps` | 恒为 partial：Hook 事件没有来源时间戳，只有 recorded_at |

后台会话的 `reasoning_summary`、`token_usage` 一律为 `unavailable`，原因"会话无 transcript（Codex 不持久化）"；`assistant_messages` 在 Stop 给出最后一条消息时为 `partial`，否则同样为 `unavailable`。`missing` 列出所有 not_captured / unavailable 项；有缺失时事件完整度为 partial。

覆盖统计尽力而为：晚于回合结束才处理的 async Hook（极少见）不计入。

### 3.2 能力声明

每个上传批次带固定的 `capabilities`，声明哪些内容**按策略不采集**、哪些**Codex 不提供**（见 `docs/sync-api.md` 第 4 节），后台据此区分"没有"和"丢了"。

## 4. 本地存储（`%LOCALAPPDATA%\CodexTaskRecorder\sync\`）

| 文件 | 写入方 | 内容 |
|---|---|---|
| `state.json` | CLI；上传进程只改 `pending_ops`、遇 403 时关闭 | 是否开启、同意版本与时间、**同意时的后台地址**、同步授权时段、待执行的后台操作 |
| `config.json` | CLI | 后台地址（默认未配置）、超时 |
| `device.bin` | 上传进程 | DPAPI 加密：device_id、device_token、base_url、注册时间 |
| `cursors\<日期>.<会话>.json` | 上传进程；重新同意时由 CLI 初始化到文件末尾 | 每个会话文件一个：相对路径、已处理字节偏移、文件头 128 字节的哈希（识别"删除后重新记录"的同名文件）、最后 seq、已上传/跳过/拒收计数 |
| `status.json` | 上传进程 | 最近成功/失败时间与类型、累计上传数、连续失败次数、下次重试时间 |
| `worker.lock` / `spawn.stamp` | 上传进程 / Hook | 单实例锁；最近一次拉起时间（限流） |
| `sync.log` | 上传进程、CLI | 只记固定标识和数字（操作、结果、HTTP 状态、条数、耗时），不记内容、令牌、地址 |

`state.json` 的读改写在 `sync\state.lock` 文件锁内完成（复用 `store` 的锁实现，抽出为 `store.locked()`）。读取方：文件损坏或读不了 → 视为未开启（失败即关闭）；写入方：读不了时重试、仍失败则报错，绝不用空白状态覆盖（否则会丢掉待送达的撤销请求）。

所有"写临时文件再替换"的写入都用 `store.replace()`：Windows 上只要有别的进程正打开目标文件，替换就会失败（实测 WinError 5），读者毫秒内释放，因此短暂重试。上传进程通过列出 `sessions\*\*.jsonl` 找会话文件，**不读取记录器的会话游标**（Hook 每次都会替换它们）。

## 5. 组件

新增 `plugins/codex-task-recorder/lib/ctr/sync/`：

| 模块 | 职责 |
|---|---|
| `state.py` | 同步状态：enable / pause / revoke、授权时段、`pending_ops`、`in_window(event)` |
| `config.py` | 路径与后台地址 |
| `vault.py` | DPAPI 保存设备令牌（与充值插件各自一份，代码不共享） |
| `api.py` | 后台客户端（urllib，不跟随重定向，区分"未发出 / 结果未知 / HTTP 错误"，与 P1 相同） |
| `outbox.py` | 扫描会话文件 → 授权过滤 → 精简与二次脱敏 → 组批；确认后提交游标 |
| `worker.py` | 上传主循环：单实例、注册、上传、退避、待执行操作、空闲退出 |
| `launcher.py` | `kick()`：判断是否需要、是否已在运行、限流后分离拉起 |
| `log.py` | 固定标识日志 |
| `__main__.py` | `python -m ctr.sync` 入口（上传进程） |

修改：`events.py`（v2、`model` 字段）、`store.py`（`seq`、`locked()`）、`hook.py`（模型标注、`turn_coverage`、同步提示、kick）、`transcript.py`（逐条标注模型）、`redact.py`（CDK / session 规则）、`cli.py`（`ctr sync …`、`status` 增加同步行、卸载时处理同步）、`plugin.json` 描述、README。

不修改 `hooks/hooks.json`，**不提升插件版本号**（版本号决定缓存路径，进而改变 Hook 命令，可能需要用户重新信任）；更新代码后用 `ctr install` 原地刷新。

## 6. 流程

### 6.1 开启同步（`开启同步.cmd` → `ctr sync enable`）
1. 后台地址未配置 → 提示并退出（开发用 `ctr sync config --base-url http://127.0.0.1:18788`）。非本机地址必须 https。
2. 本地记录从未同意 → 先显示 P2 记录同意说明，按 Y 才继续。
3. 显示同步授权说明（第 8 节），按 Y → 打开同步授权时段；如记录未开启则一并开启。
4. `ctr install` 原地刷新插件代码；拉起上传进程；显示 Hook 信任状态。
5. 设备在上传进程首次运行时注册（`POST /v1/devices`），不在 Hook 中。

### 6.2 Hook（每次）
在记录器原有流程上：
1. `SessionHandle.append` 分配 `seq`；`hook.py` / `transcript.py` 标注 `model`；遇回合结束派生 `turn_coverage`。
2. 同步开启 → 本会话首次 `UserPromptSubmit` 的提示改为同步版（每会话一次，与本地提示分别计）：
   `● Codex Task Recorder 正在记录本会话，并按你的授权同步到后台。暂停：双击「暂停同步.cmd」`
3. 会话锁释放后 `kick()`：同步开启或有待执行操作，且 `worker.lock` 未被持有，且距上次拉起超过 30 秒 → 分离拉起上传进程（P1 `launch.spawn` 同款：DETACHED、BREAKAWAY_FROM_JOB、不继承句柄）。任何异常吞掉。

### 6.3 上传进程
```
拿不到 worker.lock → 退出
循环：
  读 state：有 pending_ops → 先执行（delete_remote → revoke_device）
  未开启且无 pending_ops → 退出
  无设备令牌 → 注册
  组批（每批 ≤ 200 条、≤ 1 MiB；单条超限则单独成批）
    空 → 空闲满 10 分钟退出，否则 2 秒后再看
  重读 state：此刻已暂停 / 撤销 → 退出（不注册、不上传）
  注册失败（任何错误）→ 退避重试；**注册错误永远不会让事件被判为拒收**
  POST /v1/events/batch
    200 且 results 覆盖全部 event_id → 提交游标（accepted / duplicate 计已上传，rejected 计拒收并跳过）
    413，或 400 且 code = invalid_batch → 批量减半定位；单条仍如此 → 计拒收跳过
    其他 400（例如代理返回的错误页）→ 当作暂时错误退避重试
    401 → 丢弃令牌，下一轮重新注册（新设备 ID，记入日志）
    403 device_revoked → 关闭同步（state.enabled=false），状态提示"后台已停用此设备"
    429 / 带 Retry-After 的错误 → 按 Retry-After 等待，限制在 2–300 s
    5xx / 网络 / 结果未知 → 指数退避 2 s 起、上限 300 s、±20% 抖动
  所有等待都按 2 秒一步，每步检查状态：暂停 / 撤销后 2 秒内退出
  连续失败满 60 分钟 → 退出（下次 Hook / CLI 再拉起）
```

后台操作（删除数据、撤销设备）：网络错误、429、5xx 保留待重试；401/403（设备已不存在）与其他 4xx（后台永远不会接受）记录结果后视为完成，不会让同步永远卡住。CLI 显示每个操作的真实结果（例如"后台未能删除数据（rejected_404）"），不会笼统地说"已删除"。

上传进程以系统临时目录为工作目录：Windows 不能删除作为某进程工作目录的文件夹，而卸载、`ctr install`、清除数据都会删除插件缓存或数据目录。

组批（`outbox`）：
- 列出 `sessions\*\*.jsonl`（按修改时间，旧的先传）；文件大小 > 上传游标偏移即有待处理内容。按文件依次读游标之后的**完整行**。
- 同步未开启（暂停 / 撤销）时不组批、不动游标。
- 每条事件判定：
  1. `recorded_at` 不在同步授权时段内，或 `source_ts` 非空且不在时段内（`session_meta` 只看 `recorded_at`）→ 跳过（`outside_sync_window`）。
  2. 不是 v2 或没有 `seq` → 跳过（`old_schema`）。
  3. 否则精简后放进批次。
- 设备在有第一批要上传时才注册（不在空闲时注册）。
- 精简：`hook_invocation` 只保留 `payload_keys`、`transcript_path_present`、`notice_emitted`、`duration_ms`（去掉本机环境变量、进程链、Python 路径）；对 `data` 再跑一遍 `redact_obj`，新增命中写入 `redaction.upload_pass`。
- 游标只在后台确认后前移（含批次末尾之后被跳过的行）。会话文件被删除 → 上传进程启动时丢弃对应游标；文件变短，或文件头哈希与游标记录的不同（删除后重新记录的同名文件）→ 偏移归零，计 `file_rewound`，依靠后台去重。没有 `event_id` 的行计 `bad_line` 跳过（否则会永远卡住队列）。

### 6.4 不重复、不丢失、不错序
- **不重复**：`event_id`（记录时生成的 uuid4，写在文件里，重试不变）是后台的幂等键；客户端只在确认后前移游标。崩溃在"后台已收、游标未存"之间 → 重发 → 后台返回 `duplicate`。
- **不丢失**：数据先落本地文件；上传进程任意时刻退出，游标之后的内容下次继续。网络失败不前移游标。被后台逐条拒收（格式错误）的计数并显示在状态中，不静默。
- **不错序**：同一会话的事件按 `seq` 递增发送；在前一批确认之前不会发送后面的批次。后台按 `(session_id, seq)` 排序。

### 6.5 暂停 / 恢复 / 撤销 / 删除
| 操作 | 入口 | 效果 |
|---|---|---|
| 暂停 | `暂停同步.cmd` → `ctr sync pause` | 关闭授权时段：之后记录的内容不上传；**已在队列中（授权时段内）的内容也停止上传**，恢复后继续；上传进程在当前请求结束后退出 |
| 恢复 | `开启同步.cmd` | 同意版本未变时只需确认一行；新开一个授权时段 |
| 撤销授权 | `撤销同步.cmd` → `ctr sync revoke` | 关闭同步、清空授权时段与同意记录（队列中未上传内容永不上传）；询问是否同时删除后台数据；`pending_ops` 加入 `delete_remote`（可选）与 `revoke_device`。CLI **只在拿到上传进程锁时**前台执行（最多等 5 秒；空闲的上传进程 2 秒内就会退出），避免与正在进行的上传交错；拿不到或网络失败则留给上传进程——它在任何上传之前先执行待送达操作；成功后删除本地设备令牌 |
| 更换后台 | `ctr sync config --base-url` / `sync enable --base-url` | 本设备已在原后台注册 → 拒绝，提示先撤销（否则原后台数据再也无法删除）；未注册 → 暂停同步，须对新后台重新同意。同意记录绑定后台地址；配置文件被手工改成别的地址时上传进程不上传（`backend_changed`） |
| 重新同意 | 首次开启、撤销后、同意文本或后台变化后的开启 | 丢弃旧授权时段；把每个会话文件的上传游标初始化到当前末尾（之前的历史既不上传，也不再被反复读取） |
| 删除后台数据 | 撤销时选择 Y | `DELETE /v1/devices/me/data`，在 `revoke_device` 之前执行 |
| 删除本地数据 | `卸载.cmd` / `ctr purge`（P2 已有） | 删除会话文件；对应上传游标随之失效 |
| 卸载 | `卸载.cmd` | 先暂停同步；若设备已注册，询问是否撤销并删除后台数据，再询问是否删除本地数据 |

停止本地记录（`停止记录.cmd`）后没有新内容可传；重新开始记录时，如果同步仍开启，确认提示中写明"新内容会同步到后台"。

## 7. 敏感信息

在记录器已有规则上新增（同时作用于本地记录与上传前二次脱敏）：
- **CDK**：键名（去掉大小写、`_`、`-`、空格后）含 `cdk`、`redeemcode`、`activationcode`，或含 `兑换码`、`激活码`、`卡密`、`充值码` 的字符串值；文本中 `CDK|CDKey|redeem code|activation code|兑换码|激活码|卡密|充值码`（可带"码"）之后的值，允许"是/为"、`：:=`、引号与括号（`"` `'` `“` `「` `『` `（`）组合，例如 `兑换码为："X"`、`{"cdk":"X"}`、`--cdk "X"`、`CDK码：「X」`；没有这些标记时，值须像码（大写字母数字且含数字或连字符，或字母与数字混合且至少 6 位）；`/充值` 命令整条替换（P1 已有）；形如 `XXXX-XXXX-XXXX[-XXXX…]`（3–6 组、每组 4–6 位大写字母数字、同时含字母和数字）的码。已是 `[REDACTED…]` 的值不重复计数。
- **session / 令牌**：`sess-…`（ChatGPT 会话密钥）；充值绑定令牌 `cdkb_…`；本插件设备令牌 `ctrd_…`；键名 `sessionid`、`session_cookie`；Cookie / Authorization 头、JWT、`*_token` 键（P2 已有）。`session_id`（Codex 会话 ID）不是凭证，保留用于关联。
- 无法识别：没有任何上下文的裸 CDK（格式由后台决定）。说明中提示用户只在充值窗口输入 CDK。
- 设备令牌只在 `device.bin`（DPAPI）与请求头中出现；日志、状态、异常信息都不含令牌和后台地址以外的内容。

## 8. 同步授权说明（`ctr sync enable` 显示）

```
================ Codex Task Recorder · 数据同步授权 ================
开启后，本机记录的 Codex 任务内容会在后台持续上传到：{base_url}
只上传开启同步之后产生的内容，包括：
  · 你提交的完整 Prompt；模型公开回复与中间消息
  · 模型名称、配置与切换；工具调用、执行结果与错误
  · 公开推理摘要（模型提供时）；任务开始 / 结束 / 中断 / 错误
  · 会话 / 回合 / 工具 ID、时间戳、Token 用量
  · 工作目录、命令与输出中出现的本机路径和文件内容
  · Codex App 在后台自动运行的任务（标记为 background）
不上传：
  · 密码、API Key、令牌、Cookie、私钥、CDK / 兑换码（自动脱敏，基于规则）
  · 凭证文件（auth.json、.env 等）的内容；加密推理与原始推理；系统指令全文
  · 额度与账号信息；记录器诊断信息（本机环境变量、进程链）
身份：后台只看到一个随机设备 ID，不关联你的 ChatGPT 账号。
与充值无关：充值插件不读取这些数据；不开启同步也能正常充值。
暂停：双击「暂停同步.cmd」。查看：双击「同步状态.cmd」。
撤销：双击「撤销同步.cmd」：停止上传、丢弃尚未上传的内容，并可要求后台删除已上传数据。
提示：脱敏基于规则，无法保证识别所有敏感信息，请勿在会话中输入真实密码、CDK 等机密。
====================================================================
```

`plugin.json` 的 `description` / `longDescription` 同步改写：默认只在本地记录；数据同步需单独开启并同意。

## 9. CLI 与双击入口

| 命令 | 双击入口 | 作用 |
|---|---|---|
| `ctr sync enable [--i-consent] [--base-url URL] [--skip-install]` | `开启同步.cmd` | 第 6.1 节；`--skip-install` 供开发测试跳过插件刷新与信任检查 |
| `ctr sync pause` | `暂停同步.cmd` | 暂停 |
| `ctr sync status [--offline]` | `同步状态.cmd` | 开启状态、后台地址、设备 ID、待上传条数（按会话）、已上传 / 跳过 / 拒收、最近成功与失败、上传进程是否运行、后台记录数（`GET /v1/devices/me`，可达时；`--offline` 不查询）；上传进程未运行且有工作时顺便拉起 |
| `ctr sync revoke [--delete-remote \| --keep-remote] [--yes]` | `撤销同步.cmd` | 撤销，可删除后台数据 |
| `ctr sync config --base-url URL` | — | 设置后台地址 |
| `ctr sync run [--once]` | — | 前台运行上传进程（调试 / 测试）；`--once` 传完即退出 |

`ctr status`（`记录状态.cmd`）增加一行同步状态；`ctr uninstall --revoke-sync` 供脚本化卸载时撤销并删除后台数据。

## 10. 性能
- Hook 新增开销：读 `sync\state.json`（< 1 ms）、标注字段、尝试锁 `worker.lock`（< 1 ms）；拉起上传进程（约 20–50 ms）只在其未运行时发生，且 30 秒内最多一次。实测记录到测试报告。
- 上传进程：空闲时每 2 秒对会话文件做一次 `stat`；`pythonw` 无窗口。

## 11. 模拟后台 `scripts/mock_sync_server.py`
默认端口 18788，数据在内存中（可选 `--store <jsonl>` 落盘）。实现 `docs/sync-api.md` 全部接口与幂等；控制接口：
- `POST /_mock/fail-next {"status": 503, "count": 1}`：接下来 N 次请求返回指定状态；
- `POST /_mock/fail-next {"mode": "drop_after_store"}`：存下批次后直接断开连接（模拟"后台已收、响应丢失"）；
- `POST /_mock/revoke-all`：停用所有设备（403）；
- `GET /_mock/events`、`GET /_mock/stats`：查看收到的事件（测试与验收用）。

## 12. 测试与验收

### 12.1 单元 / 集成测试（unittest；模拟后台进程内启动）
- `seq` 连续递增；`model` 标注（Hook、transcript 切换模型前后、派生）。
- `turn_coverage`：完成 / 失败 / 中断回合，后台会话；每回合一次。
- 脱敏：CDK、`sess-`、`cdkb_`、`ctrd_`、关键字形式；`session_id` 保留。
- 同步状态：开启 / 暂停 / 恢复 / 撤销、授权时段判定、损坏文件失败即关闭。
- outbox：授权过滤（时段外、开启前历史、v1 事件）、精简与二次脱敏、批量上限、部分行、文件删除 / 变短。
- api：成功、重复、逐条拒收、413、400、401、403、429、5xx、超时、拒绝连接、重定向不跟随。
- worker：注册、上传、退避、`drop_after_store` 后重发只得到 duplicate、待执行操作顺序、空闲退出、单实例。
- launcher：已运行不拉起、限流、未开启不拉起。
- 独立性：`ctr` 不导入 `cdkr`；没有充值配置与绑定时同步照常工作。

### 12.2 验收对照
| # | 验收 | 证据 |
|---|---|---|
| 1 | 开启同步，完成一轮 Codex 任务，后台收到对应事件 | 实测：App 内核 app-server 跑一轮带工具调用的任务 → 模拟后台按 turn_id 收到 user_prompt / assistant_message / tool_call / tool_result / turn_end / token_usage / turn_coverage |
| 2 | 关闭充值窗口后同步仍工作 | 实测在充值窗口未打开（并卸载充值插件）的情况下完成验收 1；单元测试检查无依赖 |
| 3 | 模型切换、工具调用正确关联任务 | 实测：同一会话切换模型的两个回合，事件 `model` 与 `model_changed` 正确，tool_call / tool_result 的 tool_use_id、turn_id 一致；单元测试同 |
| 4 | 关闭同步后不再采集新内容 | 实测 + 单元：暂停后跑一轮 → 后台无新事件；本地记录照常 |
| 5 | 断网后恢复上传，无重复 | 单元：`fail-next`、`drop_after_store`、后台停机重启；实测：停掉模拟后台跑一轮 → 重启 → 后台事件数 = 本地授权内事件数，event_id 无重复 |
| 6 | 未捕获或不支持的内容明确标记缺失 | `turn_coverage` 与事件 `completeness`；批次 `capabilities` |

### 12.3 产物
`docs/sync-api.md`、`docs/sync-test-report.md`（单元测试输出 + 实测结果 + Hook 耗时）、`docs/sync-manual-test.md`（桌面 App 手测清单）、README 更新。

## 13. 风险
| 风险 | 应对 |
|---|---|
| 插件缓存仍是旧代码（无 `seq`） | `sync enable` 自动 `ctr install`；旧格式事件跳过并在状态页提示 |
| 上传进程被安全软件拦截 / 崩溃循环 | 拉起限流 30 秒一次；`sync.log` 与状态页显示最近错误 |
| 正文含未识别的敏感信息 | 规则 + 上传前二次脱敏；同意说明中如实提示 |
| 后台未实现 / 接口变动 | 接口约定版本化（`/v1`）；模拟后台与约定同步维护 |
| 401 后重新注册导致设备 ID 变化 | 记入日志与状态；后台可按旧设备 ID 关联（不在本期范围） |
| Codex 关闭且长时间断网 | 上传进程连续失败 60 分钟后退出；下次使用 Codex 或运行 `同步状态.cmd` 时继续 |
