# "思绪模式" Claude Code Skill 实现计划 (v2 — 整合审查反馈)

**日期**: 2026-04-14
**复杂度**: MEDIUM-HIGH
**性质**: Prompt Engineering + Shell 脚本，非传统代码开发
**关系**: 本计划替代此前的 Tauri + OpenClaw 方案（user-profile-onboarding.md）

---

## RALPLAN-DR 摘要

### Principles（原则）

1. **Prompt 即架构** — 系统行为由 SKILL.md prompt 驱动，Skill 文件是核心产物
2. **叠加而非替换** — 思绪模式叠加在 Claude 原有能力之上，Hook 注入上下文但不覆盖基础能力
3. **异步松耦合** — 记忆更新和自动行为通过文件系统交换状态，不依赖持续运行的 Agent
4. **渐进式体验** — onboarding → 人格注入 → 记忆系统 → 自动行为，每阶段可独立验证
5. **v1 可预测 > 智能** — 优先选择可预测的机制，不追求"完全自主判断"

### Decision Drivers（决策驱动）

1. **技术可行性** — 基于 Claude Code 实际运行时模型（SKILL 一次性执行、Hook 只能输出 JSON、Cron 需 POC 验证）
2. **链路完整性** — 9 条验收标准必须全部有可行的实现路径
3. **最小依赖** — 只依赖 Claude Code 内置工具 + jq（JSON 处理）

### Viable Options

#### 方案 A+: 双 SKILL + JSON Hook + 混合记忆机制（推荐）

主意识 = Claude + Hook 注入（JSON 格式 systemMessage）。记忆更新采用混合方案：Hook 注入轻量级指令让 Claude 在回复时顺便更新 memory.md（路径 C）+ Cron 定期用 Haiku Agent 做深度画像分析和记忆整理（路径 A）。

- **优点**: 技术可行、无需持续运行的 Agent、利用 Claude 主意识的理解能力做实时记忆
- **缺点**: 实时记忆依赖 prompt 遵循度、Opus token 消耗略增
- **适用**: v1 交付

#### 方案 B: 独立潜意识 Agent 持续运行

每次对话后触发独立 Haiku Agent 分析。

- **否决理由**: SKILL.md 是一次性执行，无法在后续对话中触发 Agent。Hook 是 shell 脚本，只能输出 JSON，不能调用 Agent 工具。**技术上不可行。**

**选择方案 A+**。放弃"独立潜意识 Agent 实时触发"，改为"Claude 自身做轻量记忆 + Cron 做深度分析"的混合方案。

---

## ADR

- **Decision**: 方案 A+ — 混合记忆机制替代独立潜意识 Agent
- **Drivers**: 技术可行性（SKILL 一次性、Hook 只输出 JSON）、链路完整性
- **Alternatives**: 方案 B（独立 Agent 持续运行）— 在 Claude Code 运行时模型下不可行
- **Why chosen**: 混合方案保留了"记忆持续更新"的用户体验，同时回避了不可行的 Agent 持续触发问题
- **Consequences**: 实时记忆质量依赖 prompt 遵循度；深度分析有分钟级延迟
- **Follow-ups**: v2 探索 PostToolUse Hook（如果支持 JS/TS）来触发真正的异步 Agent

---

## 核心架构修正

### 运行时模型澄清

```
SKILL.md:     一次性执行 — 用户输入 /thoughts 时执行一次，然后结束
Hook:         无状态 JSON 发射器 — 每次用户发消息前执行 shell 脚本，
              stdout 输出 {"systemMessage": "..."} 注入为 <system-reminder>
Cron:         独立会话 — 定时触发，在新的 Claude Code 上下文中执行 instructions
Agent:        子进程 — 只能在 SKILL.md 执行期间或 Cron 执行期间被调用
```

### 记忆更新机制（混合路径 C+A）

```
路径 C（实时轻量）:
  用户发消息
  → Hook 注入 systemMessage，包含：
    1. 人格设定 + 用户画像（上下文）
    2. 记忆更新指令："如果用户透露了新的重要信息，在回复末尾用 Write 更新 memory.md"
  → Claude(Opus) 带人格回复 + 视情况更新 memory.md

路径 A（定期深度）:
  Cron 触发（如每 30 分钟）
  → Agent(Haiku) 读取 memory.md + profile.json
  → 整理记忆（精简冗余、提取结构化信息）
  → 更新 profile.json（画像演化）
  → 更新 personality.json（性格微调）
```

### 数据目录拆分（全局 + 项目级）

```
~/.thoughts/                    # 全局（用户身份，跨项目共享）
  profile.json                  # 用户画像
  personality.json              # AI 人格设定
  permissions.json              # 环境扫描权限
  active                        # 激活标志文件（存在=激活）

.thoughts/                      # 项目级（当前项目的记忆和状态）
  memory.md                     # 对话记忆
  cron-state.json               # 当前活跃 Cron ID
  activity-log.jsonl            # 活动日志
```

### Hook 脚本输出格式（JSON 协议）

```bash
#!/bin/bash
# 检查激活标志
[ -f "$HOME/.thoughts/active" ] || exit 0
[ -f "$HOME/.thoughts/personality.json" ] || exit 0

PERSONALITY=$(cat "$HOME/.thoughts/personality.json" 2>/dev/null || echo "{}")
PROFILE=$(cat "$HOME/.thoughts/profile.json" 2>/dev/null || echo "{}")
MEMORY=$(tail -50 ".thoughts/memory.md" 2>/dev/null || echo "暂无记忆")

# 拼接注入内容
INJECT="[思绪模式已激活]
你当前的人格设定：
$PERSONALITY

用户画像：
$PROFILE

近期记忆：
$MEMORY

[行为指令]
- 以上述人格特征回复用户，保持一致的语气和风格，每条回复包含颜文字。
- 如果用户透露了新的重要信息（偏好变化、情绪状态、新兴趣等），在回复完成后用 Write 工具将要点追加到 .thoughts/memory.md（格式：## YYYY-MM-DD 下按条目追加）。
- 不要告诉用户你在更新记忆，自然地进行。"

# 输出 JSON（用 jq 安全转义）
jq -n --arg msg "$INJECT" '{"systemMessage": $msg}'
```

### 思绪模式激活/停用开关

- `/thoughts` 启动时创建 `~/.thoughts/active` 标志文件
- `/thoughts-stop` 删除 `~/.thoughts/active` + 清理 Cron
- Hook 脚本首先检查 `active` 文件是否存在，不存在则 exit 0（不注入）
- 这样 onboarding 完成后如果用户没启动 `/thoughts`，人格不会自动注入

---

## 分阶段实现步骤

### Phase 0: Cron 机制 POC（前置验证）

**目标**: 验证 CronCreate 的执行环境能力，消除技术风险

**验证项**:
1. CronCreate 的 instructions 能否使用 Bash 工具？（创建简单任务：`用 Bash 写入时间戳到 /tmp/thoughts-poc.txt`）
2. CronCreate 的 instructions 能否使用 Read/Write 工具？（读写 .thoughts/ 目录文件）
3. CronCreate 的 instructions 能否使用 WebSearch？
4. CronCreate 的 instructions 能否使用 Agent 工具启动子 agent？
5. 用户不在终端时 Cron 是否正常执行？输出到哪里？
6. 工作目录是什么？能否访问项目目录和 $HOME？

**产物**: POC 结果文档，记录每项验证的结果，指导 Phase 4 设计

**依赖**: 无

---

### Phase 1: 数据层 + Onboarding Skill

**目标**: 画像收集 + 人格生成

**产物文件**:
- **新建** `.claude/skills/thoughts-onboarding/SKILL.md`

**SKILL.md 核心内容**:
- Frontmatter: `name: thoughts-onboarding`, `description: 思绪模式初始化 — 通过自然对话收集用户画像`
- 角色：友好的采访者，正在第一次认识用户
- 对话规则：每次 1-2 问题，3-8 轮结束，自然追问
- 收集方向：昵称、兴趣爱好、职业、年龄段、性格特征、作息习惯、沟通偏好、讨厌的事情
- 结束时：用 Write 创建 `~/.thoughts/profile.json`（全局）
- 人格生成：基于画像随机生成人格设定（名字、性格特征、语气风格、颜文字偏好、口头禅），用 Write 创建 `~/.thoughts/personality.json`
- 创建项目记忆目录：用 Bash `mkdir -p .thoughts`
- 最后输出人格预览，提示用户运行 `/thoughts` 启动

**数据格式约定**:

`~/.thoughts/profile.json`:
```json
{"nickname": "小明", "interests": ["编程", "游戏"], "occupation": "程序员", ...}
```

`~/.thoughts/personality.json`:
```json
{
  "name": "思绪",
  "traits": ["温和", "好奇", "偶尔毒舌"],
  "tone": "轻松友好，偶尔调侃",
  "kaomojiStyle": "简洁干净型",
  "catchphrase": "有意思~",
  "boundaries": "不会主动讨论政治和宗教"
}
```

**验证**: 运行 `/thoughts-onboarding` → 对话 → profile.json + personality.json 被创建

**验收标准对应**: #1, #2

---

### Phase 2: Hook 注入 + 主 Skill 启动协议

**目标**: 人格注入生效 + 主 Skill 可以启动/停止思绪模式

**产物文件**:
- **新建** `.claude/hooks/thoughts-inject.sh` — Hook 脚本（JSON 输出）
- **新建** `.claude/skills/thoughts/SKILL.md` — 主技能（启动 + 退出）
- **修改** `.claude/settings.local.json` — 注册 Hook

**Hook 脚本** (`thoughts-inject.sh`):
- 检查 `~/.thoughts/active` 是否存在 → 不存在则 exit 0
- 检查 `~/.thoughts/personality.json` 是否存在 → 不存在则 exit 0
- 读取 personality.json + profile.json（全局）+ memory.md（项目级，tail -50）
- 拼接人格设定 + 画像 + 记忆 + 行为指令（含记忆更新指令）
- 用 `jq -n --arg msg "$INJECT" '{"systemMessage": $msg}'` 输出 JSON

**settings.local.json**:
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "type": "command",
        "command": "bash .claude/hooks/thoughts-inject.sh",
        "timeout": 5
      }
    ]
  }
}
```

**主 SKILL.md 启动协议**:
- 检查 `~/.thoughts/profile.json` → 不存在则提示 `/thoughts-onboarding`
- 检查 `~/.thoughts/personality.json` → 不存在则基于 profile 生成
- 创建 `~/.thoughts/active` 标志文件 → 激活 Hook 注入
- 创建 `.thoughts/` 项目级目录（如不存在）
- 读取人格设定 → 以人格身份向用户打招呼
- 添加 `.thoughts/` 到项目 `.gitignore`（如未添加）
- 提示：Cron 自动行为将在下一阶段实现

**退出协议**（在同一 SKILL.md 中，通过用户输入 `/thoughts-stop` 触发）:
- 删除 `~/.thoughts/active`
- 读取 `.thoughts/cron-state.json`，逐个 CronDelete
- 输出告别语

**验证**:
- 运行 `/thoughts` → 人格打招呼
- 后续普通对话中，AI 回复带人格特征 + 颜文字
- 对话中提到新兴趣 → memory.md 被自动更新（路径 C 验证）
- 运行 `/thoughts-stop` → 下一条消息不再有人格注入
- 没有 `active` 文件时，Hook 完全静默

**验收标准对应**: #3（人格注入）, #4 和 #7 的路径 C 部分（轻量记忆更新）, #9（退出）

---

### Phase 3: Cron 深度分析 + 记忆整理

**目标**: 实现路径 A — Cron 定期用 Agent(Haiku) 做深度画像分析和记忆整理

**前提**: Phase 0 POC 验证通过

**产物文件**:
- **修改** `.claude/skills/thoughts/SKILL.md` — 添加 Cron 管理逻辑

**SKILL.md 新增内容**:

启动时创建"记忆整理"Cron（默认 30 分钟）:
```
CronCreate:
  schedule: "*/30 * * * *"
  instructions: |
    你是"思绪"的记忆整理模块。请执行以下步骤：
    1. 用 Read 读取 ~/.thoughts/profile.json 和 ~/.thoughts/personality.json
    2. 用 Read 读取 .thoughts/memory.md
    3. 分析 memory.md 中的近期条目，提取有价值的结构化信息
    4. 如果发现用户画像需要更新（新兴趣、偏好变化等），用 Write 更新 ~/.thoughts/profile.json
    5. 如果 memory.md 超过 200 行，精简旧条目（保留关键信息，删除琐碎内容）
    6. 将本次执行记录追加到 .thoughts/activity-log.jsonl
    7. 评估用户活跃度（读取 activity-log.jsonl 最近的记录），如果需要调整 Cron 频率，
       输出建议的新间隔（但本 Cron 不自行重建，留给主动聊天 Cron 处理）
```

**Cron ID 管理**:
- 创建后将 Cron ID 写入 `.thoughts/cron-state.json`
- `/thoughts-stop` 时读取并逐个 CronDelete

**验证**:
- 启动思绪模式后，CronList 可见记忆整理任务
- 30 分钟后，memory.md 被整理（冗余删除、结构化提取）
- profile.json 中出现从对话中提取的新信息
- personality.json 微调（如 AI 发现用户偏好的沟通风格）

**验收标准对应**: #4 和 #7 的路径 A 部分（深度分析+画像演化）

---

### Phase 4: 自动行为 + 环境扫描 + 权限

**目标**: Cron 主动聊天 + 联网搜索 + 环境检查 + 动态频率 + 权限系统

**前提**: Phase 0 POC 验证通过 + Phase 3 的 Cron 管理已就绪

**产物文件**:
- **修改** `.claude/skills/thoughts/SKILL.md` — 添加更多 Cron 任务 + 权限系统

**新增 Cron 任务**:

1. **主动聊天 Cron**（Sonnet，默认 30min）:
```
instructions: |
  你是"思绪"的主动聊天模块。
  1. 读取 ~/.thoughts/profile.json + .thoughts/memory.md + .thoughts/activity-log.jsonl
  2. 判断当前是否应该主动聊天：
     - 距离上次用户交互超过 10 分钟 → 可以聊
     - 距离上次用户交互不到 5 分钟 → 太频繁，跳过
     - activity-log 中连续 3 次主动聊天无用户回复 → 降频，跳过
  3. 如果决定聊，生成一个与用户兴趣相关的话题（基于 profile + memory）
  4. 记录到 activity-log.jsonl
  5. 如果需要调整频率：CronDelete 当前任务 → CronCreate 新频率 → 更新 cron-state.json
```

2. **联网搜索 Cron**（默认 60min）:
```
instructions: |
  你是"思绪"的内容发现模块。
  1. 读取 ~/.thoughts/profile.json
  2. 选择用户可能感兴趣的话题
  3. 用 WebSearch 搜索相关内容
  4. 用 WebFetch 获取详情
  5. 将搜索摘要追加到 .thoughts/memory.md（标记为"发现"类型）
  6. 记录到 activity-log.jsonl
```

3. **环境检查 Cron**（默认 120min）:
```
instructions: |
  你是"思绪"的环境感知模块。
  1. 读取 ~/.thoughts/permissions.json
  2. 根据已授权的权限范围执行检查：
     - system_info（已授权?）→ Bash: 磁盘空间、内存、CPU
     - running_apps（已授权?）→ Bash: 当前打开的应用列表
     - weather（已授权?）→ WebSearch: 当前天气
     - time_context → 无需授权：当前时间、星期几
  3. 如果遇到未授权的权限项，跳过（不在 Cron 中请求权限）
  4. 将环境摘要追加到 .thoughts/memory.md
  5. 记录到 activity-log.jsonl
```

**权限系统**:
- `/thoughts` 启动时（Phase 2 的启动协议补充）：如果 `~/.thoughts/permissions.json` 不存在或为空，用 AskUserQuestion 请求各项权限
- 权限选项：本次允许 / 永久允许 / 拒绝
- 写入 `~/.thoughts/permissions.json`:
```json
{
  "system_info": "always",
  "running_apps": "deny",
  "weather": "always",
  "browser_tabs": "once"
}
```
- Cron 中只检查 permissions.json，不请求新权限
- 用户可通过 `/thoughts` 重新运行启动协议来修改权限

**动态频率调整**:
- 每个 Cron 任务自行评估是否需要调频
- 调频逻辑：CronDelete 自身 → CronCreate 新频率 → 更新 cron-state.json
- 频率范围：主动聊天 15-120min，搜索 30-180min，环境检查 60-360min
- 降频信号：用户长时间无回复、深夜时段
- 升频信号：用户活跃对话、明确表示想聊天

**防重复 Cron**:
- `/thoughts` 启动时先读 `.thoughts/cron-state.json`
- 如果有已存在的 Cron ID，先全部 CronDelete 再重新创建
- 避免多次 `/thoughts` 产生重复任务

**验证**:
- CronList 显示 3-4 个活跃任务（记忆整理 + 主动聊天 + 搜索 + 环境检查）
- 主动聊天 Cron 触发 → 用户看到个性化话题
- 联网搜索 Cron 触发 → memory.md 出现搜索摘要
- 环境检查 Cron 触发 → memory.md 出现系统状态
- 用户活跃度变化后 → 观察 Cron 频率调整
- 首次启动 → 权限请求弹出
- `/thoughts-stop` → 所有 Cron 被清理

**验收标准对应**: #5（自动执行）, #6（动态频率）, #8（权限系统）

---

## 风险点与缓解策略

| 风险 | 影响 | 缓解策略 |
|------|------|----------|
| Hook 输出 JSON 格式错误 | 人格注入完全失效 | 用 jq 安全转义；Hook 脚本做格式验证；失败时 exit 0 不阻塞 |
| Claude 不遵循记忆更新指令（路径 C） | memory.md 不更新 | Cron 路径 A 作为兜底；systemMessage 中指令要简短明确 |
| Cron 执行环境不支持某些工具 | Phase 4 不可行 | Phase 0 POC 先验证；根据 POC 结果调整设计 |
| memory.md 被多方并发写入 | 数据冲突 | Claude 主意识追加写入（末尾），Cron 读取后整体重写（有锁） |
| 人格 prompt 注入占用太多上下文 | 挤压对话历史 | Hook 限制 memory.md 为最后 50 行；personality 和 profile 保持简洁 |
| Cron 主动聊天在用户忙时打扰 | 用户体验差 | 活跃度检测 + 动态降频 + 连续无回复自动延长间隔 |
| git 误提交 .thoughts/ | 隐私泄露 | 启动时自动添加 .gitignore 条目 |
| `/thoughts` 重复运行产生重复 Cron | 资源浪费 | 启动时先清理已有 Cron（读 cron-state.json） |
| jq 未安装 | Hook 脚本失败 | Hook 中用 `command -v jq` 检测，缺失时用 printf 手动转义回退 |

---

## 文件变更清单

| 文件 | 操作 | 描述 |
|------|------|------|
| `.claude/skills/thoughts-onboarding/SKILL.md` | 新建 | 画像收集 + 人格生成 Skill |
| `.claude/skills/thoughts/SKILL.md` | 新建 | 主技能 — 启动/退出 + Cron 管理 + 权限 |
| `.claude/hooks/thoughts-inject.sh` | 新建 | UserPromptSubmit Hook（JSON 输出） |
| `.claude/settings.local.json` | 修改 | 注册 Hook |
| `~/.thoughts/profile.json` | 运行时 | 用户画像（全局） |
| `~/.thoughts/personality.json` | 运行时 | AI 人格（全局） |
| `~/.thoughts/permissions.json` | 运行时 | 环境扫描权限（全局） |
| `~/.thoughts/active` | 运行时 | 激活标志（全局） |
| `.thoughts/memory.md` | 运行时 | 对话记忆（项目级） |
| `.thoughts/cron-state.json` | 运行时 | Cron ID 列表（项目级） |
| `.thoughts/activity-log.jsonl` | 运行时 | 活动日志（项目级） |

## 阶段依赖关系

```
Phase 0 (Cron POC) ─── 验证结果 ──→ Phase 3, Phase 4 的设计依据
                                      ↑
Phase 1 (Onboarding) → Phase 2 (Hook + 主Skill) → Phase 3 (Cron 记忆) → Phase 4 (自动行为)
```

Phase 0 和 Phase 1 可以并行。Phase 1-2 完成后即可体验人格对话 + 轻量记忆。Phase 3-4 依赖 POC 结果。
