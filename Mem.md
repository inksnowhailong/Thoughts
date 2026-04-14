# 思绪第一版完整方案

## 一、产品设计

### 1. 产品定位

- `思绪` 是一个桌面端 AI 助手应用。
- 它既能响应用户主动输入，也能通过定时轮询触发一定的主动行为。
- 第一版的目标不是自研完整 Agent 系统，而是先把一个真实可运行的闭环做出来。

### 2. 第一版核心目标

- 启动应用后，自动拉起 OpenClaw。
- 用户可以直接和 OpenClaw 对话，并在界面上看到回复。
- 系统可以每隔一段时间主动触发一次 OpenClaw 检查，判断是否有值得提醒用户的内容。
- 运行状态、错误状态、自动提醒都能在界面中清楚展示。

### 3. 第一版边界

第一版采用 `思绪 + OpenClaw` 的架构。

#### `思绪` 负责

- 桌面应用界面。
- 用户输入收集。
- 定时轮询和事件触发。
- 启动、检测、管理 OpenClaw。
- 向 OpenClaw 发消息。
- 接收 OpenClaw 输出并展示。
- 展示运行状态、调用状态、错误状态。
- 保存本地配置、事件日志、会话映射。

#### OpenClaw 负责

- 记忆能力。
- 搜索能力。
- Agent 推理能力。
- 模型调用能力。
- 工具调用能力。
- 最终输出生成。
- 用户偏好的 Markdown 化保存。

### 4. 第一版不做

- 自研长期或短期记忆系统。
- 自研 RAG 管线。
- 自研多 Agent 主意识 / 潜意识协作。
- 自研复杂上下文构建器。
- 自研工具调度系统。
- 高风险自动执行动作。
- 复杂人格层设计。

## 二、实现方案

### 1. 总体架构

第一版架构分为三层：

- 前端 Vue：
  - 输入框
  - 对话展示
  - 自动提醒展示
  - 错误提示
  - 状态栏
- Rust Runtime：
  - OpenClaw Gateway 子进程管理
  - CLI 子进程调用封装（`openclaw agent`）
  - 自动轮询调度
  - 本地配置和日志存储
  - Tauri 命令与事件桥接
- OpenClaw Gateway：
  - 接收消息（通过 CLI `openclaw agent` 命令桥接）
  - 读取记忆
  - 自行决定是否搜索外部内容
  - 推理和生成回复

### 2. 关键实现决策

#### 2.1 OpenClaw 启动方式

- 由 `思绪` 在应用启动时自动拉起 OpenClaw Gateway 子进程。
- 启动命令：`openclaw gateway`（可通过 `--profile thoughts --port 18789` 指定隔离环境）。
- 停止命令：`openclaw gateway stop`。
- 健康检查命令：`openclaw health`。
- Gateway token 通过环境变量 `OPENCLAW_GATEWAY_TOKEN` 注入。
- profile `thoughts` 已初始化完成。
- v1 固定使用同一个 agent id：`main`。

推荐约定：

- profile：`thoughts`
- host：`127.0.0.1`
- port：`18789`
- agent id：`main`

这样做的好处：

- `思绪` 的 OpenClaw 环境和用户其他 OpenClaw 场景隔离。
- agent id 和记忆空间稳定，不会每次启动都变化。
- 不需要在第一版里处理复杂 agent 管理。

#### 2.2 通信方式

- 第一版采用 **CLI 子进程调用**作为唯一正式通信方式。
- `思绪` 通过 Rust 调用 `openclaw agent` 命令与 Gateway 交互。
- Gateway 本身是 WebSocket 协议（非 HTTP REST），CLI 命令内部封装了 WS 通信细节。
- 第一版不直接对接 WebSocket / ACP 协议，降低实现复杂度。
- 后续版本可升级为 WebSocket 直连以获得原生流式输出。

CLI 调用约定：

```bash
# 用户消息
openclaw agent -m "<消息内容>" --json --session-id "<session-key>" --timeout 45

# 自动轮询
openclaw agent -m "<轮询模板>" --json --session-id "<session-key>" --timeout 45
```

返回格式为一次性 JSON（非流式），结构见 4.2 节。

#### 2.3 会话策略

第一版不做复杂多会话系统，只固定两条逻辑会话：

- `thoughts-user-main`
  - 用于用户主动对话
- `thoughts-auto-main`
  - 用于自动轮询触发

设计原因：

- 用户对话和自动提醒分开，避免上下文互相污染。
- 仍然复用同一个 agent 的长期记忆。
- 本地会话映射逻辑足够简单。

#### 2.4 主动行为策略

- 主动性由 `思绪` 自己的轮询器触发，而不是由 OpenClaw 的内部定时能力主导。
- 默认每 10 分钟触发一次。
- 轮询频率必须可配置。
- 后续要预留 quiet hours，比如夜间静默时间。
- 用户对话期间，自动轮询暂停；OpenClaw 回答完用户消息后，重新开始计时。
- 自动提醒不设上限，设计目标就是一个话痨 AI。

### 3. 核心流程

#### 3.1 应用启动流程

1. 启动 `思绪` 应用。
2. Rust 读取本地配置。
3. Rust 启动 OpenClaw Gateway 子进程。
4. Rust 对 OpenClaw 做健康检查。
5. 如果成功，运行状态切换为 `ready`。
6. 如果失败，前端显示错误，停止自动轮询。

#### 3.2 用户消息流程

1. 用户在前端输入消息。
2. 前端调用 Tauri command，把消息发给 Rust。
3. Rust 将消息包装为标准文本事件模板。
4. Rust 通过 `openclaw agent -m "..." --json --session-id "thoughts-user-main"` 子进程调用 OpenClaw。
5. OpenClaw 执行记忆检索、搜索、推理并生成回复。
6. Rust 解析 CLI 返回的 JSON，提取 `result.payloads[].text`，回传前端。
7. 前端将结果作为普通对话展示。
8. Rust 写入事件日志，并更新 session 映射时间。
9. 重置自动轮询计时器（从当前时刻重新开始 10 分钟倒计时）。

#### 3.3 自动轮询流程

1. 调度器按配置时间触发。
2. Rust 判断当前是否允许执行：
   - Gateway 是否可用
   - 当前是否在 quiet hours
   - 上一轮自动任务是否仍在运行
   - 用户是否正在对话中（如果是，跳过本轮）
3. 如果允许执行，则生成自动轮询事件文本。
4. Rust 通过 `openclaw agent -m “...” --json --session-id “thoughts-auto-main”` 子进程调用 OpenClaw。
5. OpenClaw 判断本轮是否有值得提醒用户的内容。
6. 如果返回文本包含 `HEARTBEAT_OK`，则只记录日志，不展示消息。
7. 如果返回普通文本，则作为”自动提醒”插入前端消息流。
8. 如果失败，自动重试 1 次；仍失败则记录错误并跳过本轮。

### 4. 输入输出协议

#### 4.1 输入协议

第一版输入不做复杂 JSON 语义协议，统一用文本协议发送给 OpenClaw。

##### 用户消息模板

```text
[Thoughts Event]
type: user_message
time: 2026-04-13T08:30:00+08:00
source: ui

[User Message]
<用户输入内容>

[Instructions]
- 直接回复用户。
- 如果需要额外权限才能行动，明确说明。
```

##### 自动轮询模板

```text
[Thoughts Event]
type: auto_poll
time: 2026-04-13T08:30:00+08:00
source: scheduler
topics:
- 天气
- 节日
- 新闻大事
- 设备状态
- 用户可能感兴趣的内容

[Instructions]
- 请根据当前时间和上下文，自行判断本轮值得关注的信息。
- 如果没有值得提醒用户的内容，回复 HEARTBEAT_OK。
- 如果有值得告诉用户的内容，生成一段简短自然的中文提醒。
- 不执行高风险动作。
```

#### 4.2 输出协议

第一版通过 `openclaw agent --json` 获取 JSON 输出，非流式（一次性返回）。

##### CLI 返回 JSON 结构

```json
{
  "runId": "uuid",
  "status": "ok",
  "summary": "completed",
  "result": {
    "payloads": [
      {
        "text": "AI 回复的文本内容",
        "mediaUrl": null
      }
    ],
    "meta": {
      "durationMs": 1423,
      "agentMeta": {
        "sessionId": "uuid",
        "provider": "custom-api-siliconflow-cn",
        "model": "deepseek-ai/DeepSeek-V3.2"
      },
      "error": null
    }
  }
}
```

##### 关键字段提取规则

- 文本内容：`result.payloads[0].text`
- 是否成功：`status === "ok"` 且 `result.meta.error === null`
- 错误信息：`result.meta.error.message`（当 error 非 null 时）
- 错误类型：`result.meta.error.kind`（如 `context_overflow`）

##### 展示规则

- 用户主动消息返回：
  - 按普通 AI 回复展示。
- 自动轮询返回：
  - 如果 `payloads[0].text` 包含 `HEARTBEAT_OK`，不展示。
  - 如果是普通文本，作为自动提醒展示。
- 错误返回：
  - 显示为系统错误，不伪装成普通回复。
  - 展示 `error.kind` 和 `error.message`。

##### 流式输出说明

第一版不支持流式输出（CLI `--json` 模式为一次性返回）。
后续版本可通过 WebSocket 直连 Gateway 实现原生流式输出。

### 5. Rust 后端设计

#### 5.1 Rust 侧职责

- 应用启动与关闭。
- OpenClaw Gateway 子进程管理（启动 `openclaw gateway`、检测存活 `openclaw health`、停止 `openclaw gateway stop`）。
- CLI 子进程调用封装（`openclaw agent -m ... --json`）。
- 自动轮询调度。
- 配置读取和保存。
- 事件日志写入。
- 会话映射保存。
- 向前端推送状态与结果。

#### 5.2 建议目录结构

```text
src-tauri/src/
  app/
    bootstrap.rs        # 应用启动装配
    state.rs            # 全局运行时状态
  runtime/
    openclaw_manager.rs # Gateway 子进程生命周期管理
    scheduler.rs        # 自动轮询调度器
  cli/
    agent_client.rs     # openclaw agent CLI 调用封装
    types.rs            # CLI 返回 JSON 反序列化结构
  storage/
    config_store.rs     # 配置文件读写
    event_log_store.rs  # JSONL 事件日志
    session_map_store.rs # 会话映射持久化
  models/
    app_config.rs       # 配置数据模型
    event.rs            # 事件数据模型
    message.rs          # 消息数据模型
    runtime_status.rs   # 运行时状态枚举
  interfaces/
    commands.rs         # Tauri commands
    events.rs           # Tauri events
  lib.rs
```

#### 5.3 运行时状态模型

Rust 内部需要维护统一状态，至少包括：

- Gateway 状态：
  - `stopped`
  - `starting`
  - `ready`
  - `error`
- 自动轮询状态：
  - `idle`
  - `running`
  - `retrying`
- 最近一次错误。
- 最近一次自动轮询时间。
- 最近一次用户消息时间。

### 6. 本地数据设计

#### 6.1 配置文件

配置使用 JSON 文件，保存在 Tauri app data 目录。

建议字段：

```json
{
  "openclawBinaryPath": "openclaw",
  "gatewayPort": 18789,
  "profile": "thoughts",
  "agentId": "main",
  "userSessionKey": "thoughts-user-main",
  "autoSessionKey": "thoughts-auto-main",
  "autoPollEnabled": true,
  "autoPollMinutes": 10,
  "quietHours": {
    "enabled": false,
    "start": "23:00",
    "end": "08:00"
  },
  "autoTopics": [
    "天气",
    "节日",
    "新闻大事",
    "设备状态",
    "用户可能感兴趣的内容"
  ],
  "requestTimeoutSeconds": 45
}
```

#### 6.2 事件日志

- 使用 JSONL 格式。
- 只保存事件级摘要，不保存完整调用历史。
- 推荐按天分文件。

推荐路径：

```text
app-data/logs/events/2026-04-13.jsonl
```

单条事件记录建议字段：

```json
{
  "eventId": "evt_20260413_001",
  "trigger": "auto_poll",
  "sessionKey": "thoughts-auto-main",
  "sentAt": "2026-04-13T08:30:00+08:00",
  "repliedAt": "2026-04-13T08:30:12+08:00",
  "sentContent": "自动轮询模板内容摘要",
  "receivedContent": "AI 返回内容摘要",
  "status": "success",
  "resultKind": "heartbeat_ok"
}
```

#### 6.3 会话映射

- 使用单独 JSON 文件保存。
- 用于记录本地逻辑会话与 OpenClaw session key 的固定关系。

建议内容：

```json
{
  "userMain": {
    "agentId": "main",
    "sessionKey": "thoughts-user-main",
    "updatedAt": "2026-04-13T08:30:00+08:00"
  },
  "autoMain": {
    "agentId": "main",
    "sessionKey": "thoughts-auto-main",
    "updatedAt": "2026-04-13T08:30:00+08:00"
  }
}
```

### 7. 前端设计

#### 7.1 第一版页面目标

- 不追求复杂设计，优先把状态和消息闭环跑通。
- 必须清楚展示：
  - OpenClaw 当前状态
  - 当前是否正在请求
  - 最近一次错误
  - 自动轮询是否触发

#### 7.2 页面结构

- 顶部状态栏：
  - Gateway 状态
  - 自动轮询状态
  - 最后一次轮询时间
- 中部消息区：
  - 用户消息
  - AI 回复
  - 自动提醒
  - 系统错误提示
- 底部输入区：
  - 输入框
  - 发送按钮
  - 手动触发自动检查按钮

#### 7.3 消息展示规则

- 普通对话按标准消息流展示。
- 自动轮询的有效输出加“自动提醒”标识。
- 错误消息单独渲染，不混入普通对话气泡。

### 8. 错误与恢复策略

#### 8.1 OpenClaw 启动失败

- 前端显示明显错误。
- Gateway 状态切为 `error`。
- 自动轮询不启动。

#### 8.2 OpenClaw 运行中退出

- 前端显示错误。
- 自动轮询停止。
- 用户主动发送消息时直接返回“服务不可用”。

#### 8.3 请求超时

- 用户请求：
  - 直接返回错误。
- 自动轮询：
  - 自动重试 1 次。
  - 再失败则记日志并跳过本轮。

#### 8.4 调用失败

- 自动事件失败：
  - 重试 1 次。
  - 不成功则本轮结束。
- 用户主动事件失败：
  - 立即返回错误。
  - 不自动重试。

### 9. 对现有工程的改造方向

当前工程中的：

- [lib.rs](/Users/zhanghailong/Desktop/coding/thoughts/src-tauri/src/lib.rs)
- [core.rs](/Users/zhanghailong/Desktop/coding/thoughts/src-tauri/src/thoughts/core.rs)
- [App.vue](/Users/zhanghailong/Desktop/coding/thoughts/src/App.vue)

都还是实验性原型。

第一版正式方案下：

- `lib.rs` 需要从示例命令入口改为应用运行时装配入口。
- `thoughts/core.rs` 需要从本地天气轮询逻辑，替换为 OpenClaw 调度器和调用管线。
- `App.vue` 需要从默认模板页，替换为真正的对话和状态界面。

### 10. 第一版验收标准

第一版完成的判断标准如下：

- 启动 `思绪` 时，OpenClaw Gateway 能被自动拉起。
- 前端能显示 Gateway 是否就绪。
- 用户发一条消息后，能够拿到 OpenClaw 的回复并展示。
- 自动轮询能按 10 分钟配置触发。
- 自动轮询无内容时不打扰用户，有内容时能作为主动提醒展示。
- 启动失败、运行中退出、请求超时等错误都能清楚提示。
- 本地事件日志和 session 映射能正常落盘。

## 三、已确认的决定

所有前置决策已定，可以直接进入编码阶段。

### 1. OpenClaw 启动与管理

- 启动命令：`openclaw gateway`（可加 `--profile thoughts --port 18789`）
- 停止命令：`openclaw gateway stop`
- 健康检查：`openclaw health`
- profile `thoughts` 已初始化完成
- Gateway token 通过环境变量 `OPENCLAW_GATEWAY_TOKEN` 注入

### 2. 通信方式与返回结构

- 第一版使用 CLI 子进程调用：`openclaw agent -m “...” --json --session-id “...”`
- Gateway 底层是 WebSocket 协议，但 CLI 封装了通信细节，第一版不直接对接 WS
- 返回为一次性 JSON，文本在 `result.payloads[0].text`，错误在 `result.meta.error`
- 第一版不支持流式输出，后续升级 WebSocket 直连时再实现

### 3. 自动轮询策略

- 默认 10 分钟触发一次，频率可配置
- 用户对话期间暂停轮询，对话结束后重新开始计时
- 不设提醒上限，定位就是话痨 AI

### 4. 本地日志内容

每条日志记录：发送了什么、收到了什么、发送时间、回复时间

### 5. 首版实现顺序

1. OpenClaw Gateway 启动和健康检查
2. 打通 CLI 子进程调用
3. 前端最小收发闭环
4. 事件日志和 session 映射
5. 自动轮询调度

### 6. 已解决的问题

- ~~context_overflow~~ — 原因是 `openclaw.json` 中 DeepSeek-V3.2 的 `contextWindow` 误配为 16000（少一个零），已修正为 160000。对话验证正常，首次请求消耗约 13,375 input tokens。



