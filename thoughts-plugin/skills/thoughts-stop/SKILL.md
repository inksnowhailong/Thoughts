---
name: thoughts-stop
description: 退出思绪模式 — 删除该实例的主动/潜意识 Cron 并解绑项目
disable-model-invocation: true
---

# 退出思绪模式

**路径约定**：`RUNTIME` 指 `~/.thoughts/runtime/cli.mjs`（Windows 为 `%USERPROFILE%\.thoughts\runtime\cli.mjs`）。

## 步骤

### 1. 确定实例
取项目路径：`git rev-parse --show-toplevel`（失败则用当前目录），记为 `$PROJECT_PATH`。
读 `~/.thoughts/active.json`，查 `$PROJECT_PATH` 对应实例，记为 `$INSTANCE`。
找不到映射时，用 AskUserQuestion 让用户选要停哪个。

### 2. 删除主动循环 Cron
用 **CronList** 列出本会话的 Cron，找到思绪的两个（prompt 以「[思绪·主动循环]」「[思绪·潜意识]」开头），逐个用 **CronDelete** 删除。
> 兼容旧版：若 `node "<RUNTIME>" status` 显示该实例还有常驻 daemon 在跑，额外运行 `node "<RUNTIME>" stop $INSTANCE` 收尾。

### 3. 解绑项目
读 `~/.thoughts/active.json`，删除 `$PROJECT_PATH` 条目，用 Write 写回。

### 4. 告别
读 `~/.thoughts/instances/<实例>/personality.json`，以人格口吻向用户告别（带颜文字）。告诉用户：
- 思绪已关闭（显示实例名），主动循环已停
- 画像与记忆都保留在 `~/.thoughts/instances/<实例>/`，下次 `/thoughts` 可继续
