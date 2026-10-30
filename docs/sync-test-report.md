# 数据同步（P3）测试结果与遗留问题

- 日期：2026-10-28；Codex 桌面 App 26.1013.4894.0，内核 `0.163.0-alpha.10.2`；Windows Server 2025；Python 3.12.10
- 后台：本机模拟后台（`scripts/mock_sync_server.py`）。**真实后台不存在**，接口约定见 `docs/sync-api.md`。
- 设计：`docs/superpowers/specs/2026-10-28-data-sync-design.md`

## 1. 单元 / 集成测试

```
set PYTHONPATH=plugins\codex-task-recorder\lib;plugins\codex-cdk-recharge\lib;tests
python -m unittest discover -s tests -t tests
Ran 296 tests … OK      （整仓，含充值插件；P3 新增 133 个）
```

| 文件 | 数量 | 覆盖 |
|---|---|---|
| test_record_v2 | 12 | `ctr.event/v2`：`seq` 按写入顺序连续（跨 Hook 与 flush）；Hook 事件带 Hook 模型；SessionEnd 无模型时用最近的 Hook 模型；transcript 事件带所在回合的模型，切换前后各自正确；`turn_coverage`：完成回合逐项 captured、缺失项点名（推理摘要为空 / 没有推理条目 → unavailable，无 token → not_captured，无工具 → none）、失败与中断回合、每回合只发一次、后台会话（无 transcript）、统计用完即删 |
| test_redact（新增 8） | 8 | 关键字后的 CDK（`兑换码：` `cdk=` `CDK:` `redeem code` `卡密` `激活码X` `充值码为`）；**引号、JSON、中文组合**（`cdk="X"`、`--cdk "X"`、`{"cdk":"X"}`、`兑换码为：X`、`CDK码：X`、`CDK：「X」`、`兑换码是“X”`、`兑换码 abcd1234efgh`、`cdkey=X`）；任意写法的键名（`cdkCode` `userCdk` `cdkey` `activationCode` `redeem-code`）；分组码；`sess-` 会话密钥；`cdkb_` / `ctrd_` 令牌；已脱敏的值不重复计数；**不误伤**："the CDK window"、"CDK 充值插件"、"CDK API"、大写 UUID、`session_id` |
| test_sync_state | 13 | 未开启 / 开启 / 暂停 / 恢复（保留原同意时间）/ 撤销（清空同意与时段、排队后台操作）/ 完成操作；**换后台需重新同意并丢弃旧时段**；**写入方遇到读不了的状态文件时报错而不是覆盖**；损坏文件失败即关闭；同意版本升级需重新同意；配置（含 BOM）；DPAPI；日志只收固定标识 |
| test_sync_api | 11 | 对模拟后台：注册得到 `ctrd_` 令牌并带客户端头；按 event_id 幂等；逐条拒收；401 / 403；503 / 429 + Retry-After；存下后断开连接 → Unknown，重发得到 duplicate；不跟随重定向；413；删除数据后撤销设备；连接被拒 → NotSent；非 https 地址拒绝 |
| test_sync_outbox | 21 | 只上传授权时段内记录的内容（开启前、暂停期间不上传；开启前产生、开启后才读到的 transcript 内容也不上传）；暂停 / 撤销时不组批、不动游标；v1 旧格式跳过；批量上限且 seq 连续；超大单条单独成批；未提交的批次重建相同；半行等待；上传前去掉诊断信息并再次脱敏；拒收计数；被删文件的游标清理；**删除后重新记录、长度已超过旧偏移的同名文件从头重读**；**没有 / 非字符串 event_id 的行跳过而不卡住队列**；**不读取记录器游标**；**已脱敏的值不计入 upload_pass**；**重新同意时从文件末尾开始，历史不再被反复读取**；不跳进半行；待上传计数 |
| test_sync_worker | 27 | 注册并按顺序上传；只传新增；响应丢失后重发被去重；503 指数退避；**Retry-After 限制在 2–300 s**；413 减半；只有 `400 invalid_batch` 才二分并跳过；**注册被拒（400）不丢任何事件**；**非批次原因的 400（代理错误页）退避重试**；**组批期间用户撤销 → 不注册、不上传**；回复不完整不前移游标；后台忘记设备 → 重新注册；后台停用设备 → 关闭同步；**配置被手工改成别的后台 → 不上传**；暂停时不上传；未配置；单实例；空闲退出；**退避等待中暂停 → 2 秒内退出**；连续失败 1 小时退出；撤销：先删后撤、保留数据选项、离线排队、未注册直接完成、**永久 4xx 不会永远卡住**、5xx 保留重试；后台停机后重启无重复 |
| test_sync_hook | 13 | 同步提示每会话一次、中途开启再提示一次、后台会话无提示、暂停后回到本地提示；Hook 拉起上传进程（`-m ctr.sync`、同一数据目录、**工作目录为系统临时目录**）；30 秒限流；已在运行不重复拉起；未开启不拉起；有待执行撤销时拉起；拉起失败不影响 Hook；检查一次 < 5 ms；记录器不导入充值插件、同步代码不读取充值文件 |
| test_sync_cli | 19 | 未配置 / 非 https 拒绝；同意说明后开启并拉起上传进程；拒绝则不开启、**也不保存后台地址**；未同意本地记录时两次同意；暂停后恢复只需一行确认；**已注册设备时拒绝换后台**；**未注册时换后台 → 暂停并需对新后台重新同意**；状态显示设备 ID、后台已收到条数且不显示令牌；`记录状态` 含同步一行；撤销并删除；交互撤销保留后台数据；离线撤销排队；**上传进程忙时 CLI 不并发发送撤销请求**；**上传进程代为执行撤销时 CLI 如实报告结果**；**后台拒绝删除时如实报告"未能删除"**；卸载时撤销并删除 |
| test_store_replace | 2 | 确认 Windows 上有读者打开目标文件时 `os.replace` 失败（WinError 5）；`store.replace` 等读者关闭后成功 |
| test_sync_acceptance | 7 | 第 3 节的 6 条验收端到端（Hook 载荷 + transcript → 会话文件 → 上传进程 → 模拟后台）+ 隐私：CDK、API Key、记录器诊断信息不出现在后台 |

## 2. 代码审查与修复

独立审查（只读，逐个读了 sync 模块并做了 4 个实验）结论为"修复后可合并"，共 15 条，全部处理：

| # | 级别 | 问题 | 修复 |
|---|---|---|---|
| 1 | 严重 | 注册请求与上传在同一个异常分支里：注册返回 400 / 413（或代理的 400 错误页）会被当成事件有问题，几秒内把整个队列逐条判为拒收 | 注册单独处理、只退避；只有 `400 invalid_batch` 和 413 才拆批 |
| 2 | 重要 | CLI 撤销不与上传进程协调：可能在撤销后用新注册的设备把数据又传上去 | CLI 只在持有上传进程锁时发送撤销；上传进程在注册 / 上传前重读状态 |
| 3 | 重要 | Windows 上读者打开文件时 `os.replace` 失败；上传进程每 2 秒读记录器游标，Hook 保存游标失败会造成 seq 重复、transcript 重复读入（新 event_id，后台无法去重） | 所有替换写入改用带重试的 `store.replace`；上传进程改为列出会话文件，不再读记录器游标 |
| 4 | 重要 | 上传进程工作目录在插件缓存中，卸载 / 刷新插件时目录删不掉 | 工作目录改为系统临时目录；等待改为 2 秒一步、停用后立即退出；卸载前等上传进程退出 |
| 5 | 重要 | CDK 规则漏掉引号、JSON、中文组合写法和驼峰键名 | 规则扩展（见设计 §7），新增测试 |
| 6–14 | 次要 | 永久 4xx 卡住撤销；Retry-After 不设上限；不校验文件身份；缺 event_id 的行卡住队列；状态读失败时被空白覆盖；upload_pass 重复计数；同意未绑定后台地址；撤销结果报告不准确；开启后反复扫描历史 | 逐条修复并加测试（见第 1 节加粗项） |
| 15 | 次要 | 第一次实测结果早于"没有推理条目 → unavailable"的修正 | 第二次实测已验证（第 3 节） |

## 3. 实测（App 内核 app-server + 模拟后台）

`python scripts\live_sync_test.py --alt-model gpt-5.6-luna`。Hook 由 Codex 内核真实触发，上传进程由 Hook 拉起（pythonw，独立进程）。共跑两次：第 1 次（22:33，`.testdata/live-sync-run1/`）在审查前，第 2 次（23:06，`.testdata/live-sync/`）用审查修复后的最终代码。下表为第 2 次结果，两次结论一致。

| 场景 | 结果 | 证据 |
|---|---|---|
| 刷新插件后 Hook 信任 | ✅ | `ctr install`（同版本原地更新）后 12 个 Hook 仍为 trusted |
| A1 一轮带命令的任务 | ✅ | 后台收到 user_prompt（Hook + transcript）、tool_call、tool_result、reasoning_summary、assistant_message、token_usage、turn_start / turn_end、turn_coverage（`completed`，无缺失）。第 1 次实测该回合因服务端 `server_overloaded` 失败，`turn_error` 与 `turn_status = failed` 同样如实上传 |
| A2 本地与后台一致 | ✅ | 会话内授权范围事件：本地 47 条 = 后台 47 条，缺失 0、多余 0；`同步状态` 显示待上传 0、后台已收到 47。开启前的 508 条历史一条未传 |
| B 充值窗口未运行 | ✅ | 充值窗口互斥量不存在（窗口未打开；充值插件已安装、其 Hook 照常运行），上传进程在运行并完成上传 |
| A3 模型切换与工具关联 | ✅ | 回合 1 全部事件 `model = gpt-6-luna`，回合 2 全部为 `gpt-5.6-luna`；`model_changed gpt-6-luna → gpt-5.6-luna`（Hook 派生与 transcript 派生各一条）；Hook 的 `tool_use_id`（`exec-0794…`）与 transcript CommandExecution 的 `item_id` 一致 |
| C 后台停机 | ✅ | 停机期间回合 3 的 14 条留在本地（`last_error = not_sent_refused`）；后台重启并注入"存下后断开连接"后：后台 61 条、去重后 61 条、后台收到重复 14 次（重发被去重）、缺失 0；按到达顺序 seq 递增 |
| D 暂停 | ✅ | 暂停后回合 4：本地有记录，后台 0 条；恢复同步后该回合仍为 0 条（暂停期间内容不补传） |
| E 撤销并删除 | ✅ | 后台设备 0、事件 0；`pending_ops` 为空；本机上传进程退出 |
| 验收 6 | ✅ | 每个回合都有 `turn_coverage`；回合 2、3 模型没有返回推理条目 → `reasoning_summary: unavailable`（"本回合 transcript 中没有推理条目"）；批次带 `capabilities` |

第 2 次实测发现的问题：E 中上传进程在 CLI 等锁期间自己执行了删除与撤销（后台数据确已删除），CLI 随后却显示"后台未能删除数据（None）"。已修复：每个后台操作的结果写入同步状态，无论哪个进程执行，CLI 都报告真实结果。新增单元测试按同样的时序复现（上传进程持锁 → 代为执行 → 释放 → CLI 报告），修复前失败、修复后通过；该修复未再做实测。

### 延迟

| 项目 | 数值 |
|---|---|
| 同步 Hook（UserPromptSubmit）端到端，app-server `hook/completed.durationMs` | 第 1 次 384–445 ms；第 2 次 406–503 ms（503 为第一条，其余 406–416）。P2 基线 355–446 ms，主要是 PowerShell + Python 启动 |
| 记录器自身耗时（含同步状态读取与上传进程检查），`hook_invocation.duration_ms` | 中位 18.3 ms，最大 42.8 ms（10 次；P2 为 13–57 ms） |
| 检查上传进程是否在运行（单元测试） | < 5 ms / 次 |
| 后台收到事件的延迟 | 未单独测量。上传进程运行时每 2 s 检查新内容；实测 `sync.log` 中各批间隔约 6–10 s，随回合进度写入 |

## 4. 验收对照

| # | 验收 | 结论 | 证据 |
|---|---|---|---|
| 1 | 开启同步后完成一轮任务，后台收到对应事件 | ✅ | 实测 A1/A2；test_sync_acceptance 1 |
| 2 | 关闭充值窗口后同步仍工作 | ✅ | 实测 B；同步不依赖充值插件的代码、文件与进程（test_sync_hook 独立性检查） |
| 3 | 模型切换、工具调用正确关联任务 | ✅ | 实测 A3；test_sync_acceptance 3 |
| 4 | 关闭数据同步后不再采集新内容 | ✅ | 实测 D；test_sync_acceptance 4、test_sync_outbox |
| 5 | 网络中断后恢复上传、无重复 | ✅ | 实测 C；test_sync_worker（响应丢失、后台重启）、test_sync_acceptance 5 |
| 6 | 未捕获或不支持的内容明确标记缺失 | ✅ | `turn_coverage` + 事件 `completeness` + 批次 `capabilities`；实测 A、test_sync_acceptance 6 |

## 5. 遗留问题与限制

1. **真实后台未实现**：只验证了接口约定与模拟后台；上线前需按 `docs/sync-api.md` 实现幂等、删除与撤销（注意：整批有问题须返回 `400 invalid_batch`；删除接口暂不可用须返回 5xx 而不是 404）。
2. **桌面 App 手测未做**：清单见 `docs/sync-manual-test.md`（同步提示是否可见、退出 App 后是否继续上传等需要在界面中确认）。app-server 与桌面 App 是同一内核与协议，P2 已验证两者 Hook 行为一致。
3. **裸 CDK 无法识别**：没有关键字、也不是分组格式的 CDK（格式由后台决定）可能随 Prompt 上传；同意说明已提示只在充值窗口输入 CDK，`/充值` 命令整条脱敏。
4. **401 后重新注册会换设备 ID**：后台丢失设备时，之后的数据归到新设备（日志与状态页可见）。
5. **Codex 关闭且长时间断网**：上传进程连续失败 1 小时后退出，下次使用 Codex 或双击「同步状态.cmd」时继续；内容一直留在本地，不会丢。
6. **覆盖统计尽力而为**：回合结束后才处理的 async Hook（极少见）不计入该回合的 `turn_coverage`。
7. **隐私边界**：上传内容包含工作目录、命令与输出中的本机路径和文件内容（同意说明已列出）；脱敏基于规则，"CDK x86_64" 这类字母数字混排的词可能被误判为码而脱敏（宁可多删）。
