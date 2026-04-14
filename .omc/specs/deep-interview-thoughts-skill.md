# Deep Interview Spec: 思绪模式 Claude Code Skill

## Metadata
- Interview ID: thoughts-skill-2026-04-14
- Rounds: 9
- Final Ambiguity Score: 18%
- Type: greenfield
- Generated: 2026-04-14
- Threshold: 20%
- Status: PASSED

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.88 | 0.40 | 0.352 |
| Constraint Clarity | 0.80 | 0.30 | 0.240 |
| Success Criteria | 0.75 | 0.30 | 0.225 |
| **Total Clarity** | | | **0.817** |
| **Ambiguity** | | | **0.183** |

## Goal

开发一套 Claude Code Skill，实现「思绪模式」——一个叠加在 Claude Code 原有能力之上的 AI 伙伴系统。进入思绪模式后，Claude 获得人格、记忆、环境感知和主动行为能力，同时不影响正常的编码/问答功能。

### 核心架构

```
Claude 本身（Opus）
  + PersonalityHook（注入人格设定 + 用户画像）
  + SubconsciousAgent（后台 Haiku，异步分析对话 → 更新记忆）
  + CronTask（动态重建，Sonnet 主动聊天/Haiku 判断是否行动）
  + EnvironmentScanner（Bash 命令检查系统状态）
```

**不是**两个独立 Agent 协作，而是：
- Claude 本身 = 主意识（通过 Hook 注入人格/画像上下文）
- SubconsciousAgent = 后台 Agent 子进程（Haiku，异步分析+记忆管理）
- CronTask = 定时触发的自动行为

## Constraints

### 技术平台
- **实现形式**：Claude Code Skill（`.claude/skills/` 目录下的 SKILL.md 文件）+ 1 个 Hook（settings.json 中配置）
- **叠加模式**：思绪模式不接管 Claude，所有原有能力保留
- **启停控制**：`/thoughts` 启动，`/thoughts-stop` 退出

### 模型分层（token 成本优化）
| 任务类型 | 模型 | 理由 |
|---------|------|------|
| 潜意识分析、Cron "该不该行动"判断、简单记忆提取 | Haiku | 高频低成本 |
| 主动聊天内容生成、联网搜索结果整理 | Sonnet | 创意质量需要 |
| 用户主动对话 | Opus | Claude 本身就是 Opus |

### 权限系统
- 环境扫描首次需要用户授权
- 三种选项：仅此一次 / 以后都允许 / 不允许
- 权限可随时更改
- 权限设置持久化到本地文件

### 数据存储
- 所有数据存储在本地文件（`.thoughts/` 目录）
- 用户画像、AI 性格、记忆、权限设置、Cron 状态均为 JSON/MD 文件
- 不上传任何用户数据

## Non-Goals (v1)

- 特殊时间提醒（饭点、学习时间等）→ v2
- AI 主动表达赞同/反对观点 → v2
- 多用户/多人格切换
- 画像/记忆的加密保护
- 独立桌面应用打包

## Acceptance Criteria

- [ ] 1. `/thoughts` 启动后，首次使用自动进入画像收集对话（AskUserQuestion 循环）
- [ ] 2. 画像收集完成后，AI 随机生成一个适配的性格设定（写入本地文件）
- [ ] 3. Hook 注入人格+画像，后续对话带有人格特征+颜文字表情
- [ ] 4. 潜意识 Agent（Haiku）后台异步分析每次对话，自主判断是否更新记忆
- [ ] 5. 动态 Cron 每隔一段时间触发自动任务：
   - 主动聊天（Sonnet 生成个性化内容）
   - 联网搜索用户感兴趣的内容（WebSearch + WebFetch）
   - 环境状态检查（Bash 命令获取：当前应用、浏览器页面、天气、时间、系统性能）
- [ ] 6. Cron 频率根据用户活跃度动态调整（聊了就频繁，没回复就延迟），通过删旧建新 Cron 实现
- [ ] 7. 随着使用，画像和性格设定可见地演化（潜意识自主判断更新）
- [ ] 8. 环境扫描首次触发权限授权对话，支持"仅一次/永久/拒绝"，可随时更改
- [ ] 9. `/thoughts-stop` 可以退出思绪模式，清理 Cron 任务

## Assumptions Exposed & Resolved

| Assumption | Challenge | Resolution |
|------------|-----------|------------|
| 需要两个独立 Agent（主意识+潜意识）| Claude 本身就是主意识，为什么需要独立的主意识 Agent？ | 简化：Claude + Hook = 主意识，只有潜意识需要独立 Agent |
| 自动执行用固定间隔 Cron | CronCreate 不支持动态频率 | 每次执行后删旧建新 Cron，实现动态频率 |
| 记忆更新需要规则触发 | 规则式 vs AI 自主判断？ | 潜意识自主判断，完全交给 AI 智能 |
| 所有功能是核心 | 哪些可以延后？ | 特殊时间提醒和 AI 观点表达放 v2 |
| 全部用 Opus | token 成本太高 | 三级模型分层：Haiku/Sonnet/Opus |
| "查看用户在干嘛"只是看应用 | 应该更广 | 扩展为环境状态检查 + 权限系统 |

## Technical Context

### 实现形式：Skill + Hook

```
.claude/skills/
  thoughts/SKILL.md           # 主技能（启动/管理思绪模式）
  thoughts-onboarding/SKILL.md # 首次见面画像收集

~/.claude/settings.json        # Hook 配置
  hooks.UserPromptSubmit       # 注入人格+画像上下文

.thoughts/                     # 数据目录
  profile.json                 # 用户画像
  personality.json             # AI 性格设定
  memory.md                    # 对话记忆/笔记
  permissions.json             # 环境扫描权限
  cron-state.json              # 当前 Cron 状态
  activity-log.jsonl           # 活动日志（用于动态频率计算）
```

### 关键 Claude Code 工具使用

| 工具 | 用途 |
|------|------|
| AskUserQuestion | 画像收集、权限授权 |
| Agent (haiku) | 潜意识后台分析 |
| Agent (sonnet) | 主动聊天内容生成 |
| CronCreate / CronDelete | 动态定时任务 |
| WebSearch + WebFetch | 联网搜索用户感兴趣的内容 |
| Bash | 环境状态检查（系统进程、浏览器标签等） |
| Read / Write | 记忆/画像/设定的持久化 |

### 数据流

```
用户发消息
  → Hook 读取 profile.json + personality.json → 注入为上下文
  → Claude（Opus）带人格回复 + 颜文字
  → Agent(haiku) 后台异步分析对话
    → 读取 memory.md + profile.json
    → 判断是否需要更新
    → Write 更新后的文件

Cron 触发
  → Agent(haiku) 判断是否行动（读取 activity-log.jsonl + 当前时间）
  → 不行动 → 静默返回
  → 行动 → 选择任务类型
    → 主动聊天 → Agent(sonnet) 生成内容
    → 联网搜索 → WebSearch → Agent(sonnet) 整理
    → 环境检查 → Bash 命令 → Agent(haiku) 摘要
  → 输出给用户
  → CronDelete 旧任务 + CronCreate 新任务（动态间隔）
```

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| PersonalityHook | core | hookConfig, promptTemplate | 读取 UserProfile + AIPersonality，注入 Claude 上下文 |
| SubconsciousAgent | core | model(haiku), analysisPrompt | 分析对话，更新 Memory + UserProfile + AIPersonality |
| UserProfile | core | 开放式 JSON（兴趣、年龄、性别、职业等） | 被 SubconsciousAgent 更新，被 PersonalityHook 读取 |
| AIPersonality | core | 性格特征、说话风格、颜文字偏好 | 首次随机生成，被 SubconsciousAgent 演化 |
| Memory | core | 对话摘要、关键事件、用户习惯 | 被 SubconsciousAgent 写入，被各组件读取 |
| CronTask | supporting | cronId, interval, lastRun, nextAction | 动态重建，触发自动行为 |
| ModelRouter | supporting | taskType → model mapping | 决定每个任务使用哪个模型 |
| EnvironmentScanner | supporting | bashCommands, scanScope | 检查系统状态（应用、浏览器、天气、性能） |
| PermissionSystem | supporting | permissions: Record<scope, level> | 管理环境扫描授权（once/always/deny） |
| KaomojiStyle | supporting | 颜文字库、使用规则 | 约束 AI 回复必须包含颜文字 |
| ThoughtsMode | supporting | isActive, startTime | 整体模式状态管理 |

## Ontology Convergence

| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|-------|-------------|-----|---------|--------|----------------|
| 1 | 7 | 7 | - | - | N/A |
| 2 | 7 | 0 | 0 | 7 | 100% |
| 3 | 7 | 0 | 0 | 7 | 100% |
| 4 | 7 | 0 | 0 | 7 | 100% |
| 5 | 7 | 0 | 1 | 6 | 100% |
| 6 | 8 | 1 | 0 | 7 | 88% |
| 7 | 9 | 1 | 0 | 8 | 89% |
| 8 | 9 | 0 | 0 | 9 | 100% |
| 9 | 11 | 2 | 0 | 9 | 82% |

## Interview Transcript
<details>
<summary>Full Q&A (9 rounds)</summary>

### Round 1
**Q:** 用户发消息时，主意识和潜意识的协作流程是什么？
**A:** 主意识先回复，潜意识异步 — 保证响应速度，潜意识后台分析
**Ambiguity:** 79% (Goal: 0.35, Constraints: 0.15, Criteria: 0.10)

### Round 2
**Q:** 动态时间逻辑在 Claude Code 中如何实现？
**A:** 动态重建 Cron — 每次执行后删旧建新，实现真正的自适应频率
**Ambiguity:** 73% (Goal: 0.50, Constraints: 0.15, Criteria: 0.10)

### Round 3
**Q:** 记忆的"自动变化"由谁触发？
**A:** 潜意识自主判断 — 完全交给 AI 智能决定
**Ambiguity:** 69% (Goal: 0.60, Constraints: 0.15, Criteria: 0.10)

### Round 4 [Contrarian]
**Q:** 思绪模式运行时，用户能否同时用 Claude 做其他事？
**A:** 叠加模式 — Claude 保留全部原有能力，思绪是额外状态
**Ambiguity:** 59% (Goal: 0.65, Constraints: 0.40, Criteria: 0.10)

### Round 5 [Contrarian]
**Q:** 真的需要"主意识 Agent"吗？Claude 本身就是主意识。
**A:** 对，可以简化 — 主意识 = Claude + Hook，只有潜意识需要独立 Agent
**Ambiguity:** 54% (Goal: 0.75, Constraints: 0.45, Criteria: 0.10)

### Round 6
**Q:** 什么样的体验会让你觉得"这个东西成了"？
**A:** 完整功能跑通 + AI 越来越懂我 + 性格成熟 + 有观点 + 颜文字
**Ambiguity:** 39% (Goal: 0.78, Constraints: 0.45, Criteria: 0.55)

### Round 7 [Simplifier]
**Q:** 这些功能怎么分优先级？
**A:** 特殊时间提醒和 AI 观点放 v2，其他都是核心。另外需要节约 token 的方案。
**Ambiguity:** 31% (Goal: 0.82, Constraints: 0.55, Criteria: 0.65)

### Round 8
**Q:** 模型分层策略：Haiku/Sonnet/Opus 分层合理吗？
**A:** 合理 — Haiku 做后台、Sonnet 做创意、Opus 只在用户面前
**Ambiguity:** 26% (Goal: 0.82, Constraints: 0.72, Criteria: 0.65)

### Round 9
**Q:** 确认 7 条验收标准是否完整？
**A:** 第 5 条扩展为广泛的环境状态检查 + 权限系统（一次/永久/拒绝，可随时更改）
**Ambiguity:** 18% (Goal: 0.88, Constraints: 0.80, Criteria: 0.75)

</details>
