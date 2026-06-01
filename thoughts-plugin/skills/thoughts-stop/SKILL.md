---
name: thoughts-stop
description: 退出思绪模式 — 停止该项目绑定实例的常驻 daemon 并解绑
---

# 退出思绪模式

**路径约定**：`RUNTIME` 指 `~/.thoughts/runtime/cli.mjs`（Windows 为 `%USERPROFILE%\.thoughts\runtime\cli.mjs`）。

## 步骤

### 1. 确定实例
取项目路径：`git rev-parse --show-toplevel`（失败则用当前目录），记为 `$PROJECT_PATH`。
读 `~/.thoughts/active.json`，查 `$PROJECT_PATH` 对应实例，记为 `$INSTANCE`。
找不到映射时，运行 `node "<RUNTIME>" status` 列出运行中的实例，用 AskUserQuestion 让用户选要停哪个。

### 2. 停止 daemon
运行 `node "<RUNTIME>" stop <实例>`。

### 3. 解绑项目
读 `~/.thoughts/active.json`，删除 `$PROJECT_PATH` 条目，用 Write 写回。

### 4. 告别
读 `~/.thoughts/instances/<实例>/personality.json`，以人格口吻向用户告别（带颜文字）。告诉用户：
- 思绪已关闭（显示实例名），daemon 已停止
- 画像与记忆都保留在 `~/.thoughts/instances/<实例>/`，下次 `/thoughts` 可继续
