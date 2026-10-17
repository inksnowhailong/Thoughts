# 思绪远程模块化重构交接计划

> **For agentic workers:** To implement this plan, use `executing-plans` inline. Follow project Git rules; continue on the existing confirmed branch and do not create another branch or worktree. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将已经在本机 D 盘镜像完成的模块化代码原样同步到 Tailscale 远程思绪电脑，在隔离开发副本完成接线、灰度和硬件验收，禁止重新编写已有模块。

**Architecture:** 生产目录继续运行旧链路；所有新模块只进入远程隔离开发副本。先复制并验证六个模块，再串行接入语音、微信、Panel 和 Guard，最后通过默认关闭的 feature flag 灰度。

**Tech Stack:** Python 3、SQLite WAL、OpenClaw、TypeScript Guard、Tailscale SSH、乐橙 OpenAPI、现有本地摄像头云台协议。

## Global Constraints

- 远程主机：SSH 别名 `home`，Tailscale 设备 `inksnow`，地址 `100.68.77.56`，用户 `inksnow\inksn`。
- 远程生产目录：`F:\coding\guanjia`。该目录 `master` 已有大量未提交和未跟踪改动，禁止覆盖、清理、reset 或直接开发。
- 远程开发目录：`F:\coding\guanjia-v2-development`。
- 远程开发分支：`feat/core-add-modular-shadow-20261016`。
- 本机待同步代码源：`D:\coding\architecture-plan-snapshot`。
- 线上服务、真实记忆、媒体、数据库、模型、密钥和运行日志不得复制进开发副本。
- Wave 3 之前不得修改生产目录，不得重启远程生产语音、网关、TTS 或 Guard。
- 日常开发不运行全量 `tsc`、全量 ESLint 或构建；只运行高价值针对测试。
- 只保留权限、安全、重复副作用、数据丢失和关键主流程测试；不新增低价值边角测试。
- 简单代码、文档、DTO 和机械同步使用快速便宜模型；并发、持久化、权限和生产接线才使用高推理模型。
- 乐橙云适配器只负责设备信息、FLV、报警与录像查询；旋转、俯仰和停止继续使用既有本地协议，禁止接入云 `controlPTZ`。

---

## 1. 当前真相

### 1.1 远程 Git 基线

远程开发副本已有提交：

```text
0a20f22 feat(memory): 新增记忆与自循环领域基础
88f9195 feat(core): 新增共享事件契约和影子开关
37ccfa8 feat(core): 固化模块化重构开发基线
```

当前远程开发副本还存在未提交的 `output/`、`permission/` 与对应测试。这些文件来自本轮重构，应由 D 盘最终版本覆盖，不要重新实现。

### 1.2 已完成并通过关键审查

- Wave 0：共享事件契约、ID、feature flags。
- Wave 1 输入：输入账本、原子 outbox、处理结果 CAS、交付租约、媒体 CAS journal。
- Wave 1 核心：任务事件、完整 TaskSpec 历史、结果/证据幂等、调度和进度。
- Wave 2 输入：环境、语音、微信影子适配器。
- 输出 Broker：规范化 SQLite、发送前 claim、稳定 operation ID、超时 fallback。
- Worker Runtime：SQLite 恢复、revision 隔离、取消/结果竞态、外部成功恢复。
- Sandbox：完整请求字段 HMAC 签名、issuer/key 校验、fail-closed。
- 记忆与自循环：持久候选、review fencing、心理状态、预算、outbox、媒体 generation fencing。
- 乐橙只读输入适配器：官方签名、设备状态、FLV、报警、云录像查询；无 PTZ 方法。

### 1.3 仍需最终组合核验

- 权限 Todo Store 最新严格迁移与并发赢家回读。
- `AuthorizationService → AgentGate → PermissionAudit` 类型和 fingerprint 组合。
- 主程序结构化事件、模型策略与 Worker 的组合。
- 记忆、自循环和输出三者的 outbox 对接。

### 1.4 尚未完成

- Wave 3：`voice.py`、微信入站、Panel、Guard 的影子接线。
- Wave 4：任务、语音输出、统一确认、记忆/自循环、主程序灰度。
- 新麦克风和摄像头实机验收。
- 摄像头本地云台旧协议接入。
- 生产发布。

---

## 2. 复制清单

### Task 1：复制已完成模块

**Source:** `D:\coding\architecture-plan-snapshot`

**Destination:** `home:F:\coding\guanjia-v2-development`

**Copy directories:**

```text
src/guanjia/input/
src/guanjia/core/
src/guanjia/memory/
src/guanjia/self_loop/
src/guanjia/output/
src/guanjia/permission/
```

**Copy individual files:**

```text
src/guanjia/sleep/staging.py
src/guanjia/sleep/rehearsal.py
openclaw/plugins/guanjia-guard/src/core-bridge.ts
openclaw/plugins/guanjia-guard/src/core-bridge.test.ts
docs/plans/2026-10-16-thoughts-user-architecture-v1.md
docs/plans/2026-10-17-remote-development-handoff.md
```

**Copy tests:**

```text
tests/test_contract_events.py
tests/test_core_flags.py
tests/test_input_*.py
tests/test_core_*.py
tests/test_worker_runtime.py
tests/test_memory_*.py
tests/test_self_loop_*.py
tests/test_output_*.py
tests/test_permission_*.py
tests/test_sleep_rehearsal.py
tests/test_p4_staging.py
```

**Do not copy or overwrite:**

```text
src/guanjia/voice.py
src/guanjia/panel.py
src/guanjia/panel.html
src/guanjia/status.py
src/guanjia/control.py
src/guanjia/events.py
openclaw/plugins/guanjia-guard/src/guard.ts
openclaw/plugins/guanjia-guard/index.ts
data/
tmp/
models/
*.sqlite*
*.log
*.jsonl
```

- [x] 在远程开发副本执行 `git status --short --branch`，确认分支正确。
- [x] 将上述目录和文件从 D 盘打包流式复制到远程开发副本。
- [x] 再次执行 `git status --short`，确认没有触及禁止覆盖文件。
- [x] 同步包使用显式白名单，不含密钥、真实媒体、运行数据库和禁止覆盖文件。

---

## 3. 高价值验证

### Task 2：运行模块契约测试

**Working directory:** `F:\coding\guanjia-v2-development`

远程 Python：

```text
F:\python\python.exe
```

- [x] 运行输入、核心、Worker、权限、输出、记忆和自循环的高价值针对测试。
- [x] 只修复真实失败；没有为了覆盖率补边角测试。
- [x] 未运行全量静态检查或构建。

远程验证记录（2026-10-17）：

```text
输入媒体、交付：PASS
任务修订、Worker：PASS
输出重复副作用与超时恢复：PASS
权限 Todo、Sandbox：PASS
记忆媒体 fencing 与迁移：PASS
自循环持久状态与 outbox：PASS
乐橙官方签名与只读边界：PASS
```

建议命令：

```powershell
$env:PYTHONPATH='src'
F:\python\python.exe -X utf8 tests\test_input_contracts.py
F:\python\python.exe -X utf8 tests\test_input_delivery.py
F:\python\python.exe -X utf8 tests\test_input_media_store.py
F:\python\python.exe -X utf8 tests\test_core_task_revisions.py
F:\python\python.exe -X utf8 tests\test_core_scheduler.py
F:\python\python.exe -X utf8 tests\test_worker_runtime.py
F:\python\python.exe -X utf8 tests\test_output_delivery.py
F:\python\python.exe -X utf8 tests\test_permission_confirmation.py
F:\python\python.exe -X utf8 tests\test_permission_agent_gate.py
F:\python\python.exe -X utf8 tests\test_memory_maintenance.py
F:\python\python.exe -X utf8 tests\test_self_loop_orchestrator.py
F:\python\python.exe -X utf8 tests\test_input_imou_adapter.py
```

### Task 3：分模块提交

现有分支 scope 为 `core`，提交信息统一使用 `feat(core)`：

```text
feat(core): 新增输入与媒体领域模块
feat(core): 新增任务内核与工作程序适配
feat(core): 完善记忆与自循环持久编排
feat(core): 新增输出与权限安全边界
feat(core): 新增乐橙摄像头只读输入适配
```

- [x] 每个提交前检查暂存范围。
- [x] 禁止把生产目录既有未提交内容带入开发分支。
- [x] 不 push、不 merge、不 tag，除非用户再次明确要求。

---

## 4. Wave 3 串行接线

### Task 4：语音完整段影子输入

**Modify:** `src/guanjia/voice.py`

- [x] 只在 `_on_heard()` 正式 `accept` 后调用 `AcceptedInputIssuer`。
- [x] 使用有界非阻塞 `VoiceShadowAdapter`。
- [x] 队列满、sink 超时或异常只记 shadow 指标，不能阻塞 VAD、ASR 或 `text_q`。
- [x] `GUANJIA_INPUT_SHADOW_V1=false` 时不创建适配器。

### Task 5：主程序影子

**Create:** `src/guanjia/core/main_program.py`

**Create:** `src/guanjia/core/shadow.py`

**Modify:** `src/guanjia/voice.py`

- [x] 只处理完整轮次；打断或新输入取消低优影子调用。
- [x] `MainAgentRuntime(shadow=True)` 禁止 commit。
- [x] 不复用生产主会话，不创建真实任务，不调用输出渠道。

### Task 6：Panel 只读投影

**Modify:** `src/guanjia/panel.py`

**Modify:** `src/guanjia/panel.html`

- [x] 只展示开关、队列、accepted/dropped/failure、差异字段名。
- [x] 不展示任务目标、工具参数、session key、审批 ID、密钥或媒体路径。
- [x] 不增加执行按钮。

### Task 7：Guard 影子授权

**Modify:** `openclaw/plugins/guanjia-guard/src/guard.ts`

**Modify:** `openclaw/plugins/guanjia-guard/index.ts`

- [x] 旧 Guard 仍是唯一真实裁决者。
- [x] 新权限模块只记录 would-allow / would-deny / would-confirm。
- [x] 不调用新 todo resolve，不消费 latch，不改变 hook 返回值。
- [x] 当前微信 provenance 验证保持原样。

---

## 5. 摄像头与新硬件

### Task 8：乐橙只读云输入

**Existing implementation:** `src/guanjia/input/adapters/imou.py`

- [x] 在远程本机安全配置中写入 appId、appSecret、accessToken、deviceId、channelId；禁止写进仓库、聊天或模型上下文。
- [x] 调用设备状态和能力集，确认设备在线并具有 PT/PTZ 能力。
- [ ] 创建或查询 FLV 地址，并将 URL 只作为短期引用。
- [x] 本地解码 1–2 fps 做变化检测；无变化不调用视觉模型。
- [ ] 报警 API 作为事件候选，不持续轮询模型。

### Task 9：本地云台协议

- [x] 在远程生产代码或历史目录定位原云台协议实现。
- [x] 将旧协议封装为输出模块 `CameraPtzPort`。
- [x] 提供 `move(horizontal, vertical, duration)`、`stop()` 和必要的 `locate()`。
- [x] 禁止调用乐橙云 `controlPTZ`。
- [x] 云台独占资源交给 Scheduler，避免两个 Worker 同时控制。

### Task 10：实机验收

- [x] 枚举新麦克风，确认采样率、通道、默认设备和重启恢复。
- [x] 验证完整语音 → ASR → 主线，不做全量音频测试。
- [x] 验证摄像头在线、FLV 首帧、快照引用和本地变化检测。
- [x] 验证左/右/上/下/停止，每次短时动作后立即停止。
- [x] 验证断网、设备离线和凭据过期只降级输入状态，不阻塞主程序。

---

## 6. 模型建议

### 开发阶段

| 任务 | 建议模型 |
|---|---|
| 文件复制、DTO、文档、简单适配、错误文案 | Composer Fast 或同级快速便宜模型 |
| 一般 Python 接线、乐橙只读 API、Panel 展示 | GPT 5.6 Terra Medium |
| `voice.py` 并发、SQLite 恢复、输出重复副作用 | GPT 5.6 Terra Medium |
| Guard、权限、生产切换、无法复现的并发故障 | GPT 5.6 Sol High，仅必要时 |

新实例建议默认使用 **GPT 5.6 Terra Medium** 执行本计划。机械复制和小修切到快速便宜模型；不需要全程高强度思维模型。

### 思绪运行时

- 日常聊天、简单分类、格式转换、普通工具选择：优先 GLM 免费额度。
- 简单 Worker：GLM 或其它低成本模型。
- 视觉：本地变化检测优先；有变化才调用视觉模型。
- 复杂规划、权限歧义和失败恢复：才升级高能力模型。
- 模型选择必须来自 `ModelPolicy` 配置，不能在业务代码写死。

---

## 7. 完成判定

- [x] D 盘已有模块已复制到远程开发副本，没有重新编写。
- [x] 远程生产目录零改动。
- [x] 高价值针对测试通过。
- [x] Wave 3 四个接线开关默认关闭。
- [x] 新麦克风与摄像头通过实机主流程验收。
- [x] 关闭 feature flag 可立即回到旧链路。
- [ ] 用户明确批准后，才允许进入生产合并与发布。
