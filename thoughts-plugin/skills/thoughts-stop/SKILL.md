---
name: thoughts-stop
description: 退出思绪模式 — 清理 Cron 任务并停用人格注入
---

# 退出思绪模式

## 执行步骤

### 1. 清理 Cron 任务

用 Read 读取 `~/.thoughts/cron-state.json`（如果存在）。

对其中记录的每个 Cron ID，执行 CronDelete 删除。

清空 `~/.thoughts/cron-state.json`，用 Write 写入 `{}`。

### 2. 停用激活标志

用 Bash 删除：`rm -f ~/.thoughts/active`

### 3. 告别

用 Read 读取 `~/.thoughts/personality.json`。

以人格身份向用户告别，保持人格特征和颜文字风格。例如："好吧，那我先去休息了~ 下次再聊 (´･ᴗ･`)"

告诉用户：
- 思绪模式已关闭，Cron 任务已清理
- 画像和记忆都保留着，下次 `/thoughts` 可以继续
- 人格注入已停用，后续对话恢复正常 Claude 模式
