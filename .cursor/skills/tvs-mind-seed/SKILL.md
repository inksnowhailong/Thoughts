---
name: tvs-mind-seed
description: 为 team agent 初始化私有记忆。读取 .cursor/.team/config.json 中的 role 先验，通过 4-6 轮访谈生成 profile.json、personality.json、memory-active.json、memory-raw.md、memory-consolidated.md、memory-index.jsonl、memory-sources.jsonl。Use after tvs-team-spawn, for leader or each sub chat.
---

# Memory Onboarding

你正在为单个 agent 初始化私有记忆目录。每个 leader/sub 都要单独执行一次，避免 chat 崩溃后丢失上下文。

## 输入

调用格式：

```text
/tvs-mind-seed <agentName>
```

如果没有传 agentName，先读取 `.cursor/.team/config.json`，列出 `leaderName` 和 `subs[].name` 让用户选择。

## 前置检查

1. 确认 `.cursor/.team/config.json` 存在。
2. 根据 agentName 判断：
   - 等于 `leaderName`：这是 leader，定位偏编排。
   - 命中 `subs[].name`：读取其 `role`、`roleName`、`model`。
   - 未命中：询问用户是否作为 standalone agent 初始化。
3. 运行：

```bash
node .cursor/runtime/team.mjs memory-init . <agentName>
```

这会创建基础五件套骨架。已有文件不会覆盖。

## 访谈问题

用 4-6 轮自然对话收集下面字段。每轮只问 1-2 个问题。

### 必问

1. **角色定位**：这个 agent 在团队中主要负责什么？
2. **关注点**：它工作时优先看哪些东西？列 3-5 条。
3. **不该做什么**：哪些事情它应该避免或交给别人？列 2-4 条。
4. **沟通风格**：简洁直接 / 详细解释 / 技术导向 / 平易近人 / 毒舌不留情 / 自定义。
5. **硬边界**：哪些线绝对不能跨？例如不擅自部署、不替用户拍板、不改业务代码。

### 可选

6. **codename / 人设**：用户想给它名字、口头禅、小癖好吗？没有就保持空。

如果是 sub，要把 role 先验带入问题里，例如：

```text
它现在是 sub-architect（架构师）。你希望它更偏模块边界、长期演进，还是偏方案取舍审查？
```

如果是 leader，要强调：

```text
leader 的主要职责是派活、收回执、写黑板和管理链路，不应该亲自做所有实现。
```

## 写入文件

用户确认总结后，写入 `.cursor/.team/memory/<agentName>/`。

### profile.json

```json
{
    "schemaVersion": 1,
    "agent": "<agentName>",
    "role": "<roleId 或 null>",
    "roleName": "<roleName 或 null>",
    "codename": null,
    "positioning": "一句话定位",
    "focus": ["关注点 1", "关注点 2"],
    "outOfScope": ["不该做 1", "不该做 2"],
    "communicationStyle": "concise|detailed|technical|warm|sharp|custom",
    "communicationStyleNote": "",
    "boundaries": ["硬边界 1", "硬边界 2"],
    "notes": "",
    "createdAt": "ISO",
    "updatedAt": "ISO"
}
```

### personality.json

```json
{
    "schemaVersion": 1,
    "agent": "<agentName>",
    "name": "<codename 或 agentName>",
    "traits": [],
    "tone": "沟通风格的自然语言描述",
    "kaomojiPreference": [],
    "catchphrase": null,
    "quirks": null,
    "boundaries": "人格层边界摘要",
    "roleSeed": "该角色的 systemPromptTemplate；leader 或 standalone 可为 null",
    "createdAt": "ISO",
    "updatedAt": "ISO"
}
```

### memory-active.json

```json
{
    "schemaVersion": 1,
    "agent": "<agentName>",
    "role": "<roleId 或 null>",
    "hardConstraints": ["从 outOfScope / boundaries 提炼的硬约束"],
    "ongoingTasks": [],
    "recentDecisions": [],
    "memoryHints": ["从 team-roles.json 读取的 memoryHints；leader 可为空"],
    "updatedAt": "ISO"
}
```

### 其余文件

`memory-init` 已创建：

- `memory-raw.md`
- `memory-consolidated.md`
- `memory-index.jsonl`
- `memory-sources.jsonl`

如果不存在，补齐空骨架；不要覆盖已有内容。

## 写入规则

- 不要把整段访谈原文塞进记忆。
- 只写用户确认的稳定定位、偏好、边界和硬约束。
- 不写密钥、token、cookie、临时任务细节。
- 如果文件已存在且非空，先询问是覆盖、合并还是退出。
- `memory-active.json` 是启动时优先读取的硬约束，不要写软建议。

## 收尾

告诉用户：

```text
<agentName> 的私有记忆已初始化。之后它启动时会先读取 profile/personality/memory-active。
如果它在工作中需要记住长期模式，可以追加到 memory-raw.md，后续再整理进 active/index/consolidated。
```

然后停止。不要继续执行该 agent 的业务任务。
