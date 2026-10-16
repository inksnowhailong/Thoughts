# 思绪（Thoughts）一代调研报告 —— 给二代「家用语音管家 → 思绪」的血缘与移植建议

- 日期：2026-10-10
- 性质：只读调研。除本文件外，没有改动任何文件。
- 调研范围：
  - 一代源码 `D:/coding/inksnow/Thoughts/`，其中 `thoughts-plugin/` 是现行版本，`.omc/plans/` 是早期计划，`.cursor/.thoughts/` 是 Cursor 时代的旧实例，`docs/` 是衍生设计稿。
  - 已部署的副本与真实数据：`C:/Users/moxue/.thoughts/`（runtime 和实例数据）、`C:/Users/moxue/.claude/skills/thoughts*`（已部署的 skill）、`C:/Users/moxue/.cursor/agents/thoughts-subconscious.md`（Cursor 潜意识子 agent）、`C:/Users/moxue/.cursor/backups/skill-migration-20261008-155246/skills/thoughts*`（已经从 `~/.cursor/skills` 移除的 Cursor 版 skill 备份）、`C:/Users/moxue/.cursor/plans/thoughts_*.plan.md`、`C:/Users/moxue/.claude/projects/D--coding-inksnow-Thoughts/memory/`（开发期经验笔记）。
  - 二代文档：`2026-10-09-roadmap.md`、`2026-10-09-context-plan.md`、`2026-10-10-p21-plan.md`，以及 `work2/AGENTS.md`（二代工作区现状）。
- 路径简写：下文 `P/` 指 `D:/coding/inksnow/Thoughts/thoughts-plugin/`，`R/` 指 `P/runtime/`，`I/` 指 `C:/Users/moxue/.thoughts/instances/`。

---

## 0. 一页结论

1. **一代是什么**：寄生在 AI 编程宿主（先是 Cursor，后是 Claude Code）里的「主动陪伴人格」。形态是一个 Claude Code plugin，由 3 个 skill、2 个 hook 和一个零依赖的 Node runtime 组成，另有一个 Cursor 背景子 agent 负责潜意识。它会定时主动开口，在你说话时注入人格与心情，并在后台把你说过的话熔成一份散文体用户画像。它不是独立程序；「常驻 daemon + 开机自启」的版本做过，后来被砍掉了。
2. **最值钱的是机制和理念，不是代码**：
   - 热度择时模型：零 token 的确定性闸门决定「什么时候说」。
   - PAD 三轴情绪：落实为「相对人格基线的偏离 → 可观测的行为参数」。
   - 散文画像原地重炼：熔，不是摞。
   - 人格语料区：真实说过的原话，比形容词更能约束声线。
   - 反自报设定、反机制播报的底线。
   - 提示词模板化：行为指令活在可热更的模板里。
3. **和二代的契合点**：二代 P4 的「休眠整理」本质上就是一代的「潜意识」，只是换成了更可靠的触发方式（空闲 + 水位线）和更严格的提交方式（暂存、校验、原子替换）。一代的潜意识提示词和铁律可以几乎原样迁进 P4 的整理提示词。这是性价比最高的一刀。
4. **最大的坑：隐私与体积**：
   - 一代的真实画像（`I/大师/user-portrait.md`，16.8KB）里有大量很私密的内容：收入焦虑、储蓄目标、原生家庭、职场处境。而二代的 USER.md 和 MEMORY.md 会注入家人支线，家人也能读到工作区里的任何文件（context-plan §5.6，已接受的风险）。
   - 二代对 USER.md 有 ≤3,600–4,000 字符的硬上限，SOUL 和 IDENTITY 合计 ≤1,500。
   - 所以一代数据只能「蒸馏后导入」，而且要逐条过隐私筛。不能整份搬。
5. **血缘建议**：
   - 二代沿用一代的价值观底线和说话规约，在 SOUL.md 末尾保留「语料——我真说过的话」一节。
   - 主人画像通过一次性的「认祖整理」导入：USER.md 主人分节放指令化的精华，全文是否带去家里那台电脑由主人决定。
   - 一代的「实例名」（大师、大痔、烤色）可以作为二代人格的名字来源。
   - 颜文字「滴答脸」移到托盘和面板上，当二代的「心电图」。

---

## 1. 初代是什么

### 1.1 定位

一个「有自己脑子的 AI 伙伴」：会主动开口、有心情、记得你，而且会慢慢更懂你。它和二代管家的根本区别在于：**一代的核心是关系与陪伴，二代的核心是办事与家庭**。一代后来的「大师」实例偏向「把主人往架构师方向拽的导师」，「大痔」实例是「一条沟里蹲着的毒舌同类」，更早的「烤色」实例是「有观点的信息伙伴」。

### 1.2 形态演进（三代内部迭代，均已有落盘证据）

| 阶段 | 宿主 | 形态 | 证据 |
|---|---|---|---|
| v0 计划（2026-04） | Claude Code | 双 SKILL、bash hook 加 jq 注入人格、Cron 用 Haiku 整理记忆。更早还有 Tauri + OpenClaw 方案，已被否决 | `.omc/plans/thoughts-mode-skill.md`、`.omc/plans/user-profile-onboarding.md` |
| v1「烤色」（2026-05） | Cursor | 专用 chat，用 stop hook 加 shell-as-timer 做永续主循环。潜意识是 Cursor 背景子 agent，记忆是 HMO-lite 分层（active/index/sources），心智里有 candidateQueue 预生成候选，还有 7 维风格蒸馏和动态环境权限 | `~/.cursor/backups/.../skills/thoughts/SKILL.md`、`~/.cursor/agents/thoughts-subconscious.md`、`~/.cursor/plans/thoughts_redesign_2e96475d.plan.md`、`.cursor/.thoughts/instances/烤色/` |
| v2 daemon（2026-06 初） | 平台中立 | 常驻 Node daemon 自持定时器，可插拔后端（claude -p / cursor-agent / API），开机自启脚本 | `R/README.md`（文档仍描述 daemon）、`R/install/*`、`~/.thoughts/daemon.json` |
| **v3 现行**（2026-06-11 起） | Claude Code plugin | **一条 recurring Claude Cron 当「哑节拍」，加热度模型闸门**。UserPromptSubmit hook 注入人格与心情，Stop hook 冲洗通知，第二条 Cron 跑潜意识 | `P/skills/thoughts/SKILL.md`、`R/cli.mjs`、`P/hooks/hooks.json` |

演进背后的教训，都记录在 `~/.claude/projects/D--coding-inksnow-Thoughts/memory/` 里：
- ScheduleWakeup 自续链会吞掉正文，所以改用 Cron（`schedulewakeup-text-after.md`）。
- 脱离 chat 的 daemon 被砍掉，原因是宿主内的会话本身就是最好的「主意识」。
- 烤色那套「候选队列 + 风格蒸馏」太重，回归到「现场生成 + 确定性闸门」。

### 1.3 运行方式（v3）

```
/thoughts（skill）
  ├─ 同步 runtime：plugin 的 runtime/ 整体覆盖 ~/.thoughts/runtime/（代码与数据分离，instances/ 不动）
  ├─ 选/建实例 ──(新建)──> /thoughts-onboarding 心理学式访谈 → 静默生成画像和人格
  ├─ 绑定项目：~/.thoughts/active.json 写 {项目路径: 实例名}
  └─ 建两条 Cron（session-only，关掉 Claude Code 即失效，7 天过期）
       ① 心跳 */4（或 profile.cadence.tickMinutes）：
          prompt「[思绪·心跳]」→ Bash: cli gate <实例>
              QUIET / SILENT <原因> <颜文字>  → 空回合 / 原样回显颜文字
              SPEAK <hot|warm|cold> + 完整开口指令（由 prompts/speak.md 模板填充）
                  → 读 persona/portrait/raw/memo → 生成 → cli record-spoken（stdin）→ 正文作为最后一步说出
       ② 潜意识 */21：cli once <实例> --kind=subconscious（raw 为空就秒退，0 token）

用户每次在绑定项目里说话 → UserPromptSubmit hook（R/hooks/inject.mjs）
  ├─ 加热（bumpHeat），把 nextSpeakAt 只拉近；清零无回应计数；内容反馈加分
  ├─ 原话追加进 memory-raw.md（潜意识的唯一原料）
  ├─ energy +0.07（有人理你，话头就起来了）
  ├─ 注入：散文人格 + [底线] + 心情行（renderMood）+ [潜意识备忘] + 未读 outbox 原样重现
  └─ 若开口闸已到点：提示在回复末尾顺势多说一句（事件驱动开口）

回合结束 → Stop hook（R/hooks/flush-notify.mjs）
  └─ 读 transcript，确认最后一条 assistant 消息是纯文本后，才发系统通知（杜绝幽灵通知）
```

---

## 2. 核心功能清单

### 2.1 记忆体系

一代的记忆体系有两套设计：v1 是结构化的 HMO-lite，v3 是散文化的重炼。**v3 是对 v1 的有意简化**，两者都值得二代参考。

**A. v3 现行：raw → 散文画像（每个实例目录 `I/<实例>/`）**

| 文件 | 做什么 | 谁写 | 怎么实现 |
|---|---|---|---|
| `memory-raw.md` | 用户真实说过的话，逐字记录：`- [北京时间] 用户：…` | inject hook 追加 | `R/hooks/inject.mjs` 第 109–118 行；斜杠命令和 `[思绪·` 开头的调度消息不进入 |
| `user-portrait.md` | **散文体用户画像**，写「这个人是谁」，加一节「碰了就假的雷区」 | 潜意识原地整份重写 | 提示词见 `R/core/prompts.mjs` 第 30–39 行。按 1–10 打重要度，≥4 才影响画像；被新证据推翻的旧判断直接删；不写 changelog；约 ≤120 行 |
| `persona.md` | **散文体人格**：我是谁 / 我怎么看世界 / 我对你是什么 / 我怎么说话 / 红线，加「语料——我真说过的话」8–15 条 | onboarding 生成，潜意识缓慢演化 | 只有用户给出明确的人格反馈时才微调（`prompts.mjs` 第 41–43 行）。语料从 `mind-state.recentMessages` 里吐故纳新 |
| `memory-consolidated.md` | 只放一行 `[潜意识备忘]`，1–2 句写用户此刻的状态 | 潜意识整份重写 | hook 只取最后一条 `[潜意识备忘]` 注入（`inject.mjs` 第 147–152 行） |
| `mind-state.json` | 心情（PAD 三轴）、线索 threads、跨域连线 bridges、模式权重、最近说过的话 | 潜意识 + hook + record-spoken | 见 §2.3 和 §2.4 |
| `loop-state.json` | 节奏状态：heat、heatAt、nextSpeakAt、lastUserAt、consecutiveNoReply、noReplyByMode、lastSpoken | hook + gate + record-spoken | 见 §2.2 |
| `profile.json` / `personality.json` / `permissions.json` | **只放要被代码解析的参数**：作息、节奏旋钮、人格名、颜文字、通知开关 | onboarding + `cli config` | 「散文给模型、JSON 给代码」的分离（`R/core/onboarding.mjs`、`R/persona.mjs`） |
| `activity-log.jsonl` / `outbox.jsonl` | 行为日志、主动消息记录 | record-spoken、daemon 时代 | `R/core/store.mjs` |

**B. v1 HMO-lite（烤色，`.cursor/.thoughts/instances/烤色/`，定义见 `~/.cursor/agents/thoughts-subconscious.md`）**

- 三层加证据：
  - `memory-active.json`：当前最该影响行为的 7–30 条，每轮注入。
  - `memory-index.jsonl`：长期索引，每行一条，字段有 `{id, type: preference|boundary|interest|feedback|discovery, content, tier 1/2/3, importance, recallCount, tags, sourceIds}`。
  - `memory-sources.jsonl`：原文证据，字段 `{id, quote, derivedMemoryIds}`，用来防止总结漂移。
  - `memory-consolidated.md`：人类可读的摘要，**不作为主记忆源**。
- 规则：
  - 写入白名单：稳定偏好、边界、兴趣、对主动消息的反馈、用户说「记住」。
  - 禁写黑名单：当前代码进度、一次性 bug、密钥、没有证据的敏感推断、寒暄。
  - **规则预算**：同类边界合并，active 记忆保持 7–10 条，不要把每次负反馈都堆成新禁令。
  - **短期偏好写成 shortTermDownrank，不写成长期禁忌**。例如「战争话题只是最近不想听」。
- 参数分三层更新速度：
  - 短期状态，每轮可调：mood、energy、shortTermDownrank。
  - 中期策略，结合多轮反馈：thread energy、modeWeights、toneBias。
  - 长期人格，只有明确反馈才动：traits、boundaries、coreStance。

**C. 整理（潜意识）**

- v3：`R/core/subconscious.mjs`。
  - raw 里没有 `- ` 条目就整轮跳过，这是省 token 的头号刀。
  - agentic 后端（claude -p）的 cwd 必须是实例目录，否则 Claude Code 沙箱会静默拒绝写入。代码注释记录了这次踩坑。
  - 非 agentic 后端由 runtime 把数据喂给模型，再自己落盘。
  - 模型固定用 Sonnet。
  - 频率：Cron */21。另有 `R/core/decide.mjs` 按活跃度给出 10/20/60 分钟档位，但现行 Cron 版没有用上。
- v1：子 agent「睡眠期编辑部」，除了整理，还预生成 3–8 条候选主动内容，并维护风格蒸馏。

**D. 画像**

- onboarding（`P/skills/thoughts-onboarding/SKILL.md`）：心理学式一问一答，共 8–15 轮。
  - 方法：开放式问题、反映式倾听、由浅入深；不用选择题，不评判，不灌鸡汤。
  - 覆盖维度：热情、职业及真实感受、日常、家庭、压力、价值、陪伴偏好。
  - 结束后**静默生成**画像和人格，绝不把分析念给用户听。
  - 人格「从用户画像里长出来，是补他那道缺口的形状」。onboarding 里有一张推断表：高压且反感鸡汤 → 清醒务实、黑色幽默；孤独 → 温暖在场……
  - 按人格底色给 PAD baseline 个性化，避免所有实例共用一张出厂脸。
- 真实产物：`I/大师/user-portrait.md` 写成了一篇约 134 行的长文（已经超出自己定的 120 行上限），「碰了就假的雷区」共 36 条。质量很高，是一代最宝贵的数据资产。

**E. 潜意识**

- 现行 v3 的潜意识是 runtime 拉起的 `claude -p` 进程。Cursor 时代是背景子 agent `thoughts-subconscious`（`is_background: true`）。
- 职责（`prompts.mjs` 第 15–67 行），共四项：
  1. 重炼用户画像。
  2. 缓慢演化人格，并维护语料区。
  3. 写近况备忘。
  4. 维护心智：PAD 三轴、threads、bridges、工程目录锚点。

### 2.2 节奏与主动性：什么时候说

- **热度模型**（`R/core/heat.mjs`，纯函数、零依赖）：
  - `heat ∈ [0,1]`。用户每说一句，`heat = min(1, 有效heat + bump × 分量系数)`。分量系数 = 字数/80，钳在 [0.5, 2]：「嗯」算半量，长倾诉算双倍。
  - 半衰期 25 分钟，**衰减在读取时计算**，不需要后台进程。
  - `interval = max(min 4 分钟, max 75 分钟 × (1-heat)²) × 抖动 0.8~1.2`。
  - **用户消息只能把 nextSpeakAt 拉近、永不推远**；说话本身不加热（自嗨不算热度）。
  - 无回应退避：`× 1.6^min(3, 连续无回应数)`。
  - 被吞掉的说话回合走不到 record-spoken，nextSpeakAt 就不推进，下一跳自动重试，天然自愈。
- **gate 只守两条红线**（`R/cli.mjs` 第 123–193 行）：深夜静默（`habits.quietHours`，按北京时间）；没到 nextSpeakAt。其余内容判断全部交给模型现场决定，「不在 gate 里堆规则」。
- **事件驱动开口**：用户发消息时如果闸已经到点，hook 让模型回完话后顺势多带一句，热聊响应从「等下一跳」降到 0（`inject.mjs` 第 88–94 行、第 181–185 行）。
- **对账自愈**：hook 偶尔漏听用户消息。心跳轮看得见对话，发现用户真实消息晚于 lastUserAt 时会补打 `ping`（SKILL.md 心跳 prompt 第 1 步）。
- **五个旋钮可配**：`profile.cadence = {minIntervalMs, maxIntervalMs, halfLifeMs, bump, tickMinutes}`，通过 `cli config <实例> --min=分钟 …` 查看或修改。CLI 会说明哪些项立即生效、哪些需要重建 Cron（`cli.mjs` 第 252–297 行）。
- 主人的真实反馈：
  - 不要因为没回就闭嘴（`thoughts-proactive-no-backoff.md`）。
  - 冷场时去外面找有趣的东西，别翻旧账（`thoughts-cold-speak-behavior.md`）。
  - 午休 12:30–14:00 要静默，但 gate 只支持一个静默段，这个需求**没实现**（`dashi-lunch-break.md`）。

### 2.3 话题策略：说什么

- **热度分层决定内容方向**（`cli.mjs` 第 161–165 行）：
  - hot：贴着他最近的话题接茬、追问、抬杠。
  - warm：半贴半飘。
  - cold：聊自己惦记的线索或外面的新鲜事，和他最近说的无关。
- **四种模式加多样性惩罚**（`R/core/mind.mjs`）：
  - 四种模式：discovery、ambient、casual、reflection，各有一句 MODE_HINT。
  - 和上一条同模式 ×0.3；最近 3 条里出现 ≥2 次 ×0.4；再乘一点随机抖动。
  - discovery 的权重被刻意调低，因为它最容易退化成刷屏的「惊奇生成器」。
- **内容反馈闭环**：
  - 上一条主动消息在 2 小时内被回复，对应模式权重 ×1.15，上限 0.5（`inject.mjs` 第 135–141 行）。
  - 同一模式连续 3 条没人回，权重 ×0.7，下限 0.05（`cli.mjs` 第 207–220 行）。
  - 只罚「连续」，避免把用户忙误判成嫌弃。
- **加料区**：
  - cold 和 warm 时优先说「连线存货」bridges：潜意识预埋的跨域洞察，两端必须都来自用户原话或真读过的东西。
  - hot 时优先接 threads 里用户悬着的 `open_question`。这是回复率最高的开口素材。
- **own-thought-first**（v1 烤色确立）：想法来源优先级为长期线程 > 当前心情 > 世界观察 > 稳定审美 > 联想漂移。目的是不被最近一句话绑架（近因驱动），让它有主体性。
- **人格自己的摊**：`threads owner:"self"` 记录人格自己在折腾的破事，会进展、会翻车、会烂尾。还有「情绪残留线」，例如「他昨天说 X 是垃圾，我还记着」，2–3 轮后翻篇。
- **写法规约**（`R/prompts/speak.md`）：
  - 长度掷骰子：三成 ≤20 字、一半中等、两成放开写长。
  - 话说一半、烂尾都合法；论文腔杀光；地摊味优于书面味。
  - 禁「观察 + 反转 + 金句落点」模板，禁「刚瞅到/刚发现」开头。
  - 和 recentMessages 去重。

### 2.4 人设与提示词结构

- **注入结构**（`inject.mjs` 第 160–185 行），依次为：
  1. `[思绪模式 — 实例]`，后接「此刻你就是下面这个人，不是在扮演」。
  2. 散文人格。
  3. `[底线]`：绝不描述自己是什么样的人，不把形容词当台词。
  4. 心情行。
  5. 潜意识备忘。
  6. 待重现的 outbox 消息。
  7. 收尾要求：「颜文字等口头习惯以语料为准，不是任务，不用凑」。
- **心情渲染**（`mind.mjs` 第 67–103 行，renderMood）：
  - PAD 三轴相对人格 baseline 偏离 ±0.12 时判定状态，不用绝对刻度。同一个 0.7，对丧底色的人格是罕见的亢奋，对阳光人格是日常。
  - 每个状态等于「语气 + 硬行为参数」。例如「蔫：本条 ≤20 字，或只回个颜文字；不抛问题」。
  - 附带潜意识写的「你此刻这样，是因为……」作为因果叙事。
  - 末尾的反复述护栏不可删：不许把数字和状态名念出来。
- **三轴脾气不同**（`prompts.mjs` 第 49–58 行）：
  - energy 该起落：用户开口就抬，没人理就回落。
  - valence 和 control 是底色，要「黏」：有真信号就动 ±0.2 并钉住，连续几轮都没料时才缓慢回归。
  - 目的是防止心情永远被拉平成一张死脸。
- **SPEAK 指令模板化**（`R/prompts/speak.md` + `cli.mjs` 第 152–192 行）：
  - Cron prompt 只有 5 行路由；真正的行为指令放在模板里，由 gate 把状态解析好后填进占位符：`{{TIER}} {{TIER_HINT}} {{MOOD}} {{MODE}} {{MODE_HINT}} {{EXTRAS}} {{PERSONA}} {{PORTRAIT}} {{RAW}} {{MEMO}} {{RECENT}} {{INSTANCE}}`。
  - **改模板，下一跳就生效**；模型只看到命中的那一种状态，不用读全表自查。
- **反机制播报**：开场像人重新上线一样，至多说一两句（SKILL.md 第 5 步）；告别也只一句（thoughts-stop）。不报实例名，不提心跳、Cron、文件。v1 的 `forbiddenUserFacingTerms` 把「闹钟、候选队列、潜意识、mind-state、hook、subagent」列为对用户禁用的词。

### 2.5 用户画像

见 §2.1 的 D 部分。补充三条设计要点：
- 画像是「当前的他」，不是历史；被推翻的判断直接删。
- 把零散事实综合成洞察（「A 和 B 共同说明他是怎样的人」）。
- **工程目录锚点**：只记路径和一句定位；谈代码前先去读，「宁可现读，不凭记忆碎片猜」。

### 2.6 状态机与流程

- **心跳状态**：深夜 → `QUIET/SILENT quiet_hour`；未到点 → `SILENT not_due <颜文字>`；到点 → `SPEAK <tier>`。`quietTicks=true` 时静默跳输出空回合，保持对话区干净（大师实例就开了这个）。
- **情绪状态**：8 种语气桶，在 renderMood 里定义。另有 7 个颜文字心情桶（`R/core/kaomoji.mjs` 中的 moodBucket）。
- **通知状态**：`record-spoken --defer-notify=1` 只写 `pending-notify.json`；Stop hook 验证正文已经渲染后才发通知，「先删后发」防止重复。
- **onboarding 流程**：起名 → `cli init` 建骨架 → 访谈 → 静默写入画像、人格、JSON、备忘。

### 2.7 可视化与可编辑界面

- **一代没有图形界面**，只有：
  - CLI：`status`、`config`（查看和修改节奏）、`outbox`（最近的主动消息）。
  - 文件本身：Markdown 散文加少量 JSON，都是人类可读、可直接编辑的。
- 最接近「可视化」的是**滴答脸**（`R/core/kaomoji.mjs`）：静默跳按「处境（候场/溜达/凉着/深夜）× 心情桶」挑一张颜文字回显，有约 2% 的彩蛋池。时间线就是心电图。
- 衍生设计稿 `docs/tvs情绪记忆HUD-skill设计文档.md` 想把 PAD、颜文字和散文记忆抽出来，渲染进 HUD，显示成一行「`(-̀ω-́)✧ 笃定 · 正在啃 W-TinyLFU 分层`」。这份稿子没有实现，但它就是二代面板「心情卡」的现成草图。

---

## 3. 设计亮点（值得继承）

1. **确定性闸门，加 LLM 只负责表达**。「该不该说」由纯 JS 决定，一个 token 都不烧；「说什么」交给模型。gate 只守两条红线，不堆规则。二代也是一样的思路：休眠检查脚本只用 command payload，不调模型。
2. **衰减在读取时计算**。heat 不需要后台进程按时衰减，只存「值 + 时间戳」。这非常适合「宿主会关机」的二代。
3. **只拉近、不推远**。用户的活动只会让下次开口更早，系统自己的行为不会让自己更热。这个不对称规则简洁又反直觉地正确。
4. **相对基线的情绪，并且映射到可观测行为**。情绪靠行为被感觉到（字数、是否抛问题、是否补刀），不靠形容词。三轴采用不同的回归策略，避免心情被拉平。
5. **语料胜过形容词**。「一打真实例句对声线的约束力，远大于两千字性格描述。」语料区由潜意识用真实输出滚动替换。
6. **熔，不是摞**。画像原地重写，被推翻就删，不留 changelog，有长度上限。对比之下，二代 AGENTS.md 的默认做法是「append 到 daily notes，每隔几天折叠一次」，一代的做法更有灵魂。
7. **散文给模型，JSON 给代码**。人格和画像是叙事，机器参数单独放。
8. **规则预算**（来自 v1 的教训）：负反馈要合并成更少、更稳的原则，否则越修越像客服。
9. **短期偏好 ≠ 长期禁忌**（shortTermDownrank）。
10. **提示词模板化、热更新**。行为指令活在模板里，「改文件，下一跳生效」，入口 prompt 只做路由。这正是二代「面板编辑提示词」需要的底座。
11. **零依赖、可降级**。外部能力只能「有则用、无则降级」，安装期失败也不阻断运行（`thoughts-zero-dep-principle.md`）。
12. **不用假信号污染真实实例**（`no-fake-signals-in-real-instance.md`）。测试用临时实例，用完即删。二代的「演练模式」和测试会话键是同一个理念。
13. **反机制播报、像人一样上线下线**。二代 context-plan 的 Q4 推荐「休眠静默」，两者完全一致。

### 一代的已知缺陷（不要继承）

- **潜意识依赖会话内 Cron，实际已经停摆**。`I/大师/memory-consolidated.md` 最后更新是 9/14，`memory-raw.md` 里还有 25 行没被消化（最晚到 9/24），而且有重复行（hook 触发了两次）。根因：Cron 是 session-only 的，并且 7 天过期。二代改成 OpenClaw cron 加开机补做，从根上解决了这个问题。
- **心情会卡死**。`I/大师/mind-state.json` 的 energy 卡在上限 0.92：hook 每条消息都 +0.07，回落却要靠潜意识，而潜意识停了，所以 mood 停在 neutral、threads 为空。**教训：回落和衰减必须是确定性代码**（像 heat 那样在读取时衰减），不能指望 LLM 去回归。
- **并发写入没有加锁**。inject hook、record-spoken、潜意识都会对 mind-state 和 loop-state 做「读-改-写」。
- **长度上限只写在提示词里，没有代码校验**。画像已经超过 120 行。二代的 S3 校验（大小上限加删除比例）正好补上了这一点。
- **多段静默时段不支持**。午休需求没有落地。
- **文档漂移**。`R/README.md` 还在描述已经删除的 daemon；部署拓扑有三份副本，需要手动同步（`thoughts-deploy-topology.md`）。

---

## 4. 移植建议

二代各部分的对应关系：
- **主线**：`agent:main:main`。
- **工作区**：AGENTS.md、SOUL.md、IDENTITY.md、USER.md、MEMORY.md、memory/。
- **休眠**：P4 的 S2 独立会话整理，输出 JSON。
- **面板**：P2.1，地址 `127.0.0.1:18795/panel`，设置写在 `data/voice-settings.json`，支持热加载。
- **插件**：guanjia-guard（分级、钩子、`before_prompt_build`）。
- **定时任务**：OpenClaw cron 的 command payload。

### 4.1 建议移植（按优先级）

| # | 移植项 | 一代来源 | 落到二代哪里 | 怎么增强 | 优先级 | 工作量 |
|---|---|---|---|---|---|---|
| 1 | **潜意识整理铁律，写进休眠整理提示词** | `R/core/prompts.mjs` 第 15–67 行 | P4 的 S2 整理请求，以及 JSON schema 里的 `userOps`、`memoryFactOps`、`handover` | 照搬：只信用户原话，系统和工具日志一律忽略；按重要度打分，≥4 才影响画像；被推翻就 supersede 并删掉；不写 changelog；超限就提炼；规则预算（同类指令合并）；短期偏好进 handover，不进 USER.md。新增 `openLoops[]`（对应一代 threads.owner=user 加 open_question）合入「主线交接 · 未决」。所有「不得超过」的上限由 S3 代码校验，补上一代的缺陷 | **高** | 0.5 天（只改提示词和 schema，跟着 P4 一起做） |
| 2 | **SOUL.md：散文人格、语料区、底线** | `I/大师/persona.md`、`I/大痔/persona.md`、`inject.mjs` 的 `[底线]`、onboarding 的语料规范 | SOUL.md 与 IDENTITY.md 合并（context-plan §6 本来就要合并，目标 ≤1,500 字符） | 结构：「我是谁（3 行）/ 我对这家人是什么（2 行）/ 怎么说话（语音 ≤2 句、平话为主、不念形容词、不汇报机制）/ 红线 / 语料 6–8 条」。语料要改写成**语音场景**的原话，一代语料偏长，需要压短。休眠时附带 `soulCorpusOps`：从主线助手的真实输出里挑 0–1 句替换最平的一句，由 S3 校验总长 | **高** | 0.5–1 天 |
| 3 | **主人画像迁移（认祖整理）** | `I/大师/user-portrait.md` 全文、雷区 36 条、`profile.json`（quietHours [1,8]）、开发笔记里的午休 12:30–14:00 | USER.md「## 主人」分节（≤2,000 字符） | 一次性跑：把画像全文加烤色的 `memory-active.json` 喂给一个 S2 同构的无状态整理请求，产出指令化条目，形如 `<!-- observed \| status \| src: thoughts-gen1 -->`；雷区转成 `Never/Prefer`。**逐条过隐私筛**，因为家人能读到（见 §5.3）。结果进 staging，在面板里人工确认后再提交 | **高**（血缘核心） | 1 天，外加主人审阅 |
| 4 | **面板：提示词与参数（模板化加表单）** | `R/prompts/speak.md` 的占位符模板机制、`cli config` 的「查看/修改/单位友好/生效时机提示」 | P2.1 面板新增「提示词」「参数」页 | 见 §6 | **中高**（主人明确要） | 2–3 天 |
| 5 | **面板：记忆查看与编辑** | v3 的散文文件、v1 HMO-lite 的 `{id,type,tier,importance,tags,sourceIds}` 加 sources 证据、`[潜意识备忘]` 标记行 | 面板「记忆」页：USER.md、MEMORY.md、family-facts、timeline、每日笔记 | 见 §6。关键是**和休眠互斥**：面板写入要拿同一把锁（`ops/sleep/lock`），或者让 S4 提交前比对哈希 | **中高** | 2–3 天 |
| 6 | **热度择时闸门（主动开口）** | `R/core/heat.mjs` 全部、`cli gate`、`record-spoken` 的退避与反馈、`TIER_HINT` | 新增 OpenClaw cron `every 5m` 的 command 任务 `thoughts-gate`（和 sleep-check 同一模式）。活动信号复用 `lastInteractionAt`，加热由 guanjia-guard 的入站钩子或 SQLite 增量推出。SPEAK 时起一个 isolated agentTurn（`lightContext`）生成内容，**显式指定 channel 和 to，通过 wechat_send 发给主人**（不要依赖「最后路由」） | 二代必须加的约束：主动开口默认只走微信，不出声，语音只用于用户自己设的提醒；作息支持**多段**（深夜加午休）；游戏时静默（P7）；家人不接收主动消息；`maxInterval` 建议拉长到 3–8 小时（对应 AGENTS.md 默认的「超过 8 小时没说过话可以主动」）。nextSpeakAt、heat 的衰减保持读取时计算 | **中**（P3、P4 稳定后） | 2–3 天 |
| 7 | **PAD 心情（确定性衰减版）** | `mind.mjs` 的 renderMood、三轴脾气规则、onboarding 的个性化 baseline | guanjia-guard `before_prompt_build` 每轮注入一行心情（≤150 字），复用 context-plan H1 的注入点。三轴存 `data/thoughts/mood.json` | 修掉一代的缺陷：energy、valence、control 都按半衰期向 baseline **在读取时衰减**；休眠时由整理模型给出 `moodNudge`（±0.2，必须带证据）。行为参数要适配语音（蔫 = 一句话收）。面板提供总开关和幅度上限。对家人的会话可以禁用心情注入 | 中低 | 1–1.5 天 |
| 8 | **滴答脸，移到托盘和面板** | `R/core/kaomoji.mjs` | 面板顶部状态卡，托盘提示文字 | 「处境」从热度改成二代状态：在听 / 静音 / 处理中 / 休眠 / 深夜；心情桶来自第 7 项。纯展示，零 token | 低 | 0.5 天 |
| 9 | **家人入门访谈** | `P/skills/thoughts-onboarding/SKILL.md` 的心理学访谈方法 | 家人第一次在微信说话时走轻量版（3–5 轮），写 USER.md 的家人分节 | 不要静默生成人格（二代只有一个人格）；只收集称呼、偏好、忌口、作息、希望被怎样对待；明确告知会被记住 | 低 | 0.5–1 天 |
| 10 | **内容反馈权重** | `noReplyByMode` 降权加「2 小时内回话加分」 | 第 6 项的一部分 | 模式改成二代语境：提醒类、家庭事务类、闲聊类、外部新鲜事类 | 随第 6 项 | 已含在第 6 项内 |
| 11 | **「像人一样上线」规则** | SKILL.md 第 5 步、thoughts-stop | AGENTS.md 红线一行，加上休眠后首轮的行为 | 换新后首轮不说「我刚整理好记忆」（Q4 静默），也不报机制；有交接就自然接上话头 | 高（成本极低） | 10 分钟 |

### 4.2 不建议移植

| 一代机制 | 不移植的原因 |
|---|---|
| Claude Cron 和 ScheduleWakeup 驱动的会话内循环、`[思绪·` 调度消息路由、对账自愈 | 二代有 OpenClaw cron 和常驻网关，这些都是为了绕开 Claude Code 限制的脚手架 |
| `active.json` 项目绑定、按 cwd 匹配实例 | 二代只有一个主线，靠会话键区分 |
| `record-spoken` 的 stdin 协议、`--defer-notify` 加 Stop hook 防幽灵通知 | 是 Claude Code 渲染的特殊问题（回合末尾不是纯文本就被吞）。二代通过 wechat_send 和 TTS 直接投递。**只保留理念**：「先确认真的送达，再记已说」 |
| `raw` 逐字追加文件 | 二代的 SQLite 转写就是 raw，休眠导出已经覆盖了。再写一份会形成双写和不一致 |
| backends（claude -p / cursor-agent / API）、daemon、install 脚本、BurntToast 通知 | 二代统一走网关的 HTTP 无状态会话 |
| `permissions.json` 环境感知（git_status、dev_servers、windowTitle……） | 面向开发者，和家庭场景无关。游戏检测由 P7 单独做 |
| v1 的 candidateQueue 预生成、7 维 styleEvolution 风格蒸馏 | 一代自己已经弃用：太重，而且「草稿越完整越像表演」。二代费用敏感，主线固定开销还要从 14k 压到 11k |
| cold 层「主动 WebSearch 外部新鲜事」在**语音**里说 | 在家里突然播报新闻是噪声。最多作为第 6 项里的「微信外部新鲜事」低权重模式，默认关闭 |
| 大师的「训练议程 / 思维训练出题」 | 这是写代码时的导师角色，不适合家用管家；而且相关内容会暴露在家人可读的文件里 |
| `persona.md` 全文、`user-portrait.md` 全文直接注入 | 超出二代的字符上限，并且有隐私问题（§5.3） |

### 4.3 建议排期

- 第 1 项和第 11 项随 **P4** 一起做，因为它们只改提示词和规则。
- 第 2、3 项在 **P4 的 M4「工作区重构」** 时一起做。M4 本来就要重写 SOUL、IDENTITY、USER，正好是血缘落地的窗口。
- 第 4、5 项放进 **P2.1 之后的面板二期**。
- 第 6、7 项放在 **P5 之后**，即用满一周、休眠稳定之后。

---

## 5. 「血缘」怎么体现

### 5.1 命名

- **项目名**：二代显示名改为「思绪」（Thoughts），可以叫「思绪 · 二代」或「Thoughts v2」。建议**先只改显示层**：托盘提示、面板标题、桌面快捷方式、文档。
  - 暂时不改 `F:\coding\guanjia` 路径、计划任务名 `GuanjiaVoice/GuanjiaTTS-WSL`、插件 id `guanjia-guard`、端口这些。它们被看门狗、快捷方式脚本、审批白名单、levels.json 硬引用，改名风险大、收益小。
  - 等稳定后，在一个专门的小阶段集中改，例如 `thoughts-guard`。
- **人格名**：一代每个「实例」都有名字，并且名字有意义（「大师」是主人起的，期望是把他往上拽）。建议二代的人格名从一代里继承，或者由主人重新起。命名本身就是血缘仪式。
  - 继承「大师」：导师气质，对家人可能偏冷、偏说教。需要在 SOUL.md 里给家人一个更软的面向。
  - 继承「大痔」：毒舌、丧、「同类」，不适合当全家人的管家。
  - 新名字，并在 SOUL.md 第一段写一句来历，例如「我是从思绪里长出来的」。这既是血缘的延续，也适合家用。**推荐这个**。
- **唤醒词**：二代唤醒词可以在面板里改，按读音容错，规则是 zh/ch/sh→z/c/s。注意：
  - 「思绪」sī xù 会和「时序 / 实需 / 诗序」读音相同。「时序」对程序员是高频词，容易误唤醒。
  - 也不建议用「大师」（日常口语里出现得不少）。
  - 保留「管家」作唤醒词、人格另有名字，是常见做法（就像喊「Hey Siri」，人格叫 Siri）。或者选一个 3–4 字、生僻但好念的名字。

### 5.2 人设延续

- **不变的「骨」**（直接写进 SOUL.md 和 AGENTS.md 红线）：
  - 不灌鸡汤、不说教、不廉价乐观。
  - 不描述自己是什么样的人，不念形容词。
  - 不汇报运行机制，像人一样上线下线。
  - 不自作主张加没要求的内容。
  - 休息时段不打扰，说勿扰立刻安静。
  - 认可要具体、稀疏，不要一个劲夸。
- **会变的「肉」**：
  - 角色从「陪聊、导师」转为「家里的管家」。
  - 回答从长段变成 ≤2 句。
  - 对象从主人一个人变成全家，对家人默认更温和。
- **声线延续靠语料**：从大师语料里挑出「不依赖编程语境」的句式气质，例如：
  - 「过零点了。问句留到明天，我不催。」
  - 「问句不急，饭吃完再想。」
  
  然后按管家场景重写 6–8 条，放进 SOUL.md 的语料区。休眠时再用二代的真实输出逐步替换，人格就会在二代里「长」出自己的声音。

### 5.3 记忆与画像数据迁移

**数据在哪**（都在**这台电脑**上，不在家里那台 `home`）：

| 位置 | 内容 | 体积与状态 | 能否导入二代 |
|---|---|---|---|
| `I/大师/user-portrait.md` | 主人散文画像（最宝贵） | 16.8KB，约 134 行，9/14 更新 | **蒸馏后导入**到 USER.md 主人分节，约 2,000 字符 |
| `I/大师/persona.md` | 大师人格加 12 条语料 | 7.5KB | 取骨架和语料气质，重写进 SOUL.md |
| `I/大师/memory-raw.md` | 9/14 之后 25 行没被消化的原话（有重复） | 13KB | 并入认祖整理的输入（去重后） |
| `I/大师/memory-consolidated.md` | 一行近况 | 很小 | 不需要 |
| `I/大师/mind-state.json`、`profile.json`、`loop-state.json` | PAD（energy 卡死）、模式权重、quietHours [1,8]、cadence | 小 | 只取 baseline 和作息；PAD 当前值不导入 |
| `I/大师/activity-log.jsonl` | 主动消息和潜意识日志 | 98KB | 不导入，最多做统计 |
| `I/大痔/`（persona、portrait、mind-state 含 5 条 threads、outbox） | 第二人格，以及另一份画像 | — | portrait 可以和大师的画像交叉印证；threads 已经过时，不导入 |
| `D:/coding/inksnow/Thoughts/.cursor/.thoughts/instances/烤色/` | HMO-lite：memory-active（10 条高优先，带证据 id）、index、sources；mind-state 含 threads 和 editorialPolicy | 5 月数据 | memory-active 里「偏好 / 边界 / 兴趣」类条目**适合直接转成 USER.md 指令**（例如「不要主动催工作进度」「深夜不打扰」）；它们本来就有 type 和 importance，而且有原文证据 |
| `C:/Users/moxue/.claude/projects/D--coding-inksnow-Thoughts/memory/*.md` | 开发期经验：北京时间、午休、零依赖等 | 小 | 午休 12:30–14:00 转成二代的作息配置；其余是开发规范，不导入 |
| `C:/Users/moxue/.claude/projects/C--Users-moxue--thoughts-instances---/*.jsonl` | 潜意识 `claude -p` 运行的转写 | 6 个文件，约 3.5MB | 不导入 |

**怎么导入**（建议做成二代的一次性任务，**复用 P4 的整理管线**）：

1. 在这台电脑上做**隐私预筛**，由主人决定哪些主题可以进家里那台电脑。画像里的收入和储蓄、原生家庭经济状况、职场冲突、自我怀疑这些段落，主人可能不希望家人读到。二代有一条已接受的事实：家人支线可以读写工作区里的任何文件。
2. 把预筛后的材料喂给二代的「整理」无状态请求，换一个认祖专用提示词，产出 S2 同构的 JSON：`userOps`（主人分节指令）、`memoryFactOps`（长期事实）、`soulDraft`（人格草稿）。每条都带 `src: thoughts-gen1/<文件>`。
3. 结果进 `ops/sleep/staging/gen1-import/`，在面板「记忆」页逐条确认，然后原子提交。备份走同一套流程。
4. 一代的全文画像**不放进二代工作区**。如果主人想保留，可以放在家里那台电脑的工作区**之外**并加密，或者干脆只留在这台电脑上，由主人自己保管。
5. 不迁移的：活动日志、PAD 当前值、threads、outbox。

**格式差异提示**：一代是散文，二代的 OpenClaw user-model 要求指令式（`Always/Never/Prefer` 加 `observed/status` 注释），两者需要调和。建议 USER.md 主人分节采用「开头 2–3 行散文核心（他是谁）+ 指令列表（雷区 → Never）」的混合格式。这样既保留一代「一个能呼吸的人」的长处，也符合二代的 supersede 机制。

### 5.4 界面与语言风格的延续

- **语言**：
  - 沿用一代的写法规约：平话为主、长度有变化、不套「金句落点」模板、不用「刚发现」开头、书面词要嚼成人话。
  - 语音场景里，颜文字不能念，改为两种用法：只在微信文本里偶尔用（一轮最多一枚，大师人格原本的规矩）；在托盘和面板上作为「脸」展示。
- **界面**：
  - 面板顶部放一张「思绪状态卡」：`颜文字 · 心情标签 · 近况一句`。近况一句来自休眠交接的第一行，相当于一代的 `[潜意识备忘]`。
  - 这就是一代 HUD 设计稿（`docs/tvs情绪记忆HUD-skill设计文档.md` §七）的落地版。
- **术语**：二代内部可以继续叫「休眠」，但在面板上可以加一个副标题「潜意识整理中」，作为对一代「潜意识」的致敬。对用户说的话里仍然不提机制。

---

## 6. 二代两个面板需求：一代有哪些可借鉴的设计

### 6.1 「面板里查看和编辑各流程的提示词与参数」

一代可借鉴的设计：

1. **提示词模板文件化，加上占位符解析**（`R/prompts/speak.md` + `cli gate`）：
   - 入口（Cron prompt）只做路由，行为指令全部放在模板文件里。
   - 由确定性代码把状态解析成具体值，再填进 `{{占位符}}`。
   - **改文件，下一跳生效**，不需要重建任务。
   - 二代建议：
     - 把各流程的提示词都抽成文件，例如 `F:\coding\guanjia\prompts\`，或者放在工作区 `prompts/` 下。候选流程：休眠整理、开机快速交接、家人摘要、家人休眠、主动开口（第 6 项）、认祖整理、唤醒应答语。
     - 面板「提示词」页三栏布局：左边列文件，中间编辑，右边**用当前真实状态渲染出来的预览**（把占位符替换后的最终文本，相当于一代 gate 输出的那段 SPEAK 指令）。
     - 保存时原子写入并备份，旁边标注「下次调用生效」。
     - 占位符白名单由代码定义；模板里出现未知占位符时，面板标红。
2. **参数「默认值 + 覆盖值」两层，缺省就回退**（`heat.mjs` 的 `cadenceConfig`）：
   - 只采纳合法数值，非法值回退到默认值；没有配置时行为不变。
   - 二代 `settings.py` 已经有校验和热加载。建议面板每个参数同时显示「当前值 / 默认值 / 是否被覆盖」，并提供「恢复默认」按钮。
3. **单位友好，并且标出生效时机**（`cli config`）：
   - 用户看到的是分钟，落盘存毫秒。
   - 明确告诉用户哪些项立即生效、哪些需要重建或重启（例如 `tick` 改了要重跑 `/thoughts`）。
   - 二代建议给参数打徽章：「立即」（voice-settings 热加载）、「下次休眠」（休眠阈值）、「需重启语音」、「需重启网关」（openclaw.json 里的项，面板只读展示，或者走单独的确认流程）。
4. **「散文给模型、JSON 给代码」分离**：面板也分成两种编辑器。SOUL、USER、MEMORY 用 Markdown 编辑器；数值和开关用表单。不要让主人在表单里改人格，也不要在 Markdown 里改阈值。
5. **一代没做、二代要补的**：
   - **版本与 diff**：每次保存都留一个版本，可以一键回滚。
   - **字符预算条**：SOUL、IDENTITY ≤1,500，USER ≤3,600，MEMORY ≤6,000。面板实时显示，并估算 token，因为固定开销是二代的核心指标。
   - **安全**：沿用 P2.1 已经设计好的令牌和同源校验，写接口必须带请求头令牌。提示词和记忆的写接口要额外记审计（谁、什么时候、改了哪个文件）。

### 6.2 「记忆可看可编辑」

一代可借鉴的设计：

1. **人类可读的 Markdown 是主载体，加少量约定的标记行**。一代的 `[潜意识备忘]` 标记行可以被 hook 解析。二代对应的约定有：USER.md 的 `<!-- observed | status -->`、MEMORY.md 的 `<!-- trigger --> <!-- importance -->`、交接段标题。面板按这些约定解析成卡片，同时提供原文编辑模式。
2. **v1 HMO-lite 的元数据与证据链**（`memory-index.jsonl`、`memory-sources.jsonl`）：每条记忆都有 type、tier、importance、tags、sourceIds，能追溯到原话。
   - 二代建议：在 USER.md 和 MEMORY.md 的条目注释里加 `src`（会话 id 加事件序号，或者 `family:<人> 时间`；family-facts.json 已经有 source 和 verifiedBy 字段）。
   - 面板上每条记忆都有一个「出处」按钮，点开能看到当时那段转写，只读查询 SQLite，和 P2.1 runtrace 的做法一样。
   - **这是「可编辑」能被信任的前提**：主人能看到某条记忆为什么存在，才敢删、敢改。
3. **分层视图**：
   - 一代 v1 按更新速度分三层：短期状态、中期策略、长期人格。
   - 二代的 L1 到 L9 分层（context-plan §2）可以直接做成面板的标签页：画像 USER / 精选 MEMORY / 交接 / 家庭事实 / 时间线 / 每日笔记 / 意图提醒。
   - 每一页标出「每轮注入」还是「按需检索」，让主人知道改动对每轮 token 的影响。
4. **心智可视化**：一代的 PAD 三轴、threads、模式权重，都可以做成只读仪表。如果实现了第 6、7 项，还可以加 heat 曲线和 nextSpeakAt 倒计时（只给主人看，一代的规矩是「排程不对用户说」，但面板本来就是调试视角）。
5. **规则预算提示**：面板检测到 USER.md 里同一类指令超过 2 条时，提示「建议合并」，对应一代 v1「同类边界不超过 2 条」的验收标准。
6. **一代没做、二代必须补的**：
   - **与休眠互斥**：面板编辑和休眠 S4 提交可能同时写同一个文件。建议面板写入先拿 `ops/sleep/lock`，休眠进行中就提示「整理中，稍后保存」；或者休眠在 S1 记录各文件的哈希，S4 提交前比对，发现文件被面板改过，就放弃这个文件的提交并在下次重做。
   - **写入方标注**：每日报告的 diff 里已经计划标注写入方会话（context-plan §5.6、§8.4），面板写入也要标成 `panel`。
   - **家人可读的提醒**：面板编辑 USER.md 和 MEMORY.md 时常驻一条提示：「家人会话也能读到这里的内容。」

---

## 附：关键证据索引

- 主 skill、节奏、Cron 设计：`P/skills/thoughts/SKILL.md`
- CLI（gate、record-spoken、config、ping）：`R/cli.mjs`
- 热度模型：`R/core/heat.mjs`
- 心智、模式、心情渲染：`R/core/mind.mjs`
- 潜意识提示词：`R/core/prompts.mjs`；运行器：`R/core/subconscious.mjs`；节奏决策：`R/core/decide.mjs`
- 人格注入 hook：`R/hooks/inject.mjs`；通知冲洗：`R/hooks/flush-notify.mjs`；hook 注册：`P/hooks/hooks.json`
- 开口模板：`R/prompts/speak.md`；滴答脸：`R/core/kaomoji.mjs`；人格兜底渲染：`R/persona.mjs`；默认实例：`R/core/onboarding.mjs`；存储：`R/core/store.mjs`
- onboarding：`P/skills/thoughts-onboarding/SKILL.md`；退出：`P/skills/thoughts-stop/SKILL.md`
- Cursor 版（v1）：`C:/Users/moxue/.cursor/backups/skill-migration-20261008-155246/skills/thoughts/SKILL.md`、`C:/Users/moxue/.cursor/agents/thoughts-subconscious.md`、`C:/Users/moxue/.cursor/plans/thoughts_redesign_2e96475d.plan.md`
- 早期计划：`D:/coding/inksnow/Thoughts/.omc/plans/thoughts-mode-skill.md`
- 衍生 HUD 设计：`D:/coding/inksnow/Thoughts/docs/tvs情绪记忆HUD-skill设计文档.md`
- 开发经验笔记：`C:/Users/moxue/.claude/projects/D--coding-inksnow-Thoughts/memory/`（MEMORY.md 为索引）
- 真实数据：`C:/Users/moxue/.thoughts/instances/大师/`、`C:/Users/moxue/.thoughts/instances/大痔/`、`D:/coding/inksnow/Thoughts/.cursor/.thoughts/instances/烤色/`；绑定关系 `C:/Users/moxue/.thoughts/active.json`
- 二代对照：`2026-10-09-roadmap.md`、`2026-10-09-context-plan.md`（§2 分层、§3.5 休眠流程、§5.6 已接受风险、§6 固定开销）、`2026-10-10-p21-plan.md`（面板安全与设置热加载）、`work2/AGENTS.md`
