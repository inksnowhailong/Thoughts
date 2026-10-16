# 思绪模块化架构 Implementation Plan

> **For agentic workers:** To implement this plan, use `executing-plans` (inline) or parallel GPT 5.6 Terra workers. Follow project Git rules — do not create branches or worktrees unless the user confirms. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不中断当前思绪服务的前提下，以影子运行和灰度切换方式建立输入、核心、记忆、自循环、输出和权限六个解耦模块。

**Architecture:** 保留现有 `VoiceDaemon → OpenClaw → Guard` 作为生产链，新模块全部先以独立目录和独立存储运行。输入、核心、输出、权限和记忆通过版本化事件契约连接；高冲突文件只在最后的集成波次由单一执行者修改。

**Tech Stack:** Python 3、SQLite WAL、本机文件媒体库、TypeScript Guard 插件、OpenClaw、现有 WSL ASR/TTS。

## Global Constraints

- 当前线上链路在影子期仍是唯一实际执行链。
- 日常开发只跑涉及模块的针对性测试，不跑全量静态检查或构建。
- 不把密钥、用户媒体、真实记忆和运行数据库复制到开发快照。
- 新模块默认 feature flag 关闭。
- 影子模式禁止 TTS、微信发送、工具执行、任务派发和 workspace 写入。
- 高冲突文件 `voice.py`、`panel.py`、`panel.html`、`status.py`、`events.py`、`control.py`、Guard 核心文件不得并行编辑。
- 当前微信 provenance shim 已应用；迁移时先保留，统一确认完成后再评估移除。
- 所有跨模块 DTO 使用不可变结构、schema version、correlation id 和 idempotency key。
- 每个实施任务先写失败测试，再写最小实现。

---

## 0. 目标目录与状态真相

```text
src/guanjia/contracts/       跨模块共享的最小事件信封与 ID
src/guanjia/input/           输入、媒体、状态缓存、处理器与交付
src/guanjia/core/            主程序、任务、调度、Agent 适配与模型策略
src/guanjia/memory/          整体记忆、OpenClaw 后端适配、维护与学习
src/guanjia/self_loop/       自主候选、心理历程、视觉筛选与预算
src/guanjia/output/          渠道、语音、送达、回退与打断
src/guanjia/permission/      角色、意图授权、grant 与跨渠道确认

data/input/input.sqlite3     输入模块独占元数据与失败队列
data/media/sha256/           内容哈希媒体库
data/thoughts-core.sqlite3   核心、任务、输出与权限事件真相
OpenClaw workspace           当前文字正文与文字记忆后端
```

模块只能经接口访问其它模块。`input.sqlite3`、`thoughts-core.sqlite3` 和 OpenClaw workspace 不能被其它模块直接查询。

---

## Wave 0：共享契约与影子开关（串行基础）

### Task 0.1：共享事件信封与 ID

**Files:**
- Create: `src/guanjia/contracts/__init__.py`
- Create: `src/guanjia/contracts/ids.py`
- Create: `src/guanjia/contracts/events.py`
- Test: `tests/test_contract_events.py`

**Interfaces:**
- Produces: `EventEnvelope`, `EventId`, `CorrelationId`, `CausationId`, `JsonValue`.

- [ ] **Step 1: 写失败测试**

```python
def test_event_envelope_round_trip_and_idempotency():
    event = EventEnvelope.create(
        event_type='input.message.received',
        correlation_id='corr-1',
        idempotency_key='input-1',
        payload={'input_id': 'input-1'},
    )
    restored = EventEnvelope.from_json(event.to_json())
    assert restored == event
    assert event.schema_version == 1
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
.\.venv\Scripts\python.exe -X utf8 tests\test_contract_events.py
```

- [ ] **Step 3: 实现最小不可变契约**

```python
JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | list['JsonValue'] | dict[str, 'JsonValue']

@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    event_type: str
    occurred_at: datetime
    correlation_id: str
    causation_id: str | None
    idempotency_key: str
    schema_version: int
    payload: Mapping[str, JsonValue]
```

- [ ] **Step 4: 运行测试确认通过**

- [ ] **Step 5: 审查命名与序列化稳定性**

### Task 0.2：Feature Flags 与影子运行约束

**Files:**
- Create: `src/guanjia/core_flags.py`
- Test: `tests/test_core_flags.py`

**Interfaces:**
- Produces: `CoreFlags.from_env()`.

```python
@dataclass(frozen=True)
class CoreFlags:
    input_shadow_v1: bool = False
    core_shadow_v1: bool = False
    core_task_v1: bool = False
    output_shadow_v1: bool = False
    permission_shadow_v1: bool = False
    memory_shadow_v1: bool = False
    self_loop_shadow_v1: bool = False
```

- [ ] 缺失或非法环境变量全部回落 `False`。
- [ ] 影子 flag 不能隐式打开正式执行 flag。
- [ ] 针对性测试通过。

---

## Wave 1：四组纯领域模块（GPT 5.6 Terra 并行）

Wave 1 只新增独立目录和测试，不修改现有主链文件。

### Task A1：输入契约、媒体库与输入账本

**Files:**
- Create: `src/guanjia/input/contracts.py`
- Create: `src/guanjia/input/store.py`
- Create: `src/guanjia/input/media_store.py`
- Create: `src/guanjia/input/delivery.py`
- Test: `tests/test_input_contracts.py`
- Test: `tests/test_input_media_store.py`
- Test: `tests/test_input_delivery.py`

**Core types:**

```python
@dataclass(frozen=True)
class MediaRef:
    media_id: str
    sha256: str
    media_type: Literal['audio', 'image', 'video', 'attachment']
    bytes: int
    metadata: Mapping[str, JsonValue]

@dataclass(frozen=True)
class InputMessage:
    input_id: str
    source: Literal['voice', 'keyboard', 'wechat']
    received_at: datetime
    openclaw_message_ref: str | None
    text: str | None
    media: tuple[MediaRef, ...]
    identity_evidence: tuple['IdentityEvidence', ...]
    current_result: 'CurrentProcessingResult'
    state_context_version: int
    reply_target: Mapping[str, JsonValue] | None
```

**Required behavior:**

- [ ] SQLite WAL 初始化、schema version 和幂等 `input_id`。
- [ ] CAS 流式 SHA-256、原子写、同内容去重、引用计数。
- [ ] 仅返回媒体引用，不在事件中放二进制。
- [ ] 最多三次交付：1s、5s、25s；之后进入失败队列。
- [ ] 人工重放沿用原 `input_id`。
- [ ] 媒体时间 + 容量清理只删除无长期引用内容。

### Task A2：状态缓存与处理器契约

**Files:**
- Create: `src/guanjia/input/state_cache.py`
- Create: `src/guanjia/input/processing.py`
- Create: `src/guanjia/input/confidence.py`
- Test: `tests/test_input_state_cache.py`
- Test: `tests/test_input_processing.py`
- Test: `tests/test_input_confidence.py`

**Required behavior:**

- [ ] 状态包含来源、采集时间、有效期、健康和校准可信度。
- [ ] 过期为 `stale`，异常为 `unknown`，冲突为 `uncertain`。
- [ ] 消息路径只读缓存，不同步执行 collector。
- [ ] 必要处理器失败走备用链；增强失败不阻塞。
- [ ] 不同模型原始分数未经校准不能互比。
- [ ] 新结果只在统一可信度明显更高时覆盖；接近时待复核。

### Task B1：核心事件库与任务真相

**Files:**
- Create: `src/guanjia/core/contracts.py`
- Create: `src/guanjia/core/event_store.py`
- Create: `src/guanjia/core/task_repository.py`
- Create: `src/guanjia/core/task_projection.py`
- Test: `tests/test_core_event_store.py`
- Test: `tests/test_core_task_revisions.py`
- Test: `tests/test_core_task_graph.py`

**Core types:**

```python
@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    revision: int
    parent_task_id: str | None
    goal: str
    constraints: tuple[str, ...]
    material_refs: tuple[str, ...]
    permission_scope: 'PermissionScope'
    priority: int
    success_criteria: tuple[str, ...]
    delivery_requirements: 'DeliveryRequirements'
    resource_requests: tuple['ResourceRequest', ...]
```

**Required behavior:**

- [ ] SQLite 事件追加与投影在同一事务。
- [ ] idempotency key 重放不重复创建任务。
- [ ] 父子任务只通过成果引用连接。
- [ ] 同 task ID 修订递增，旧修订结果不能更新当前投影。
- [ ] 运行中任务重启后进入明确 interrupted。

### Task B2：调度、资源与进度聚合

**Files:**
- Create: `src/guanjia/core/scheduler.py`
- Create: `src/guanjia/core/progress.py`
- Test: `tests/test_core_scheduler.py`
- Test: `tests/test_core_progress.py`

**Required behavior:**

- [ ] 全局上限、能力上限、资源租约三层都通过才启动。
- [ ] 共享/独占资源、GPU、前台桌面、摄像头、文件锁可声明。
- [ ] 仅在安全检查点协作抢占，不强杀步骤。
- [ ] 简单任务不产生中间进度包。
- [ ] 普通任务少量里程碑；长任务低频累计进度。

### Task C1：记忆契约、可信度与后端适配

**Files:**
- Create: `src/guanjia/memory/contracts.py`
- Create: `src/guanjia/memory/confidence.py`
- Create: `src/guanjia/memory/conflicts.py`
- Create: `src/guanjia/memory/repository.py`
- Create: `src/guanjia/memory/openclaw_adapter.py`
- Test: `tests/test_memory_confidence.py`
- Test: `tests/test_memory_repository.py`
- Test: `tests/test_memory_openclaw_adapter.py`

**Required behavior:**

- [ ] 全系统只有一份逻辑记忆。
- [ ] OpenClaw 文件和检索隐藏在适配器后。
- [ ] 所有候选带来源、证据、统一可信度、重要度和媒体引用。
- [ ] 用户明确纠正直接覆盖；其它只在可信度明显更高时覆盖。
- [ ] 当前值直接覆盖旧值，不保留记忆版本历史。
- [ ] 可信度接近时产生冲突请求，不自动覆盖。

### Task C2：自循环纯策略与心理历程状态

**Files:**
- Create: `src/guanjia/self_loop/contracts.py`
- Create: `src/guanjia/self_loop/collector.py`
- Create: `src/guanjia/self_loop/policy.py`
- Create: `src/guanjia/self_loop/state.py`
- Test: `tests/test_self_loop_policy.py`
- Test: `tests/test_self_loop_state.py`

**Required behavior:**

- [ ] 候选事件实时积累，不立即调模型。
- [ ] 普通复盘默认最多每 30 分钟一次且必须有候选。
- [ ] 紧急候选可立即触发。
- [ ] 同主题冷却、每日主动预算、影音/游戏/勿扰抑制。
- [ ] 心理历程独立状态可压缩，不直接等于长期记忆。
- [ ] 外部副作用只能产生活动候选，不能直接执行。

### Task D1：输出领域与送达状态机

**Files:**
- Create: `src/guanjia/output/contracts.py`
- Create: `src/guanjia/output/broker.py`
- Create: `src/guanjia/output/policy.py`
- Create: `src/guanjia/output/speech.py`
- Create: `src/guanjia/output/interruption.py`
- Test: `tests/test_output_broker.py`
- Test: `tests/test_output_delivery.py`
- Test: `tests/test_output_interrupt.py`

**Required behavior:**

- [ ] 主程序生成语义内容；输出模块不改写意思。
- [ ] 完整安全语义片段才能进入 TTS。
- [ ] 渠道状态为 waiting/sent/delivered/failed/fallback。
- [ ] dedup key 重放不重复朗读或发微信。
- [ ] 用户插话取消未播片段和本轮未提交输出。
- [ ] 已提交任务不因语音打断被撤销。

### Task D2：权限领域与任务级授权

**Files:**
- Create: `src/guanjia/permission/contracts.py`
- Create: `src/guanjia/permission/roles.py`
- Create: `src/guanjia/permission/policy.py`
- Create: `src/guanjia/permission/grants.py`
- Create: `src/guanjia/permission/confirmations.py`
- Create: `src/guanjia/permission/authorizer.py`
- Create: `src/guanjia/permission/agent_gate.py`
- Test: `tests/test_permission_policy.py`
- Test: `tests/test_permission_grants.py`
- Test: `tests/test_permission_confirmation.py`
- Test: `tests/test_permission_agent_gate.py`

**Required behavior:**

- [ ] 明确任务意图授权同目的、对象和风险范围内步骤。
- [ ] 关键集合二次确认；普通安装、关机、结束程序和回收站删除不因类别重复确认。
- [ ] grant 绑定角色、任务、意图、对象、风险和有效期。
- [ ] 可信渠道共享同一待办。
- [ ] 主程序解释自然语言，权限模块只接收结构化决定。
- [ ] 核心权限与外部 Agent 沙箱必须同时允许。

---

## Wave 2：适配器与影子运行（模块内并行）

### Task A3：环境状态影子适配

**Files:**
- Create: `src/guanjia/input/adapters/environment.py`
- Create: `src/guanjia/input/projections/eventhub.py`
- Test: `tests/test_input_environment_shadow.py`

- [ ] 包装 `program_context.py`、`program_quiet.py`、`runtime_state.py`、服务健康。
- [ ] 只写新状态缓存和诊断投影。
- [ ] 不让主线读取新缓存，不改变现有静默行为。

### Task A4：语音与微信影子输入

**Files:**
- Create: `src/guanjia/input/adapters/voice.py`
- Create: `src/guanjia/input/adapters/wechat.py`
- Test: `tests/test_input_voice_shadow.py`
- Test: `tests/test_input_wechat_adapter.py`

- [ ] 语音只观察已正式放行的完整段。
- [ ] 写入必须经过有界异步队列，不阻塞 VAD/ASR。
- [ ] 微信影子失败不得阻止 OpenClaw 当前收消息。
- [ ] 影子 sink 不调用 Brain、TTS、工具或微信输出。

### Task B3：主程序事件解析与模型策略

**Files:**
- Create: `src/guanjia/core/agent_runtime.py`
- Create: `src/guanjia/core/main_event_parser.py`
- Create: `src/guanjia/core/model_policy.py`
- Test: `tests/test_core_main_events.py`
- Test: `tests/test_core_model_policy.py`

- [ ] 支持 reply.delta、reply.silent、task.create、task.control、tool.read、output.request、memory.request、turn.completed。
- [ ] 没有 turn.completed 不提交副作用。
- [ ] 非法事件只允许一次修复。
- [ ] 主程序、worker、视觉和记忆角色使用独立模型策略。

### Task B4：OpenClaw 与工作程序适配

**Files:**
- Create: `src/guanjia/core/openclaw_adapter.py`
- Create: `src/guanjia/core/worker_runtime.py`
- Test: `tests/test_core_openclaw_adapter.py`
- Test: `tests/test_worker_runtime.py`

- [ ] 逻辑会话映射到 OpenClaw session。
- [ ] 每任务独立工作会话。
- [ ] 工作程序统一支持启动、进度、子任务、暂停、恢复、取消、修订与结果。
- [ ] 任务上下文不能读取完整主线与长期记忆。

### Task C3：维护计划与媒体关联

**Files:**
- Create: `src/guanjia/memory/maintenance.py`
- Create: `src/guanjia/memory/media.py`
- Modify: `src/guanjia/sleep/rehearsal.py`
- Modify: `src/guanjia/sleep/staging.py`
- Test: `tests/test_memory_maintenance.py`
- Extend: `tests/test_sleep_rehearsal.py`

- [ ] rehearsal 始终只生成计划和差异。
- [ ] 永久/长期/自适应/临时保留等级。
- [ ] 视觉记忆包含摘要、媒体引用和本地特征引用。
- [ ] 冲突、SHA 变化和预算超限整批拒绝。

### Task C4：自循环视觉与压缩

**Files:**
- Create: `src/guanjia/self_loop/visual.py`
- Create: `src/guanjia/self_loop/orchestrator.py`
- Create: `src/guanjia/self_loop/compress.py`
- Test: `tests/test_self_loop_visual.py`
- Test: `tests/test_self_loop_orchestrator.py`

- [ ] 本地变化检测未命中时不调用视觉模型。
- [ ] 同场景默认至少冷却 10 分钟。
- [ ] 心理历程写入 internal_thought 事件。
- [ ] 休眠时压缩为关注点、未解想法、沉默候选、主动表达和等待条件。

### Task D3：输出与权限影子决策

**Files:**
- Create: `src/guanjia/output/adapters/legacy_voice.py`
- Create: `src/guanjia/output/adapters/panel.py`
- Create: `src/guanjia/output/adapters/wechat.py`
- Create: `src/guanjia/permission/audit.py`
- Create: `openclaw/plugins/guanjia-guard/src/core-bridge.ts`
- Test: `tests/test_output_legacy_parity.py`
- Test: `openclaw/plugins/guanjia-guard/src/core-bridge.test.ts`

- [ ] 镜像现有回复、任务通知和权限请求。
- [ ] 只记录新决策，不实际 TTS、微信发送或放行。
- [ ] 修复 3 分钟界面 TTL 与 10 分钟闩锁 TTL 不一致。
- [ ] 确认查询绑定 session、actor 和待办。

---

## Wave 3：高冲突接线（严格串行）

### Task 3.1：输入影子接线

**Owner:** 单一集成执行者

**Files:**
- Modify: `src/guanjia/voice.py`
- Modify: `src/guanjia/events.py`

- [ ] 只增加 shadow observe；原 Brain/text_q 路径保持唯一正式主线。
- [ ] 有界队列满时丢影子记录并记指标，不能阻塞音频。
- [ ] 对照旧 `Heard` 与新 `InputMessage`。

### Task 3.2：核心影子接线

**Files:**
- Create: `src/guanjia/core/main_program.py`
- Create: `src/guanjia/core/shadow.py`
- Modify: `src/guanjia/voice.py`
- Test: `tests/test_core_shadow.py`

- [ ] 影子主程序禁止工具、任务、输出和会话写入。
- [ ] 比较新旧回复意图、任务计划和权限意图。
- [ ] 用户输入可取消低优影子调用。

### Task 3.3：Panel 影子投影

**Files:**
- Modify: `src/guanjia/panel.py`
- Modify: `src/guanjia/panel.html`
- Modify: `src/guanjia/status.py`
- Test: `tests/test_panel_architecture_shadow.py`

- [ ] 展示新旧差异、事件滞后和失败队列。
- [ ] 不增加执行按钮。
- [ ] 不暴露任务目标、工具参数、密钥或媒体路径。

### Task 3.4：Guard 权限影子

**Files:**
- Modify: `openclaw/plugins/guanjia-guard/src/guard.ts`
- Modify: `openclaw/plugins/guanjia-guard/index.ts`
- Modify: `src/guanjia/audittail.py`

- [ ] 旧 Guard 继续真实拦截。
- [ ] 新 Authorizer 只做影子决定并记录差异。
- [ ] 当前已应用微信 provenance shim 保持原样。

---

## Wave 4：灰度切换

### Task 4.1：新核心任务灰度

- [ ] 仅 feature flag 创建的新任务进入 `thoughts-core.sqlite3`。
- [ ] 旧任务继续由现有 `TaskManager` 管理到终态。
- [ ] OpenClaw Worker 仅作为 `WorkerRuntime`。
- [ ] 真实任务修订、父子任务、资源冲突和恢复验收通过。

### Task 4.2：语音输出单渠道切换

- [ ] Output Broker 驱动 TTS，现有播放设备作为 adapter。
- [ ] 句子级流式、去重、插话和静默验收。
- [ ] 旧 `/say` 保留为 feature flag 回退。

### Task 4.3：统一确认

- [ ] 语音、微信和面板统一写 `confirmation_todos`。
- [ ] Guard 降级为外部 Agent 第二层安全闸门。
- [ ] 任务级 grant 和长期授权规则灰度启用。
- [ ] 微信 provenance 继续验证 actor 后再提交确认。

### Task 4.4：记忆与自循环灰度

- [ ] 先启用只读检索包与 maintenance plan。
- [ ] 再启用自动记忆写入。
- [ ] 启用无模型候选与冷却预算。
- [ ] 最后开启低频模型复盘和视觉候选。
- [ ] 每一步均有独立开关和成本统计。

### Task 4.5：主程序切换

- [ ] 日常闲聊先保留旧 Brain。
- [ ] 新主程序先接管任务创建、查询、修改与进度决策。
- [ ] 结构化事件稳定后再接管全部主线。
- [ ] OpenClaw 继续保存文字会话和文字记忆。

---

## 并行执行矩阵

```text
Wave 0
  0.1 Shared contracts ──> 0.2 Flags

Wave 1（四路 Terra 并行）
  A1/A2 输入
  B1/B2 核心任务
  C1/C2 记忆自循环
  D1/D2 输出权限

Wave 2（四路 Terra 并行）
  A3/A4 输入适配
  B3/B4 Agent适配
  C3/C4 维护与自循环
  D3 输出权限影子

Wave 3（单一集成执行者，串行）
  voice → core shadow → panel → guard

Wave 4（逐能力灰度）
  task → voice output → confirmation → memory/self-loop → main
```

## 文件冲突所有权

- `voice.py`：Wave 3 集成执行者独占。
- `events.py`、`control.py`：共享契约负责人定义后，Wave 3 集成执行者接线。
- `panel.py`、`panel.html`、`status.py`：Wave 3 面板接线独占。
- `guard.ts`、`approvals.ts`、`approval-ipc.ts`、`index.ts`、`wechat-provenance.ts`：权限负责人规划，Wave 3 串行接线。
- `sleep/*`：记忆负责人；核心和输入执行者不得同时编辑。
- 新目录 `input/`、`core/`、`memory/`、`self_loop/`、`output/`、`permission/` 在 Wave 1 可并行。

## 影子阶段验收门

进入真实灰度前必须满足：

- 影子输入 7 天无重复正式投递；
- 新旧环境状态的 stale/unknown/uncertain 差异可解释；
- 影子主程序无任何副作用；
- 任务修订与旧结果隔离测试通过；
- 输出去重、插话、渠道失败回退通过；
- 权限影子与旧 Guard 差异完成审阅；
- 输入、任务、输出事件可以按 correlation id 串联；
- 影子模式额外延迟不影响现有语音链；
- 所有 feature flag 默认关闭；
- 回滚只需关闭 flag，不删除新数据库。

## 回滚总则

- 新数据库和媒体库在回滚时保留只读，不反向写旧系统。
- 关闭 feature flag 即恢复旧链。
- 已发送消息、已执行外部动作不重复。
- 核心故障不能降低现有 Guard 安全边界。
- 新核心创建的运行任务切回时进入 interrupted，不自动重跑。
- 每波次保留独立部署备份和兼容适配期。

## 当前自循环实施频率

线上事实：

- 游戏 / 运行态检测：2 秒，本地代码；
- 状态心跳文件：30 秒，本地代码；
- P4 rehearsal 调度器：60 秒 tick，但开关关闭；
- OpenClaw heartbeat：30 分钟配置，已禁用；
- 习惯提取：每天 03:00；
- 天气检查：每天 06:30；
- 技能复盘：每 7 天，目前错误。

新架构建议默认：

- 系统状态采集：2–5 秒，本地代码；
- 摄像头本地变化检测：1–2 帧/秒；
- 候选积累：实时，无模型；
- 普通自循环复盘：有候选时最多 30 分钟一次；
- 紧急候选：即时；
- 同场景视觉模型：至少 10 分钟冷却，明显变化除外；
- 深度整理：休眠时，可额外每天一次；
- 非紧急主动表达：默认每日预算 2–4 次，无价值时为 0。
