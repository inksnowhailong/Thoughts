---
name: tvs-team-spawn
description: 初始化当前项目的多 Agent 团队协作系统。通过对话询问 sub 数量和目标，从 19 个角色中推荐配比，生成 leader/sub skills、maildir 邮箱、黑板、stop hook 和 per-agent 记忆目录。Use when the user wants team mode, multi-agent collaboration, leader/sub chats, mailbox communication, or worktree-backed agent teams.
---

# Team Onboarding

你正在为当前项目一次性安装多 Agent 团队协作系统。你是装配工，不是 leader；装完之后，日常编排交给生成出来的 leader skill。

## Runtime

统一使用：

```bash
node .cursor/runtime/team.mjs <command> [args...]
```

所有脚本都是 Node.js，适配 Windows / macOS / Linux。`mailbox-watch` 优先使用全局 `chokidar`，没有时回退到 `fs.watch + poll`。

## 执行流程

### 0a. 资产安装（首次必须，重复跑安全）

如果当前项目还没有 `.cursor/runtime/team.mjs`，先用全局 runtime 把全套资产复制到当前项目。这一步幂等：已经装过的文件会自动跳过，不会破坏现有改动。

Windows PowerShell：

```powershell
node "$env:USERPROFILE\.cursor\runtime\team.mjs" install-assets .
```

macOS / Linux：

```bash
node "$HOME/.cursor/runtime/team.mjs" install-assets .
```

会复制：

- `.cursor/runtime/team.mjs` + `team-roles.json`
- `.cursor/hooks/team-stop-driver.mjs`
- `.cursor/schemas/team-{message,config}.schema.json` + `agent-memory.schema.json`
- 创建或合并 `.cursor/hooks.json`，把 team stop hook 追加到 `hooks.stop`

如果你看到输出里的 `missingSources` 不为空，说明你的全局 runtime 安装不完整，让用户检查 `~/.cursor/{runtime,hooks,schemas}/` 三个目录的资产是否齐全。

**装完之后，后续所有命令统一用项目相对路径** `node .cursor/runtime/team.mjs ...`，不要继续用全局绝对路径。这样生成的 leader/sub skill 也能跟随项目可移植。

### 0. 依赖检查

先运行：

```bash
node .cursor/runtime/team.mjs check-deps
```

如果 `chokidar.available` 为 false，询问用户是否现在全局安装。用户同意后运行：

```bash
node .cursor/runtime/team.mjs install-deps
```

安装失败不阻塞流程，但要说明 watcher 会回退到 `fs.watch + poll`。

### 1. 访谈

只问必要问题：

1. 要几个 sub？常见 3-5 个；超过 7 个提醒协调成本很高。
2. 这支团队主要要干什么？让用户用自然语言描述目标（这段话之后会写入黑板的 shared-context）。
3. 是否有明确想要的角色？没有则由你推荐。
4. 团队叫什么名字？**必须是短、可读、由字母数字和连字符组成的标识**（例：`refactor-store`、`mvp-fullstack`、`auth-redesign`）。
   - 不接受时间戳默认值，因为生成的 leader skill 名是 `team-leader-<teamName>`，用户重开 chat 需要手动输入这个名字。
   - 长度建议 4-24 字符。如果用户给出的名字太长 / 含空格 / 含中文，建议一个等价短名让用户确认。
5. **leader 在这个团队里具体要干什么？** 用户用自然语言描述，例如：
   - "重点把控架构边界，不要纠结实现细节"
   - "严管 Critic 链，每条代码改动都得过审"
   - "放手让 sub 自己干，只在拍板和合并时介入"
   - "默认每个 sub 各自一个 worktree，避免冲突"
   - "不要在用户面前暴露任何内部机制，全程拟人化对话"

   这一题如果用户回答"按默认就行"，跳过即可——`generate-leader` 会用默认职能段填充。

每轮最多 1-2 个问题。能用结构化提问工具就用；没有就普通聊天问。

把第 2 题用户的回答**完整保存**到 `<workspace>/.cursor/.team/.onboarding-purpose.txt`，把第 5 题（如果回答了）保存到 `<workspace>/.cursor/.team/.onboarding-leader-profile.md`。后面阶段 5 写黑板和阶段 6 生成 leader 时要用。

### 2. 角色推荐

先运行：

```bash
node .cursor/runtime/team.mjs list-roles
```

19 个核心角色包括：

- `architect`：架构师
- `executor`：实现者
- `explore`：代码勘察
- `document-specialist`：文档研究员
- `designer`：前端设计
- `writer`：撰写
- `vision`：视觉理解
- `planner`：战略规划
- `critic`：毒舌审查
- `analyst`：前期分析
- `qa-tester`：测试
- `tracer`：追踪
- `security-reviewer`：安全审查
- `debugger`：调试
- `test-engineer`：TDD 测试工程师
- `code-reviewer`：代码审查
- `scientist`：数据科学
- `git-master`：Git 操作
- `code-simplifier`：代码简化

推荐规则：

- 新功能：`planner + executor + critic + test-engineer`
- 大型重构：`architect + executor + code-reviewer + git-master`
- Bug 排查：`tracer + debugger + critic`
- 安全敏感：`security-reviewer + code-reviewer + critic`
- 前端：`designer + executor + critic`
- 文档：`document-specialist + writer + critic`
- 数据：`scientist + writer + critic`

至少包含一个审查角色：`critic` / `code-reviewer` / `security-reviewer`。

把推荐配比给用户确认；用户确认前不要写文件。

### 3. 初始化目录

用户确认后：

```bash
node .cursor/runtime/team.mjs ensure-team . --team-name "<teamName>" --leader-name leader
```

这会生成：

```text
.cursor/.team/
├── config.json
├── inbox/
├── blackboard/
├── memory/
├── worktrees/
└── watchers/
```

### 4. 添加成员

每个 sub 执行：

```bash
node .cursor/runtime/team.mjs add-member . <subName> <roleId>
```

默认命名：`sub-<role>`。同角色多个时用 `sub-<role>-1`、`sub-<role>-2`。

### 5. 写黑板初始内容

成员添加完之后，先把团队介绍、角色信息、系统共识和第一条决策写入黑板。这样生成的 leader/sub skill 启动协议里读黑板时立刻有团队共识可用，重开 chat 也能恢复角色。

调用：

```bash
node .cursor/runtime/team.mjs seed-blackboard . --purpose-file "<workspace>/.cursor/.team/.onboarding-purpose.txt"
```

`--purpose-file` 指向阶段 1 第 2 题保存的用户原话。

可选追加用户在访谈中提到的项目特殊约定（命名、风格、不能动的边界等）：

```bash
node .cursor/runtime/team.mjs seed-blackboard . --purpose-file <purpose> --conventions-extra-file <project-conventions>
```

`seed-blackboard` 会写入：

- `.cursor/.team/blackboard/shared-context.md`：团队名、目标、成员清单（含每个成员的角色和 model）、当前阶段占位。
- `.cursor/.team/blackboard/conventions.md`：通信约定、worktree 规则、记忆约定、对用户语气，可选 + 项目特殊约定。
- `.cursor/.team/blackboard/decisions.jsonl` 追加一条 `d-team-formed-<teamName>` 决策记录。

写完之后用 Read 工具读一遍这三份产物给用户**简短确认**（不要全文复述）；用户说有需要补的，重跑 `seed-blackboard --force`。

写完后**删除**临时文件 `.onboarding-purpose.txt`，它不属于运行态。

### 6. 生成 skills

生成 leader：

```bash
node .cursor/runtime/team.mjs generate-leader . --profile-md-file "<workspace>/.cursor/.team/.onboarding-leader-profile.md"
```

`--profile-md-file` 用阶段 1 第 5 题保存的 leader 职能描述。文件不存在或第 5 题用户跳过时，去掉这个参数直接跑：

```bash
node .cursor/runtime/team.mjs generate-leader .
```

会用默认职能段填充。返回值里的 `withProfile: true|false` 告诉你是否注入了用户自定义职能。

生成每个 sub：

```bash
node .cursor/runtime/team.mjs generate-sub . <subName>
```

写完后**删除**临时文件 `.onboarding-leader-profile.md`，它不属于运行态（同阶段 5 删 `.onboarding-purpose.txt`）。

生成的 leader skill 固化：

- 启动协议（强制 Shell 调用 bind / watcher-claim / mailbox-consume，再读 profile/personality/memory-active/memory-consolidated + 黑板三件套）
- **本团队的 Leader 职能段**（来自阶段 1 第 5 题，或注入默认）
- 主循环
- 派任务消息格式
- Critic / Review 链
- 黑板写入规则
- worktree 管理
- 隐藏内部机制规则
- 启动 watcher 后停止说话

生成的 sub skill 固化：

- 角色专属 prompt
- 启动协议（同 leader，强制 Shell 调用 + 完整身份恢复 + 读黑板）
- 只消费自己的 inbox
- 只读黑板
- 在 assigned worktree 工作
- 写回执格式
- 记忆写入规则
- 启动 watcher 后停止说话

### 7. 确认 stop hook

项目中应存在：

```text
.cursor/hooks/team-stop-driver.mjs
.cursor/hooks.json
```

如果不存在，停止并说明当前安装不完整。不要自己临时拼别的 hook。

调试时可设置环境变量 `THOUGHTS_TEAM_DEBUG=1`，stop hook 会把每次触发的决策事件写到 `.cursor/.team/logs/stop.log`，帮你定位"为什么 chat 没被叫醒"这类静默失败。

### 8. 更新 .gitignore

把本地运行态排除：

```text
.cursor/.team/
```

如果已经存在则不要重复追加。

### 9. 收尾

告诉用户：

1. 在新 chat 运行 `/team-leader-<teamName>` 启动 leader。
2. 在 leader chat 先运行 `/tvs-mind-seed leader` 初始化 leader 记忆。
3. 为每个 sub 开独立 chat，运行对应的 `/sub-...` skill。
4. 每个 sub chat 先运行 `/tvs-mind-seed <subName>` 初始化自己的记忆。
5. 回到 leader chat 布置第一个任务。

特别强调（写给用户的提醒）：

- 每个 chat 关闭后重新打开同一个 chat **会变成新的 conversation_id**。重新输入对应 skill（例如 `/sub-architect`）即可触发启动协议恢复角色——`bind` 会自动用新的 conversation_id 替换旧的，记忆和黑板会把角色重新装回来。
- 如果不小心多个 chat 都 bind 同一个 agent，最近的一个会赢，旧的不会再被 stop hook 唤醒，不会丢消息但要避免混淆。
- 不要主动启动任何 chat，也不要替用户运行生成出来的 leader/sub。

## 重要约束

- 不要覆盖已有 `.cursor/.team/config.json`；如果已存在，先读状态并询问用户是追加成员、重生成 skill、还是重建。
- 不要直接编辑 plan 文件。
- 不要把 `.cursor/.team/` 提交到 git。
- 不要生成审计日志；邮箱消费即删除。
- worktree 只由 leader 在用户指导下按需创建。
