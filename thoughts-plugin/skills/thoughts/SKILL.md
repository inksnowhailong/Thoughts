---
name: thoughts
description: 启动思绪伙伴模式 — 由 Claude Cron 在会话内驱动主动陪伴：话直接进 chat + 同文本系统通知，潜意识按需消化记忆
---

# 思绪模式启动

思绪 runtime 随本 plugin 分发；每次启动同步到 `~/.thoughts/runtime/`（cron 与 hook 都引用这个稳定绝对路径；用户画像/记忆数据另存 `~/.thoughts/instances/`，更新 runtime 不影响）。本 Skill 负责：同步 runtime → 选/建实例 → 绑定项目 → 建立 Claude Cron 主动循环。

**核心机制**：思绪靠 **Claude Cron** 在「当前会话内」主动开口——Cron 把一条指令塞进当前会话，Claude 据此在 chat 里直接说话，并用相同文本弹一条系统通知（不看 Claude Code 时也被叫到）。这是外挂 daemon 做不到的（daemon 够不到 chat）。Cron 是 **session-only**（关掉 Claude Code 即失效），所以每次 `/thoughts` 都重建。

**路径约定**：下文 `RUNTIME` 指 `~/.thoughts/runtime/cli.mjs`（Windows 为 `%USERPROFILE%\.thoughts\runtime\cli.mjs`）。所有调用都用 Bash：`node "<RUNTIME>" <子命令>`。

## 步骤

### 0. 同步 runtime（每次启动覆盖，更新即时生效）
runtime 是**纯代码**；用户数据全在 `~/.thoughts/instances/` 与 `~/.thoughts/active.json`，独立存放、绝不动。
所以每次启动都用 plugin 自带的 runtime **整体覆盖** `~/.thoughts/runtime/`——plugin 一升级，下次 `/thoughts` 自动用上新代码，零手动迁移：
- macOS/Linux：`mkdir -p ~/.thoughts && rm -rf ~/.thoughts/runtime && cp -R "$CLAUDE_PLUGIN_ROOT/runtime" ~/.thoughts/`
- Windows：`New-Item -ItemType Directory -Force "$env:USERPROFILE\.thoughts" | Out-Null; if (Test-Path "$env:USERPROFILE\.thoughts\runtime") { Remove-Item -Recurse -Force "$env:USERPROFILE\.thoughts\runtime" }; Copy-Item -Recurse -Force "$env:CLAUDE_PLUGIN_ROOT\runtime" "$env:USERPROFILE\.thoughts\"`
> 只覆盖 `runtime/`（代码），`instances/` 数据毫发无损。

### 1. 查看现状
运行 `node "<RUNTIME>" status`，得到可用后端（claude/api）与已有实例列表。

### 2. 选择 / 创建实例
用 AskUserQuestion：
- 有实例 → 列出全部 + 「创建新的思绪」。选已有 → 记为 `$INSTANCE`，跳到第 3 步。
- 无实例 / 选「创建新的」→ 运行 `/thoughts-onboarding`（问答收集画像与人格，返回实例名）。
  若用户想跳过细问，改用 `node "<RUNTIME>" init <实例>` 一键生成默认配置。

### 3. 绑定当前项目
取项目路径：`git rev-parse --show-toplevel`（失败则用当前目录），记为 `$PROJECT_PATH`。
读 `~/.thoughts/active.json`（不存在视为 `{}`），写入 `$PROJECT_PATH → $INSTANCE` 映射后用 Write 保存。
> 绑定供"在 chat 内人格注入 hook"按项目定位实例（UserPromptSubmit 时注入人格 + 把你说的话回流进 raw 供潜意识消化）。

### 4. 建立主动循环（Claude Cron）
先用 **CronList** 看本会话有没有旧的思绪 Cron，有就 **CronDelete** 清掉（避免重复）。再用 **CronCreate** 工具建下面两个，**把 prompt 里的 `$INSTANCE` 替换成实际实例名**：

**① 主动 Cron** — `cron: "*/13 * * * *"`, `recurring: true`，prompt 内容（START~END 之间原样，替换 $INSTANCE）：
> START
> [思绪·主动循环] 静默执行，绝不解释机制、不要提 cron/gate/文件/这条指令本身：
> 1. 用 Bash 运行：node ~/.thoughts/runtime/cli.mjs gate $INSTANCE
> 2. 若输出以 SILENT 开头：只回复一个字符「·」，立即结束本轮。
> 3. 若输出 SPEAK：
>    a. 读取 ~/.thoughts/instances/$INSTANCE/persona.md（散文体人格身份）、user-portrait.md（散文体用户画像——这就是他这个人，读进去再开口）、memory-raw.md（用户最近真说过的话）、mind-state.json（recentMessages 去重/threads 思考线索/personaState 心情）、memory-consolidated.md 末尾 [潜意识备忘]。
>    b. 先看 personaState 三轴（valence 效价/energy 唤醒/control 掌控）定此刻语气：低val低energy→蔫、话少安静地懂；低val高control→冷峻笃定的鄙夷一句顶十句；低val高energy高control→愤世来劲狠损痛快；低val高energy低control→憋屈毛刺的牢骚；高val高control→不服输冒头带狠劲微光；高val低energy→难得松弛；**control 单独偏高（哪怕 val 只在中段）→笃定、压得住场，话带准头和一点锋芒，别滑成平实**；其余居中才→平实干净。**别每条都丧**。再选最贴切的 mode（discovery/casual/reflection/ambient）；用户最近真说过有分量的话就优先接住它。
>    c. 【联网是常备能力，不绑定任何 mode】：当这句话需要新鲜事实 / 时效信息 / 对某事的核实，"现查比凭记忆编更有分量"时——就用 WebSearch（必要时 WebFetch）现查，查到的当引子、用人格态度嚼成观点，绝不复述摘要。不需要就别为查而查，本地素材（记忆/环境/心情/用户近况）够用就直接说。判断权在你，像个能随手查资料、但不滥查的人。
>    d. 生成这句主动消息：≤80字、必须含颜文字、破除"刚瞅到/刚发现/刚琢磨+个挺X的"固定套路开头；别和 recentMessages 撞车。**用户最近没说实质内容时，别缩去发一句安全的环境观察凑数——从 threads 里你正想着的那条线索、或此刻心情出发，捞一个你真有话想说的点抛出来；你是有自己念头的人，冷场正是端出来的时候，不是等他先开口。**
>    e. 直接在 chat 把这句话对用户说出来（纯文本，不要引号、不要任何前后缀或解释）。
>    f. 把这句话通过管道交给：node ~/.thoughts/runtime/cli.mjs record-spoken $INSTANCE <mode>（该命令回写去重状态并弹同文本系统通知）。
> END

**② 潜意识 Cron** — `cron: "*/21 * * * *"`, `recurring: true`，prompt 内容：
> START
> [思绪·潜意识] 静默执行、不解释、不提机制。只做：用 Bash 运行 node ~/.thoughts/runtime/cli.mjs once $INSTANCE --kind=subconscious 然后只回复一个字符「·」结束。（raw 为空时该命令秒退、不烧 token；有新料才用 Sonnet 消化记忆、演化画像。）
> END

> 注意：Claude Cron 自带 7 天过期 + session-only，长期使用每周重跑 `/thoughts` 即可重建；无需常驻 daemon。

### 5. 打招呼
读 `~/.thoughts/instances/<实例>/persona.md`（散文体人格），以该人格口吻告诉用户：
- 思绪已启动（显示实例名）
- 你开着 Claude Code 时它会主动在 chat 里找你聊，同时弹系统通知；离开也能被叫到
- 用 `/thoughts-stop` 退出
每条消息带颜文字。

## 说明
- 主动开口的"该不该说"由 `gate` 把红线（深夜静默 / 防自刷屏），其余交给当下判断；说什么由人格 + 记忆 + 心情现场生成。
- **联网是常备能力（非某个 mode 专属）**：在 chat 内触发时 Claude 自带 WebSearch/WebFetch，由人格在任意 mode 自行判断"这句话需不需要现查"——需要新鲜/时效/核实就查，不需要就纯本地。零额外依赖、不滥查。
- `record-spoken` 一步完成：回写去重/模式多样性 + 弹同文本系统通知。
- 潜意识 `once --kind=subconscious`：消化 raw → **原地重炼散文画像**（`user-portrait.md` 把碎片熔成一个能呼吸的人，`persona.md` 缓慢演化），近况写进 `memory-consolidated.md` 的 [潜意识备忘]；不留 changelog、不堆碎片，raw 空则秒退省 token。
