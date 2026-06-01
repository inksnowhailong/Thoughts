# 思绪运行时（thoughts-runtime）

平台中立的"思绪"AI 伙伴运行时。**不依赖任何特定 AI 宿主**：循环靠自己的常驻进程驱动，模型调用通过可插拔后端（Claude / Cursor / 直连 API）。跨 Windows / macOS / Linux。

## 设计：大脑与外壳分离

```
core/      平台中立大脑：记忆、决策(该不该说/间隔)、环境感知、通知
backends/  AgentBackend 适配层：claude -p / cursor-agent -p / 直连 API
daemon/    常驻循环：主动循环 + 潜意识循环，setTimeout 自持心跳
install/   开机自启：Windows 任务计划 / macOS launchd / Linux systemd
cli.mjs    命令入口
```

两个核心能力：

- **自我继续对话**：daemon 的"主动循环"自持定时器，到点由 `core/decide.mjs` 判断该不该主动说话、下次隔多久（动态调频），完全不依赖宿主的 hook/cron。
- **子 agent 潜意识**：daemon 的"潜意识循环"定时拉起后端整理记忆、演化画像。`agentic` 后端（claude/cursor）自行读写记忆文件；无工具的 `api` 后端由 daemon 喂数据并落盘。

## 快速开始

```bash
# 1. 查看可用后端与实例
node runtime/cli.mjs status

# 2. 手动跑一次主动行为（测试，会真实调用模型）
node runtime/cli.mjs once 小思 --backend=auto

# 3. 前台启动常驻 daemon
node runtime/cli.mjs start 小思 --backend=auto

# 4. 开机自启
#   Windows:
powershell -ExecutionPolicy Bypass -File runtime/install/install-win.ps1 -Instance 小思
#   macOS:
bash runtime/install/install-mac.sh 小思
#   Linux:
bash runtime/install/install-linux.sh 小思
```

## 后端选择

`--backend` 取值：`auto`（默认，按 claude → cursor → api 优先级探测）、`claude`、`cursor`、`api`。

- `claude` / `cursor`：需安装对应 CLI，自带文件工具，潜意识可自行读写记忆。
- `api`：设置 `ANTHROPIC_API_KEY`（可选 `THOUGHTS_API_MODEL`），完全脱离 AI 宿主，但潜意识由 daemon 代理文件读写。

## 数据目录

所有实例数据在 `~/.thoughts/`（跨 OS）：

```
~/.thoughts/
├── active.json              项目路径 → 实例名 映射（供宿主 hook 复用）
├── daemon.json              运行中的 daemon 状态（PID）
├── logs/                    daemon 日志
└── instances/<实例>/
    ├── profile.json         用户画像
    ├── personality.json     人格设定
    ├── memory-raw.md        原始记忆（主意识追加）
    ├── memory-consolidated.md 长期记忆（潜意识维护）
    ├── permissions.json     环境感知权限
    ├── loop-state.json      循环状态 + 动态间隔
    └── activity-log.jsonl   行为日志
```

> 注：`profile.json` / `personality.json` / `permissions.json` 由 onboarding 流程生成（见 `thoughts-plugin` 的 onboarding skill）。daemon 启动时会自动补齐 memory / loop-state 等运行文件。
