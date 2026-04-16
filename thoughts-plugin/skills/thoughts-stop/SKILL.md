---
name: thoughts-stop
description: 退出思绪模式 — 清理 Cron 任务并停用人格注入
---

# 退出思绪模式

## 执行步骤

### 1. 确定当前实例

用 Bash 获取当前项目路径：`git rev-parse --show-toplevel 2>/dev/null || pwd`，记为 `$PROJECT_PATH`。

用 Read 读取 `~/.thoughts/active.json`，查找 `$PROJECT_PATH` 对应的实例名称，记为 `$INSTANCE`。

如果找不到映射，告诉用户："当前项目没有激活思绪模式哦 (´･_･`)" 然后结束。

`$INSTANCE_DIR` = `~/.thoughts/instances/$INSTANCE`

### 2. 清理 Cron 任务

用 Read 读取 `$INSTANCE_DIR/cron-state.json`（如果存在）。

对其中记录的每个 Cron ID，执行 CronDelete 删除。

清空 `$INSTANCE_DIR/cron-state.json`，用 Write 写入 `{}`。

### 3. 移除项目绑定

用 Read 读取 `~/.thoughts/active.json`，删除 `$PROJECT_PATH` 的条目，用 Write 写回。

如果 `active.json` 变成空对象 `{}`，用 Bash 删除文件：`rm -f ~/.thoughts/active.json`

### 4. 告别

用 Read 读取 `$INSTANCE_DIR/personality.json`。

以人格身份向用户告别，保持人格特征和颜文字风格。例如："好吧，那我先去休息了~ 下次再聊 (´･ᴗ･`)"

告诉用户：
- 思绪模式已关闭（显示实例名称），Cron 任务已清理
- 画像和记忆都保留在 `~/.thoughts/instances/$INSTANCE/` 中，下次 `/thoughts` 可以继续
- 人格注入已停用，后续对话恢复正常 Claude 模式
