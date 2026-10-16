# 思绪二代：宏观架构研究与目标设计

> 日期：2026-10-15  
> 状态：架构研究稿，不是实现计划；本文形成期间未修改运行代码或配置。

## 1. 执行结论

当前主要问题不是 GLM-5.3-FlashX 或 DeepSeek-V4-Flash 智力不足，而是系统把太多职责、工具和规则同时交给一个主线 Agent。

当前实测每轮固定 prompt 为约 16,667 token：

- AGENTS：约 2,638 token；
- 工具摘要：约 3,779 token；
- 工具 schema：约 6,086 token；
- 技能目录：约 716 token；
- 其余平台包装约 2,988 token；
- 真正的人格、身份、用户画像、记忆只占很少一部分。

即：模型每轮约 60% 的固定上下文都在理解工具，而且主线同时承担聊天、意图判断、工具选择、长任务、审批、记忆、渠道差异和结果表述。对于日常任务，模型能力足够；失败来自职责与信息架构。

推荐方向不是更换 OpenClaw，也不是继续为单个故障加规则，而是：

1. 保留 OpenClaw，降级为渠道 / 会话 / 模型运行时适配器；
2. 建立一个统一的 `guanjia-core` 模块化核心，掌握任务、权限、记忆、状态和输出；
3. 主线 Agent 只负责理解与交流，不直接承担慢执行；
4. 所有动作统一通过“能力契约 + 任务状态 + 结果验证”；
5. 提示词按角色编译，主线、任务、记忆、视觉不再共享一份大 AGENTS；
6. 记忆写入统一走证据—候选—审核—提交，模型不得自由写长期记忆；
7. 语音、微信、摄像头、系统事件先归一成同一种事件，再进入核心。

目标形态应是“模块化单体 + 少量边缘进程”，不是继续增加微服务。

## 2. 当前架构事实

### 2.1 运行结构

当前系统由四个中心拼接而成：

1. `VoiceDaemon`
   - 音频输入、VAD、ASR、唤醒、声纹、TTS、打断；
   - Panel、Tray、控制 HTTP；
   - 任务调度、worker、影音静默、P4 scheduler；
   - 当前实际是本机编排中心，也是最大单点。
2. OpenClaw
   - 模型供应商、会话、历史、微信插件、工具宿主；
   - 主人语音与微信均进入 `agent:main:main`；
   - worker 使用 `agent:main:task-<id>`，但仍属于 main agent。
3. `guanjia-guard`
   - 工具分级、审批闩锁、专用工具；
   - 微信身份、主线 watermark、任务审批；
   - 通过 workspace 文件、loopback IPC 与 VoiceDaemon 协作。
4. WSL / GPU sidecars
   - IndexTTS、FireRedASR2；
   - 受游戏与显存状态影响。

### 2.2 当前正确的核心能力

这些设计值得保留：

- 主人语音与微信共享一个语义主线；
- 长任务拥有独立会话和持久状态；
- 权限最终由 Guard 拦截，而不是靠模型自觉；
- 审批绑定原会话、原工具、原参数，只允许一次；
- 主动播报有统一静默决策；
- 任务重启后转 `interrupted`，不伪装继续运行；
- 声纹仅作身份信号，不直接作为授权；
- P4 记忆导入已具备 staging / review / commit 安全链。

### 2.3 当前架构债

#### A. VoiceDaemon 职责过密

一个进程同时管音频、UI、任务、审批客户端、静默、记忆演练和运行状态。任何初始化顺序、线程或状态问题都可能影响不相干能力。

#### B. 状态源分散

任务、审批、水位、微信 provenance、设置、记忆 staging、IPC descriptor 分布在多个 JSON / JSONL / Markdown 文件中。缺统一 schema、事务、迁移和生命周期。

#### C. 权限与能力定义分散

新增一个工具通常要同步：

- Guard 注册；
- `levels.json`；
- OpenClaw allowlist；
- AGENTS 规则；
- 专用脚本；
- 测试与部署副本。

“模型能看到什么”“能不能调用”“调用后如何证明成功”没有同一个来源。

#### D. Agent 角色并未真正隔离

worker 虽然使用独立 session，但仍由 main agent 运行。它仍受到主 Agent 的平台 prompt、AGENTS、工具体系影响；只是历史不同，不是独立角色。

#### E. 完成判断仍依赖模型文本

worker 通过最后一行 `TASK_OUTCOME` 判断成功或失败。模型协议不是外部效果证据，无法替代安装状态、窗口存在、文件哈希、HTTP 状态等 verifier。

#### F. Prompt 冲突

当前 AGENTS 同时包含：

- OpenClaw 通用工作区说明；
- 主动检查和主动联系；
- 语音短句规则；
- 工具调用说明；
- 审批策略；
- 任务编排规则；
- 记忆自由写入规则。

其中“主动联系 / 自由维护记忆”与 SOUL 的“不主动打扰”、P4 的受控提交相冲突。

#### G. 上游补丁耦合

微信 provenance 依赖锁定版本与源码 hash 的 shim。它可以作为短期兼容层，但不能成为长期领域架构。

## 3. 统一产品契约

后续所有能力必须遵守六条横向契约：

1. 首响应与实际完成分离；
2. 长任务拥有独立生命周期；
3. 身份识别与权限授权分离；
4. “已完成”必须有外部效果证据；
5. 静默不等于不可见；
6. 依赖异常必须进入明确降级态。

进一步形成以下不可破坏规则：

- 主线不能被慢操作占住；
- 不经过 verifier，系统只能说“请求已发出 / 尚未核实”，不能说成功；
- confirmation 授权的是 `ActionRequest` 指纹，不是自然语言文本；
- 生物识别只能辅助身份，不能提升权限；
- 主动外发必须有明确 channel、account、target；
- 长期记忆只能从带来源证据的候选进入；
- 任何后台任务都必须可查询、可取消、可恢复为 interrupted；
- 静默状态只改变输出渠道，不改变任务与审计。

## 4. 目标宏观架构

```text
输入适配层
  Voice Edge / WeChat Adapter / Vision / OS Context / Timers
        │
        ▼
统一事件模型 InteractionEvent
        │
        ▼
Guanjia Core（模块化单体，唯一业务真相）
  ├─ Identity & Context Resolver
  ├─ Interaction Coordinator
  ├─ Capability Registry + Policy Engine
  ├─ Task Engine
  ├─ Result Verifier
  ├─ Memory Service
  ├─ Notification Broker
  └─ Event Journal / State Store
        │
        ├───────────────┬────────────────┬───────────────┐
        ▼               ▼                ▼               ▼
 Frontdesk Agent    Task Agent      Memory Curator    Vision Agent
 对话与派单          独立执行         staging-only      按需看图
        │               │                │               │
        └───────────────┴────────────────┴───────────────┘
                                │
                                ▼
能力适配器 / 执行器
 Desktop / App / File / Web / Message / Device / Camera
                                │
                                ▼
输出代理
 Voice / WeChat / Tray / Panel / Silent log
```

### 4.1 输入适配层

所有输入先归一为 `InteractionEvent`：

```text
event_id
source: voice | wechat | camera | os | timer
actor_candidate
channel_context
content / intent hint
device_context
timestamp
correlation_id
privacy_class
```

输入适配器只负责采集和标准化，不负责决定权限、记忆或执行。

### 4.2 Identity & Context Resolver

输出统一 `InteractionContext`：

- actor：主人 / 老婆 / 未知；
- identity evidence：微信账号、声纹、人脸、Wi‑Fi、跟踪；
- authority：独立计算，不从声纹/人脸直接继承；
- location / foreground / quiet / gaming；
- reply channel；
- conversation key；
- relevant memory scope。

身份置信度和权限等级是两条独立轴。

### 4.3 Interaction Coordinator

它是系统真正的编排入口，决定一次交互属于：

1. 纯聊天；
2. 确定性确认 / 拒绝；
3. 秒操作；
4. 长任务；
5. 状态查询 / 取消；
6. 系统事件；
7. 视觉请求。

它不靠 AGENTS 提示词要求模型自律，而按能力元数据做确定性路由。

### 4.4 Capability Registry

每项能力只有一个声明来源：

```text
capability_id
input_schema
latency_class: instant | job
side_effect: none | reversible | destructive
confirmation_policy
allowed_actors
executor
verifier
progress_adapter
cancellation_semantics
privacy_class
resource_cost
```

由该声明自动生成：

- 模型工具 schema；
- 权限策略；
- Panel 描述；
- 审计字段；
- 测试模板；
- 结果验证；
- 文档。

不再分别维护 allowlist、levels、AGENTS 和脚本说明。

### 4.5 Task Engine

任务是代码状态机，不是 Agent 的一轮聊天：

```text
accepted
queued
running
waiting_confirmation
verifying
succeeded
failed
cancel_requested
cancelled
interrupted
unverifiable
```

关键规则：

- durable task 创建成功后即可返回 acknowledgement；
- worker 只消费 task，不拥有 task 状态真相；
- progress 来自执行器事件，而不是模型自由描述；
- task completion 只能由 verifier 确认；
- 模型结论只能作为候选结果，不是最终状态；
- 每个 task 都记录发起人、发起渠道和回复渠道。

### 4.6 Result Verifier

示例：

- 安装软件：包管理器状态 + 可执行文件 / 注册项；
- 打开程序：进程 + 可见窗口；
- 下载：文件存在 + 大小 / hash；
- 写文件：落盘 + hash；
- 发消息：渠道返回 delivery id；
- 摄像头转向：设备位置 / 画面反馈；
- 关机：只需接受请求，不能在关机后同步验证。

工具返回 exit code 0 只代表“命令已被接受”，不代表业务成功。

### 4.7 Notification Broker

所有输出统一经过：

```text
NotificationEvent
  source task / interaction
  importance
  preferred channel
  fallback channels
  interruption policy
  quiet policy
  dedupe key
```

规则：

- 主人主动语音轮可朗读；
- 微信任务优先回微信；
- 影音/游戏时不主动朗读，但保留托盘和 Panel；
- 进度节流，阶段变化才通知；
- pending confirmation 不依赖模型自由组织；
- 同一事件不会多渠道重复轰炸。

## 5. Agent 与提示词设计

### 5.1 模型策略

先继续使用 GLM-5.3-FlashX / DeepSeek-V4-Flash。模型不是当前主要瓶颈。

模型选择改为“按角色策略”，而不是全系统一个模型：

- Frontdesk：低延迟、低成本；
- Task：同一 Flash 模型即可，必要时按任务类型升级；
- Memory Curator：批处理，可更慢；
- Vision：视觉专用模型；
- fallback：由角色分别定义。

### 5.2 Prompt Compiler

每次请求由代码按角色编译，不再共享一个大 AGENTS。

#### Frontdesk Agent

固定输入：

1. 平台最小契约；
2. 角色：理解、对话、调用高层能力、创建任务；
3. 精简人格；
4. 当前 actor / channel / quiet；
5. 少量核心画像；
6. 当前会话摘要；
7. 与当前意图相关的少量记忆；
8. 只包含高层、快速工具。

不包含：

- exec / process / 任意 write；
- worker SOP；
- 记忆整理规则；
- 摄像头持续跟踪规则；
- 平台通用主动工作说明。

目标固定预算：约 3k–5k token。

#### Task Agent

固定输入：

1. 任务执行契约；
2. 完整 TaskSpec；
3. 当前阶段与已完成证据；
4. 允许能力；
5. 相关少量环境事实；
6. 审批 / 取消状态。

不包含：

- 主线聊天历史；
- 人格语料；
- 全部 USER / MEMORY；
- 家庭成员私密画像；
- 无关工具。

目标固定预算：约 2k–4k token。

#### Memory Curator

输入：

- 原始证据窗口；
- 当前结构化记忆；
- 来源、可见性和预算；
- 冲突规则。

输出只能是 `MemoryProposal`，无 workspace 写权限。

#### Vision Agent

输入：

- 本次明确问题；
- 必要场景背景；
- 一帧 / 短序列；
- 固定结构输出。

不加载聊天全历史或人物完整画像。

### 5.3 规则应放在哪里

放代码：

- 权限；
- task 路由；
- confirmation；
- latency class；
- cancellation；
- verifier；
- quiet channel；
- 记忆写入；
- 输出去重。

放 prompt：

- 语言与人格；
- 理解模糊表达；
- 任务规划；
- 解释结果；
- 在允许能力中选择下一步。

放工具 schema：

- 能力语义；
- 输入字段；
- 结果状态；
- 失败码；
- 不可做的事。

## 6. 记忆宏观设计

人格 / 政策不应继续称为记忆 L0。建议拆成两条平面。

### 6.1 配置平面

- Persona；
- Household policy；
- Channel policy；
- Permission policy；
- Capability catalog；
- Model policy。

只有版本化配置可以修改，模型不能自由写。

### 6.2 记忆平面

1. Working Memory：当前会话摘要；
2. Profile：人物稳定偏好和关系；
3. Episodic：发生过的重要事件；
4. Semantic：长期事实与决策；
5. Evidence：原始消息 / 日志 / 图像来源索引。

所有记忆记录至少包含：

```text
id
content
subject
type
source_event_ids
confidence
visibility
created_at
last_confirmed_at
supersedes
```

写入流程：

```text
Evidence → Candidate → 去重/冲突 → 审阅或阈值 → Commit → Index
```

任务状态、审批状态、设备状态不能放入长期记忆。

## 7. 状态与数据

建议以一个本地 SQLite WAL 数据库作为业务真相：

- interactions；
- tasks / task_events；
- action_requests / attempts / verification；
- approvals；
- identities / bindings；
- notifications；
- memory_candidates / memories / evidence；
- routes；
- health / resource state。

Markdown 继续作为：

- 人可读导出；
- OpenClaw 兼容输入；
- 可审阅版本；
- 备份。

但不再承担跨进程事务和 IPC 真相。

## 8. OpenClaw 的长期定位

保留 OpenClaw 的优势：

- 微信渠道；
- LLM provider / fallback；
- session / transcript；
- Agent runtime；
- 插件工具宿主。

从 OpenClaw 移出的职责：

- 任务状态真相；
- 权限策略真相；
- 记忆写入真相；
- 成功判定；
- 通知渠道选择；
- 设备 / 视觉状态。

即：OpenClaw 是基础设施适配器，不是整个家庭管家的领域核心。

微信 shim 只作为短期兼容层；长期要么推动上游暴露可信 sender provenance，要么做独立 channel adapter。

## 9. 未来接入

### 摄像头

- 本地 Vision Service 输出 ObservationEvent；
- 人脸、声纹、Wi‑Fi、衣着、轨迹进入 Identity Resolver；
- 视觉结果默认是 observation，不直接写成事实；
- 主人明确“看看”时才将单帧送云端 VLM；
- 跟随/转向作为独立 capability，有安全停止 verifier。

### 家人

- 每人 actor + conversation lane；
- 默认共享家庭事实，不共享主人私密 profile；
- 权限由 Household policy 定义，不由人脸/声纹直接赋予；
- 主人可配置代理权限。

### 设备与 Home Assistant

未来只作为 capability adapter 接入，不把设备协议写进 Agent prompt。

## 10. 迁移原则

不推倒重写，采用绞杀式迁移：

1. 冻结继续新增单点补丁；
2. 建立 typed event 与 SQLite shadow journal；
3. 建 Capability Registry，把现有专用工具先纳入；
4. 将主线工具缩到高层能力和 task API；
5. 建独立 worker agent / workspace / prompt；
6. 建统一 verifier 与 Notification Broker；
7. 将 approval / task / watermark 文件状态迁入核心；
8. 将 memory staging 接到统一 Memory Service；
9. 最后收缩 VoiceDaemon 与 Guard；
10. 删除被新核心替代的旧补丁和 shim。

任何阶段都必须可回滚，且新旧链路以同一场景测试对比。

## 11. 需要主人确认的架构决策

建议选择：

1. OpenClaw 保留，但定位为渠道 / 模型运行时：是；
2. 业务核心采用 Python 模块化单体 `guanjia-core`：是；
3. 状态真相迁到 SQLite，Markdown 作为人可读视图：是；
4. 主线与 worker 使用不同 agent / prompt / tool surface：是；
5. 日常继续使用 Flash 模型，通过角色策略控制成本：是；
6. 长任务只有 verifier 成功才能宣称完成：是；
7. Persona / Policy 与 Memory 分离：是；
8. 摄像头只发 Observation，不直接写长期事实：是。

确认这些方向后，下一步不是立刻重构代码，而是先写：

- 领域事件 schema；
- CapabilitySpec schema；
- Task / Action / Verification 状态机；
- PromptSpec；
- Memory schema；
- 新旧架构迁移边界。
