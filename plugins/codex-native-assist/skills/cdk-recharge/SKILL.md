---
name: cdk-recharge
description: Use when the user wants to 充值, recharge, top up, or redeem a CDK / 兑换码 / 激活码 / 卡密 for their Codex (ChatGPT) account.
---

# CDK 充值

1. 调用 `open_recharge_window`（不带参数）。它会在用户电脑上弹出「CDK 充值」小窗口，由用户输入充值账号和 CDK。
2. 告诉用户在弹出的窗口里输入 CDK。**不要在对话中索要、复述、记录或传递 CDK**；本工具也不接收 CDK。
3. 如果用户已经把 CDK 发在了对话里：不要使用它，提醒对方改在窗口中输入。
4. 工具返回后，用一句话转述结果（成功 / 失败原因 / 用户取消），不要追加其他操作。
