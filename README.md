# Cursor Thoughts Skill

Cursor 版"思绪模式"实验项目。

目标是做一个运行在 Cursor IDE 内的主动型 AI 伙伴:

- 通过 onboarding 收集用户画像、兴趣领域、探索领域和主动频率偏好。
- 在一个固定的专用 chat 中进入思绪模式。
- 使用 `sessionStart` hook 注入人格上下文。
- 使用 `stop` hook 的 `followup_message` 驱动持续循环。
- 使用 shell sleep + `next_active_at` 控制动态延迟。
- 使用 `.cursor/agents/thoughts-subconscious.md` 中的 background subagent 作为"潜意识",整理记忆、演化画像、调整节奏。
- 主动内容以"信息发现"为主,避免无意义询问用户当前代码进度。
- 通知按系统自动适配: Windows BurntToast、macOS osascript、Linux notify-send,缺失时降级为终端输出。

## 结构

```text
.cursor/
├── hooks.template.json
├── agents/
│   └── thoughts-subconscious.md
├── hooks/
│   ├── thoughts-before-submit.mjs
│   ├── thoughts-loop-driver.mjs
│   └── thoughts-session-start.mjs
├── runtime/
│   └── thoughts.mjs
└── skills/
    ├── thoughts/
    │   └── SKILL.md
    ├── thoughts-onboarding/
    │   └── SKILL.md
    └── thoughts-stop/
        └── SKILL.md
```

## 使用流程

1. 将 `.cursor/skills/`、`.cursor/agents/`、`.cursor/hooks/`、`.cursor/runtime/` 安装到 `~/.cursor/`。
2. 将 `.cursor/hooks.template.json` 作为 `~/.cursor/hooks.json`。
3. 重启 Cursor 或重新加载窗口,确保全局 hooks 被加载。
4. 在一个普通 chat 中运行 `/thoughts-onboarding`,创建思绪实例。
5. 新开一个干净的专用 chat,运行 `/thoughts` 进入思绪模式。
6. 后续这个 chat 会由 stop hook 驱动主动循环。
7. 用 `/thoughts-stop` 退出思绪模式。

## 运行时数据

所有用户数据都写入本机用户目录,不会进入 Git:

```text
~/.cursor/.thoughts/
├── active.json
├── sessions.json
└── instances/<实例名>/
    ├── profile.json
    ├── personality.json
    ├── memory-raw.md
    ├── memory-consolidated.md
    ├── activity-log.jsonl
    └── loop-state.json
```

## 设计约束

- 这是 Cursor skill/hook 项目,不需要 `package.json`、`node_modules` 或 TypeScript 构建链。
- 脚本使用 Cursor 自带/系统可用的 Node 执行。
- 项目内不保存用户画像、记忆或 API key。
- 当前优先支持 Windows 10/11。
- 长时间等待使用 5 分钟以内 sleep 分片循环,避免单个长 shell 被系统提前终止。

