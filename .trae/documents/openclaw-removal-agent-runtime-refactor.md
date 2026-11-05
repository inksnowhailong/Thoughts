# 思绪 · 移除 OpenClaw:TS Agent Runtime 重构计划

> 日期:2026-11-05 · 状态:待主人批准
> 核心结论:**TS 只做"大脑"(Agent 编排),Python 业务核心全保留,绞杀式切换,全程可回滚。**

---

## 0. 计划概要

用 TypeScript + LangChain.js/LangGraph.js 新建 `agent-runtime` 进程,接管 OpenClaw 的"大脑"角色(模型调用、主 Agent 对话、Worker Agent 工具循环、审批中断)。现有 Python 成熟模块(任务、权限、记忆、输入输出、语音管道)全部保留并继续作为业务事实来源,通过本机回环 HTTP/SSE + 双端契约测试桥接。分 7 个 Phase 绞杀式迁移,每个 Phase 独立可验收、可回滚,OpenClaw 网关在 Phase 6 之前保持可随时切回。

**不重写的东西(明确排除):**
- 语音管道(voice/asr/vad/speaker/tts/textseg/barge/turns)
- Python 业务核心:core/、permission/、memory/、task_*、input/、output/、self_loop/、sleep/
- TTS/ASR sidecar 服务与所有计划任务、看门狗、面板、托盘

**新建的东西:**
- `agent-runtime/`(TS 包,Node ≥22):Main Agent、Worker Agent、Prompt Compiler、Model Provider、Tool Registry 门面、OpenAI 兼容语音入口
- 双端契约 fixtures 与桥接适配器(Python 侧 ~4 个新文件)

---

## 1. 现状审计(已完成)

### 1.1 生产链路

```
VoiceDaemon(Python) ──HTTP/SSE──▶ OpenClaw 网关:18789(agent:main:main)
                                      ├─ AGENTS.md 大 prompt(~16.6k token)
                                      ├─ 工具宿主(launch-app.ps1、screen-capture.py 等)
                                      └─ guanjia-guard 插件(分级/审批/审计)
Worker(agent:main:task-<id>,仍属 main agent,仅会话不同)
```

### 1.2 Python 侧已有资产(全部保留)

| 模块 | 位置 | 现状 |
|---|---|---|
| 自研 runtime 骨架 | src/guanjia/core/(agent_runtime、worker_runtime、scheduler、event_store、task_repository、shadow) | 完整状态机 + SQLite,WorkerAdapter 协议当前指向 OpenClaw,反转为指向 TS Runtime 即可 |
| 权限 | permission/(authorizer、policy、grants、roles、confirmations、audit、agent_gate) | L0-L3、ALLOW/CONFIRMATION_REQUIRED/DENY、一次性审批凭证已实现 |
| 记忆 | memory/、staging_review.py | staging/review/commit 链、confidence、conflicts 已实现;embedding 走硅基流动 |
| 任务 | task_dispatch/executor/manager/service/verify/ipc | 生命周期、结果验证(task_verify,外部效果证据)已实现 |
| 输入/输出 | input/(wechat/imou/local_camera 适配器、InteractionEvent)、output/(broker、interruption、speech、ptz) | 归一化、打断策略、静默决策已实现 |
| Worker 契约 | core/worker_runtime.py 的 Materials / MemoryPacket(≤8 条×500 字) | 正是 Worker 上下文隔离需要的边界,直接复用 |
| 测试 | tests/ 150+ 文件 | 迁移后继续有效 |

### 1.3 OpenClaw 耦合点(必须全部替换)

1. `brain.py`:`/v1/chat/completions` OpenAI 兼容流式客户端(替换方式:换 URL/Token,协议保留)
2. `core/openclaw_adapter.py` + `worker_runtime.py` 的 WorkerAdapter:调 OpenClaw 跑 Worker(反转为调 TS Runtime Worker API)
3. `approval_ipc.py`:读 OpenClaw workspace 审批服务配置,调 `/latest`、`/decide`(审批服务由 Guard 插件承载,随 OpenClaw 一起退役;新审批走 TS Runtime interrupt + Python permission)
4. `memory/openclaw_adapter.py`:OpenClaw 记忆后端(存量数据迁移后退役)
5. 工具脚本宿主:openclaw/workspace/tools/*.ps1/*.py(脚本本身移植复用)
6. 配置真相:AGENTS.md 规则、levels.json 分级表、exec-approvals.json(拆解迁移)
7. 微信插件 @tencent-weixin/openclaw-weixin(**本次退役,未来独立接入**,input/adapters/wechat.py 适配器骨架已备)

### 1.4 Token 消耗证据(2026-10-15 实测,scripts/prompt_size.py)

每轮固定 prompt ≈ **16,667 token**:AGENTS 2,638 + 工具摘要 3,779 + 工具 schema 6,086 + 技能目录 716 + 平台包装 2,988。约 60% 用于工具理解;人格/画像/记忆占比极小。**目标:Main Agent 固定 ≤5k,Worker ≤4k + 任务相关工具。**

### 1.5 已确认的三项决策(主人 2026-11-05 拍板)

1. **进程边界**:TS 大脑 + Python 核心。Task Engine/Policy/Memory 留 Python 演进,本机 JSON-RPC/HTTP 桥接。
2. **core/ 处置**:保留为 Python 业务核心,WorkerAdapter/OpenClawAdapter 反转为调 TS Runtime。
3. **存量资产**:全部迁移(记忆数据、工具脚本、levels.json、AGENTS.md 规则)。

---

## 2. 技术验证结论(已核实,含出处)

| 项 | 结论 |
|---|---|
| 版本 | @langchain/core 1.3.x、langchain 1.6.0、**@langchain/langgraph 1.5.3(1.0 已 GA)**、zod 4.x、Node ≥22(机器上 fnm 已有 Node 24) |
| 模型接入 | @langchain/openai 1.8.0 `ChatOpenAI({configuration:{baseURL}})` 直连 OpenAI 兼容端点(DeepSeek/智谱 v4/硅基流动均兼容);@langchain/deepseek 1.1.21 可选 |
| 工具 | `tool(fn, {schema: z.object()})`、bindTools、ToolNode、工具可返回 Command 写状态 |
| 结构化输出 | `withStructuredOutput(zod)` 可用;**不支持流式**(校验在生成完之后)——意图路由用 tool-calling-in-stream 替代 |
| LangGraph | StateGraph(Zod StateSchema)、条件边、subgraph、streamMode values/updates/messages、`interrupt()` + `Command(resume)`(human-in-the-loop 审批)、SqliteSaver(better-sqlite3,Windows 有预编译)全部可用 |
| 已知坑 | prebuilt `createReactAgent` 已弃用(自建图);JS 无 defaultRetryOn;官方 checkpointer 后端少于 Python;@langchain/openai 最新版需 Node 22+ |
| 多 Agent | @langchain/langgraph-supervisor 存在,但**不采用**——主/Worker 派发走自研 Task Engine,不把业务绑到框架编排 |

**职责边界总表(框架 vs 自研):**

| 能力 | 由谁提供 |
|---|---|
| 模型调用、消息抽象、流式、tool-calling、结构化输出 | LangChain(`@langchain/openai` + core) |
| Worker 内部工具循环、条件路由、checkpoint、interrupt/恢复 | LangGraph(StateGraph + SqliteSaver) |
| Prompt 模板原语 | LangChain PromptTemplate(仅作拼装工具) |
| 会话/上下文构建、意图路由、任务派发、任务生命周期、权限、记忆、工具注册表、审批、验证、通知 | **自研**(Python 核心 + TS Runtime 薄层) |

---

## 3. 目标架构

### 3.1 进程拓扑

```
┌─ 边缘进程(保留) ──────────────────────────────────────────┐
│ VoiceDaemon(Python)      Panel/Tray(18795)   ASR/TTS sidecars │
└──────────────┬─────────────────────────────────────────────┘
               │ InteractionEvent / SSE
┌─▼ agent-runtime(新建,TS,Node 22+,端口 18800,仅回环) ────────────┐
│ ① 语音入口:/v1/chat/completions(OpenAI 兼容流式,brain.py 无痛切换)  │
│ ② Main Agent:轻量对话/意图/派单,Prompt Compiler 按角色编译      │
│ ③ Worker Runtime:/v1/worker/*(LangGraph 执行,checkpoint+interrupt)│
│ ④ Model Provider:ChatOpenAI+baseURL,按角色策略选模型,可直连各 API │
│ ⑤ Tool Registry 门面:能力声明→自动生成 LangChain tool schema     │
└──┬──────────────┬──────────────┬──────────────┬────────────┘
   │ 本机 HTTP      │              │              │
┌─▼──────────────▼──────────────▼──────────────▼────────────┐
│ guanjia-core(Python 模块化单体,业务事实唯一来源,保留)         │
│ Task Engine(task_*、core/scheduler、thoughts-core.sqlite3)    │
│ Policy Engine(permission/、L0-L3、审批凭证、审计)              │
│ Memory Service(memory/、staging/review/commit、embedding)     │
│ Result Verifier(task_verify.py)                              │
│ Response Manager(output/broker、interruption、speech)         │
│ Tool Executor(工具真实执行:本机进程/Python 原生工具)            │
└────────────────────────────────────────────────────────────┘
```

### 3.2 数据流

**语音轮(快路径):**
ASR 文本 → brain.py(SSE,URL 换 18800)→ Main Agent(Prompt Compiler 编译 ≤5k:最小契约+人格+语音规则+当前上下文+少量记忆;工具仅 5-6 个高层轻工具)→ 流式回复 → turns.py 按句切分 → TTS。Barge-in 语义不变:**只停播放,流读到 [DONE],不断开连接,不影响后台任务**。

**任务轮:**
Main Agent 判定长任务 → `task.create`(结构化 tool call)→ Python Task Engine(durable,立即 ack)→ Task Engine 调 TS `/v1/worker/runs`(TaskSpec + Materials + MemoryPacket,经反转后的 WorkerAdapter)→ Worker Agent(LangGraph 工具循环,checkpoint 持久化)→ 每个工具:TS Registry 解析能力 → **先调 Python Policy `/internal/policy/check`** → ALLOW 才执行(Python Executor 或本机进程)→ CONFIRMATION_REQUIRED 则 `interrupt()` → 审批事件推 Python → 口头"允许"/面板决定 → `/v1/worker/runs/{id}/resume` → 结果经 **task_verify 外部效果验证** → Task Engine 定状态 → 推送 Main Agent → 用户听到/看到。

**审批轮:** Worker `interrupt()` → TS 发 approval_request(session/工具/参数指纹)→ Python 权限服务建 ActionRequest + 一次性凭证 → 语音确认词检测(现有逻辑)或面板决定 → resume;凭证用后即焚,绑定原会话原参数。

### 3.3 核心接口契约(全部 Zod + Python dataclass 双端镜像,fixtures 共享)

```
# TS → Python(桥接客户端)
POST /internal/policy/check      {session, actor, capability_id, args} → {decision, credential?}
POST /internal/tasks            TaskSpec → {task_id, ack}
POST /internal/memory/search    {query, scope, limit} → MemoryPacket(≤8×500字)
POST /internal/tools/<id>/exec  {args, credential} → ToolResult(unified)
POST /internal/verify           {task_id, artifact} → Evidence

# Python → TS(Worker 适配器反转,替换 OpenClawAdapter)
POST /v1/worker/runs            {task_id, TaskSpec, materials, memory_packet, allowed} → {run_id}
GET  /v1/worker/runs/{id}/events  SSE(progress / approval_request / tool_trace / outcome)
POST /v1/worker/runs/{id}/resume  {decision, credential}
POST /v1/worker/runs/{id}/cancel  {reason}

# 语音(兼容保留)
POST /v1/chat/completions       OpenAI 兼容 SSE(brain.py 仅改 URL/Token)
GET  /internal/diag/prompt-report  各角色 prompt 分段 token(性能验证用)
```

**事件信封沿用 contracts/events.py 的 EventEnvelope**(event_id/correlation_id/idempotency_key/schema_version),TS 侧 Zod 镜像。

---

## 4. 关键设计

### 4.1 Main Agent(轻量)
- 单一 ChatOpenAI 流式调用 + 少量高层工具(task.create/status/cancel、get_weather、speak、get_location、launch_app 这些秒级能力)。**不加载 exec/process/任意 write 等底层 schema。**
- 意图路由用 stream 中的 tool-call(结构化输出不能流式,故不用两段式)。
- 会话记忆:TS 内只存 thread_id + 滚动摘要;长期记忆按需向 Python Memory Service 查最小包。
- Prompt Compiler 输入分节(persona/契约/语音规则/上下文/记忆),**测试断言各节字符预算**,总分段可观测。

### 4.2 Worker Agent(LangGraph)
- 通用 Worker Graph:plan → act(ToolNode)→ observe → 条件边循环 → verify → outcome;每任务类型只换 Prompt、工具子集、验证器。
- State 用 Zod schema;`SqliteSaver` 存 `data/agent-runtime/checkpoints.sqlite3`(thread_id=run_id)。**graph checkpoint ≠ 任务状态**——业务状态永远在 Python thoughts-core.sqlite3。
- 审批用 `interrupt()`;恢复用 `Command(resume)`;runtime 重启后 Task Engine 按现有语义标 `interrupted`,不伪装运行。
- Worker 只见 TaskSpec/Materials/MemoryPacket(现有契约),不见主线历史。

### 4.3 Tool Registry(TS 门面 + Python 执行)
- 能力声明为唯一来源(capability_id、Zod input schema、latency_class、side_effect、risk_level L0-L3、executor、verifier、cancellation_semantics),**自动生成** LangChain tool schema、权限元数据、审计字段——对应宏观设计 4.4,消灭五处同步。
- 执行后端两类:`local-process`(child_process 跑 ps1/py 脚本,从 openclaw/workspace/tools 移植到 `agent-runtime/tools/`)与 `http-python`(调 Python Executor:app_index、speak、声纹相关等 Python 原生工具)。
- **模型只能看到 Registry 暴露的子集;每次执行先过 Python Policy,任何路径(含 LangGraph 内)无法绕过。**

### 4.4 语音打断三态(明确分离,voice.py 现有行为保持)
1. **语音打断**=停 TTS 播放,SSE 照读完——不取消本轮、不取消任务;
2. **模型规划取消**=Task Engine 发 cancel → Worker graph 收敛退出,checkpoint 标 cancelled;
3. **后台任务取消**=显式 task.cancel,与 1 完全独立。
Response Manager(Python output/broker)仍是语音通道唯一仲裁,Worker 无输出通道权限。

### 4.5 存量迁移映射
| OpenClaw 资产 | 去向 |
|---|---|
| AGENTS.md/AGENTS-S2-section.md 规则 | 拆解:语音规则→Main prompt;任务 SOP→Worker prompt;主动检查→self_loop;其余(平台包装/工具说明)丢弃 |
| workspace/tools/*.ps1/*.py | `agent-runtime/tools/` + Registry 声明 + Policy 分级(levels.json 映射 L0-L3) |
| OpenClaw 记忆(USER/MEMORY/每日笔记) | 经现有 P4 导入链(staging/review/commit)进 Python Memory Service |
| exec-approvals.json / 审批服务 | Python permission 服务为唯一审批真相,approval_ipc 改指向它 |
| 微信插件 | 退役;未来走独立 adapter 接 input/(已有 wechat.py 骨架) |

---

## 5. 分阶段迁移计划

> 原则:每 Phase 独立交付、独立验收、独立回滚;OpenClaw 网关进程全程保留至 Phase 6;所有切换用环境变量 feature flag。

### Phase 0:契约与脚手架(纯增量,零风险)
- **交付物**:`agent-runtime/` TS 包(Node 22、vitest、tsx;依赖 @langchain/core、@langchain/openai、@langchain/langgraph、@langchain/langgraph-checkpoint-sqlite、zod、fastify);`contracts/fixtures/*.json` 双端契约样例(EventEnvelope、TaskSpec、MemoryPacket、ToolResult、PolicyDecision);Python 侧 `bridge/` 模块(httpx 客户端,trust_env=False);双端 round-trip 测试进 run-tests.ps1 分层。
- **验收**:fixtures 在 vitest 与 pytest 双侧解析一致;`pnpm test` + 新增 pytest 全绿。
- **风险/回滚**:无线上影响;git revert 即可。

### Phase 1:Main Agent 最小闭环(语音换脑)
- **交付物**:Model Provider(角色模型策略配置:main=现网关默认 Flash 级、game=glm-5.3,env 驱动);Prompt Compiler v1(AGENTS 语音规则+人格迁移,预算测试);`/v1/chat/completions` OpenAI 兼容 SSE;`brain.py` 增加 `GUANJIA_BRAIN_URL`(默认不变仍指 18789);`/internal/diag/prompt-report`。
- **验收**:设 URL 切 18800 后,语音端到端(唤醒→问答→打断→TTS)与 OpenClaw 链路同场景通过;首字延迟不劣于基线;prompt-report 显示固定段 ≤5k token 等值字符预算。
- **风险**:模型直连差异(现由网关统一 fallback)→ 保留 zhipu_gate/网关地址作为可选 baseURL,失败回切 18789 只需清环境变量。
- **回滚**:`GUANJIA_BRAIN_URL` 复位,重启语音。

### Phase 2:Tool Registry + Policy 桥接(高频工具先行)
- **交付物**:Registry 声明 launch_app/get_weather/get_location/speak/screen_capture/web_fetch;Python `/internal/policy/check`、`/internal/tools/<id>/exec` 暴露;levels.json→权限规则映射迁移;Main Agent 绑定这些秒级工具;审计接 permission/audit。
- **验收**:语音"开个联盟/今天天气"走新链路;L2+ 操作触发口头确认且凭证一次性(复用现有 perm 测试场景);绕过路径均 403(负向测试)。
- **风险**:工具行为差异 → 对照 OpenClaw 版工具逐个冒烟(现有 verify-s3 模式)。
- **回滚**:Main Agent 工具开关 env(GUANJIA_TOOLS_MODE=openclaw|runtime);回 OpenClaw 工具链。

### Phase 3:Task Engine 桥接 + Worker Agent(核心阶段)
- **交付物**:Python WorkerAdapter 反转(`core/openclaw_adapter.py` 同步改造,GUANJIA_WORKER_BACKEND 开关);TS `/v1/worker/*` 全套;Worker Graph(vitest + 假模型单测);interrupt↔审批凭证打通;task_verify 接 Evidence;checkpoint 持久化+重启恢复。
- **验收**:长任务全生命周期(创建→排队→执行→审批中断→恢复→验证→成功/失败→反馈)通过;runtime 进程重启后任务转 interrupted 不伪装;打断语音不取消已提交任务(专项测试);全部 core/ 现有 worker 测试在新后端下通过。
- **风险**:审批时序(interrupt 挂起期间超时)→ 沿用 120s 超时 + interrupted 语义;LangGraph JS interrupt 边界 → Phase 0 先做 spike 验证(见 §7)。
- **回滚**:GUANJIA_WORKER_BACKEND=openclaw 切回旧链(此阶段 OpenClaw 仍在跑)。

### Phase 4:Memory Service 接入 + 存量迁移
- **交付物**:Python `/internal/memory/search`;Main/Worker 按需检索接入;OpenClaw 记忆(USER/MEMORY/笔记)经 staging/review/commit 导入;`memory/openclaw_adapter.py` 退役;Context Gardener 接新链路。
- **验收**:导入全走 review 链(模型无直写);检索延迟不影响首字(P95 可测);家人隐私 scope 用现有 visibility 规则测试。
- **回滚**:导入是新增数据,不覆盖;检索开关可关。

### Phase 5:全量切换(灰度→默认)
- **交付物**:GUANJIA_BRAIN_URL 默认改 18800;approval_ipc 改指 Python 权限服务;panel/status 显示新链路健康;连续 N 天双链路对比报告(同场景日志 diff)。
- **验收**:48h 无回滚事件;任务成功率、打断行为、审批流与基线持平。
- **回滚**:任一指标劣化 → 环境变量复位,10 分钟内回旧链。

### Phase 6:OpenClaw 退役
- **交付物**:停 `OpenClaw Gateway` 计划任务(保留安装与配置备份 ≥30 天);删除 brain.py OpenClaw 残留逻辑、workspace 兼容代码;`openclaw/` 目录归档;文档(README/HANDOFF)更新。
- **验收**:开机自启清单无 OpenClaw;全功能回归通过。
- **回滚**:重新 schtasks /Run OpenClaw Gateway + 环境变量复位。

### Phase 7:性能验证与报告(与 Phase 5 并行启动)
- **交付物**:对比报告——固定 token(prompt-report 16667 vs 新链)、首字延迟(elog/last_meta 现有指标)、任务成功率、资源占用;scripts/prompt_size.py 改造或 TS 等价物。
- **验收**:固定 token 降 ≥60%(16.6k→≤5k);首字不劣化;长任务稳定性≥基线。

---

## 6. 测试策略(分层,接入现有 run-tests.ps1)

1. **契约测试**:fixtures 双端 round-trip(每 Phase 增量)
2. **TS 单测**(vitest):Prompt Compiler 预算、Registry schema 生成、Policy 门禁路径、Worker Graph 用 FakeChatModel
3. **集成测试**:Python harness 拉起 agent-runtime 子进程,跑任务生命周期/审批/打断语义
4. **架构测试**(守门):静态断言——Main Agent 工具白名单外不可 bind;Worker 不可 import 主线会话;工具执行必经 Policy(依赖方向检查)
5. **语音端到端**:现有 fake_voice_child/fake_tts_child 管道冒烟 + 真机验收清单
6. **Spike(Phase 0 内)**:LangGraph JS interrupt+SqliteSaver+断点重启 的最小 PoC,验证 §2 表格结论

---

## 7. 总体风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| LangGraph JS 与文档行为不符(ToolNode 注入差异等) | 中 | Phase 0 Spike 前置;Worker Graph 自建不依赖 prebuilt |
| 模型直连失去网关统一 fallback | 中 | baseURL 可指 zhipu_gate/网关;角色级 fallback 配置 |
| 双进程时序问题(审批挂起、任务恢复) | 中 | 复用现有 120s 超时 + interrupted 语义;contract 幂等键 |
| 记忆导入质量 | 低 | 全走 review 链,人可审 |
| OpenClaw 停用后发现隐性依赖(如面板读 workspace 文件) | 中 | Phase 5 双链对比期专门排查 workspace 读取点 |
| Token 预算超标回弹 | 低 | 架构测试固定断言 prompt 分节预算 |

---

## 8. 批准后立即执行(Phase 0 任务清单)

1. 初始化 `agent-runtime/`(package.json、tsconfig、vitest、eslint、目录骨架、.gitignore;Node 22 via fnm)
2. 建立共享契约:TaskSpec/MemoryPacket/ToolResult/PolicyDecision/EventEnvelope 的 Zod + dataclass 镜像与 fixtures
3. LangGraph Spike:SqliteSaver checkpoint + interrupt/resume + 进程重启恢复 的可运行 PoC 测试
4. Prompt Compiler 骨架 + AGENTS.md 规则拆解表(迁移映射清单,人工确认)
5. 双端契约测试接入 scripts/run-tests.ps1 新分层
