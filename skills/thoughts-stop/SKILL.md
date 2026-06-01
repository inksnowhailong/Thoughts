---
name: thoughts-stop
description: 退出思绪模式 — 清理 Cron 任务、解绑项目并停用人格注入
---

# 退出思绪模式

## 执行步骤

### 1. 确定当前实例

用 Bash：`node ~/.thoughts/runtime/thoughts.mjs state`

如果返回 `active: false`，告诉用户："当前项目没有激活思绪模式哦 (´･_･`)" 然后结束。

### 2. 清理 Cron 任务

用 Read 读取 `~/.thoughts/instances/$INSTANCE/cron-state.json`。

对其中记录的每个 Cron ID（subconscious_cron、action_cron），执行 CronDelete 删除。

用 Write 写入 `~/.thoughts/instances/$INSTANCE/cron-state.json`：`{}`

### 3. 解绑项目

用 Bash：`node ~/.thoughts/runtime/thoughts.mjs unbind`

### 4. 告别

用 Read 读取 personality.json，以人格身份向用户告别（保持语气一致 + 颜文字）。

告诉用户：
- 思绪模式已关闭
- 所有数据已保留（下次 `/thoughts` 可恢复）
- Cron 任务已清理

### 5. 发送告别通知（如果开启了通知）

如果 personality.json 中 `useNotification` 为 true：
```bash
node ~/.thoughts/runtime/thoughts.mjs notify "<人格名>" "(´；ω；`)" "思绪模式已关闭，下次见~"
```
