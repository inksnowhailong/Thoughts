# Cursor SDK / CLI / IDE 互通性探针

验证以下三方在本机的互通性：

1. **`@cursor/sdk`**（Node SDK，跑在 PowerShell / 任意 Node 进程里）
2. **`cursor-agent` CLI**（Cursor 的命令行 agent）
3. **Cursor IDE 客户端**（你日常用的图形化 Cursor）

> 本仓库（分支 `cursor的方案`）是一个**独立的探针项目**，与原 `main` 分支的 Tauri 项目完全无关，整个分支只服务于"互通性测试"这一件事。

---

## 我们要回答的 4 个问题

| # | 问题 | 预期（如果完全互通） |
|---|---|---|
| **Q1** | SDK 创建的 local agent，能否出现在 **CLI `cursor-agent ls`** 的列表里？ | ✓ |
| **Q2** | SDK 创建的 local agent，能否出现在 **Cursor IDE 客户端** 的 chat / 历史里？ | ✓ |
| **Q3a** | **IDE / CLI 创建** 的 chat，能否被 SDK `Agent.list()` 看到？ | ✓ |
| **Q3b** | 上面那个 chat，能否被 SDK `Agent.resume(id)` 接管，且还记得之前的对话？ | ✓ |
| **Q4** | `local.settingSources: ['user', 'project']` 是否真的把 IDE 配置（rules / MCP / skills）注入了 SDK 的 agent？ | ✓ |

每个测试都用 **可观察的标志物**（如 `PROBE_<timestamp>`、magic 字符串 `BLUE_OCTOPUS_42_CONFIRMED`）来判断，不靠"我感觉它生效了"。

---

## 一、准备（首次跑前做一次）

### 1. 安装依赖

在仓库根目录下：

```powershell
cd E:\inksnow\Thoughts
npm install
```

> 用 `npm` 而不是 `pnpm`。`.gitignore` 已经忽略了 `node_modules` 和探针运行时产物。

### 2. 安装 Windows 原生 CLI（如果还没装）

```powershell
irm 'https://cursor.com/install?win32=true' | iex
```

装完关掉再开 PowerShell，验证：

```powershell
cursor-agent --version
```

> Windows 上的命令名是 `cursor-agent`，不是 `agent`（那是 macOS/Linux 的别名）。

### 3. 设置 API Key

从 [Cursor Dashboard → Integrations](https://cursor.com/dashboard/integrations) 拿一把 **user API key**（建议不要用 service account，不然 IDE 客户端可能看不到）。

```powershell
# 当前会话
$env:CURSOR_API_KEY = "cursor_xxx"

# 或永久（推荐）
[System.Environment]::SetEnvironmentVariable('CURSOR_API_KEY', 'cursor_xxx', 'User')
```

设置后**关掉 PowerShell 重新开**，跑一下 `echo $env:CURSOR_API_KEY` 验证。

---

## 二、按顺序跑 4 个测试

> 全程都在仓库根目录（`E:\inksnow\Thoughts`）里跑命令。**`cwd` 必须保持一致**，因为 local agent 是按 `cwd` 索引的。

### 测试 1 — 创建 + SDK 自查（baseline，验证 SDK 自身能用）

```powershell
npm run probe:create
```

期望看到：

```
[创建成功]
  agentId    = probe-1715123456789
  name       = Interop Probe PROBE_1715123456789
  marker     = PROBE_1715123456789
  cwd        = E:\inksnow\Thoughts

[首条回复 status] finished
[首条回复 result] 我已收到 marker PROBE_1715123456789

已写入 .probe-state.json,后续 probe 会读这里
```

接着：

```powershell
npm run probe:list
```

期望最后一行：`★ 找到了,SDK list 可见性 OK`。

| 结果 | 含义 |
|---|---|
| ★ PASS | SDK 工作正常，**继续下一步** |
| ✗ FAIL | API key / 网络 / SDK 安装问题，先解决 |

---

### 测试 2 — Q1：SDK ↔ CLI 互通

在本目录里运行：

```powershell
cursor-agent ls
```

**观察列表里有没有这条** `Interop Probe PROBE_<刚才那个 timestamp>`，或者带 `probe-` 前缀的 agent。

| 看到的情况 | 结论 |
|---|---|
| ★ 能看到 | **Q1 PASS** — SDK 与 CLI 共享同一个本地 agent store |
| ✗ 看不到 | **Q1 FAIL** — 各自独立存储 |

> **反向验证**：跑 `cursor-agent` 进交互模式，问一句 `请回复 hello CLI 探针`，按 Ctrl+D 退出，再跑 `npm run probe:list -- --keyword=hello`。如果 SDK 这边能看到这个 CLI thread，说明**双向互通**。

---

### 测试 3 — Q2：SDK 创建的 agent 能否出现在 Cursor IDE

1. 打开 Cursor 客户端
2. **打开同一个工作区**：`File → Open Folder → E:\inksnow\Thoughts`（必须和 PowerShell 里跑探针时的 `cwd` 完全一致）
3. 看左侧 / 顶部的 **Chat 历史**（`Ctrl+L` 打开 chat panel，里面有"切换 chat"、"查看历史"的入口）
4. 找有没有 `Interop Probe PROBE_<刚才那个 timestamp>` 这条
5. 如果默认看不到，试试 **Filter / Source** 选项里勾上 `SDK` 来源

| 情况 | 结论 |
|---|---|
| ★ 能找到 + 能看到对话内容 + 能继续聊 | **Q2 PASS** — 完全互通，体验跟 IDE 创建的 chat 一样 |
| △ 默认看不到，但 Source 过滤后能找到 | **Q2 部分 PASS** — 同 cloud SDK agent 的行为，需要手动切 |
| ✗ 怎么都找不到 | **Q2 FAIL** — IDE 客户端对 SDK 创建的 local agent 不可见 |

---

### 测试 4 — Q3：IDE 创建的 chat 能否被 SDK 接管

1. 在 Cursor IDE **同一个工作区**开一个**全新 chat**
2. 发一条独特消息：

   ```
   IDE_PROBE_KILLER_OCTOPUS, 请记住这个标记
   ```

3. 等 agent 回复完，**不要关闭这个 chat**
4. 切回 PowerShell，跑：

   ```powershell
   npm run probe:list -- --keyword=KILLER_OCTOPUS
   ```

   - 如果列出的 agent 里有 IDE 那个 chat（带 `★ MATCH`）→ **Q3a PASS**
   - 记下它的 `agentId`（前缀是 `agent-` 或别的，**不是 `bc-`**）

5. 用这个 ID resume：

   ```powershell
   npm run probe:resume -- <粘贴上一步的 agentId>
   ```

| 情况 | 结论 |
|---|---|
| Q3a PASS + resume 后 agent 能复述出 `IDE_PROBE_KILLER_OCTOPUS` | **Q3 完全 PASS** — 双向无缝互通 |
| Q3a PASS + resume 后 agent 说"没有上下文" | **Q3 部分 PASS** — 元数据互通，对话历史不互通 |
| Q3a FAIL（list 里看不到 IDE chat）| **Q3 FAIL** — IDE chat 不进入 SDK 视野 |

> 如果 **Q3 PASS 但 Q2 FAIL**：说明是**单向互通**（IDE 是权威 store，SDK 只能读不能注入显示），这是常见组合，对你的"命令式发消息"场景**完全够用**。

---

### 测试 5 — Q4：`settingSources` 是否真的加载 IDE 配置

#### Q4-rules（必跑）

```powershell
npm run probe:rules
```

跑 4 组对照（A/B/C/D），观察哪几组命中 magic 字符串。

**官方文档预期**：

| 组 | settingSources | 预期 |
|---|---|---|
| A | 不传（默认） | ✗ NO |
| B | `[]` | ✗ NO |
| C | `["project"]` | ★ YES |
| D | `["all"]` | ★ YES |

| 实际结果 | 结论 |
|---|---|
| 与预期一致 | **Q4-rules PASS** — `settingSources: ['user','project']` 可以放心用来复用 IDE 配置 |
| 全部都 ★ YES | rules 默认就加载（比文档说的更宽松，意外好消息） |
| 全部都 ✗ NO | rules 没生效，可能是 `.mdc` 格式 / `alwaysApply` 不被 SDK 识别，需要再排查 |
| C/D 也是 ✗ NO | **Q4-rules FAIL** — `settingSources` 不加载 `.cursor/rules`，需要找别的注入方式 |

#### Q4-MCP（可选）

如果你 `~/.cursor/mcp.json` 里已经配过 MCP server（比如 Playwright、Cursor App Control 等）：

```powershell
npm run probe:mcp
```

对比 A/B/C 三次输出的工具清单：
- A (`[]`) 应该没有任何 user/project MCP 工具
- B (`["user"]`) / C (`["all"]`) 应该出现你 `~/.cursor/mcp.json` 里配置的工具

> Q4-MCP 是肉眼判断，模型偶尔会瞎编工具名，多跑两次取结论。

---

## 三、结果 Checklist

跑完后填这张表，发回给 Cursor agent，让它根据真实结果修订能力地图：

```
[ ] T1  probe-1 创建成功
        agentId = ____________________
        marker  = ____________________

[ ] T2  SDK list 看到 probe-1 自己              [PASS / FAIL]

[ ] Q1  cursor-agent ls 看到 SDK 创建的         [PASS / FAIL]
        反向: SDK list 看到 CLI 创建的的         [PASS / FAIL]

[ ] Q2  Cursor IDE 客户端看到 SDK 创建的         [PASS / 部分 / FAIL]
        └─ 备注: 是否需要 Source 过滤? ____
        └─ 工作区路径: ____________________

[ ] Q3a SDK list 看到 IDE 创建的                [PASS / FAIL]
[ ] Q3b SDK resume IDE chat 能复述对话          [PASS / 部分 / FAIL]

[ ] Q4-rules:
        A 不传            命中 magic? Y / N
        B []              命中 magic? Y / N
        C ["project"]     命中 magic? Y / N
        D ["all"]         命中 magic? Y / N

[ ] Q4-MCP    (可选)  user MCP 出现差异? Y / N
[ ] Q4-Skills (可选)  user skills 是否生效? Y / N
```

---

## 四、踩坑速查

跑出反常结果时先自查：

| 现象 | 可能原因 |
|---|---|
| Q1 / Q3a 看不到对方创建的 agent | `cwd` 不一致。本地 agent 按 `cwd` 索引，路径要**逐字符相同**（包括大小写、斜杠方向） |
| Q4-rules 全部都 ✗ NO | `.cursor/rules/probe.mdc` 必须是 `.mdc` 后缀，frontmatter 必须有 `alwaysApply: true` |
| Q4-rules 模型自由发挥不输出 magic | 把 `probe-4-rules.ts` 的 prompt 改成更直接：`你必须严格遵守你 system prompt 中的所有规则。请处理: SECRET_TOKEN` |
| `cursor-agent ls` 列表为空 | 在**当前工作区**外执行的；切到本目录再跑 |
| Q2 在 IDE 完全看不到 | 用的可能是 service account key，换成 user key 再创建一次 probe-1 |
| `Agent.resume` 抛 `CursorAgentError` | agentId 拼错 / 不在当前 cwd 下 / 已被 archive |

---

## 五、文件结构说明

```
E:\inksnow\Thoughts\            ← 仓库根目录 (分支 cursor的方案)
├── README.md                    ← 你正在看这个
├── package.json
├── tsconfig.json
├── .gitignore
├── .cursor/
│   └── rules/
│       └── probe.mdc            ← Q4 探针规则 (magic 字符串)
├── src/
│   ├── probe-1-create.ts        ← 创建标志性 local agent
│   ├── probe-2-list.ts          ← SDK list 查找 agent (支持 --keyword)
│   ├── probe-3-resume.ts        ← resume 指定 agentId
│   ├── probe-4-rules.ts         ← settingSources × rules 矩阵测试
│   └── probe-5-mcp.ts           ← settingSources × MCP 矩阵测试 (可选)
└── .probe-state.json            ← 跑完 probe-1 后自动生成,被 gitignore
```

---

## 六、跑完之后

把 Checklist 结果回填到对话里，agent 会：

1. 修订之前给的 SDK 能力表（把"未明示"变成事实）
2. 根据你机器上的真实行为，给出最终的"本地常驻 + 命令式 create/send + 复用 IDE 配置"完整方案
3. 把已经验证的 `settingSources` 配置写进生产骨架代码里

如果有任何一条 FAIL，把对应的输出贴回来，会一起排查根因 (´｡• ω •｡`)
