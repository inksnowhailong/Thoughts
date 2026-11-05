# AGENTS.md 规则拆解映射表（Phase 0 任务C）

> 状态：**待主人确认**。本表把 `openclaw/workspace/AGENTS.md`（下称"原文件"，行号以该文件为准）逐条拆解到新架构的去向上，作为 Phase 1 Prompt Compiler v1 的迁移清单。骨架阶段的落地物：`agent-runtime/src/prompts/sections.ts`（节划分+预算）与 `agent-runtime/prompts/main.md`、`agent-runtime/prompts/worker.md`（模板）。

## 0. 背景与口径

- 现状（2026-10-15 实测，见重构计划 §1.4）：每轮固定 prompt ≈ **16,667 token** = AGENTS 2,638 + 工具摘要 3,779 + 工具 schema 6,086 + 技能目录 716 + 平台包装 2,988。目标：Main Agent 固定 ≤5k，Worker ≤4k + 任务相关工具。
- 去向定义（与计划 §4.5 对齐）：
  - **main-prompt**：进 Main Agent 的 system prompt（按节编译，见 §3 预算表）；
  - **worker-prompt**：进 Worker Agent 的任务执行契约；
  - **self-loop**：主动循环（原 OpenClaw automations 职责）的逻辑/参数，不占对话 prompt；
  - **policy-code**：权限与确定性规则进代码（permission/ 分级审批、Task Engine 机制、视觉循环参数、架构测试）；
  - **tool-schema**：能力语义进 Tool Registry 的工具描述（Phase 2 起按需注入 schema）；
  - **丢弃**：OpenClaw 平台包装、与 OpenClaw 机制绑定的说明；
  - **保留-暂缓**：先不进任何 prompt/代码，等后续 Phase 或 adapter（如微信）接入再评估。
- token 估算口径：中文 2 字符 ≈ 1 token、英文 4 字符 ≈ 1 token（保守混合口径；glm 系实际更省，预算即上限）。"预估"列写该条在新架构中**是否占 Main/Worker prompt token**；标注为 0 的条目进代码/工具描述/循环，不占对话 prompt。
- 一条规则可能有多个去向（行为口径留 prompt、机制进代码），统计时在各类都计入，§2 注明含重叠。

## 1. 逐条拆解映射表

### 1.1 AGENTS.md 前半（OpenClaw 默认模板，L1-L132）

| # | 位置（段 / 行号） | 规则（短引用或概括） | 去向 | 理由 | 预估 |
|---|---|---|---|---|---|
| 1 | First Run / L5-7 | 存在 BOOTSTRAP.md 则照做并删除 | 丢弃 | OpenClaw 首次运行机制 | 0 |
| 2 | Session Startup / L9-17 | 优先用 runtime 注入的启动上下文，不重读文件 | 丢弃 | 新架构由 compiler 用 {{CONTEXT}}/{{MEMORY}} 注入，机制本身无需告知模型 | 0（机制进 compiler） |
| 3 | Memory / L19-25 | daily/USER.md/MEMORY.md 三文件分工 | 丢弃 | 记忆走 Python Memory Service（P4 导入链），模型不再直接管文件 | 0 |
| 4 | Memory / L27 | 记决定与上下文、不记秘密 | main-prompt | 隐私口径并入 platform-min 红线一句 | ≈20 tok |
| 5 | USER.md / L29-33 | observed/superseded 指令格式 | 丢弃 | 属于 P4 导入链的数据格式 | 0 |
| 6 | MEMORY.md / L35-39 | 仅主会话加载，不进共享上下文 | policy-code | worker 模板不含 memory 节（sections.ts 固化），可见性由 Memory Service visibility 规则测试守门 | 0 |
| 7 | Write It Down / L41-47 | 先读后写、写具体更新、教训记档 | 丢弃 | 写记忆改走 Memory Service API，等价约束在服务端 | 0 |
| 8 | Memory Maintenance / L49-51 | 定期整理归并每日笔记 | self-loop | 主动维护循环（原 scheduled automation 职责） | 0 |
| 9 | Red Lines / L55 | 不外泄隐私 | policy-code + main-prompt | 出网/权限策略代码强制；prompt 留一句红线 | ≈30 tok |
| 10 | Red Lines / L56 | 破坏性命令先问 | policy-code + main-prompt | permission L2+ 口头审批强制；prompt 留"只有工具返回需要确认才问"口径 | （含在 #9 红线内） |
| 11 | Red Lines / L57 | 改配置/调度器先查现状、默认保留合并 | worker-prompt | 任务执行契约 | ≈40 tok |
| 12 | Red Lines / L58 | trash 优先于 rm | policy-code + worker-prompt | exec 工具实现层兜底可恢复删除；worker 提一句 | ≈15 tok |
| 13 | Red Lines / L59 | 拿不准就问 | main-prompt | 一句 | ≈10 tok |
| 14 | Existing Solutions Preflight / L61-63 | 造方案前先查现成开源/库 | worker-prompt | 长任务规划约束；语音轮用不到 | ≈35 tok |
| 15 | External vs Internal / L65-69 | 读文件自由；出机器（邮件/发帖）先问 | policy-code | levels.json 权限分级映射；main-prompt 红线已覆盖"出机器先问"口径 | 0（prompt 侧含在红线） |
| 16 | Group Chats / L71-73 | 群聊保守隐私、以自己身份参与 | 保留-暂缓 | 多渠道随 OpenClaw 退役；未来 input/ adapter 时按渠道注入 | 0 |
| 17 | Know When to Speak / L75-81 | 群聊发言时机、不发碎片消息 | 保留-暂缓 | 同上，属渠道策略 | 0 |
| 18 | React Like a Human / L83-85 | 用表情回应、每消息至多一个 | 保留-暂缓 | 表情回应是 Discord/Slack 渠道能力 | 0 |
| 19 | Tools / L87-89 | 工具程序用 skill，本地笔记留这节 | 丢弃 | SKILL.md 机制退役，能力语义进 Registry 工具描述 | 0 |
| 20 | Local notes / L91-93 | 登记摄像头名/SSH/设备昵称 | 丢弃 | 设备清单属配置数据（apps.json 等），由工具实现读取 | 0 |
| 21 | 摄像头 / L95-97 | ONVIF 不报绝对位置、航位推算会漂、15步/12小时重锚 | tool-schema | camera_ptz/camera_look 描述写明局限；重锚计数在视觉循环代码 | 0 |
| 22 | 摄像头 / L98 | 绿玩偶 YOLO 误检成"人"；画面静止别轻信 person_present | tool-schema | camera_look/person 描述写明看 person_static_lock；VLM 仲裁已在代码（policy-code 兜底） | 0 |
| 23 | 摄像头 / L99 | 合并斜转不按预期执行，必须单轴分步 | policy-code | camera_ptz 实现层拒绝 pan+tilt 同发，不是模型责任 | 0 |
| 24 | 摄像头 / L100 | 无人护位在 proactive.json，改后需重启 | policy-code | 视觉循环配置，代码读取 | 0 |
| 25 | Voice storytelling / L102 | sag TTS 可用时故事/总结用语音 | tool-schema | speak/tts 工具描述 | 0 |
| 26 | Platform formatting / L104-108 | Discord 用列表/链接包 <>；WhatsApp 用粗体 | 丢弃 | 渠道格式化随多平台退役 | 0 |
| 27 | Automations / L110-112 | 用 openclaw automations 命令管定时任务 | 丢弃 | OpenClaw 平台 CLI | 0 |
| 28 | Automations / L114 | 轮查清单：邮件/日历/社交/天气，每天 2-4 次 | self-loop | 主动循环的检查项 | 0 |
| 29 | Automations / L116 | 主动触达时机（重要邮件/事件<2h/8小时没说话） | self-loop | 循环逻辑 | 0 |
| 30 | Automations / L118-120 | 静默时段 23:00-08:00、忙时不扰、30 分钟节流 | self-loop | 循环逻辑 | 0 |
| 31 | Automations / L122 | 免问主动工作清单（git status/文档/commit push） | self-loop + policy-code | 白名单进循环；权限边界仍由 policy 管 | 0 |
| 32 | Make It Yours / L124-126 | 随学习添加约定 | 丢弃 | 工作区自进化理念；新架构规则进编译器与代码 | 0 |
| 33 | Related / L128-132 | OpenClaw 文档链接 | 丢弃 | 平台文档 | 0 |

### 1.2 S2 管家规则段（L134-156，与 `openclaw/AGENTS-S2-section.md` 同源）

| # | 位置（段 / 行号） | 规则（短引用或概括） | 去向 | 理由 | 预估 |
|---|---|---|---|---|---|
| 34 | S2 / L137 | 回答默认不超过两句话，长内容先给要点再问展开 | main-prompt | voice-rules 核心（voice.py SYSTEM_PROMPT 同源约束） | ≈60 tok |
| 35 | S2 / L138 | 只放说给用户听的中文，不输出过程说明 | main-prompt | voice-rules | ≈40 tok |
| 36 | S2 / L139 | 语音轮整体：[语音 …] 标记语义、每句≤30字、禁 Markdown/列表/链接/表情、说话人只作参考、禁止"我先查一下"收尾 | main-prompt | voice-rules 核心；与 voice.py L245-248 SYSTEM_PROMPT 合并收敛 | ≈250 tok |
| 37 | S2 / L140 | 反报告腔：工具结果先消化成一句口语，不念错误码 | main-prompt | voice-rules | ≈60 tok |
| 38 | S2 / L141 | 禁复读：换应答词或省掉 | main-prompt | voice-rules；**分歧点 D1**：后期可 self-loop 检测替换 | ≈40 tok |
| 39 | S2 / L142 | 要看人先调工具：[视觉\|…] 注记不是说话人画面，先 camera_look 再答 | main-prompt + tool-schema | 注记局限进 prompt 一句；详述进 camera_look 描述 | ≈50 tok |
| 40 | S2 / L143 | 微信要看照片：camera_photo 拍一张自动送达 | 保留-暂缓 | 微信插件退役（§4.5）；未来 wechat adapter 接入时恢复 | 0 |
| 41 | S2 / L144 | 游戏攻略先 screen_capture 截屏看清再答 | main-prompt | game 模式即时轮规则；**分歧点 D2**：也可随 game 角色（§4.1 game=glm-5.3）单独编译 | ≈35 tok |
| 42 | S2 / L145 | 摄像头三件事用现成工具，禁 exec 跑脚本、禁手写调外部模型 API | policy-code + tool-schema | Main 工具白名单由架构测试守门（§6.4）；风险理由进 camera 工具描述 | 0 |
| 43 | S2 / L146 | 问时间直接用语音标记里的时间，不调工具 | main-prompt | voice-rules 一句 | ≈25 tok |
| 44 | S2 / L147 | 大多数事直接做；被拦才问一句；说"允许"同参重试；不绕过；已拒绝不重试；只有真返回"需要确认"才能说需要确认 | main-prompt + policy-code | 行为口径留 prompt；拦截/同参重试/记住授权等机制由 permission 服务保证 | ≈150 tok |
| 45 | S2 / L148 | 专用工具调法（tool_call id 填同名的兼容层） | 丢弃 | OpenClaw 工具协议包装；新架构 Registry 直接出 schema | 0 |
| 46 | S2 / L149 | 让电脑念出来：speak 用法、别用 tts/exec、失败如实说 | tool-schema | speak 工具描述 | 0 |
| 47 | S2 / L150 | 开程序：launch_app 用 apps.json 英文键，未登记则报、新增先问 | tool-schema | launch_app 描述 | 0 |
| 48 | S2 / L151 | 定位与天气：get_weather/get_location 参数 | tool-schema | 两个工具的描述 | 0 |
| 49 | S2 / L152 | 三件事各有技能（notes/lookup/launch SKILL.md） | 丢弃 | SKILL.md 机制退役；语义并入对应工具描述 | 0 |
| 50 | S2 / L153 | 记事写 memory/timeline.md，一行一条只追加 | 保留-暂缓 | **分歧点 D3**：P4 Memory Service 接管前的过渡；建议先落到 notes 工具描述 | 0 |
| 51 | S2 / L154 | 联网优先 web_fetch 提正文；禁 exec 下载后直接运行 | tool-schema + policy-code | web_fetch 描述；exec 危险分级拦截下载执行 | 0 |
| 52 | S2 / L155 | 后台任务：>5 秒多步的活 task_start，短活直接做 | main-prompt + policy-code | 派单口径留 prompt 一句；生命周期机制进 Task Engine | ≈60 tok |
| 53 | S2 / L156 | 任务边界：不把创建说成完成、cancel 协作语义、worker 仍走 Guard/审批 | main-prompt + policy-code | "创建≠完成"诚实口径留 prompt；领取条件/取消语义是机制进代码 | ≈30 tok |

## 2. 去向汇总统计（含重叠）

| 去向 | 条数（编号） | 说明 |
|---|---|---|
| main-prompt | 15（4,9,10,13,34-39,41,43,44,52,53） | 全部收敛进 platform-min/persona/voice-rules 三节 |
| worker-prompt | 3（11,12,14） | 进 worker 模板 tool-constraints 节 |
| self-loop | 5（8,28,29,30,31） | 主动检查/触达/静默/记忆维护，Phase 3+ 的循环设计输入 |
| policy-code | 12（6,9,10,12,15,23,24,31,42,44,51,52,53 中相应半边） | 多与上两类重叠（机制半边） |
| tool-schema | 9（21,22,25,39,42,46,47,48,51） | Phase 2 Registry 工具描述的素材清单 |
| 丢弃 | 13（1,2,3,5,7,19,20,26,27,32,33,45,49） | 全部为 OpenClaw 平台包装/机制绑定 |
| 保留-暂缓 | 5（16,17,18,40,50） | 渠道 adapter（微信/群聊）与 P4 Memory Service 接入时再定 |

token 流向估算：原 AGENTS 2,638 token 中，S2 段约 2,000 token、前半模板约 640 token。迁移后 main-prompt 文本骨架实测 ≈1,550 字符（约 800 token，因骨架只收"语音相关核心"）；S2 全量并入后 voice-rules 预算 2,400 字符（约 1,200 token）。工具摘要/schema/技能目录/平台包装共 11,369 token 不再随 AGENTS 走，由 Registry 按角色注入（Phase 2 起）。

## 3. Main Agent 固定 prompt 组成与预算（待主人确认）

section 划分与 `agent-runtime/src/prompts/sections.ts` 的 `ROLE_SECTIONS.main`/`DEFAULT_BUDGETS.main` 一致，模板在 `agent-runtime/prompts/main.md`：

| section | 内容来源（映射表条目） | 字符预算 | token 等值（≈2 字符/token） |
|---|---|---|---|
| platform-min | 最小契约+红线（#4,9,10,13；#15 口径） | 800 | ~400 |
| persona | 人设占位（SOUL.md P4 重写后替换，**分歧点 D4**） | 600 | ~300 |
| voice-rules | 语音轮规则（#34-39,41,43,44,52,53） | 2,400 | ~1,200 |
| context | {{CHANNEL}}/{{SPEAKER}}/{{CONTEXT}} 动态注入 | 1,200 | ~600 |
| memory | {{MEMORY}} 动态注入（P4 Memory Service 摘要） | 1,600 | ~800 |
| **文本合计** | | **6,600** | **~3,300** |
| 工具 schema（Phase 2 起，Registry 按需注入 5-6 个高层工具） | | | ≤1,500 |
| **总预算** | | | **≤4,800 ≤ 5k ✓** |

Worker Agent（≤4k 目标）：platform-min 600 + goal 1,200 + materials 2,000 + acceptance 1,200 + tool-constraints 900 + report 600 = 6,500 字符 ≈ 3,250 token，加任务相关工具子集 ≤0.5k。goal/materials/acceptance 由 Task Engine 派单时填 {{GOAL}}/{{MATERIALS}}/{{ACCEPTANCE}}。

## 4. 分歧点决议(主人 2026-11-05 拍板)

- **D1 禁复读(#38)**:✅ 先放 main-prompt voice-rules;上线后若应答词仍生硬重复再考虑移 self-loop。
- **D2 游戏攻略截屏(#41)**:✅ **游戏攻略规则彻底砍掉**。新能力 = 通用「看屏幕」:screen_capture 工具截屏 + VLM 理解(视觉模型可配置,默认智谱 GLM-4.5V / 硅基流动 VL),回答"我屏幕上是什么/哪里有问题"。不建第三个 prompt 角色。
- **D3 记事 timeline.md(#50)**:✅ **直接丢弃**。记事功能下线,待 P4 Memory Service 上线后重新设计,无过渡方案。
- **D4 persona 来源**:✅ **成品态——不做占位**。今天完成全部任务,人格直接写成品中文 persona(简短自然口语化),不经"占位→P4 重写替换"两步。
- **D5 群聊/微信五条(#16-18,40)**:✅ 保留-暂缓。未来微信重新独立接入时恢复到 wechat adapter/对应角色。
- **D6 确认流 prompt 深度(#44)**:✅ 认可三句口径:机制进权限服务+审批系统,prompt 只留"被拦才问/说允许同参重试/已拒不重试"。

**总原则(主人指示):今天完成所有阶段,不留中间过渡态,直接交付成品。**

## 5. 代码落点对照

| 交付物 | 路径 |
|---|---|
| 节划分+变量+预算 | `agent-runtime/src/prompts/sections.ts`（ROLE_SECTIONS / ROLE_VARS / DEFAULT_BUDGETS） |
| 编译器 | `agent-runtime/src/prompts/compiler.ts`（compileRole / assertBudget / PromptBudgetError） |
| main 模板 | `agent-runtime/prompts/main.md`（platform-min / persona / voice-rules / context / memory 五节） |
| worker 模板 | `agent-runtime/prompts/worker.md`（platform-min / goal / materials / acceptance / tool-constraints / report 六节） |
| 测试 | `agent-runtime/test/prompts/compiler.test.ts` |
