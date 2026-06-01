---
name: thoughts
description: 启动思绪伙伴模式 — 由平台中立的常驻 daemon 驱动主动陪伴与潜意识分析，跨 Win/Mac/Linux，不依赖 Claude Cron
---

# 思绪模式启动

思绪运行时已全局安装在 `~/.thoughts/runtime/`。本 Skill 负责：选/建实例 → 绑定当前项目 → 拉起常驻 daemon。daemon 一旦启动会自行循环（主动聊天 + 潜意识整理），与本对话无关，关掉对话也继续跑。

**路径约定**：下文 `RUNTIME` 指 `~/.thoughts/runtime/cli.mjs`（Windows 为 `%USERPROFILE%\.thoughts\runtime\cli.mjs`）。所有调用都用 Bash：`node "<RUNTIME>" <子命令>`。

## 步骤

### 1. 查看现状
运行 `node "<RUNTIME>" status`，得到可用后端（claude/cursor/api）与已有实例列表。

### 2. 选择 / 创建实例
用 AskUserQuestion：
- 有实例 → 列出全部 + 「创建新的思绪」。选已有 → 记为 `$INSTANCE`，跳到第 3 步。
- 无实例 / 选「创建新的」→ 运行 `/thoughts-onboarding`（问答收集画像与人格，返回实例名）。
  若用户想跳过细问，改用 `node "<RUNTIME>" init <实例>` 一键生成默认配置。

### 3. 绑定当前项目
取项目路径：`git rev-parse --show-toplevel`（失败则用当前目录），记为 `$PROJECT_PATH`。
读 `~/.thoughts/active.json`（不存在视为 `{}`），写入 `$PROJECT_PATH → $INSTANCE` 映射后用 Write 保存。
> 绑定供可选的"在 chat 内人格注入 hook"按项目定位实例；daemon 本身不依赖它。

### 4. 启动常驻 daemon（后台，不要阻塞对话）
- Windows：`Start-Process -WindowStyle Hidden node -ArgumentList '"<RUNTIME>"','start','<实例>','--backend=auto'`
- macOS/Linux：`nohup node "<RUNTIME>" start <实例> --backend=auto >/dev/null 2>&1 &`

如需开机自启，提示用户可运行 `~/.thoughts/runtime/install/` 下对应平台脚本（install-win.ps1 / install-mac.sh / install-linux.sh）。

### 5. 打招呼
读 `~/.thoughts/instances/<实例>/personality.json`，以该人格口吻告诉用户：
- 思绪已启动（显示实例名）
- 会在后台默默陪着，偶尔主动找ta聊
- 用 `/thoughts-stop` 退出
每条消息带颜文字。

## 说明
- daemon 自持 setTimeout 心跳，**不依赖 Claude Cron**，跨 Win/Mac/Linux。
- 后端 `--backend=auto` 会按 claude → cursor → api 顺序自动探测。
