# Agent Memory Engine 技术架构设计规格 V1.1

**版本：** V1.1
**目标：** 构建独立于 Agent 的长期记忆系统，为 Main Agent 和 Worker Agent 提供统一的记忆存储、检索、关联、整理和上下文组装能力。

---

# 一、项目目标

## 1.1 系统定位

Memory Engine 是一个独立的记忆服务，不是简单的向量数据库，也不是传统的 RAG 知识库。

它需要模拟人类记忆的一些核心特征：

1. 记忆形成：从对话、语音、视觉和环境事件中形成记忆。
2. 记忆关联：发现不同记忆之间的关系，建立可追溯的关联。
3. 记忆巩固：通过后台整理，将零散记忆组织成更稳定的知识。
4. 记忆衰减：不重要或长期未使用的记忆逐渐降低检索优先级。
5. 记忆遗忘：允许低价值记忆归档或删除，而不是无限积累。
6. 记忆再激活：通过新线索重新找到很久以前没有访问过的记忆。
7. 按需回忆：Agent 不需要每次接收全部记忆，而是根据当前需求逐步检索。

系统需要同时服务：

- Main Agent：负责实时感知、认知和语音交互。
- Worker Agent：负责复杂任务和深度分析。

两个 Agent 共享同一套记忆，不需要分别维护个人记忆库。

## 1.2 核心设计原则

- 程序负责确定性工作，AI 负责语义判断。
- 不要求小模型完成复杂的跨记忆推理。
- 原始事件与整理后的长期记忆分离。
- 重要性、可信度、时间和可访问性独立管理。
- 记忆关系是增强信息，不要求每条记忆都建立关系。
- 不要求所有记忆永久保留。
- 不因为记忆重要就自动注入上下文。
- 允许第一次检索失败，通过多轮检索重新回忆。
- 后台整理不能阻塞实时记忆写入和检索。
- 所有 AI 整理结果都必须经过程序校验。
- 重要信息的原始来源必须能够追溯。

## 1.3 非目标

第一阶段不实现：

- 完整的知识图谱平台。
- 独立向量数据库集群。
- 多 Agent 自主讨论式记忆整理。
- 复杂的强化学习记忆策略。
- 自动训练用户专属模型。
- 全量视频和音频永久存储。
- 每次对话自动加载全部用户画像。

---

# 二、总体架构

系统划分为六个核心模块。

```text
                    Main Agent
                         |
                    Worker Agent
                         |
                 Memory API Gateway
                         |
          +--------------+--------------+
          |                             |
      Recall Engine                Write Engine
          |                             |
          +--------------+--------------+
                         |
                    Memory Store
                         |
          +--------------+--------------+
          |                             |
    Consolidation Engine          Index Engine
          |                             |
    Memory Evaluator               Search Index
          |
    Model Router
          |
    Qwen / GLM / Xing / Other Models

              Context Assembly
                     |
              Agent Context
```

## 2.1 模块职责

### Memory API Gateway

对外提供统一 API。

负责：
- 请求校验。
- Agent 身份识别。
- Context 管理。
- Token Budget 接收。
- 请求限流。
- 日志和追踪。

### Write Engine

负责实时记忆写入。

- 接收原始事件。
- 保存原始内容。
- 提取必要元数据。
- 生成基础索引任务。
- 记录变更事件。

不要求实时调用大模型。

### Recall Engine

负责记忆检索。

- 混合检索。
- 时间感知排序。
- 重要性排序。
- 关联记忆扩展。
- 多轮回忆。
- 检索去重。

### Memory Evaluator

负责判断记忆的语义价值。

- 重要性评估。
- 记忆是否仍然有效。
- 新旧记忆关系判断。
- 冲突识别。
- 遗忘建议。
- 记忆分类和归纳。

### Consolidation Engine

负责后台记忆整理。

- 增量整理。
- 记忆巩固。
- 关系发现。
- 版本更新。
- 记忆衰减。
- 过期评估。
- 任务调度。

### Context Assembly

负责将检索到的记忆组织为 Agent 可以直接使用的上下文。

- Brief / Full 两级内容。
- 上下文压缩。
- Token 预算管理。
- 来源和可信度标注。
- 当前 Context 去重。

---

# 三、记忆数据模型

## 3.1 设计思路

不要为人物画像、经历、任务、偏好、经验分别创建完全独立的记忆系统。

使用统一的 Memory Entity，通过类型和属性描述不同的记忆。

每条记忆可以同时拥有多个标签和关系。

例如：

```json
{
  "content": "用户购买了一辆小鹏 G7 Ultra",
  "memory_type": "fact",
  "entities": ["user", "xiaopeng_g7"],
  "importance": 0.85,
  "confidence": 0.98,
  "origin": "explicit",
  "temporal_type": "stable",
  "decay_rate": 0.01
}
```

## 3.2 Raw Memories

原始记忆，保存事件本身。

建议表：`raw_memories`

字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| event_type | VARCHAR | conversation / voice / vision / environment / task |
| source | VARCHAR | 来源设备或 Agent |
| session_id | VARCHAR | 会话 ID |
| occurred_at | TIMESTAMPTZ | 实际发生时间 |
| received_at | TIMESTAMPTZ | 系统接收时间 |
| content | JSONB | 原始事件内容 |
| media_refs | JSONB | 媒体文件引用 |
| metadata | JSONB | 扩展元数据 |
| processing_status | VARCHAR | pending / processed / failed |
| created_at | TIMESTAMPTZ | 创建时间 |

原则：

- Raw Memory 尽可能追加，不直接覆盖。
- 保留消息边界和原始来源。
- 大型音视频文件只保存外部引用。
- 支持按事件批次整理。
- 原始数据保留策略与长期记忆保留策略分离。

## 3.3 Canonical Memories

规范化记忆。

建议表：`memories`

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| content | TEXT | 规范化记忆内容 |
| brief_content | TEXT | 简短检索描述 |
| memory_type | VARCHAR | fact / profile / episode / preference / task / skill / relationship / environment |
| tags | TEXT[] | 标签 |
| entities | JSONB | 涉及的实体 |
| importance | FLOAT | 重要性 |
| confidence | FLOAT | 可信度 |
| accessibility | FLOAT | 可访问性 |
| access_count | INTEGER | 成功访问次数 |
| created_at | TIMESTAMPTZ | 创建时间 |
| updated_at | TIMESTAMPTZ | 最近更新时间 |
| last_accessed_at | TIMESTAMPTZ | 最近访问时间 |
| last_confirmed_at | TIMESTAMPTZ | 最近确认时间 |
| valid_from | TIMESTAMPTZ | 生效时间 |
| valid_until | TIMESTAMPTZ | 失效时间 |
| decay_rate | FLOAT | 衰减速度 |
| temporal_type | VARCHAR | transient / episodic / stable / recurring |
| origin | VARCHAR | explicit / derived / inferred |
| status | VARCHAR | active / superseded / archived / retracted |
| retention_policy | VARCHAR | normal / protected / expirable |
| created_by | VARCHAR | 来源 Agent 或处理模块 |

注意：

**importance、confidence、accessibility 是三个完全独立的维度。**

例如：

- 用户明确说过的长期偏好：重要性高、可信度高。
- 模型推断用户可能喜欢某个产品：重要性未知、可信度较低。
- 用户昨天临时购买的商品：可信度高，但长期重要性可能低。

不能因为重要性高就认定内容可信。

### Memory Type

建议使用以下类型，但允许扩展：

- `fact`：事实。
- `profile`：用户画像。
- `preference`：偏好。
- `episode`：经历和事件。
- `task`：任务和计划。
- `skill`：经验和技能。
- `relationship`：关系。
- `environment`：环境信息。

类型只影响检索和生命周期策略，不需要为每种类型建立独立存储系统。

## 3.4 Memory Sources

建议表：`memory_sources`

负责建立规范记忆与原始证据的关联。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| memory_id | UUID | 记忆 ID |
| raw_memory_id | UUID | 原始事件 ID |
| evidence_type | VARCHAR | explicit / supporting / contradicting |
| evidence_text | TEXT | 关键证据片段 |
| confidence | FLOAT | 证据可信度 |
| created_at | TIMESTAMPTZ | 创建时间 |

一条规范记忆可以关联多个原始事件。

一条原始事件也可以支持多条规范记忆。

## 3.5 Memory Relations

建议表：`memory_relations`

关系是可选增强信息，不要求每条记忆必须建立关系。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | UUID | 主键 |
| source_memory_id | UUID | 起始记忆 |
| target_memory_id | UUID | 目标记忆 |
| relation_type | VARCHAR | 关系类型 |
| confidence | FLOAT | 关系可信度 |
| origin | VARCHAR | program / model |
| evidence_ids | JSONB | 关系证据 |
| created_at | TIMESTAMPTZ | 创建时间 |
| status | VARCHAR | active / superseded / retracted |

关系类型示例：

- related_to：相关。
- caused_by：由某事件导致。
- part_of：属于。
- occurred_after：发生在之后。
- contradicts：冲突。
- updates：更新。
- supports：支持。
- derived_from：来源于。

禁止模型仅凭语义相似性就创建因果关系。

## 3.6 Memory Versions

建议表：`memory_versions`

保存规范记忆的历史版本。

字段：

- id
- memory_id
- version
- content
- metadata
- change_reason
- changed_by
- created_at

更新记忆时保留旧版本。

不允许直接覆盖后丢失历史。

## 3.7 Memory Indexes

建议表：`memory_indexes`

负责检索所需的数据。

包括：

- embedding vector。
- 全文检索内容。
- 关键词。
- 实体索引。
- Embedding 模型标识。
- 索引版本。
- 索引更新时间。

Embedding 与原始记忆、规范记忆分离。

允许后续更换 Embedding 模型并重建索引。

---

# 四、记忆生命周期：形成、巩固、衰减、遗忘、再激活

这是 V1.1 相比 V1.0 的核心调整。

## 4.1 记忆形成

原始事件进入系统后：

1. 保存 Raw Memory。
2. 程序提取时间、来源、基础实体和文本。
3. 根据配置决定是否生成初始 Canonical Memory。
4. 创建索引任务。
5. 进入后台评估队列。

对于简单、明确的事件，可以直接形成规范记忆。

对于复杂事件，先保留 Raw Memory，等待后台整理。

## 4.2 记忆重要性

重要性是一个动态值，不是永久固定值。

建议初始评估因素：

- 用户明确要求记住。
- 是否属于长期偏好。
- 是否反复出现。
- 是否与长期目标有关。
- 是否对未来决策有帮助。
- 是否具有较高的关联价值。
- 是否具有较高的时效性。

重要性建议使用 0～1。

初始分类：

| 分数 | 级别 | 策略 |
|---|---|---|
| 0.8～1.0 | 高 | 较高检索优先级 |
| 0.5～0.8 | 中 | 正常检索 |
| 0.2～0.5 | 低 | 更依赖上下文线索 |
| 0～0.2 | 极低 | 低优先级，考虑遗忘 |

阈值是初始配置，需要通过真实数据调整。

重要性高不意味着自动注入 Agent 上下文。

## 4.3 记忆衰减

记忆衰减的目标是降低检索优先级，而不是立即删除。

可以先采用程序化衰减公式：

```text
Accessibility(t) =
  BaseAccessibility
  * exp(-decay_rate * elapsed_time)
```

其中：

- BaseAccessibility：基础可访问性。
- decay_rate：衰减速度。
- elapsed_time：距上次有效访问或确认的时间。

不同记忆类型采用不同策略。

例如：

| 类型 | 衰减策略 |
|---|---|
| 长期偏好 | 极慢 |
| 稳定个人事实 | 极慢 |
| 当前项目状态 | 中等 |
| 临时任务 | 较快 |
| 日常环境事件 | 较快 |
| 一次性闲聊 | 很快 |

访问、确认和重新使用都可以提高可访问性。

注意：访问次数不能无限增加重要性。重复检索不等于记忆价值提高。

## 4.4 记忆遗忘

遗忘必须分阶段。

```text
Active
  |
  | 长期低价值 / 已过期
  v
Low Accessibility
  |
  | 后台评估
  v
Forget Candidate
  |
  | 再次确认 / 等待策略
  v
Archived
  |
  | 用户要求删除或满足删除策略
  v
Deleted
```

遗忘评估考虑：

- 重要性。
- 最近访问时间。
- 最近确认时间。
- 记忆有效期。
- 关联记忆数量。
- 是否存在未完成任务。
- 是否被其他记忆引用。
- 是否属于用户明确要求保留的信息。

不能仅仅因为一条记忆长期没有访问就删除。

对于高价值记忆、用户明确要求记住的内容、仍然有效的重要任务，应使用保护策略。

对于用户要求彻底删除的内容，必须执行删除策略，而不是仅归档。

## 4.5 记忆再激活

这是必须实现的能力。

低可访问性记忆不应从普通检索系统中彻底消失。

当 Agent 找到相关线索时，可以扩大检索范围，重新发现它们。

重新访问后：

- 更新 last_accessed_at。
- 提高 accessibility。
- 根据实际情况更新 importance。
- 不自动修改 confidence。
- 不自动将推断记忆变成事实。

---

# 五、Memory Evaluator：小模型评估系统

## 5.1 核心理念

不要要求小模型理解整个记忆库。

应该由程序先筛选、切分、召回候选，再把有限的结构化内容交给模型判断。

模型只回答简单问题：

1. 这条记忆有多重要？
2. 现在是否仍然有效？
3. 和哪些记忆有关？
4. 是否与已有记忆冲突？
5. 是否值得保留？
6. 是否应该降低检索优先级？
7. 是否存在可以合并的重复记忆？

模型不负责直接操作数据库。

## 5.2 模型路由

实现 Model Router，允许根据任务难度选择模型。

初始候选模型：

| 模型 | 初始定位 |
|---|---|
| Qwen/Qwen3-8B | 轻量分类、提取、简单评估 |
| THUDM/GLM-Z1-9B-0414 | 需要一定推理的关系判断和冲突判断 |
| XingChenAGI/Xing4.0-29B | 复杂关联、跨记忆归纳和高难度判断 |

模型路由不能只按照参数量决定。

需要通过实际测试评估：

- JSON 输出成功率。
- 判断准确率。
- 关系识别准确率。
- 推理耗时。
- Token 消耗。
- 服务可用性。
- 免费额度和限流情况。

硅基流动作为初始模型服务提供方，但模型提供方和模型 ID 必须可配置。

不要把业务逻辑绑定到某一家模型服务商。

## 5.3 任务分级

### Level 1：简单任务

使用轻量模型。

- 重要性初步评分。
- 记忆类型分类。
- 是否仍然有效。
- 是否存在明显重复。
- 简单关系判断。

### Level 2：中等任务

使用具备较好推理能力的模型。

- 新旧记忆冲突判断。
- 多条记忆关系判断。
- 记忆合并建议。
- 过期和失效判断。

### Level 3：复杂任务

使用更强模型，低频触发。

- 多次经历的归纳。
- 长期行为模式发现。
- 多条记忆之间的复杂关系分析。
- 长期知识和经验总结。

Level 3 不应该成为常规后台任务。

## 5.4 模型输出协议

模型统一返回结构化 JSON。

例如：

```json
{
  "memory_id": "mem_123",
  "importance": {
    "score": 0.82,
    "reason": "与用户长期目标有关"
  },
  "validity": {
    "status": "still_valid",
    "reason": "没有发现失效证据"
  },
  "relations": [
    {
      "target_memory_id": "mem_456",
      "relation_type": "related_to",
      "confidence": 0.91
    }
  ],
  "conflicts": [],
  "forget_recommendation": {
    "action": "keep",
    "reason": "仍然具有长期参考价值"
  }
}
```

输出必须经过 Zod Schema 校验。

对于无效输出：

- 有限重试。
- 必要时切换模型。
- 超过重试次数进入失败队列。
- 不得直接修改正式记忆。

## 5.5 防止模型误判

模型只提交 Proposal。

程序负责：

- 验证目标记忆是否存在。
- 验证来源证据。
- 检查关系是否允许。
- 检查记忆版本是否变化。
- 检查是否存在并发更新。
- 检查置信度阈值。
- 决定是否提交。

模型推断的关系和内容必须保留 origin=inferred。

不允许把模型推断直接伪装成用户明确表达的事实。

---

# 六、Recall Engine：多维度记忆检索

## 6.1 检索目标

检索不能只看语义相似度。

需要综合考虑：

1. 当前问题与记忆的语义相关性。
2. 记忆重要性。
3. 时间相关性。
4. 记忆可访问性。
5. 当前场景关联度。
6. 记忆可信度。

## 6.2 Hybrid Retrieval

第一阶段采用本地 SQLite + FTS5；后续按需要加入 sqlite-vec。

检索渠道：

- Dense Retrieval：后续通过 sqlite-vec 加入向量语义检索。
- Sparse Retrieval：全文关键词检索。
- Entity Retrieval：人物、地点、项目和实体检索。
- Temporal Retrieval：时间范围检索。
- Relation Retrieval：根据已召回记忆扩展关系。

不引入独立向量数据库和图数据库。

## 6.3 RRF 排名融合

不同检索渠道的分数不可直接比较。

使用 RRF 合并不同召回结果：

```text
RRF(d) = Σ 1 / (k + rank_i(d))
```

初始 k=60。

后续再通过评估数据调整。

## 6.4 综合排序

在候选召回后，使用多维度排序。

建议初始公式：

```text
FinalScore =
  0.40 * Relevance
  + 0.18 * Importance
  + 0.14 * Temporal
  + 0.12 * Accessibility
  + 0.10 * ContextRelevance
  + 0.06 * Confidence
```

所有维度需要归一化到 0～1。

以上权重仅为初始实验值。

特别注意：

### Temporal Score

时间相关性必须根据记忆类型决定。

例如：

- 临时任务：越接近当前时间，相关性可能越高。
- 长期偏好：时间影响很小。
- 具有明确有效期的记忆：超过有效期后显著降低。
- 历史经历：即使很久以前发生，仍然可以具有很高价值。

不能使用所有记忆统一的时间衰减函数。

### Importance

重要性提高排名，但不能直接绕过相关性。

一条重要但与当前问题无关的记忆，不应该挤占所有检索结果。

### Accessibility

用于控制记忆被回忆的难易程度。

长期没有访问的记忆可以降低排序，但不能让它完全无法通过扩大检索找到。

## 6.5 两级及多轮检索

检索分为三个层级。

### Level 1：快速 Recall

输入：
- query。
- 当前场景。
- 时间范围。
- Context ID。
- Token Budget。

返回少量轻量线索。

例如：

```json
{
  "memory_id": "mem_123",
  "brief": "用户曾讨论过长期项目的架构设计",
  "importance": 0.82,
  "confidence": 0.95,
  "relevance": 0.89,
  "has_more_details": true
}
```

不直接返回所有原始内容。

### Level 2：Expand

Agent 判断信息不足时，指定记忆 ID 或关联线索，进一步展开。

返回：

- 完整记忆内容。
- 关键来源。
- 时间信息。
- 关联记忆。
- 冲突信息。

### Level 3：Deep Recall

如果仍然找不到：

- 扩大时间范围。
- 降低语义相似度门槛。
- 增加实体和关系检索。
- 搜索低可访问性记忆。
- 必要时回溯原始事件。

这个阶段不应该默认执行。

Agent 可以决定是否继续检索。

## 6.6 检索状态与去重

一个 Context 对应一次完整的认知或任务执行过程。

通过 `context_id` 实现去重。

唯一约束：

```sql
UNIQUE(context_id, memory_id)
```

同一 Context 中：

- 已加载的 Brief 不重复返回。
- Brief 可以升级为 Full。
- 已加载的 Full 不重复返回。
- 新增检索请求只返回尚未加载的内容。
- 允许明确请求刷新已有记忆。

去重必须通过数据库事务保证并发正确性。

---

# 七、Context Assembly：上下文组装

Context Assembly 负责把记忆整理为 Agent 可以使用的内容。

不能把数据库原始字段直接传给 Agent。

## 7.1 两级内容

Brief：

- 一句话摘要。
- 时间。
- 重要性。
- 可信度。
- 是否存在更多信息。

Full：

- 完整记忆内容。
- 关键证据。
- 相关记忆。
- 冲突情况。
- 有效时间。

## 7.2 Token Budget

Token Budget 由请求传入。

不是全局固定值。

需要分别控制：

- 检索候选数量。
- Reranker 或模型评估 Token。
- 摘要生成 Token。
- 最终 Agent 上下文 Token。

需要注意，最终上下文 Token 和内部模型处理成本是两回事。

Context Assembly 应当提供预算控制和实际消耗统计。

## 7.3 上下文去重

同一 Context 不允许重复追加相同记忆。

如果 Brief 已存在，Full 应当替换或升级 Brief，而不是再次追加。

## 7.4 上下文可信度

Agent 必须知道：

- 这是明确事实。
- 这是模型推断。
- 这是历史事件。
- 这是可能过期的信息。
- 这是尚未确认的关联关系。

不能把不同可信度的内容混在一起，伪装成确定事实。

---

# 八、Consolidation Engine：后台记忆整理

## 8.1 设计目标

后台整理类似人类睡眠时的记忆巩固。

它是独立于实时交互的后台流程。

允许慢，但不能阻塞读写。

## 8.2 触发机制

初始配置：

```yaml
consolidation:
  interval_hours: 12
  raw_memory_threshold: 50
  canonical_change_threshold: 20
  batch_size: 20
  concurrency: 1
  max_retries: 3
```

支持：

- 每 12 小时定时触发。
- Raw Memory 达到阈值触发。
- Canonical Memory 变化达到阈值触发。
- 手动触发。
- 系统维护任务。

配置必须可调整。

## 8.3 后台任务类型

### Task A：基础记忆整理

- 文本切分。
- 记忆提取。
- 重复识别。
- 实体识别。
- 时间提取。

优先使用程序，必要时使用轻量模型。

### Task B：重要性评估

- 重要性评分。
- 是否仍然有效。
- 是否需要保留。
- 是否需要降低可访问性。

### Task C：关系发现

- 检索潜在关联记忆。
- 小模型判断关系。
- 生成关系 Proposal。
- 程序校验并提交。

### Task D：冲突检测

- 查找语义相似的记忆。
- 比较时间和来源。
- 判断是否更新、冲突或属于不同事件。
- 保留历史版本。

### Task E：记忆巩固

把多条相关经历归纳为稳定知识。

例如：

多次讨论某个项目的技术选型，可以形成项目背景、架构决策和已确认结论。

归纳内容必须保留来源关系。

### Task F：记忆衰减与遗忘评估

- 更新可访问性。
- 检查过期信息。
- 识别遗忘候选。
- 执行归档策略。
- 检查是否需要重新评估。

## 8.4 增量整理

禁止每次扫描整个历史记忆库。

使用 Change Log 或 Outbox 记录待处理变更。

每个 Job 需要：

- job_id。
- job_type。
- input_ids。
- checkpoint。
- status。
- retry_count。
- created_at。
- updated_at。

状态：

```text
pending
  -> processing
  -> completed

processing
  -> retrying
  -> failed
```

任务需要支持：

- 幂等。
- 分批。
- Checkpoint。
- 有限重试。
- 失败恢复。
- 并发保护。

## 8.5 后台整理安全规则

1. LLM 不直接写数据库。
2. 每次整理只处理有限数量的记忆。
3. 复杂任务拆分成多个独立 Job。
4. 处理前记录输入版本。
5. 提交前检查版本是否发生变化。
6. 重要记忆不能被低置信度结果覆盖。
7. 归纳记忆必须保留来源。
8. 不确定的结果进入待审核或低可信度状态。
9. 后台整理失败不能影响实时读写。

---

# 九、数据库与索引

## 9.1 技术选型

第一阶段建议：

| 模块 | 技术 |
|---|---|
| Runtime | Node.js |
| Language | TypeScript |
| API | Fastify |
| Database | SQLite（better-sqlite3） |
| Vector | 后续加入 sqlite-vec |
| Full Text | SQLite FTS5 |
| ORM / SQL | Drizzle + 必要原生 SQL |
| Validation | Zod |
| Logging | Pino |
| Metrics | Prometheus |
| Testing | Vitest |

## 9.2 数据库表清单

第一阶段：

- raw_memories
- memories
- memory_indexes
- memory_sources
- memory_relations
- memory_versions
- memory_contexts
- context_memory_loads
- consolidation_jobs
- memory_change_logs

## 9.3 索引要求

至少建立：

- memories(status, importance)
- memories(last_accessed_at)
- memories(memory_type, status)
- memories(valid_until)
- raw_memories(processing_status, occurred_at)
- memory_sources(memory_id)
- memory_sources(raw_memory_id)
- memory_relations(source_memory_id)
- memory_relations(target_memory_id)
- memory_indexes(memory_id)
- consolidation_jobs(status, created_at)
- memory_change_logs(processing_status, created_at)

初期以 FTS5 与关键词检索为主；向量检索后续采用 sqlite-vec。

Embedding 模型必须记录版本，支持未来迁移和重建。

---

# 十、API 设计

## 10.1 Context API

### 创建 Context

`POST /api/v1/contexts`

```typescript
interface CreateContextRequest {
  agentId: string;
  sessionId?: string;
  scene?: string;
  tokenBudget?: number;
}
```

### 获取 Context

`GET /api/v1/contexts/:contextId`

### 关闭 Context

`POST /api/v1/contexts/:contextId/close`

## 10.2 Write API

### 写入原始事件

`POST /api/v1/memories/raw`

```typescript
interface RawMemoryInput {
  eventType: string;
  source: string;
  sessionId?: string;
  occurredAt: string;
  content: Record<string, unknown>;
  mediaRefs?: string[];
  metadata?: Record<string, unknown>;
}
```

### 创建规范记忆

`POST /api/v1/memories`

### 更新规范记忆

`PATCH /api/v1/memories/:id`

更新需要版本控制。

## 10.3 Recall API

`POST /api/v1/recall`

```typescript
interface RecallRequest {
  contextId: string;
  query: string;
  scene?: string;
  timeRange?: {
    from?: string;
    to?: string;
  };
  limit?: number;
  tokenBudget?: number;
  includeBrief?: boolean;
}
```

返回：

```typescript
interface RecallResponse {
  results: Array<{
    memoryId: string;
    brief: string;
    importance: number;
    confidence: number;
    relevance: number;
    hasMoreDetails: boolean;
  }>;
  hasMore: boolean;
  nextCursor?: string;
}
```

## 10.4 Expand API

`POST /api/v1/recall/expand`

```typescript
interface ExpandRequest {
  contextId: string;
  memoryIds: string[];
  depth?: number;
  includeRelations?: boolean;
  tokenBudget?: number;
}
```

## 10.5 Deep Recall API

`POST /api/v1/recall/deep`

用于扩大检索范围。

```typescript
interface DeepRecallRequest {
  contextId: string;
  query: string;
  relatedMemoryIds?: string[];
  expandTimeRange?: boolean;
  includeLowAccessibility?: boolean;
  tokenBudget?: number;
}
```

## 10.6 Memory Evaluator API

内部接口：

- `POST /internal/evaluator/importance`
- `POST /internal/evaluator/relations`
- `POST /internal/evaluator/conflicts`
- `POST /internal/evaluator/retention`

建议通过任务队列调用，不要求 Agent 直接使用。

## 10.7 Consolidation API

- `POST /api/v1/consolidation/trigger`
- `GET /api/v1/consolidation/jobs`
- `GET /api/v1/consolidation/jobs/:id`
- `POST /api/v1/consolidation/jobs/:id/retry`

---

# 十一、故障降级

| 故障 | 降级方案 |
|---|---|
| Embedding 服务不可用 | 使用全文和关键词检索 |
| LLM 服务不可用 | 保存原始事件，延迟整理 |
| 模型输出格式错误 | 重试，失败后进入失败队列 |
| sqlite-vec 不可用 | 降级 FTS5 与关键词检索 |
| 后台整理失败 | 保留 Job 和 Checkpoint |
| 关系索引不可用 | 仅执行基础检索 |
| Token 预算不足 | 优先返回高相关、高价值 Brief |
| Context 服务异常 | 不阻塞实时交互，返回明确降级状态 |

实时写入和读取必须尽量独立于 LLM 服务。

---

# 十二、可观测性与评估

不能只看系统是否运行，还要评估记忆质量。

## 12.1 Recall 质量

建立真实测试集，建议第一批准备 100～200 个问题。

指标：

- Recall@K：正确记忆是否被召回。
- MRR：正确记忆的排名。
- NDCG：整体排序质量。
- Expand Success Rate：二次展开成功率。
- Deep Recall Success Rate：深度回忆成功率。

## 12.2 记忆质量

- Memory Extraction Accuracy。
- Importance Classification Accuracy。
- Relation Precision / Recall。
- Conflict Detection Accuracy。
- False Forget Rate。
- Invalid Memory Update Rate。
- Memory Re-activation Success Rate。

特别关注 False Forget Rate，即不应该遗忘的记忆被错误遗忘的比例。

## 12.3 性能和成本

- Recall P50 / P95。
- Expand P50 / P95。
- Embedding 成本。
- Evaluator Token 消耗。
- Consolidation 平均耗时。
- 每次 Context 实际 Token 数。
- 模型失败率。
- 后台任务积压量。

---

# 十三、安全与数据完整性

1. 原始记忆与规范记忆分离。
2. 重要更新必须保留历史版本。
3. 模型推断不能伪装成用户事实。
4. 所有关联必须保留证据。
5. 删除操作必须能够追溯。
6. 允许用户明确要求保留或删除特定记忆。
7. 不能因为后台模型建议就直接删除重要记忆。
8. 记忆数据应具备备份和恢复能力。
9. Agent 之间共享记忆，但需要明确来源 Agent。
10. 不能将未经验证的模型输出直接作为高可信记忆。

---

# 十四、实施阶段

不要一次性实现全部功能。

## Phase 1：基础记忆系统

实现：

- SQLite 数据库与迁移记录。
- Raw Memories。
- Canonical Memories。
- Memory Sources。
- Memory Versions。
- Memory Indexes。
- 基础 FTS。
- Write API。
- Recall API。
- Context API。

验收：

- 能写入原始事件。
- 能创建规范记忆。
- 能根据全文和关键词检索；sqlite-vec 加入后再验收语义检索。
- 能追溯来源。
- 能更新并保留历史版本。

## Phase 2：Context 与两级检索

实现：

- Brief / Full。
- Context State。
- Recall。
- Expand。
- Deep Recall。
- Token Budget。
- Context 去重。
- 多轮检索。

验收：

- 同一个 Context 不重复加载。
- Brief 能升级为 Full。
- Agent 能根据线索继续检索。
- 不需要每次加载所有记忆。

## Phase 3：Memory Evaluator

实现：

- Model Router。
- 多模型配置。
- 重要性评估。
- 关系判断。
- 冲突判断。
- 保留建议。
- 结构化 Proposal。
- 程序校验与提交。

验收：

- 可以切换不同模型。
- 模型不可用时不影响记忆读写。
- 模型错误不会直接污染正式记忆。
- 所有评估结果可以追溯。

## Phase 4：记忆生命周期

实现：

- Accessibility。
- 时间衰减。
- 重要性动态评估。
- 记忆失效。
- 遗忘候选。
- 归档。
- 再激活。
- 保护策略。

验收：

- 低价值记忆可以逐渐降低检索优先级。
- 长期记忆不会因为未访问就自动消失。
- 低可访问性记忆仍可通过 Deep Recall 找到。
- 重新访问后能够恢复可访问性。
- 用户明确要求删除时可以执行删除策略。

## Phase 5：后台记忆巩固

实现：

- Consolidation Scheduler。
- Job Queue。
- Incremental Processing。
- 关系发现。
- 记忆合并。
- 跨事件归纳。
- 定期重要性复评。
- 记忆冲突处理。
- 后台失败恢复。

验收：

- 能够增量整理。
- 能够断点恢复。
- 能够处理新旧记忆关系。
- 能够形成更高层级的稳定记忆。
- 不影响实时读写。
- 记忆整理不会丢失原始证据。

---

# 十五、Codex 实施约束

Codex 在实施过程中必须遵守：

1. 严格按照阶段实施，不要一次性实现所有功能。
2. 每个阶段先检查已有项目结构，再设计改动。
3. 不要擅自引入 Redis、Kafka、K8s 或独立向量数据库。
4. 不要为了使用框架而增加不必要的抽象。
5. 不要把 LLM 直接作为数据库写入者。
6. 不要把所有记忆类型设计成独立子系统。
7. 所有重要业务逻辑必须有单元测试。
8. 所有 API 必须有输入输出 Schema。
9. 所有后台任务必须支持幂等和重试。
10. 所有记忆更新必须有版本控制。
11. 所有模型必须通过统一 Model Router 调用。
12. 不要在没有 Benchmark 的情况下盲目调整排序权重。
13. 每个阶段完成后输出实现说明、测试结果和未完成事项。
14. 不要为了提前优化而过度设计。

---

# 十六、最终架构原则

这个系统最终需要具备以下能力：

**记忆形成：** 能够从多模态事件中形成记忆。

**记忆组织：** 能够通过程序和 AI 将零散内容组织起来。

**记忆关联：** 能够发现不同记忆之间的关系。

**记忆巩固：** 能够把多次经历提炼为稳定知识。

**记忆衰减：** 能够让不重要的记忆逐渐变得不容易被想起。

**记忆遗忘：** 能够淘汰长期无价值的记忆。

**记忆再激活：** 能够通过新线索重新找到历史记忆。

**记忆回忆：** 能够根据当前任务逐步检索，而不是暴力注入全部记忆。

最终目标不是让 Agent 永远记住所有事情，而是让 Agent 在需要的时候，能够以合理的成本找到真正有价值的信息。

**Memory Engine 应当是一套独立、可演进、可评估、可恢复的长期记忆基础设施。**
