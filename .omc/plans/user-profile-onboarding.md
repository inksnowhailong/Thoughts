# 用户画像与 Onboarding 实现计划 (v2 — 整合审查反馈)

**日期**: 2026-04-14
**复杂度**: MEDIUM
**涉及文件**: 9 个文件（1 新建 Rust + 2 新建 Vue 组件 + 6 修改）

---

## RALPLAN-DR 摘要

### Principles（原则）

1. **最小侵入** — 在现有架构上增量添加，不重构已有模块
2. **标记检测复用** — 沿用 `HEARTBEAT_OK` 的模式，但升级为行锚定匹配
3. **Schema 自由** — Rust 只校验 JSON 合法性，不定义画像结构体字段
4. **状态驱动 UI** — 前端通过 AppState 中的 AppPhase 枚举决定渲染模式
5. **调度器协同** — 调度器始终启动，但通过信号机制等待 onboarding 完成后再进入轮询循环

### Decision Drivers（决策驱动）

1. **开发速度** — v1 快速交付，避免引入新依赖或复杂抽象
2. **与现有模式一致** — 标记检测、状态推送、CLI 调用沿用已有模式
3. **状态转换可靠性** — 调度器延迟启动需要确定性的信号机制

### Viable Options（可行方案）

#### 方案 A+: AppPhase 枚举 + ProfileStore struct + 单文件存储（推荐）

在 `AppState` 添加 `app_phase: AppPhase` 枚举（Onboarding / Active），画像存储封装为 `ProfileStore` struct（持有 `path: PathBuf`），作为 Tauri managed state 注入。调度器始终 spawn，通过 oneshot channel 等待 onboarding 完成信号。

- **优点**: 状态语义清晰，扩展自然，ProfileStore 内聚（4 个方法 + path 字段），调度器启动时序确定性高
- **缺点**: 比纯 bool 多约 20 行代码
- **适用**: v1 快速交付且为 v2 留好扩展点

#### 方案 B: 独立 ProfileManager 模块 + 事件总线

创建独立模块管理画像生命周期，通过事件总线与 scheduler 解耦通信。

- **优点**: 解耦最彻底
- **缺点**: v1 仅 load/save/merge 三操作，事件总线引入不必要的间接层。但 Architect 指出方案 B 的"过度设计"判断被高估——ProfileStore struct 本身已接近 B 的核心价值，实际成本差异仅约 15 行
- **适用**: 未来画像功能复杂化（版本化、迁移、导出）时升级

**选择方案 A+**。吸收了方案 B 的合理部分（ProfileStore struct），同时保持方案 A 的简约性。

---

## ADR（架构决策记录）

- **Decision**: 方案 A+ — AppPhase 枚举 + ProfileStore struct + 单文件存储
- **Drivers**: 开发速度、模式一致性、状态转换可靠性
- **Alternatives**: 方案 A（bool，过于简化）、方案 B（独立 Manager，v1 不需要事件总线）
- **Why chosen**: A+ 在 A 的简约性和 B 的结构性之间取得平衡。枚举成本极低但语义清晰；ProfileStore struct 让 app_data_dir 不需要在函数间传递
- **Consequences**: 画像无版本历史；shallow merge 对嵌套结构行为不可预测（通过 prompt 约束 JSON 为扁平结构缓解）
- **Follow-ups**: v2 可考虑画像变更日志；可考虑画像 Schema 验证；清理废弃的 onboarding session

---

## 调度器延迟启动机制（关键设计）

**问题**: `lib.rs` 中 `run_scheduler` 通过 `tauri::async_runtime::spawn` 一次性启动，所有资源（state, app_handle, app_data_dir, stop_rx）被 move 进闭包。onboarding 完成后无法再次调用。

**方案**: 调度器**始终 spawn**，但在 onboarding 场景下，先等待一个 oneshot 信号再进入轮询循环。

```rust
// lib.rs setup() 中：
let (onboarding_tx, onboarding_rx) = tokio::sync::oneshot::channel::<()>();

// 如果不需要 onboarding，立即发送信号
if !needs_onboarding {
    let _ = onboarding_tx.send(());
} else {
    // 把 tx 存入 AppState，供 send_onboarding_message 完成时发送
    // AppState 新增: pub onboarding_signal: Option<tokio::sync::oneshot::Sender<()>>
}

// 调度器始终 spawn
tauri::async_runtime::spawn(async move {
    // 等待 onboarding 完成信号（非 onboarding 场景立即通过）
    let _ = onboarding_rx.await;
    // 然后正常进入轮询循环
    scheduler::run_scheduler(state_for_boot, app_handle, app_data_dir, stop_rx).await;
});
```

**send_onboarding_message 完成时**:
```rust
// 从 AppState 中取出 onboarding_signal 并发送
let signal = {
    let mut s = state.write().await;
    s.app_phase = AppPhase::Active;
    s.onboarding_signal.take()
};
if let Some(tx) = signal {
    let _ = tx.send(());
}
```

---

## 标记解析算法（精确规范）

### 格式要求

标记必须**独占一行**，JSON 块从**下一行**开始：

```
一些 AI 对话文本...

ONBOARDING_COMPLETE
{"nickname": "小明", "age": 25, "hobbies": ["游戏", "编程"]}
```

### 解析算法 `extract_marker_json(text: &str, marker: &str) -> Option<(serde_json::Value, String)>`

1. 按行分割文本
2. 查找 `marker` 独占一行（`line.trim() == marker`）
3. 如果找到，取该行之后的剩余文本
4. 在剩余文本中定位第一个 `{`
5. 从 `{` 开始，尝试 `serde_json::from_str` 解析（用花括号计数找到匹配的 `}`，截取子串尝试解析）
6. 解析成功 → 返回 `Some((json_value, 清理后的文本))`
7. 解析失败 → 返回 `None`，原文原样返回给前端

**清理后的文本**：移除标记行和 JSON 块，保留其余对话内容。

### 统一现有标记检测

将 `HEARTBEAT_OK` 也改为 `extract_marker(text, "HEARTBEAT_OK") -> bool`（行级匹配），保持一致性。

---

## Onboarding 系统 Prompt 模板（草案）

```text
[Thoughts Event]
type: onboarding
time: {timestamp}
source: ui

[System Instructions]
你正在进行用户画像收集。这是你和用户的第一次见面。

目标：通过自然、友好的对话，了解用户的基本信息和偏好。
收集方向（不限于此）：昵称、性别、年龄段、职业、爱好、感兴趣的话题、讨厌的事情、日常习惯等。

对话规则：
- 每次只问 1-2 个问题，不要一次性列出所有问题
- 保持轻松自然的语气，像朋友聊天一样
- 根据用户的回答自然地追问或转向新话题
- 当你觉得已经对用户有了足够的了解（通常 3-8 轮对话），结束收集

结束方式：
当你判断信息已充足时，在回复的最后独占一行输出标记，格式严格如下：
1. 先输出你的告别语/总结语
2. 然后空一行
3. 然后独占一行写 ONBOARDING_COMPLETE
4. 然后下一行写一个扁平的 JSON 对象（不要嵌套对象，数组可以用）

示例格式：
很高兴认识你！我会记住这些，以后聊天会更有针对性~

ONBOARDING_COMPLETE
{"nickname": "小明", "gender": "男", "ageRange": "25-30", "occupation": "程序员", "hobbies": ["游戏", "编程", "音乐"], "interests": ["科技新闻", "AI发展"], "dislikes": ["加班", "无聊的会议"]}

注意：JSON 必须是单行、合法的 JSON。字段名用 camelCase。

[User Message]
{user_message}
```

### PROFILE_UPDATE 的 Prompt 注入

在现有 `build_user_template` 的 `[Instructions]` 中追加：

```text
- 如果用户在对话中提到了新的偏好、兴趣变化、或个人信息更新，在回复最后独占一行输出 PROFILE_UPDATE，下一行输出需要更新的字段 JSON（扁平结构，数组字段提供完整值而非增量）。
```

---

## 分阶段实现步骤

### 阶段 1: Rust 画像存储层

**目标**: 实现 ProfileStore struct，提供画像文件读写和 merge 能力

**文件变更**:
- **新建** `src-tauri/src/storage/profile_store.rs`
- **修改** `src-tauri/src/storage/mod.rs` — 注册模块

**具体变更**:

```rust
pub struct ProfileStore {
    path: PathBuf,
}

impl ProfileStore {
    pub fn new(app_data_dir: &Path) -> Self {
        Self { path: app_data_dir.join("user-profile.json") }
    }

    /// 画像文件是否存在
    pub fn exists(&self) -> bool

    /// 读取画像 JSON
    pub fn load(&self) -> Option<serde_json::Value>

    /// 写入画像 JSON（全量覆盖）
    pub fn save(&self, value: &serde_json::Value) -> Result<(), String>

    /// 读取现有画像，shallow merge 新字段，写回
    /// 数组字段: replace 语义（由 prompt 保证完整值）
    pub fn merge(&self, partial: &serde_json::Value) -> Result<serde_json::Value, String>
}
```

`merge` 实现：`serde_json::Value::Object` 的顶层 key 遍历，新值覆盖旧值。

**验证方式**: 单元测试 — exists(无文件)=false, save+load 往返, merge 保留已有字段且新字段覆盖

**依赖**: 无

---

### 阶段 2: AppState 扩展 + 启动流程

**目标**: 启动时检测画像文件，设置 AppPhase，配置调度器延迟启动

**文件变更**:
- **修改** `src-tauri/src/models/mod.rs` — 新增 `AppPhase` 枚举，`AppStatus` 添加 `appPhase` 字段
- **修改** `src-tauri/src/app/state.rs` — AppState 添加 `app_phase` 和 `onboarding_signal` 字段
- **修改** `src-tauri/src/lib.rs` — 启动检测 + oneshot channel + 条件信号发送 + ProfileStore 注入

**具体变更**:

1. `models/mod.rs` 新增:
   ```rust
   #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
   pub enum AppPhase {
       Onboarding,
       Active,
   }
   ```
   `AppStatus` 新增 `pub app_phase: AppPhase`

2. `state.rs` 变更:
   ```rust
   pub struct AppState {
       // ... 现有字段 ...
       pub app_phase: AppPhase,
       pub onboarding_signal: Option<tokio::sync::oneshot::Sender<()>>,
   }
   ```
   `to_status()` 映射 `app_phase`

3. `lib.rs` setup() 变更:
   - 创建 `ProfileStore::new(&app_data_dir)`，`app.manage(profile_store)`
   - 检测 `profile_store.exists()` → 决定 `AppPhase`
   - 创建 oneshot channel
   - 如果 `AppPhase::Active` → 立即 `tx.send(())`
   - 如果 `AppPhase::Onboarding` → 将 tx 存入 AppState.onboarding_signal
   - 调度器 spawn 中：先 `await onboarding_rx`，再进入 `run_scheduler`

**验证方式**:
- 删除 `user-profile.json` 启动 → get_status 返回 `appPhase: "Onboarding"`，调度器未进入轮询
- 有 `user-profile.json` 启动 → get_status 返回 `appPhase: "Active"`，调度器正常运行

**依赖**: 阶段 1

---

### 阶段 3: Commands + 标记解析

**目标**: 实现 onboarding 对话、标记解析、画像保存/更新

**文件变更**:
- **修改** `src-tauri/src/commands.rs` — 新增 `send_onboarding_message`、`get_user_profile`；修改 `send_user_message`
- **修改** `src-tauri/src/lib.rs` — 注册新 commands

**具体变更**:

1. **标记解析辅助函数** `extract_marker_json(text, marker) -> Option<(Value, String)>`
   - 按行分割，查找 `line.trim() == marker`
   - 取标记行后剩余文本，定位 `{`，花括号计数截取，`serde_json::from_str` 校验
   - 返回 (JSON, 清理后文本) 或 None

2. **统一 HEARTBEAT_OK 检测** `has_marker(text, marker) -> bool`
   - 行级匹配：`text.lines().any(|l| l.trim() == marker)`
   - 替换现有 `text.contains("HEARTBEAT_OK")`（scheduler.rs 和 commands.rs 中）

3. **新增 `send_onboarding_message` command**:
   - 使用 session key `thoughts-onboarding`（独立，不污染主对话）
   - 构建 onboarding 专用模板（见上方 prompt 草案）
   - 调用 `send_to_openclaw`
   - 检测 `ONBOARDING_COMPLETE` → 提取 JSON → `profile_store.save()` → 切换 AppPhase → 发送 onboarding_signal → emit `onboarding-complete` 事件
   - 返回清理后的 AI 回复文本

4. **修改 `send_user_message`**:
   - 获取回复后检测 `PROFILE_UPDATE`
   - 如果命中 → `profile_store.merge()` → emit `profile-updated`
   - 返回清理后文本

5. **新增 `get_user_profile` command**:
   - `profile_store.load()` → 返回 `Option<serde_json::Value>`

6. **注册新 commands** 到 `lib.rs` invoke_handler

**验证方式**:
- Onboarding 对话中 AI 输出 `ONBOARDING_COMPLETE\n{...}` → JSON 保存成功，AppPhase 切换，调度器开始轮询
- AI 输出格式不规范（无 JSON）→ 标记被忽略，原文正常返回，onboarding 继续
- 普通对话中 `PROFILE_UPDATE\n{...}` → merge 成功
- `get_user_profile` → 返回当前画像 JSON

**依赖**: 阶段 1, 阶段 2

---

### 阶段 4: 前端 UI

**目标**: 前端根据 AppPhase 切换 UI，支持画像查看，提供跳过 onboarding 按钮

**文件变更**:
- **新建** `src/components/OnboardingView.vue` — onboarding 对话界面
- **新建** `src/components/ProfileModal.vue` — 画像查看弹窗
- **修改** `src/App.vue` — 条件渲染容器 + 事件监听

**具体变更**:

1. **`OnboardingView.vue`**:
   - 欢迎提示："你好！我是思绪~ 让我先了解一下你吧"
   - 对话区（复用主界面的消息气泡样式）
   - 输入框 + 发送按钮
   - **"跳过设定"按钮** — 点击后创建空画像 `{}`，切换到主界面（逃生出口）
   - 发送调用 `send_onboarding_message`

2. **`ProfileModal.vue`**:
   - 调用 `get_user_profile` 获取画像
   - 以 key-value 列表展示 JSON 顶层字段
   - 数组字段用逗号分隔展示
   - 关闭按钮

3. **`App.vue` 变更**:
   - `AppStatus` 接口添加 `appPhase: 'Onboarding' | 'Active'`
   - 条件渲染：`appPhase === 'Onboarding'` → `<OnboardingView />` / 否则 → 现有主界面
   - 监听 `onboarding-complete` → 刷新状态，切换到主界面
   - 监听 `profile-updated` → 如果 ProfileModal 打开则刷新
   - 状态栏添加画像图标按钮 → 点击打开 ProfileModal

**验证方式**:
- 首次启动 → 看到 OnboardingView，可对话
- 对话中 onboarding 完成 → 自动切换到主界面
- 点击"跳过设定" → 创建空画像，进入主界面
- 主界面点击画像图标 → 弹窗显示画像内容
- 画像更新后弹窗自动刷新

**依赖**: 阶段 3

---

## 风险点与缓解策略

| 风险 | 影响 | 缓解策略 |
|------|------|----------|
| AI 输出的 JSON 格式不规范 | 画像保存失败 | `extract_marker_json` 做 `serde_json::from_str` 校验，失败时返回 None，原文正常返回，onboarding 继续 |
| AI 始终不输出 ONBOARDING_COMPLETE 标记 | onboarding 永远无法完成 | **前端提供"跳过设定"按钮**作为逃生出口 |
| Onboarding 期间用户关闭应用 | 下次启动仍进 onboarding | 符合预期 — 画像未保存则重新开始 |
| `PROFILE_UPDATE` 误触发 | 错误 merge | 行锚定匹配（独占一行）+ JSON 校验双重保障 |
| 调度器启动时 Gateway 已关闭 | 调度器报错 | `run_scheduler` 内部已有 `gateway_status == Ready` 检查 |
| 并发文件访问（merge 期间） | 数据竞争 | ProfileStore 操作在 AppState RwLock 保护的上下文中调用（command handler 持有写锁时操作文件） |
| shallow merge 对数组的 replace 语义 | 用户丢失已有偏好 | prompt 约束：PROFILE_UPDATE 中数组字段必须提供完整值 |

---

## 已知技术债务（v1 接受，v2 处理）

1. 废弃的 onboarding session 会在 OpenClaw 侧累积（无清理机制）
2. 画像无版本历史，无法 undo
3. 无法从主界面主动重新进入 onboarding（需手动删除文件）
4. `AppConfig.profile` 字段（OpenClaw profile 名称）与 "user profile"（用户画像）命名可能混淆

---

## 文件变更清单

| 文件 | 操作 | 描述 |
|------|------|------|
| `src-tauri/src/storage/profile_store.rs` | 新建 | ProfileStore struct — exists/load/save/merge |
| `src-tauri/src/storage/mod.rs` | 修改 | 注册 profile_store 模块 |
| `src-tauri/src/models/mod.rs` | 修改 | 新增 AppPhase 枚举，AppStatus 添加 appPhase |
| `src-tauri/src/app/state.rs` | 修改 | AppState 添加 app_phase + onboarding_signal |
| `src-tauri/src/lib.rs` | 修改 | ProfileStore 注入 + oneshot channel + 条件信号 + 注册新 commands |
| `src-tauri/src/commands.rs` | 修改 | 新增 send_onboarding_message, get_user_profile; 修改 send_user_message; 标记解析函数 |
| `src-tauri/src/runtime/scheduler.rs` | 修改 | HEARTBEAT_OK 改为行级匹配 |
| `src/components/OnboardingView.vue` | 新建 | Onboarding 对话界面 + 跳过按钮 |
| `src/components/ProfileModal.vue` | 新建 | 画像查看弹窗 |
| `src/App.vue` | 修改 | 条件渲染容器 + 事件监听 + 画像按钮 |

## 阶段依赖关系

```
阶段1 (ProfileStore) → 阶段2 (AppPhase + 启动流程) → 阶段3 (Commands + 标记解析) → 阶段4 (前端 UI)
```

每阶段可独立验证，阶段 1-2 可安全先行合入不影响现有功能。
