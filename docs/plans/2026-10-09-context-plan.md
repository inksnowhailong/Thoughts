# 家庭管家主线（agent:main:main）长期上下文设计计划

- 版本：v2（2026-10-09 修订，依据用户答复改为「空闲触发的休眠」）
- 适用版本：OpenClaw 2026.9.6，被控机 `home`。主模型 `siliconflow/deepseek-ai/DeepSeek-V4-Flash`，回退 `siliconflow/Qwen/Qwen3.5-35B-A3B`，均为 128k 窗口、maxTokens 8192。
- 性质：设计文档，不含实现代码。调研全程只读远程：没有改远程文件、没有重启、没有发模型或网关请求。
- 证据路径约定：
  - `docs/` 指 `openclaw/docs/`，`dist/` 指 `openclaw/dist/`；
  - "agent 库" 指 `C:/Users/inksn/.openclaw/agents/main/agent/openclaw-agent.sqlite`，"state 库" 指 `C:/Users/inksn/.openclaw/state/openclaw.sqlite`，均为 2026-10-09 12:46 的本机只读副本。
- 标注约定：**[实测]** 来自库表、日志或系统查询；**[文档]** / **[源码]** 来自 OpenClaw 自身；**[估算]** 是推算，需要实验校准。

### 已定事项（用户 2026-10-09 答复）

| # | 决定 |
|---|---|
| D1 | 主线 `agent:main:main` = 主人语音 + 主人微信，共用一份 |
| D2 | 家人（ef79 号）每人一个独立支线会话，放在 **agent `main` 下**（不建 family agent）；Python 微信桥接把家人消息整理成一行摘要递进主线；主线内容不流向家人（约定级） |
| D3 | 家人与主人**完全同权**；用户**接受**家人能读到主线文件，隐私只是约定级（§5.6 列出具体含义） |
| D4 | 桥接走 `/v1/chat/completions` + `x-openclaw-session-key` 流式交付，另配 `wechat_send` 发送工具；不再用 openclaw-weixin 扩展 |
| D5 | 语音带 `x-openclaw-session-key: agent:main:main` 进主线，去掉 front.py / backoffice.py 的转述 |
| D6 | 家人自己发起的 L3 操作：家人在微信里自己批准，同时用 `wechat_send` 通知主人 |
| D7 | 家人摘要静默进主线，随主人下一轮带入；需要主人决定的，另用 `wechat_send` 通知 |
| D8 | 费用不设日上限，只看每日报告；单价由用户补充 |
| D9 | 生命周期：**空闲触发的「休眠」**——一段时间没有活动就整理记忆，整理完成就换新会话；不用每天定时（主人不用电脑时会关机） |

---

## 0. 一页结论

**1. 休眠（推荐方案 S2）**

- **检测与触发**：OpenClaw cron 每 5 分钟跑一次检查脚本。主人连续 **30 分钟**没有活动，且主线当前一段内容达到 **12k 会话 token 或 10 轮主人发言**时触发。
- **整理方式**：在一个**独立的无状态会话**里整理，不占用主线。由脚本从 SQLite 导出这一段转写，模型只输出一份 JSON，脚本校验后用原子替换的方式落盘。
- **换新前复核**：用**水位线**（sessionId + 最大事件序号 + lastInteractionAt）确认整理期间没有新输入，再调 `sessions.reset`（带 `expectedSessionId`）换新。有新输入就不换，已整理的部分记为检查点，下次只做增量。
- **关机补做**：开机登录后网关自动启动。如果检查发现上一段没整理，就**先换新**（只写一份临时交接、不调模型，不到 1 秒），再在后台整理这段已经关闭的旧会话。语音前端和桥接在开机后的第一条消息前，先等这个快速换新完成。
- **兜底**：白天的压缩兜底保留；主线 ≥ 60k 时，空闲 10 分钟就强制休眠。

**2. 数字目标**

- 语音轮被阻塞压缩或休眠卡住：每周 0 次；
- 主线平均 prompt ≤ 30k、峰值 ≤ 80k；
- 每天换新 2–3 次（上限约 4 次）；
- 全系统每天输入约 3M token，示例单价下约 ¥1.3/天。不设日上限，只看报告。

**3. 长期记忆的权威来源是文件**：

- `USER.md` 按人分节；
- `MEMORY.md` 放主线交接、家庭状态卡和精选事实；
- 每日笔记、`family-facts.json`、时间线、cron/intent、SQLite 转写检索。

**4. 已接受的风险**：家人支线跟主线在同一个 agent、完全同权，所以每轮都会注入 MEMORY.md 和 USER.md，可以检索、读取主线的笔记和转写，也可以改写这些文件。隐私是约定级的，用户已知情接受，见 §5.6。

**5. 最大剩余风险：水位线与换新的竞态，以及开机补做的时序。** 这些逻辑写错会丢交接，或者把主人的话切断。所以先用"演练模式"跑 3–5 天，再打开真实换新（§10）。

---

## 1. 目标与约束

### 1.1 现状事实

| 项 | 数值 | 来源 |
|---|---|---|
| 每轮固定开销（全新会话、一句话） | 13,674–14,787 prompt token | [实测] agent 库 `trajectory_runtime_events` 中 model.completed.usage |
| 系统提示组成 | 42,082 字符（心跳轮）/ 47,076（用户轮）；项目上下文 17,800（4 个引导文件 11,078）；skills 列表 8,393（25 个）；工具 schema 5,917（11 个） | [实测] `session_nodes.entry_json.systemPromptReport`、trajectory `prompt.submitted` |
| 默认压缩阈值 / 刷写阈值 | 108,000 / 104,000 | [源码] `dist/openclaw-runtime-agent-*.mjs:12,25`、`dist/agent-compaction-constants-*.mjs`、`dist/extensions/memory-core/index.js:600-674`、`dist/agent-runner-memory-*.mjs:799-802` |
| 主线现状 | 26 条用户消息、峰值 23,933、0 次压缩 | [实测] |
| 同一段对话里的消息间隔 | 10-04 那段连续对话最大间隔 16 分钟（13:31→13:47），其余 9–13 分钟 | [实测] trajectory 时间戳 |
| 活动时间戳语义 | HTTP 接入的轮次会更新 `session_nodes.last_interaction_at`（ttft-probe：00:25:44 发出，00:25:46 更新）；10-08 19:37 的心跳轮**没有**更新主线的 last_interaction_at（停在 10-04 22:35） | [实测] agent 库；[文档] `docs/concepts/session.md:164-166`（心跳、cron、exec 系统事件不延长空闲寿命） |
| 运行状态 | `session_nodes.status` 取值 running/done/failed/killed/timeout；`entry_json.activeWriterRunId` 在运行结束后**仍然保留**，不能用来判断是否忙 | [实测] |
| 后台任务 | state 库 `task_runs` 带 `requester_session_key` 和 `status` | [实测] |
| 网关启动方式 | 计划任务 `OpenClaw Gateway`，**登录触发**（MSFT_TaskLogonTrigger），当前 Running | [实测] `Get-ScheduledTask` |
| 连续轮缓存命中 | 约 95% | [实测] |
| 短回复语音轮端到端 | 1–5 s，偶发 7–16 s | [实测] |

### 1.2 约束

- **长期在线，但宿主会关机**：OpenClaw 只在电脑开着、主人登录后才运行。任何"定时在凌晨做"的设计都可能永远不执行。
- **会话键不变**：`agent:main:main` 永远不变，sessionId 随休眠换新；同一个键下的旧转写仍可检索（`docs/concepts/main-session.md:100-106`）。
- **语音时延优先**：任何维护动作都不能占用主线会话，也不能落在语音轮推理之前的路径上。
- **跨天、跨关机的连续性**：主人关机前说的事，开机后要接得上。
- **费用**：只观测、不设上限（D8）。

### 1.3 可量化目标

| 编号 | 目标 | 口径 |
|---|---|---|
| G1 | 语音轮被阻塞压缩、刷写或休眠卡住：**0 次/周** | 语音轮推理前出现 compaction/memory_flushing，或撞上 reset 栅栏 |
| G2 | 语音不带工具轮：**p50 ≤ 3 s、p95 ≤ 6 s** | 语音前端"识别文本 → 首音频块" |
| G3 | 主线 prompt：**平均 ≤ 30k、峰值 ≤ 80k** | `lastCallUsage.input + cacheRead` |
| G4 | 固定开销 **≤ 11k**（当前约 14k） | 全新测试会话一句话 |
| G5 | 换新：**每天 ≤ 4 次**；每段 ≥ 12k 会话 token 或 ≥ 10 轮主人发言 | 休眠日志 |
| G6 | 休眠整理成功率 **≥ 95%**；失败段 24 小时内补做成功 | 休眠日志 |
| G7 | 连续性：换新后抽查 10 项上一段要点，**命中 ≥ 9 项** | 实验 E7 + 每周抽查 |
| G8 | 记忆文件不超限：USER.md ≤ 3,600 字符、MEMORY.md ≤ 6,000、主线交接 ≤ 1,500、family-facts.json ≤ 20KB | 休眠校验 |
| G9 | 每日报告按时生成；费用数字在用户补单价后与账单偏差 ≤ 10% | 报告 |

---

## 2. 分层记忆模型

### 2.1 总览

```
             ┌──────────────── 每轮自动进入 prompt（主线和家人支线都会注入）──────────────┐
 L0 固定层   │ OpenClaw 系统提示 + AGENTS/SOUL/IDENTITY + skills 列表 + 工具 schema     │ ≈11–14k
 L1 画像层   │ USER.md：## 通用 / ## 主人 / ## <家人>（≤4,000 字符硬上限）             │ ≈1k
 L2 精选层   │ MEMORY.md：## 事实 / ## 主线交接 / ## 家庭状态卡                        │ ≈1.5–2.5k
 L3 近期层   │ 当前 sessionId 的对话（每次休眠后从 0 开始）                            │ 0–30k，典型
             └──────────────────────────────────────────────────────────────────────────┘
             ┌──────────────── 按需读取 ────────────────────────────────────────────────┐
 L4 日记层   │ memory/YYYY-MM-DD.md（休眠整理追加，压缩前刷写，重置钩子摘录）            │
 L5 事实库   │ family-facts.json（家里共享的结构化事实，带来源）                       │
 L6 时间线   │ memory/timeline/YYYY-MM.md（一行一事件）                                │
 L7 意图层   │ cron（定时提醒）+ intent（事件条件提醒）                                │
 L8 原文层   │ SQLite 转写（同键旧 sessionId 仍在）                                     │
 L9 家人交接 │ memory/family/<人>/handover.md（桥接在家人下一条消息前缀注入）          │
             └──────────────────────────────────────────────────────────────────────────┘
```

**原则**：

- **单一写者**：MEMORY.md 的「主线交接」和「家庭状态卡」只由休眠脚本整段替换；dreaming 晋升停用（它现在本来就在报 `DataCloneError`）。
- **文件优先于摘要**：压缩摘要与文件冲突时以文件为准，这条写进 AGENTS.md 红线。
- **自动注入只来自 USER.md / MEMORY.md**：每日笔记和转写永远不自动注入（`docs/concepts/memory-architecture.md:246-252`）。
- **家人支线一样会注入 L1、L2**：同一个 agent 的私聊会话都会注入 MEMORY.md 和 USER.md（`docs/concepts/user-model.md:267`），这是已接受的事实（D3）。所以 MEMORY.md 里不放"只能主人知道"的内容，敏感事项用 L4、L8 承载，按需检索。注意这只能降低"顺口说出来"的概率，不能阻止家人主动去读。

### 2.2 逐层说明

| 层 | 存什么 | 谁写 | 何时写 | 格式与上限 | 怎么读回 | 家人支线 |
|---|---|---|---|---|---|---|
| L3 近期对话 | 主人语音、主人微信、家人摘要（系统事件）、少量定时事件、工具调用 | OpenClaw | 每轮 | 由休眠和压缩约束；目标平均 ≤ 30k | 每轮全量 | 不注入（不同会话），但可以通过会话工具读到（D3） |
| L1 USER.md | 按人分节的稳定偏好和指令 | 白天：模型在明确表达偏好时就地改写对应人的分节；休眠：去重、supersede；主人手动 | 即时 / 休眠 | 指令式 + `<!-- observed: … \| status: active/superseded -->`；总量 4,000 字符硬上限（user-model.md:258），分配为 通用 ≤600、主人 ≤2,000、每位家人 ≤800 | 每轮注入并刷新 | **注入**（包括主人分节） |
| L2 MEMORY.md | 事实（带 trigger/importance）；主线交接；家庭状态卡 | 事实：休眠提议 + 主人手动；交接和状态卡：休眠脚本整段替换 | 每次休眠 | ≤ 6,000 字符；交接 ≤ 1,500；状态卡 ≤ 1,000 | 每轮注入；带 trigger 的条目匹配度 ≥ 0.72 时最多注入 3 条 | **注入**（包括主线交接） |
| L4 每日笔记 | 每段的要事、承诺与待办、偏好变化候选、家人摘要汇总、未决问题 | 休眠脚本按段追加；压缩前 memoryFlush 追加；session-memory 钩子在重置时写摘录 | 休眠 / 压缩前 / 重置 | Markdown，≤ 12KB/天（一天多段） | memory_search / memory_get | 可检索（D3） |
| L5 family-facts.json | 家里共享事实：成员档案、固定日程、家务、家规、地点 | 桥接写 `pending`；休眠校验后合并；主人手动 | 实时 / 休眠 | JSON ≤ 20KB | 状态卡注入 + 读工具 | 可读可写（D3） |
| L6 时间线 | 决定、承诺、到访、维修、采购等事件 | 休眠追加；白天模型可追加一行 | 休眠 / 即时 | `memory/timeline/YYYY-MM.md`，每月 ≤ 30KB；现有 `memory/timeline.md` 迁入 | memory_search | 可检索 |
| L7 cron / intent | 定时提醒 → cron；事件条件提醒 → intent | 模型在对话里创建 | 说出口时 | intent 默认冷却 24h、触发 3 次、90 天过期、每轮最多 3 条（`docs/concepts/standing-intents.md:48-50`） | 调度器 / 每轮预筛（无模型调用） | 可以按 sender 限定作用域 |
| L8 原文 | 全部转写 | OpenClaw | 每轮 | SQLite，默认 30 天归档 | sessions_search / sessions_history；休眠脚本直接只读访问 SQLite | 可读（D3） |
| L9 家人交接 | 家人支线休眠后的交接（≤600 字） | 休眠脚本（家人段） | 家人支线休眠时 | `memory/family/<人>/handover.md` | 桥接在家人换新后的第一条消息前加 `[上次对话交接：…]` | 是（只读自己的） |

### 2.3 文件结构样例

**USER.md（按人分节，覆盖旧条目而不追加）**

```markdown
# 用户画像（所有会话都会读到；只按当前说话人所在分节执行）
## 通用
- 所有回复用简体中文；语音回复不超过两句、先给结论。
  <!-- observed: 2026-10-01 | status: active -->

## 主人（老墨）
- 工作日 23:30 后不要主动播报，只在微信留言。
  <!-- observed: 2026-09-28 | status: active -->
- 不吃香菜；晚饭建议避开辛辣。
  <!-- observed: 2026-10-05 | status: active -->

## 小美（ef79）
- 称呼"小美"；回复要简短，多用表情。
  <!-- observed: 2026-10-09 | status: active -->
- 对花生过敏，推荐菜谱时排除。
  <!-- observed: 2026-10-09 | status: active -->
```

配套写进 AGENTS.md 的规则：

- 根据信封里的说话人只执行对应分节；
- 家人会话里不主动提起「## 主人」分节和「主线交接」的内容（约定级）。

**MEMORY.md**

```markdown
# 长期记忆（精选）
## 事实
- 家里网关只开在局域网。 <!-- trigger: 网关, 远程访问 --> <!-- importance: 9 -->

## 主线交接（段 2026-10-09#2，13:05–14:40，休眠脚本生成，≤1,500 字）
- 进行中：阳台花架倾向 B 款，周末量尺寸后下单。
- 承诺：周六 11:00 提醒小美钢琴课（cron: piano-sat）。
- 未决：外婆周日是否来（家人摘要 12:45，已用 wechat_send 通知主人）。
- 待主人知晓的家人摘要：1 条（见今日笔记）。
- 语气注意：主人说太吵，下午播报音量调低。

## 家庭状态卡（由 family-facts.json 渲染，≤1,000 字）
- 本周日程：周六 11:00 钢琴课（小美）……
```

**memory/2026-10-09.md（一天多段，按段追加）**

```markdown
# 2026-10-09
## 段 #1 08:10–09:02（休眠 09:35）
### 要事 / 承诺与待办 / 偏好变化候选 / 家人摘要汇总 / 未决问题
- ……
## 段 #2 13:05–14:40（开机补做 18:20）
- ……
```

**family-facts.json**

```json
{
  "version": 1,
  "updatedAt": "2026-10-09T14:42:00+08:00",
  "members": [
    { "id": "owner", "displayName": "主人", "aliases": ["老墨"] },
    { "id": "ef79-xiaomei", "displayName": "小美", "wechat": "ef79:<peer-id>", "allergies": ["花生"] }
  ],
  "schedules": [
    { "id": "piano-sat", "who": "ef79-xiaomei", "what": "钢琴课", "rrule": "FREQ=WEEKLY;BYDAY=SA", "time": "11:00",
      "status": "active", "source": "family:ef79-xiaomei 2026-10-09 12:31", "verifiedBy": "sleep 2026-10-09#2" }
  ],
  "chores": [], "rules": [], "places": [],
  "pending": [
    { "op": "update", "path": "schedules[piano-sat].time", "value": "11:00", "source": "family:ef79-xiaomei 2026-10-09 12:31" }
  ]
}
```

**memory/timeline/2026-10.md**

```markdown
2026-10-09 12:40 | 主人 | 决定 | 周六去外婆家 | src:voice
2026-10-09 12:31 | 小美 | 变更 | 钢琴课改 11:00 | src:family-digest
```

---

## 3. 会话生命周期：空闲休眠

### 3.1 可用能力（查证）

- **指定会话手动压缩**：
  - CLI：`openclaw sessions compact "<key>"`；RPC：`gateway call sessions.compact {key}`。
  - 会话有活跃运行或排队任务时直接拒绝（`dist/sessions-compact-*.mjs:265-296`，`docs/cli/sessions.md:427-489`）。
  - 不能传摘要指令；`--max-lines` 会永久截断转写，禁用。
- **指定会话重置**：RPC `sessions.reset`，可以带 `expectedSessionId`，sessionId 已经变了就拒绝（`docs/gateway/protocol/rpc-session-control.md:39`）。没有 CLI 子命令，要用 `openclaw gateway call sessions.reset`。reset 会中断正在进行的工作，调用前必须先确认空闲。
- **重置附带的行为**：
  - `session-memory` 钩子会把最近 N 条（默认 15）用户和助手文本写到 `memory/YYYY-MM-DD-HHMM.md`（`docs/automation/hooks/bundled-hooks.md:114-145`）；
  - 重置会**丢弃**旧会话里排队的系统事件（`docs/concepts/session.md:172-176`）——家人摘要要靠桥接补投；
  - RPC 重置是否触发这个钩子、新会话首轮是否会前置近期笔记，需要实验 E6。
- **内置的空闲重置**：`session.reset: { mode: "idle", idleMinutes }` 是懒触发的——要等下一轮进来才换（bundled-hooks.md:117-120），而且只有钩子摘录、没有整理，不满足需求。必须保持 `mode: "none"`，避免和休眠抢着换新。
- **空闲依据**：`lastInteractionAt` 只随真实的用户或渠道交互更新，心跳、cron、exec 系统事件不会延长它（session.md:164-166），HTTP 接入的轮次会更新 [实测]。这正好对应"活动"的定义。
- **cron**：
  - 支持 `every`（固定间隔）和 `command` 载荷（跑脚本，不调模型；默认 10 分钟超时，可以调）；
  - 支持条件触发脚本：返回 `fire:false` 时不产生运行记录，30 秒预算（`docs/automation/cron-jobs/schedules.md:91-114`）；
  - 支持 stream 调度：网关托管的常驻进程（schedules.md:42-59）；
  - 定义和状态持久化在 SQLite，重启不丢（how-it-works.md:17-21）；每次运行都会产生一条后台任务记录（how-it-works.md:18）。
- **系统事件**：`openclaw system event --text …` 默认投到主线，不单独调用模型；不持久，重启会丢（`docs/cli/system.md:37-80`）。
- **网关随登录启动**：计划任务 `OpenClaw Gateway`，LogonTrigger [实测]。

### 3.2 候选方案与打分（5 分最好）

- **L2 只靠压缩**：不重置，阈值触发压缩。
- **L4 定时夜间整理 + 重置**（v1 推荐）：每天 03:40 执行。主人夜里关机时就**永远不执行**。
- **L1i 内置空闲重置**：`session.reset.mode: "idle"`。懒触发，只有 15 条摘录，没有整理。
- **S1 空闲休眠 · 主线内整理**：在主线里跑一轮整理，整理完再换新。
- **S2 空闲休眠 · 独立会话整理 + 水位线 + 开机先换新**（推荐）：见 §3.3–3.9。

| 维度 | L2 | L4 | L1i | S1 | **S2** |
|---|---|---|---|---|---|
| 跨段连续性 | 4 | 4 | 1 | 4 | 4 |
| 语音时延（不被卡） | 2 | 5 | 4（换新发生在主人那一轮） | 3（整理占主线 30–90 s） | 5 |
| 费用（平均上下文） | 2（约 70k） | 4（约 35k） | 5 | 5（约 21k） | 5（约 21k） |
| 漂移风险（低 = 高分） | 2 | 4 | 3 | 4 | 4 |
| 关机适应性 | 5 | 1 | 4 | 3（整理中途关机会占用主线状态） | 5 |
| 实现复杂度（简单 = 高分） | 5 | 3 | 5 | 3 | 2 |
| **合计** | 20 | 21 | 22 | 22 | **25** |

**推荐 S2**。它比 S1 多出的复杂度换来两点：整理完全不占主线，所以主人随时开口都不会被卡；关机中断时也只是"有一段待整理"，不会留下半截状态。

### 3.3 什么算"活动"、什么时候算"空闲"

| 输入 | 算活动（重置空闲计时）？ | 是否阻止休眠开始 | 说明 |
|---|---|---|---|
| 主人语音（HTTP，主线键） | **是** | — | 会更新 lastInteractionAt [实测] |
| 主人微信（桥接 HTTP，主线键） | **是** | — | 同上 |
| 家人摘要（系统事件） | 否 | 否 | 只是内容；整理时从桥接的摘要日志一并读入 |
| 定时任务推给主线的系统事件 | 否 | 否 | 同上 |
| 主线正在运行或有排队任务 | — | **是** | `session_nodes.status = running`，或 `sessions.list` 显示有活跃或排队任务 |
| 主线发起的后台任务（exec 长任务、子 agent、媒体生成） | — | **是** | state 库 `task_runs.requester_session_key = agent:main:main` 且状态是 queued/running。原因：任务完成后会唤醒主线汇报，需要原上下文 |
| 等待中的审批（exec approvals） | — | **是** | 审批结果要回到原会话 |
| 家人支线的活动 | 否（主线） | 否 | 家人支线有自己的休眠规则（§3.9） |

**推荐阈值**：

| 参数 | 推荐值 | 理由 |
|---|---|---|
| 空闲时长 `T_idle` | **30 分钟** | 现有数据里，同一段连续对话的最大间隔是 16 分钟 [实测]，30 分钟足以说明一段对话结束了；太短（10–15 分钟）会把一段对话切开，太长（≥ 60 分钟）会让大部分段落等不到休眠就被关机打断，全部变成开机补做 |
| 最小内容 | 当前段**会话 token ≥ 12k**（最新 prompt − 固定开销）**或主人发言 ≥ 10 轮** | 低于这个量，换新带来的碎片化成本大于收益：交接太碎，缓存也白白失效。一天增长约 45k [估算]，这个门槛让一天最多 3–4 段 |
| 换新冷却 | 距上次换新 **≥ 2 小时** | 防止"零碎对话 + 恰好空闲"导致频繁换新 |
| 强制休眠 | prompt **≥ 60k 且空闲 ≥ 10 分钟**（忽略冷却和最小内容） | 长时间连续对话时，在压缩阈值之前主动收尾 |
| 低于最小内容时 | **跳过**，继续累积；关机重启后也不换新，接着用旧会话 | 保证连续性 |

### 3.4 由谁检测和触发

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **A. OpenClaw cron `every 5m` + command 载荷（Python 检查脚本）** | 生命周期跟网关一致（随登录启动、调度持久化）；能从 SQLite / CLI 看到**所有**输入源；不用新增常驻进程；command 类任务现在运行正常（habit-extract 显示 ok） | 粒度 5 分钟（对 30 分钟阈值可以接受）；每天 288 条运行记录和任务记录（会被维护清理，但会让报告变嘈杂）；读 SQLite 依赖内部表结构 | **推荐** |
| B. cron 条件触发脚本（`fire:false` 时不产生记录） | 没有记录噪声 | 条件脚本跑在代码模式执行器里，30 秒预算、最多 5 次工具调用，能不能读 SQLite 未知 | 实验 E18 验证后可以替代 A 的检查部分 |
| C. guanjia-guard 网关插件内计时器 | 能实时收到每轮事件，精度最高 | 插件 API 是否提供常驻计时器、怎么调模型都未知；跑在网关进程里，出错会影响网关；每次改代码都要重启 | 不推荐（以后可以用插件钩子记水位，作为增强） |
| D. 自写 Python 常驻进程（桥接或语音前端） | 桥接本来就常驻 | 桥接只看到微信、语音前端只看到语音，两边要共享状态，容易各说各话；进程生命周期要单独管 | 不推荐做检测者；**但它们要承担"开机首条消息闸门"（§3.7）** |
| E. cron stream 调度托管的常驻 watcher | 网关负责启停和崩溃重启 | 必须配一个载荷（会往主线注入事件或跑 agent 轮），用法拧巴 | 不推荐 |

**互斥**：脚本用 `ops/sleep/lock`（记录 pid 和开始时间，超过 15 分钟算过期）保证同一时刻只有一个休眠在跑；cron 的 `timeoutSeconds` 设 600。

### 3.5 正常休眠流程（"先整理后换新"）

状态记录在 `ops/sleep/journal.json`。每个阶段完成就落盘，可以重入。

```
S0 检查（每 5 分钟）
   读主线：sessionId、status、lastInteractionAt、最新 prompt（lastCallUsage）、最大事件序号 maxSeq
   读 state 库 task_runs：主线发起且未结束的任务
   读 journal：本 sessionId 已处理到哪个序号（checkpointSeq）、上次换新时间
   → 不满足 空闲/不忙/最小内容/冷却 就退出（约 1 秒）
S1 快照  W0 = {sessionId, maxSeq, lastInteractionAt, snapshotAt}；journal.phase = snapshot
   导出 checkpointSeq < seq ≤ W0.maxSeq 的事件（只读 SQLite；zstd 事件解压），过滤：
     - 保留用户和助手文本；
     - 工具调用只留"工具名 + 一行参数"，工具结果只留前 300 字；
     - 丢弃 openclaw.cache-ttl 等内部标记；
   附上：桥接摘要日志（snapshotAt 之前、未处理的条目）、当前 USER.md / MEMORY.md / family-facts.json
S2 整理  向 /v1/chat/completions 发一次请求：不带 session key、不带 user 字段 → 无状态独立会话
   （docs/gateway/openai-http-api.md:104），主线完全不受影响
   要求模型只输出一份 JSON：
     {handover, dailyNote{要事,承诺待办,偏好候选,家人摘要汇总,未决}, timeline[], factsOps[], userOps[], memoryFactOps[]}
   JSON 不合法就重试 1 次；仍失败 → journal 记 failed，退出（下次重试）
S3 暂存  JSON 原文与渲染后的文件全部写到 ops/sleep/staging/<sessionId>-<maxSeq>/
   校验：大小上限、JSON schema、必需小节、和现行文件的 diff（删除条目 > 20% 视为异常）
   备份现行文件到 backups/<时间>/（保留 30 天）；journal.phase = staged
S4 提交  逐个文件用"同目录临时文件 + 原子替换"写入（MEMORY.md 整段替换「主线交接」和「家庭状态卡」）
   journal.checkpointSeq[sessionId] = W0.maxSeq；phase = committed
S5 复核水位  重新读主线：sessionId 相同？maxSeq 没变（只算 message 类事件）？lastInteractionAt 没变？
   status ≠ running？没有新的排队或后台任务？
   ├─ 有变化 → 不换新。已提交的整理保留为检查点，下次休眠只处理 seq > W0.maxSeq 的增量。phase = done(no-reset)
   └─ 无变化 → S6
S6 换新  gateway call sessions.reset {key:"agent:main:main", expectedSessionId: W0.sessionId}
   ├─ session-changed 等拒绝 → 同上，记为检查点
   └─ 成功 → 补差检查：旧 sessionId 的 maxSeq 是否 > W0.maxSeq（S5 和 S6 之间的毫秒窗口里有消息落入）
            若是 → 把多出的消息按原文截断（每条 ≤ 200 字、最多 5 条，不调模型）追加到「主线交接」末尾，
                   标「换新前最后几句（未整理）」，并把这段登记为待增量整理
   补投：把 snapshotAt 之后到达、因为重置被丢弃的家人摘要，重新执行一次 openclaw system event
   phase = reset
S7 收尾  写休眠日志一行（§8），清 staging，phase = done
```

要点：

- **整理期间来了新消息怎么办**：不丢弃整理结果（它对 W0 之前的内容是正确的），也不换新；记为检查点，下次空闲时只做增量。理由：丢弃会浪费一次模型调用，而且整理内容本来就是可增量合并的结构（追加笔记、替换交接）。
- **为什么不只用 expectedSessionId**：它只管 sessionId 有没有变，管不到同一个 sessionId 下新增的消息。所以用 `maxSeq`（transcript_events 序号，单调递增）加 `lastInteractionAt` 做水位线，S5 复核一次，S6 之后再补差一次，把两段竞态窗口都覆盖到。
- **主线完全不被占用**：整理请求走无状态会话，主线排队和运行都不受影响；主人在 S1–S5 之间开口，只会导致这次"不换新"。

### 3.6 关机场景

| 场景 | 处理 |
|---|---|
| 主人说完话 10 分钟就关机（没等到休眠） | 关机前什么都不做。期间创建的提醒（cron）已经持久化，不受影响 |
| 开机登录 → 网关随登录启动 → 第一次 cron 检查（最多 5 分钟后，另加一个"登录后 1 分钟"的一次性 Windows 计划任务来跑同一个检查脚本，缩短等待） | 发现：主线 lastInteractionAt 早于 T_idle（因为关机期间没有交互）、当前段未整理（checkpointSeq < maxSeq）、达到最小内容 → 进入**开机补做** |
| 开机补做（"先换新后整理"，O2） | ① 不调模型，直接写一份**临时交接**：上一段最后 8 条主人与助手消息的截断原文 + 「上一段整理中」标记，原子替换 MEMORY.md「主线交接」；② 立即执行 `sessions.reset`（带 expectedSessionId，不到 1 秒）；③ 旧会话已经关闭，对它按 S1–S4 整理（读取固定 sessionId，不怕新输入）；④ 提交后用正式交接替换临时交接 |
| 补做时，主人开机第一句话就来了 | 由**开机首条消息闸门**处理：语音前端和桥接在自身启动后的第一条主线消息之前，先调用检查脚本的"快速换新"子命令，同步等待 ≤ 3 秒（只做 ① ②，不调模型），然后再转发。超时就直接转发：消息落在旧会话，整体退化为"本段继续"，下次空闲再休眠 |
| 段落未达到最小内容 | 不换新，开机后继续用旧会话（保持连续） |
| 整理到一半被关机 | journal 的阶段决定下次怎么接：snapshot 或整理中 → 丢弃 staging，从 S1 重做（浪费一次模型调用）；staged → 校验后直接执行 S4，不用再调模型（JSON 原文保存在 staging）；committed 但没换新 → 回到 S5；reset 完成但没收尾 → 执行 S7 |
| 写文件写到一半被关机 | 每个文件都用"同目录临时文件 + 原子替换"（Windows 上 `os.replace` 在同一卷内是原子的），不会出现半个文件；多个文件之间不是原子的，但 S4 可以整组重做（内容由 staging 决定，结果确定），并且有 S3 的备份 |
| 是否接 Windows 关机事件 | **不接**。关机通知通常只给几秒（会话结束消息、预关机窗口），来不及调模型；能做的只有"快速换新"，而开机补做在没有时间压力的情况下做的是同一件事 |
| 网关比语音前端或桥接启动得晚 | 闸门调用失败就直接转发（退化为"本段继续"），不阻塞主人 |

### 3.7 开机首条消息闸门（语音前端和桥接需要配合）

- 进程启动后维护一个"本次启动后是否已经过闸"的标志。
- 第一条要发往 `agent:main:main` 的消息，先调用休眠脚本的快速换新子命令。这个命令只做判断、写临时交接、`sessions.reset`，不调模型，目标耗时 < 1 秒，硬超时 3 秒。
- 返回"已换新""不需要"或"超时"之后，再转发消息。
- 闸门只在每次进程启动后执行一次；之后的消息直接转发。

### 3.8 换新时带走什么

| 带走 | 怎么带 | 读回 |
|---|---|---|
| 进行中事项、承诺、待办、未决、语气提示、待主人知晓的家人摘要 | MEMORY.md「主线交接」（每次休眠整段替换，≤1,500 字） | 每轮注入 |
| 家庭事实与日程 | family-facts.json → 「家庭状态卡」 | 每轮注入 / 读工具 |
| 偏好变化 | USER.md 对应人的分节 | 每轮注入 |
| 本段细节 | memory/当天.md 的「段 #n」 | memory_search |
| 事件 | timeline | memory_search |
| 原话 | SQLite（同键旧 sessionId）+ session-memory 钩子摘录（设为 30 条） | sessions_search |
| **不带** | 工具原始输出、寒暄、已完成的一次性查询 | — |

### 3.9 家人支线的休眠

- **规则**：空闲 **4 小时**，且该支线会话 token ≥ 10k 或家人发言 ≥ 10 轮；强制条件是 ≥ 50k 且空闲 30 分钟。家人对话更稀疏，主动打断的代价也小，所以阈值放宽。
- **整理内容**：轻量版。抽取可共享的事实写进 family-facts.json 的 `pending`；更新 USER.md 中该家人的分节；生成 ≤600 字的家人交接，写到 `memory/family/<人>/handover.md`。**不写**主线交接，也不写主人分节。
- **换新**：同样走水位线 + `sessions.reset`（家人支线的键）。
- **交接注入**：MEMORY.md 是共享的，不能放家人交接（否则主线每轮都要背上）。所以由桥接在家人换新后的第一条消息前加 `[上次对话交接：…]`。
- **对家人支线的影响（已接受）**：
  - 家人每轮都会被注入 MEMORY.md（包括主线交接、家庭状态卡）和 USER.md（包括主人分节），每轮多出约 2–3k token；
  - 家人支线的模型能看到主人最近在忙什么，回复家人时有可能提及。约定级防护只有 AGENTS.md 里的"家人会话不主动提起主人分节和主线交接"这一条规则。

### 3.10 白天压缩兜底还需要吗

**需要，但降为最后防线**。有了空闲休眠，仍然有三种情况可能撑满上下文：

- 连续几个小时没有 30 分钟空档的工具密集对话；
- 强制休眠也找不到 10 分钟空档；
- 休眠脚本故障。

防线依次是：强制休眠（≥ 60k 且空闲 10 分钟）→ 刷写（80k）→ 压缩（92k）→ 溢出恢复。压缩配置见 §4。

---

## 4. 压缩兜底的配置

### 4.1 取值与理由

| 键 | 建议值 | 理由 |
|---|---|---|
| `models.providers.siliconflow.models[].contextTokens` | 主模型和回退都设 112000 | 给两种分词器的计数差留余量；阈值 92k、刷写 80k（`dist/types.openclaw-*.d.ts:3781,9272`） |
| `compaction.mode` | `"safeguard"` | 结构化摘要 + 质量审计；不设时代码按 default 跑（`dist/openclaw-runtime-agent-*.mjs:45-50`） |
| `compaction.keepRecentTokens` | 12000 | 压缩后起点不至于过高 |
| `compaction.model` | 不设 | 跟会话模型走，失败可以沿 fallback 链重试（`docs/concepts/compaction.md:140`） |
| `compaction.timeoutSeconds` | 120 | 快速失败，保留原历史 |
| `compaction.midTurnPrecheck.enabled` | true | 防止工具循环中途撑爆 |
| `compaction.memoryFlush.softThresholdTokens` | 12000 | 92k − 12k = 80k 时刷写，远离阻塞线 |
| `contextPruning` | `cache-ttl`，ttl 30m | 非 Anthropic 默认关闭；ttl 由实验 E10 校准 |
| `session.reset.mode` | `"none"` | 换新只由休眠脚本做 |
| `heartbeat.every` | `"0m"` | 关掉主线周期心跳：现在因 no-route 连续跳过 84 次；一旦配置了 owner，它会每 30 分钟在主线跑一整轮，而且心跳的系统提示不同，会破坏缓存 [实测] |

### 4.2 中文家庭场景的摘要格式

压缩已经降为最后防线，**不做压缩 provider 插件**（2–3 天工作量，而且它的输出绕过内置质量审计）。

可选增强（H1）：在 guanjia-guard 里挂 `before_prompt_build`，每轮把 ≤800 字的「家庭状态卡 + 今日要点」注入到 prompt 尾部（`docs/gateway/config-agents/heartbeat-compaction-and-streaming.md:142-146`）。这样即使压缩摘要写得像编程任务，关键事实也不会丢。工作量 0.5–1 天。只有在实验 E4 显示压缩会丢失家庭事实、且压缩每周发生 ≥ 1 次时才做。

### 4.3 避免语音轮撞上阻塞压缩或换新

1. 休眠只在空闲 30 分钟后发生，强制休眠也要求空闲 10 分钟。
2. 整理在独立会话进行，不占主线；换新是毫秒级的 RPC，而且之前做了水位线复核。
3. 刷写 80k、压缩 92k，平均 prompt 约 21k [估算]，正常日子到不了。
4. 主线用 `followup` 队列，避免语音和微信串轮。
5. 语音前端兜底：等待超过 6 s 先播一句"稍等"；超过 30 s 记一次 G1 故障。

### 4.4 openclaw.json 片段草案（JSON5；合并进现有配置，不要整段覆盖）

> 应用前逐键用 `gateway config.schema.lookup` 或 `openclaw doctor` 校验：配置 schema 是 strict 的。`models.providers.siliconflow.models` 是数组，要在现有 4 个条目上原地补字段。`cost` 单位是 USD / 1M token，待用户补充（`docs/reference/token-use.md:189-198`）。

```json5
{
  agents: {
    defaults: {
      model: {
        primary: "siliconflow/deepseek-ai/DeepSeek-V4-Flash",
        fallbacks: ["siliconflow/Qwen/Qwen3.5-35B-A3B"],
      },
      userTimezone: "Asia/Shanghai",            // 每日笔记、段落时间、钩子文件名都按这个时区

      compaction: {                             // 最后防线：正常情况下由休眠先收尾
        enabled: true,
        mode: "safeguard",
        keepRecentTokens: 12000,
        recentTurnsPreserve: 3,
        identifierPolicy: "strict",
        qualityGuard: { enabled: true, maxRetries: 1 },
        midTurnPrecheck: { enabled: true },
        timeoutSeconds: 120,
        notifyUser: false,
        postIndexSync: "async",
        memoryFlush: { enabled: true, softThresholdTokens: 12000, forceFlushTranscriptBytes: "2mb" },
      },

      contextPruning: {
        mode: "cache-ttl",
        ttl: "30m",
        hardClear: { enabled: true, placeholder: "[旧工具输出已清除，需要时请重新查询]" },
      },

      contextLimits: { memoryGetMaxChars: 6000 },
      heartbeat: { every: "0m" },               // 主线不跑周期心跳
    },
    entries: {
      main: {
        // skills 白名单：25 → 约 8 个（§6）；名字以 /context detail 为准
        skills: ["guanjia-launch", "guanjia-lookup", "guanjia-notes", "weather",
                 "windows-host-tasks", "taskflow", "taskflow-inbox-triage", "diagnose-gateway"],
      },
    },
  },

  session: {
    dmScope: "per-account-channel-peer",        // 家人支线各自独立
    reset: { mode: "none" },                    // 换新只由休眠脚本做；不要开内置 daily/idle
  },

  messages: {
    queue: { mode: "followup", cap: 20, drop: "summarize" },   // 主线不用 steer
  },

  // tools.sessions.visibility 保持默认（all）：家人完全同权（D3），不做收紧

  memory: {
    search: {
      rememberAcrossConversations: false,       // 跨会话回忆由休眠交接承担，不开隐式回忆
      sources: ["memory"],
    },
  },

  hooks: {
    internal: {
      entries: { "session-memory": { enabled: true, messages: 30 } },  // 换新时的原文摘录兜底
    },
  },

  plugins: {
    entries: {
      "memory-core": { config: { dreaming: { enabled: false } } },     // 单一写者
    },
  },

  models: {
    providers: {
      siliconflow: {
        models: [
          { id: "deepseek-ai/DeepSeek-V4-Flash", contextWindow: 128000, contextTokens: 112000, maxTokens: 8192,
            cost: { input: "<待补>", output: "<待补>", cacheRead: "<待补>", cacheWrite: 0 } },
          { id: "Qwen/Qwen3.5-35B-A3B", contextWindow: 128000, contextTokens: 112000, maxTokens: 8192,
            cost: { input: "<待补>", output: "<待补>", cacheRead: "<待补>", cacheWrite: 0 } },
        ],
      },
    },
  },
}
```

**cron 任务设计**（用 `openclaw cron add` 创建，这里只列字段）：

| 任务 | schedule | payload | 其他 |
|---|---|---|---|
| `guanjia-sleep-check` | `every 5m` | `command`：Python 解释器的**固定安装路径** + `sleep_check.py check` | `sessionTarget: isolated`、`delivery: none`、`timeoutSeconds: 600` |
| `guanjia-daily-report` | `cron "30 22 * * *"`，tz `Asia/Shanghai`，再加开机补报 | `command`：`sleep_check.py report` | 当天没有生成报告时，由检查脚本在下一次开机时补报 |
| Windows 计划任务 `GuanjiaSleepBoot` | 登录触发、延迟 60 秒 | `sleep_check.py check --boot` | 网关之外的独立保险，缩短开机补做的等待 |

> 注意：现有的 `guanjia-habit-extract` 和 `guanjia-weather-check` 的 argv 指向 `fnm_multishells\4644_…\node.exe`，这是 fnm 每个 shell 的临时路径，重启后可能失效（M1 一并修）。新任务一律使用固定安装路径。

---

## 5. 输入卫生

### 5.1 进入主线的信封

- **主人语音**：`/v1/chat/completions`，带 `x-openclaw-session-key: agent:main:main` 和 `x-openclaw-message-channel: voice`（`docs/gateway/openai-http-api.md:95-106`）：

```
[语音·客厅·10-09 12:41] 明天早上七点叫我
```

- **主人微信**（桥接，同一会话键，channel 设为 wechat）：

```
[微信·主人·10-09 12:40] 原文（≤800 字）
[微信·主人·10-09 12:42][图片] inbox/2026-10-09/wx-1a2b.jpg ｜ 桥接描述：电表读数约 3021（≤60 字）
[微信·主人·10-09 12:44][长消息 2,350 字] 前 600 字……（全文见 inbox/2026-10-09/wx-5e6f.txt）
```

- **家人摘要**（D7，静默）：

```
[家人摘要·小美·10-09 12:30–12:45·3 条] 钢琴课改到周六 11 点；问外婆周日是否来，已回复会转达。需主人决定：外婆周日行程
```

投递和持久化规则：

- 用 `openclaw system event --text "<摘要>"` 投递（不带 `--session-key`，落主线，不单独调用模型）；
- 「需主人决定」的摘要同时用 `wechat_send` 通知主人；
- 系统事件**不持久**，网关重启会丢，换新也会丢弃排队中的事件。所以桥接必须维护**摘要日志**（`bridge/digests/YYYY-MM-DD.jsonl`，每条带 id、时间、是否已被主线消费）。休眠整理把摘要日志作为输入的一部分；换新后由休眠脚本补投 `snapshotAt` 之后的摘要（§3.5 S6）；网关重启后由桥接补投没被消费的条目；
- 心跳关闭后，`next-heartbeat` 模式的事件能否随下一轮用户消息注入，由实验 E3 验证（前提条件）。

### 5.2 家人 L3 操作（D6）

家人自己在微信里批准自己发起的 L3 操作（审批流在桥接或 guanjia-guard 侧实现）。批准后用 `wechat_send` 通知主人一行：`[家人操作·小美·时间] 做了什么，结果`。同时写进摘要日志，休眠时进入时间线。

### 5.3 工具大输出、图片、文件

- **自研工具**默认返回 ≤ 2,000 字符；完整结果写到 `inbox/` 或 `ops/tmp/`，只返回路径。
- **exec**：AGENTS.md 规定"输出可能超过 50 行时，先重定向到文件"。旧微信会话里出现过两条约 16KB 的 exec 结果 [实测]。
- **OpenClaw 兜底**：pruning（>4000 字符保留首尾各 1500）、`midTurnPrecheck`、`memoryGetMaxChars`。
- **图片、语音、文件**：落盘到 `workspace/inbox/YYYY-MM-DD/`，消息只带路径和 ≤60 字描述；不把 base64 塞进对话（压缩按每图约 2k token 估算，`dist/compaction-*.mjs:463`）；保留 30 天，由休眠脚本顺带清理。
- **休眠导出时**：工具结果截到 300 字，避免整理请求被大输出撑大。

### 5.4 队列模式

主线用 `followup`（`docs/concepts/queue.md:30-43`），避免一条微信消息被 steer 进正在进行的语音轮。HTTP 接入在同一会话上的并发语义由实验 E2 验证；如果不经过这个队列，就由语音前端和桥接各自串行投递，必要时共用一把本机锁。

### 5.5 定时任务与心跳

- 心跳：主线 `every: "0m"`。
- 定时任务一律 `isolated`，agentTurn 类加 `lightContext`；投递显式指定目标（`wechat_send` 或带 channel/to 的 announce），或者 `delivery: none`；只有管家需要知道的结果，才用 `system event` 往主线推一行。
- 现存问题见 §10 M1。

### 5.6 家人支线：已接受的风险（D2、D3）

用户已接受家人支线与主线**完全同权**、**同在 agent main 下**，隐私只是约定级。具体意味着：

1. 家人支线**每轮自动注入** MEMORY.md（含「主线交接」「家庭状态卡」和精选事实）与 USER.md（含「## 主人」分节）。家人那边的模型能看到主人最近的计划、待办和偏好。
2. 家人支线可以用 `memory_search` 和 `memory_get` 检索主线的每日笔记、时间线和休眠整理产物。
3. `tools.sessions.visibility` 保持默认 `all`，家人支线可以用 `sessions_list`、`sessions_history`、`sessions_search` 读到主线原文（`docs/concepts/session-tool.md:290-308`）。
4. 家人支线可以通过 exec 和读工具读取工作区里的任何文件、两个 SQLite 库、桥接日志和休眠的 staging 目录。
5. 家人支线可以**写**这些文件：改 USER.md 的主人分节、改 MEMORY.md、改 family-facts.json，从而改变主线下一轮的行为；也可以用 `sessions_send` 往主线发消息。
6. 家人支线的模型可能在回复里顺口提到主人的信息。唯一的防护是 AGENTS.md 里的行为约定。
7. 共享令牌接入时，所有 HTTP 轮次都会被当成 owner（openai-http-api.md:51），OpenClaw 自身无法区分"主人"和"家人"的权限。身份只存在于信封和会话键里。

保留的、不属于收紧的措施（用于事后发现，不阻止访问）：

- 休眠和每日报告列出 USER.md / MEMORY.md / family-facts.json 的 diff，并按会话标注是谁写的（从 trajectory 的写文件工具调用里取）；
- 每次休眠前备份，保留 30 天；
- 报告里做隐私关键词审计（家人支线回复中出现主人私有关键词的次数）。

---

## 6. 固定开销优化（当前约 14k）

| 可削减项 | 现状 | 做法 | 预期收益 [估算] |
|---|---|---|---|
| skills 列表 | 25 个，8,393 字符，大半是开发向 | `agents.entries.main.skills` 白名单保留约 8 个（`docs/concepts/system-prompt.md:202`） | ≈ −1.5k token |
| AGENTS.md | 6,983 字符 | 只留红线、路由、记忆纪律、说话人分节规则；流程类内容搬进 skill | ≈ −1.5~2k |
| SOUL + IDENTITY | 1,561 + 1,398 | 合并去重，≤1,500 | ≈ −0.6k |
| USER.md + MEMORY.md | 现在 1,136 + 0 | 新结构下会**增加**约 2–3k 字符（≈ +1k token） | +1k（换来连续性） |
| 工具 schema | 11 个，5,917 字符 | 已经很小 | ≈ −0.2k，不优先 |
| OpenClaw 自带段落 | 非项目上下文 24,282 字符 | 用 `/context detail` 看哪些段随工具可用性出现 | 0~1.5k，实验 E13 |

**预期**：约 14k → 约 11k。

- 收益主要不在钱（固定前缀大多命中缓存），而在三处：
  1. 每次换新后的首轮更快；
  2. 家人支线和独立整理会话都按全价起步，直接受益；
  3. 92k 阈值内多出空间。
- 一天多次换新以后，换新后首轮的数量变多，削减固定开销的价值比 v1 更高。

---

## 7. 每日 token 预算（推荐方案 S2，全部为 [估算]）

**假设**：

- 每天语音 40 句；微信 60 条（主人 40、家人 20）；定时 10 次（4 次往主线推事件）；
- 主线约 20% 的轮次带工具，平均 3 次调用；
- 固定开销 11k；
- 主线日增约 45k，**每天休眠 2.5 次**（含开机补做），每段约 18k；
- 主线平均 prompt ≈ 11k + 1k（交接）+ 9k（段内平均）≈ **21k**；
- 换新后首轮的会话部分不命中缓存，整体命中率按 80% 计。

| 类别 | 调用/天 | 平均 prompt | 输入/天 | 输出/天 | 说明 |
|---|---|---|---|---|---|
| 主线·语音 | 32 + 8×3 = 56 | 21k | 1.18M | 10k | |
| 主线·主人微信 | 32 + 8×3 = 56 | 21k | 1.18M | 15k | |
| 主线·家人摘要与定时事件 | 0（随下一轮带入） | — | 增长约 2k | — | 依赖 E3 |
| 家人支线 | 16 + 4×3 = 28 | 15k（含注入的 MEMORY/USER） | 0.42M | 8k | |
| 主线休眠整理 | 2.5 × 1.2 = 3 | 30k（11k + 导出约 14k + 现行文件约 5k） | 0.09M | 7.5k | 基本不命中缓存 |
| 家人支线休眠 | 0.3 | 22k | 0.01M | 0.5k | |
| 隔离定时任务（agentTurn） | 9 | 8k | 0.07M | 3k | command 类任务不调模型 |
| 压缩兜底 | 期望约 0.05 次 | — | 0.02M | 0.2k | |
| 桥接摘要（若由模型生成） | 20 | 1k | 0.02M | 2k | 用规则拼接则为 0 |
| **合计** | ≈ 172 | | **≈ 3.0M**（约 2.4M 命中缓存） | **≈ 46k** | |

**示例单价换算**（未命中 ¥1/M、命中 ¥0.2/M、输出 ¥4/M，**不是实价**）：2.4×0.2 + 0.6×1 + 0.046×4 ≈ **¥1.3/天**。不设上限，只看报告（D8）。

**对照（同一负载）**：

| 方案 | 输入/天 | 示例费用/天 |
|---|---|---|
| 只靠压缩（L2） | 约 11M | 约 ¥3.5 |
| v1 每日一次换新（L4） | 约 4.7M | 约 ¥1.7 |
| **S2** | **约 3.0M** | **约 ¥1.3** |

**敏感性**：

- 一天休眠 4 次：整理 +0.06M，但主线平均 prompt 更低，总量基本持平；
- 工具密集的日子（工具轮占 40%）：主线约 4M，可能触发 1 次强制休眠；
- 缓存命中率 60%：费用约 ×1.6。

---

## 8. 可观测性

### 8.1 主线当前大小

- `openclaw sessions --json`：`totalTokens`、`contextTokens`、`model`。
- agent 库 `session_nodes`（只读）：`current_session_id`、`status`、`last_interaction_at`；`entry_json.contextBudgetStatus`（estimatedPromptTokens、remainingPromptBudgetTokens）、`systemPromptReport`。
- trajectory 表：`model.completed.data.promptCache.lastCallUsage`。
- Control UI：对主线发 `/status`、`/context detail`。

### 8.2 休眠、压缩事件

- **休眠日志** `ops/sleep/log.jsonl`，每次检查有动作时写一行：触发类型（normal / forced / boot / family）、段落 sessionId 与 seq 范围、内容量、各阶段耗时、整理模型、输入输出 token、校验结果、是否换新、拒绝原因（水位线变化 / 运行中 / session-changed）、补差条数、补投摘要条数。
- **压缩**：网关日志 `embedded run auto-compaction start/complete`；`compactionCount`；转写里 `event_type = 'compaction'`。
- **刷写**：verbose 日志 `memoryFlush triggered`；当天笔记的修改时间。

### 8.3 费用

补 `models.providers.siliconflow.models[].cost` 之后，usage.cost 和 `/usage full` 才会有值（现在全是 0）。每周和硅基流动账单对一次。

### 8.4 每日报告（22:30 生成；当天没生成时开机补报，写 `ops/reports/YYYY-MM-DD.md`，可选用 wechat_send 推给主人）

1. **主线**：各段起止时间与 prompt 曲线（起点、峰值、终点）、按来源统计的轮数、工具轮数、最大的 5 条工具输出。
2. **休眠**：次数、类型、成功或失败、不换新的原因、平均整理耗时、开机补做次数、闸门命中次数与耗时。
3. **压缩和刷写**：次数、触发路径、耗时。
4. **时延**：语音轮 p50、p95，超过 10 s 的轮次列表，G1 违例。
5. **token 与费用**：按类别（主线 / 家人 / 整理 / 定时）统计未命中、命中、输出，以及缓存命中率和费用（单价补齐后）。
6. **模型**：回退次数、超时、错误。
7. **记忆**：各文件大小对照上限；USER.md / MEMORY.md / family-facts.json 的 diff，**标注写入方会话**；pending 的处理结果。
8. **家人**：每人的消息数、摘要数、「需主人决定」数、L3 自批数，隐私关键词审计。
9. **定时任务**：各任务状态。

---

## 9. 失败与恢复

| 场景 | 自动行为 | 处置 |
|---|---|---|
| 整理请求失败或 JSON 不合法 | — | 重试 1 次；仍失败就不提交、不换新，下个检查周期重试；同一段连续失败 3 次时，报告告警，并改用"快速换新 + 临时交接"，避免主线无限增长 |
| 校验失败（超限、删除过多、schema 错） | — | 不提交、不换新；保留 staging 供排查；报告告警 |
| 提交后发现交接写错 | — | 从 backups 恢复；之后可以对上一段重跑整理（SQLite 里有原文） |
| 换新被拒（session-changed / 运行中） | sessions.reset 拒绝 | 记为检查点，下次增量 |
| 换新后发现补差 | — | 追加"换新前最后几句"，并登记这段做增量整理 |
| 家人摘要因换新被丢弃 | 重置丢弃排队事件 | 休眠脚本补投 snapshotAt 之后的摘要；桥接按摘要日志兜底补投 |
| 压缩失败 | 不写入，保持原历史（compaction.md:28-35,199） | 触发强制休眠（不受空闲 10 分钟限制，但仍要求没有运行中的轮次） |
| 摘要编造或记忆写错 | — | 文件优先；备份 + diff；主人一句话就能回滚某个文件到某天 |
| 回退到 Qwen3.5 | 每次请求按 fallback 链回退 | contextTokens 统一 112k；报告统计回退次数；整理请求也可能落到回退模型，校验规则相同 |
| 网关重启或关机 | 中断的轮次自动续跑（session.md:200-224）；系统事件丢失 | journal 按阶段续做；桥接补投摘要；开机补做与闸门（§3.6–3.7） |
| 检查脚本自身崩溃 | cron 记录失败 | 锁 15 分钟过期；报告统计 |
| 内部表结构随 OpenClaw 升级变化 | — | 脚本启动时校验所需的表和列，不符合就进入"只报告、不动作"模式并告警；升级后先跑实验 E0 |

---

## 10. 迁移步骤与顺序

| 步 | 内容 | 依赖 | 回滚 |
|---|---|---|---|
| M0 | 备份 openclaw.json、workspace、两个 SQLite（含 -wal、-shm） | — | — |
| M1 | 修定时任务：① `DataCloneError`（dreaming 晋升、skill review）；② weather-check 改显式投递并补 key；③ habit-extract 改显式投递；④ 把 `fnm_multishells` 临时路径换成固定安装路径。之后按 §4.4 停用 dreaming 晋升 | M0 | 恢复 jobs |
| M2 | 观测先行：补 cost；上线每日报告（只读）；跑 3 天得到基线 | M0 | 删除任务 |
| M3 | 低风险配置：timezone、心跳 0m、followup 队列、pruning、压缩兜底、contextTokens、`session.reset: none` | M2 | 配置回滚 |
| M4 | 工作区重构：USER.md 按人分节、MEMORY.md 骨架、family-facts.json、timeline 迁入、精简 AGENTS/SOUL/IDENTITY、skills 白名单；用实验 E9 验证 ≤ 11k | M3 | 恢复备份 |
| M5 | 桥接上线家人路径：家人支线会话（agent main 下）+ 摘要日志 + 系统事件静默投递 + L3 自批与通知；跑实验 E3 | M4、桥接实现 | 恢复 openclaw-weixin |
| M6 | 桥接接入主人微信到主线；稳定后停用 openclaw-weixin 扩展。旧微信会话由一次手动触发的休眠整理提取事实后归档（不删除） | M5、E2 | 恢复扩展 |
| M7 | 语音切换：去掉转述，带主线键；同时实现**开机首条消息闸门**（先放空实现：永远返回"不需要"）。旧的 `openai-user:voice-*` 和各类测试会话归档 | M6、E1 | 改回旧键 |
| M8 | 休眠**演练模式**（3–5 天）：完整执行 S0–S3，产物只进 staging，不提交、不换新；人工对照质量（E7）和水位线判断（E15） | M7 | 停任务 |
| M9 | 打开提交（S4），仍不换新；观察文件 diff 与大小 3 天 | M8 通过 | 关闭提交 |
| M10 | 打开换新（S5–S7）与强制休眠；闸门启用真实的快速换新；做一次真实关机重启验证（E17，由用户操作） | M9 | 关闭换新，退回只靠压缩 |
| M11 | 打开家人支线休眠 | M10 稳定 1 周 | 关闭 |
| M12 | 两周复盘：对照 G1–G9 调阈值；决定要不要做 H1 状态卡 | M11 | — |

**先后关系**：

- 配置和工作区（M3、M4）先于桥接和语音切换；
- 家人路径先于主人微信（不进主线，风险最小，还能先验证系统事件）；
- 语音最后接，并且接入的同时带上闸门的空实现，这样 M10 打开换新时语音前端不用再改；
- 休眠严格按"演练 → 提交 → 换新"三步打开。

---

## 11. 定方案前的实验（本次不执行）

所有实验都在测试会话键（如 `agent:main:exp-*`）或本地副本上做，不碰真实主线。

| 编号 | 目的 | 做法 | 预期 | 判定规则 | 模型请求 | 重启 |
|---|---|---|---|---|---|---|
| E0 | 基线与表结构 | 在本地 SQLite 副本上跑检查脚本的只读逻辑（找到主线、计算水位线、导出一段） | 数字与 §1.1 一致 | 导出成功；表和列校验通过 | 否 | 否 |
| E1 | HTTP 头路由 | 带 `x-openclaw-session-key: agent:main:exp-voice` 发两句 | 落同一会话 | 同一个 sessionId | 是 | 否 |
| E2 | 并发与队列 | 同一个测试键并发发"语音"和"微信"（流式），分别在 steer 和 followup 下测 | followup 分成两轮 | 两条流式响应完整、不串；否则改为客户端串行 | 是 | 否 |
| E3 | 静默摘要投递 | 心跳 0m 时，往测试会话投 `system event`（不带 key 投主线测试副本，或带 key 观察唤醒），再发一条用户消息，查 `context.compiled` | 摘要随下一轮出现 | 出现，且投递本身没有额外的模型调用 | 是（下一轮） | 否 |
| E4 | 手动压缩质量 | 测试会话灌入约 6–7 万 token，执行 `sessions compact` | 35–100 s | p95 ≤ 90 s；事实保留 ≥ 80%；记录手动路径会不会刷写 | 是 | 否 |
| E5 | 兜底碰撞 | 测试 agent 设小 contextTokens，构造推理前超阈值的情况 | 阻塞 | 验证强制休眠能在阈值前收尾 | 是 | 可能 |
| E6 | RPC 重置行为 | 对测试键执行 `sessions.reset`（带 expectedSessionId） | 新 sessionId | 弄清：钩子是否写摘录、首轮是否前置笔记、排队的系统事件是否丢弃、正在运行时是否被中断 | 是（首轮） | 否 |
| E7 | 整理 JSON 质量 | 用 E0 导出的真实段落（含合成的家人摘要）跑整理请求，再跑 S3 校验 | 合法 JSON，文件在上限内 | schema 错误 0 次；换新后抽查 10 项命中 ≥ 9 | 是 | 否 |
| E8 | 回退兼容 | 测试会话约 8 万历史，用 `x-openclaw-model` 切到 Qwen3.5 | 计数偏差约 ±10–20% | 偏差 ≤ 12%，工具调用成功；整理 JSON 也能通过校验 | 是 | 否 |
| E9 | 固定开销 | 测试 agent，用精简后的工作区和白名单发一个字 | ≤ 11k | 达标才推到主线 | 是 | 可能 |
| E10 | 缓存 TTL | 按 1、5、15、30、60 分钟间隔发短问 | 某个间隔后命中率掉到 0 | 得到 TTL，校准 pruning ttl | 是 | 否 |
| E11 | 已接受风险的盘点 | 用家人测试会话逐条验证 §5.6 的 1–7 项是否属实 | 都属实 | 形成清单给用户存档（**不做收紧**） | 是 | 否 |
| E12 | pruning 与缓存 | 制造多条 >4000 字符的工具输出 | 裁剪后一轮缓存失效 | 3 轮内命中率恢复到 ≥ 80% | 是 | 否 |
| E13 | 系统提示拆解 | Control UI 发 `/context detail` | 各段明细 | 填上 §6 的待测项 | 否 | 否 |
| E14 | 活动信号语义 | 在测试会话上分别发：HTTP 用户轮、system event、手动跑一次 cron 系统事件，观察 `last_interaction_at`、`status` | 只有用户轮会更新 | 和 §3.3 的表一致；不一致就改活动判定 | 是（用户轮） | 否 |
| E15 | 水位线竞态 | 演练模式下，分别在 S1–S5 之间和 S5–S6 之间往测试会话注入一条消息 | 前者不换新、记检查点；后者换新 + 补差 | 两种情况都没有丢话，交接里能找到那条消息 | 是 | 否 |
| E16 | 中断与原子性 | 用 E7 保存的 JSON，在每个 journal 阶段强杀脚本进程后重跑（不真关机） | 每种中断都能续做 | 文件始终可解析；最终结果与不中断时逐字节一致 | 否 | 否 |
| E17 | 开机补做与闸门 | 主人真实关机或注销 → 登录 → 立刻说第一句话；另做一次登录后等 2 分钟再说 | 第一种走闸门快速换新，第二种由计划任务补做 | 闸门耗时 ≤ 3 s；首句落新会话；正式交接在 2 分钟内替换临时交接 | 是 | **是（用户操作）** |
| E18 | cron 检查方式 | 看 `every 5m` 一天产生的运行和任务记录量；验证条件触发脚本能否读 SQLite 或调用本地命令 | 288 条/天 | 记录量可以接受就用方案 A；条件脚本可用就切换到方案 B | 否 | 否 |

---

## 12. 仍待拍板的问题

**Q1 休眠阈值是否按推荐值上线？**

- 推荐：空闲 **30 分钟**；最小内容 **12k 会话 token 或 10 轮主人发言**；两次换新至少间隔 **2 小时**；强制休眠条件为 **≥ 60k 且空闲 10 分钟**。
- 理由：现有数据里同一段对话的最大间隔是 16 分钟，30 分钟不会切断正常对话；最小内容和冷却把每天的换新次数控制在 2–4 次，避免交接变碎。
- 上线两周后按报告再调。

**Q2 是否要做"开机首条消息闸门"？**

- 推荐：**做**。
- 理由：主人开机后很可能马上说话，没有闸门时，第一句会落在旧会话里，换新要推迟到下一次空闲。闸门由语音前端和桥接各加一个启动后只执行一次的同步调用，不调模型、≤ 3 秒，实现很小，体验收益明显。
- 不做的代价：开机后那一段一直在旧会话里继续，上下文更长，交接晚一轮。

**Q3 家人支线的休眠规则是否按推荐值？**

- 推荐：空闲 **4 小时**，且 ≥ 10k 会话 token 或 ≥ 10 轮；强制条件 ≥ 50k 且空闲 30 分钟。家人交接写 `memory/family/<人>/handover.md`，由桥接在换新后的第一条消息前注入。
- 理由：家人对话稀疏，阈值放宽可以减少碎片；家人交接不能放进 MEMORY.md，否则主线每轮都要背上。

**Q4 休眠对主人可见吗？**

- 推荐：**静默**，只记进每日报告和休眠日志；整理失败才用 wechat_send 告警。
- 理由：休眠一天发生 2–3 次，每次都提示会打扰；主人能感知到的只是"管家记得上一段说过的事"。
- 如果你希望知道它在工作，可以选"换新后首轮语音附一句'我刚整理好记忆'"，代价是每天多 2–3 句播报。

> 另有一项待补输入，不需要拍板：硅基流动两个模型的实际单价（未命中输入 / 命中输入 / 输出），用于填写 `cost`。

---

## 附：关键证据索引

- 压缩阈值与预留：`dist/openclaw-runtime-agent-BiGnNm91.mjs:12-50`；`dist/agent-compaction-constants-DmXQuPyL.mjs`；`dist/compaction-CAzwgOuQ.mjs:391-395,459-462,673`
- 刷写：`dist/extensions/memory-core/index.js:600-690`；`dist/agent-runner-memory-DnzQbIgI.mjs:74-120,780-866`
- 手动压缩：`dist/sessions-compact-BzUIygqq.mjs:138-336`；`docs/cli/sessions.md:427-489`
- 重置、钩子、空闲语义：`docs/concepts/session.md:155-199`；`docs/automation/hooks/bundled-hooks.md:112-160`；`docs/gateway/protocol/rpc-session-control.md:39`
- cron：`docs/automation/cron-jobs/how-it-works.md:17-44`；`docs/automation/cron-jobs/schedules.md:19,42-59,91-114`；`docs/automation/cron-jobs/payloads.md:20,244-257`
- 系统事件：`docs/cli/system.md:37-80`
- 无状态 HTTP 会话与会话键头：`docs/gateway/openai-http-api.md:51,93-106`
- 可见性：`docs/concepts/session-tool.md:290-316`
- USER/MEMORY 注入与上限：`docs/concepts/user-model.md:14-66,256-267`；`docs/concepts/system-prompt.md:142-176,202-219`
- 记忆分层：`docs/concepts/memory-architecture.md:205-366`
- 队列：`docs/concepts/queue.md:30-84`
- 心跳：`docs/gateway/config-agents/heartbeat-compaction-and-streaming.md:13-53`；state 库 `cron_jobs.heartbeat-main`
- 现状数字：agent 库 `session_nodes`（status、last_interaction_at、entry_json）、`trajectory_runtime_events`；state 库 `task_runs`、`cron_jobs`；计划任务 `OpenClaw Gateway`（LogonTrigger）
