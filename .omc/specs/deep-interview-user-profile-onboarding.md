# Deep Interview Spec: 用户画像 Onboarding 与个性化轮询

## Metadata
- Interview ID: user-profile-onboarding-2026-04-14
- Rounds: 9
- Final Ambiguity Score: 15%
- Type: brownfield
- Generated: 2026-04-14
- Threshold: 20%
- Status: PASSED

## Clarity Breakdown
| Dimension | Score | Weight | Weighted |
|-----------|-------|--------|----------|
| Goal Clarity | 0.92 | 0.35 | 0.322 |
| Constraint Clarity | 0.80 | 0.25 | 0.200 |
| Success Criteria | 0.80 | 0.25 | 0.200 |
| Context Clarity | 0.85 | 0.15 | 0.128 |
| **Total Clarity** | | | **0.850** |
| **Ambiguity** | | | **0.150** |

## Goal

为"思绪"桌面应用增加**首次启动用户画像采集**和**基于画像的个性化自动轮询**能力。

核心流程：
1. 首次启动时检测本地画像文件是否存在
2. 不存在 → 进入 onboarding 模式，AI 通过自然对话收集用户信息
3. AI 自主判断信息足够后，输出 `ONBOARDING_COMPLETE` + JSON 画像数据
4. Rust 解析并保存画像到本地文件
5. 后续启动跳过 onboarding，进入正常模式
6. 自动轮询的个性化完全依赖 OpenClaw 记忆系统（不修改轮询模板）
7. 普通对话中 AI 检测到偏好变化时，自动输出更新指令，Rust merge 更新本地画像

## Constraints

### 存储架构（双轨制）
- **本地画像文件**：确定性存储，用于首次启动检测、前端展示、状态管理、未来扩展
- **OpenClaw 记忆**：行为驱动，用于自动轮询时的个性化内容生成
- 两者各司其职，不互相替代

### 画像 Schema
- **完全开放式**：AI 自由决定输出哪些字段，Rust 侧只做基本 JSON 校验（是否为合法 JSON）
- 不预定义固定字段集，不做强类型校验
- 前端展示需处理任意 key-value 结构

### Onboarding 完成判定
- **AI 自主决定**：AI 判断信息收集充足后，在回复中输出 `ONBOARDING_COMPLETE` 标记 + JSON 数据
- 与现有 `HEARTBEAT_OK` 模式一致的设计哲学
- Rust 侧检测标记后自动保存并切换状态

### 轮询模板
- **不修改现有轮询模板**：`[Thoughts Event] + topics + [Instructions]` 格式保持不变
- 个性化能力完全由 OpenClaw 的会话记忆提供
- 因为 onboarding 对话在 `thoughts-auto-main` 或相关 session 中，OpenClaw 自然记住用户偏好

### 动态更新机制
- 用户在普通对话中提到新偏好时，AI 自动识别并输出更新标记（如 `PROFILE_UPDATE` + 部分 JSON）
- Rust 检测到更新标记后，将新数据 merge 到现有画像文件
- 这是 v1 必须实现的功能，不是延后项

## Non-Goals

- 不做表单式/向导式 UI 采集
- 不做固定字段 Schema 或强类型校验
- 不修改现有轮询模板格式
- 不做画像的手动编辑 UI（直接编辑 JSON）
- 不做多用户/多画像切换
- 不做画像的加密或安全保护（v1）
- 不做画像同步/备份

## Acceptance Criteria

- [ ] 1. 首次启动检测到无画像文件，自动进入 onboarding 对话模式
- [ ] 2. AI 通过自然对话收集用户信息，自主输出 `ONBOARDING_COMPLETE` + JSON
- [ ] 3. 画像 JSON 成功保存到本地文件
- [ ] 4. 再次启动时检测到画像文件，跳过 onboarding，直接进入正常对话模式
- [ ] 5. 自动轮询产生的内容明显与用户兴趣偏好相关（依赖 OpenClaw 记忆）
- [ ] 6. 在普通对话中提到新偏好时，AI 能检测并动态更新本地画像文件
- [ ] 7. 前端能查看当前画像摘要信息

## Assumptions Exposed & Resolved

| Assumption | Challenge | Resolution |
|------------|-----------|------------|
| 需要结构化提取 JSON 注入轮询模板 | OpenClaw 有记忆能力，可能不需要显式注入 | 不注入模板，依赖 OpenClaw 记忆。本地画像仅用于确定性功能 |
| 第一版只需重新 onboarding 来修改画像 | 对话中动态更新是否 v1 必须？ | 是 v1 必须，用户明确要求对话中动态更新 |
| 画像需要固定 Schema | 开放 vs 固定 vs 半开放？ | 完全开放，AI 自由决定字段，Rust 只校验 JSON 合法性 |
| 可以简化掉本地存储，完全靠 OpenClaw 记忆 | 记忆是模糊的，本地是确定性的 | 必须本地存储。确定性 > 简化 |

## Technical Context

### 现有代码库关键文件

**Rust 后端：**
- `src-tauri/src/lib.rs` — 应用启动装配，需增加 onboarding 检测逻辑
- `src-tauri/src/app/state.rs` — `AppState` 需增加 onboarding 状态字段
- `src-tauri/src/models/mod.rs` — `AppConfig` 定义，可能需扩展
- `src-tauri/src/commands.rs` — Tauri commands，需增加 onboarding 相关命令
- `src-tauri/src/cli/agent_client.rs` — `send_to_openclaw()` 封装，复用
- `src-tauri/src/runtime/scheduler.rs` — 轮询调度器，onboarding 期间需暂停
- `src-tauri/src/storage/config_store.rs` — 配置存储，需增加画像存储

**前端：**
- `src/App.vue` — 主 UI，需增加 onboarding 状态判断和画像展示

### 存储路径
- 画像文件建议路径：`{app_data_dir}/user-profile.json`
- 首次启动检测：检查该文件是否存在

### 协议设计

**Onboarding 完成标记：**
```
ONBOARDING_COMPLETE
{"nickname": "小明", "age": 25, "hobbies": ["游戏", "编程"], ...}
```

**动态更新标记：**
```
PROFILE_UPDATE
{"hobbies": ["游戏", "编程", "摄影"]}
```

Rust 侧检测逻辑：
- 检查 `payloads[0].text` 是否包含 `ONBOARDING_COMPLETE` → 提取后续 JSON，全量保存
- 检查 `payloads[0].text` 是否包含 `PROFILE_UPDATE` → 提取后续 JSON，merge 到现有画像

### 状态机扩展

AppState 需增加：
```
onboarding_status: OnboardingStatus  // NotStarted / InProgress / Completed
```

启动流程变更：
```
启动 → 加载配置 → 检查画像文件
  ├── 存在 → onboarding_status = Completed → 正常模式
  └── 不存在 → onboarding_status = NotStarted → 前端进入 onboarding UI
```

## Ontology (Key Entities)

| Entity | Type | Fields | Relationships |
|--------|------|--------|---------------|
| UserProfile | core domain | 开放式 JSON（AI 自由决定字段） | 被 OnboardingConversation 创建，被 ProfileUpdateDetector 更新 |
| OnboardingConversation | supporting | sessionKey, rounds, completionMarker | 创建 UserProfile，使用 OnboardingPrompt |
| ProfileFile | supporting | filePath, existsFlag, jsonContent | 持久化 UserProfile，用于首次启动检测 |
| AutoPollTemplate | existing | topics, instructions, timestamp | 不修改，个性化由 OpenClaw 记忆提供 |
| OnboardingPrompt | supporting | templateText, instructions | 引导 AI 进行 onboarding 对话 |
| ProfileEditor | supporting | updateMarker, mergeLogic | 通过 ProfileUpdateDetector 触发画像更新 |
| ProfileUpdateDetector | supporting | detectionLogic, updateMarker | AI 在普通对话中检测偏好变化 |

## Ontology Convergence

| Round | Entity Count | New | Changed | Stable | Stability Ratio |
|-------|-------------|-----|---------|--------|----------------|
| 1 | 4 | 4 | - | - | N/A |
| 2 | 5 | 1 | 0 | 4 | 80% |
| 3 | 5 | 0 | 0 | 5 | 100% |
| 4 | 6 | 1 | 0 | 5 | 83% |
| 5 | 6 | 0 | 0 | 6 | 100% |
| 6 | 6 | 0 | 0 | 6 | 100% |
| 7 | 7 | 1 | 0 | 6 | 86% |
| 8 | 7 | 0 | 0 | 7 | 100% |
| 9 | 7 | 0 | 0 | 7 | 100% |

## Interview Transcript
<details>
<summary>Full Q&A (9 rounds)</summary>

### Round 1
**Q:** 用户画像的采集方式，你设想的是哪种形式？
**A:** 对话式采集 — 由 AI 通过自然对话逐步询问用户信息
**Ambiguity:** 71% (Goal: 0.40, Constraints: 0.15, Criteria: 0.10, Context: 0.60)

### Round 2
**Q:** 对话式采集的核心问题：AI 聊完后怎么变成结构化画像？
**A:** AI 自行输出 JSON — onboarding 最后一轮 AI 整理成 JSON 返回
**Ambiguity:** 65% (Goal: 0.55, Constraints: 0.15, Criteria: 0.10, Context: 0.65)

### Round 3
**Q:** Onboarding 对话的"完成"怎么判定？
**A:** AI 输出特殊标记 — 类似 HEARTBEAT_OK，AI 自主决定何时采集完成
**Ambiguity:** 58% (Goal: 0.70, Constraints: 0.20, Criteria: 0.10, Context: 0.70)

### Round 4
**Q:** 验收标准：功能做完后怎么判断成功？
**A:** 还需要可修改 — 个性化轮询 + 可修改画像
**Ambiguity:** 44% (Goal: 0.75, Constraints: 0.25, Criteria: 0.50, Context: 0.70)

### Round 5 [Contrarian Mode]
**Q:** OpenClaw 有记忆能力，"思绪"侧真的需要自己保存结构化画像吗？
**A:** 必须本地存储 — 本地是确定性的，OpenClaw 记忆是模糊的
**Ambiguity:** 38% (Goal: 0.80, Constraints: 0.40, Criteria: 0.50, Context: 0.75)

### Round 6
**Q:** 用户画像的 JSON 字段集是固定的还是开放式的？
**A:** 完全开放 — AI 自由决定字段，Rust 只做 JSON 校验
**Ambiguity:** 34% (Goal: 0.80, Constraints: 0.55, Criteria: 0.50, Context: 0.75)

### Round 7 [Simplifier Mode]
**Q:** 第一版的"修改画像"能力，最简单版本是什么？
**A:** 对话中动态更新 — 用户在普通聊天中提到新偏好时 AI 自动更新
**Ambiguity:** 31% (Goal: 0.85, Constraints: 0.60, Criteria: 0.50, Context: 0.75)

### Round 8
**Q:** 具体验收条件确认（7 条）
**A:** 全部 7 条都是 v1 必须
**Ambiguity:** 20% (Goal: 0.90, Constraints: 0.65, Criteria: 0.80, Context: 0.80)

### Round 9
**Q:** 用户画像怎么注入到 OpenClaw 上下文中？
**A:** 不注入模板 — 完全依赖 OpenClaw 记忆系统，不修改轮询模板
**Ambiguity:** 15% (Goal: 0.92, Constraints: 0.80, Criteria: 0.80, Context: 0.85)

</details>
