# 2026-10-14 统一上线包：上线就绪审查

## 结论

**代码阻塞项：无。**

**上线前必须完成的部署阻塞项：**

1. **O2 运行工作区尚未更新 `tools\launch-app.ps1`。** 仓库版已在显式别名未命中时调用 `guanjia.app_index`，并在拉起后核实进程或窗口；线上工作区版本仍是旧实现：只做精确别名、`Start-Process` 后直接返回成功。两者 SHA-256 不同。`apps.json` 仓库工作区副本与线上副本的 SHA-256 一致，不能只同步映射，必须一起同步启动器。
2. **P4-1b 尚未应用。** 只读 `--check` 显示将写入 `tools.toolSearch=false`、主会话 allowlist、`memory-core.dreaming=false` 三项；schema dry-run 已通过。必须在网关停止后执行 apply，不能靠热更新。
3. **O1-B、O2 与 P4-1b 都要求网关重新加载插件/配置；Q0–Q2 要求语音守护进程重启。** 当前 `approval-ipc.json` 不存在，符合“当前运行网关尚未启动新 IPC 服务”的预期；不能把它当作上线后已可用的证据。

## 审查事实

### O1-B：审批一次性闩锁

- Guard 将原始 `toolName`、稳定序列化参数、session、source、摘要、指纹、创建/过期时间持久化在 `pending-approvals.json`；批准后只允许同 session、同 source、同工具、同 canonical params 的一次 consume，且在执行入口前改为 `executed`。
- Python 语音 IPC 客户端只读取 `{port, capability}` 与服务返回的摘要/id/状态/过期时间；不读取、更不传入原始参数。确认词命中后发送内部 retry marker，仍经主模型正常调用入口。
- IPC 仅监听 `127.0.0.1`，能力值由 32 字节随机数生成；`approval-ipc.json` 在服务 stop 时删除。现场 ACL 检查显示 `workspace\guanjia` 仅 `SYSTEM`、`Administrators`、`inksnow\inksn` 拥有完全控制，满足同用户私有文件的前提。Node 的 `mode: 0o600` 在 Windows 不是 ACL 控制，实际安全性以该目录 ACL 为准。
- 服务启动时写 descriptor，服务停止时 unlink；异常退出可能留下旧 descriptor，但新服务启动会覆盖它，语音端对旧端口会 fail-closed（无法批准，不会放行）。上线后必须实测 descriptor 出现、错误 capability 得 403、停止网关后 descriptor 消失。
- 语音确认/拒绝词在已经存在 pending 时优先处理，不进主模型；pending 时 nudge 已被抑制；批准后开 30 秒免喊窗口。

### O2：应用索引与受控启动

- `app_index.py` 的开始菜单、App Paths、Get-StartApps 收集均可注入 fixture；不扫描整盘、不启动程序。回退匹配只针对同一 `apps.json` 建索引，因此不扩大可启动白名单。
- `launch-app.ps1` 保持 apps.json 精确键/别名优先；仅 unique 回退匹配可以继续，ambiguous 返回候选，not_found 如实返回。拉起后最多等 3 秒，以期望进程名或窗口标题核实；核实失败返回 `unverified`，TS 工具不会声称成功。
- 当前 `apps.json` 的仓库工作区副本和线上副本一致；当前线上启动器仍旧，故 O2 上线必须同步新版启动器到 `C:\Users\inksn\.openclaw\workspace\tools\launch-app.ps1`。

### P4-1b：配置补丁

- `apply_1b()` 深拷贝配置，只更新三个目标键；保留原 `deny`，明确保留 `process`，不覆盖模型、网关、memory search 等其他配置。
- 脚本写前会以 `openclaw config patch --dry-run` 校验，写后 `config validate`，失败自动恢复其独立的 `.bak-p4-1b` 备份；重复 apply 为 no-op。
- 注意：P4-1b 的 rollback 会恢复**执行该步骤前的整份 `openclaw.json`**。因此它必须在同一维护窗口的最后一个配置写入步骤执行；之后若有人手工改过配置，不应直接 rollback，而应人工三方合并。

### 程序感知静默 Q0–Q2

- 默认 `program_quiet.enabled=false`，影音/浏览器/关键词均为空，因此新代码默认不改变任何 TTS、唤醒、免喊、VAD、barge-in 或 `_suspend_audio()` 行为。
- Q0 的 Win32 前台窗口读取失败会返回错误；Q1 看到失败立即退出候选（fail-open）。Q1 以进入 5 秒、退出 15 秒的默认滞后给出稳定的 `quiet_active`。
- Q2 的唯一播报门只读取该稳定状态。影音静默时，已实际接线的自动 nudge 不进入模型/TTS，待确认维持托盘/结构化事件；用户主动语音回复仍照常念。O3 任务完成/失败尚未接线，不能把 Q2 当作已覆盖任务通知。
- Q2 配置仅存于 `data\voice-settings.json`，面板热更新可改；代码生效仍需要重启语音进程。默认关闭使首次重启风险可控。

### 面板 P0

- 面板/控制口要求随机 token，拒绝外来 Host 和 Origin；`/say` 不接受 Origin；页面无外部 CDN 且以文本方式渲染日志。
- 程序静默配置和诊断仅为设置/观测；没有新增“立即静默”控制按钮。

## 建议上线顺序

预计维护窗口 **3–5 分钟**；其中网关不可用约 **45–90 秒**，语音不可用约 **15–30 秒**。不要同时启动两个语音看门狗。

1. **上线前只读准备（不中断）**
   - 备份现有 `C:\Users\inksn\.openclaw\openclaw.json`、`workspace\tools\launch-app.ps1`、`workspace\apps.json`。
   - 再次核对 `workspace\guanjia` ACL 仍只授予 SYSTEM/Administrators/inksnow。
   - 准备待同步文件：guard 插件、`app_index.py`、新版 `launch-app.ps1`；`apps.json` 仅在 hash 不一致时同步。
2. **停止网关**
   - 停止 OpenClaw Gateway，并确认 18789 不再监听；此刻语音仍在，但请求会失败，不要做口头审批验证。
3. **同步网关侧与工作区文件**
   - 部署 guard O1-B 源码和 O2 Python 模块。
   - 同步新版 `workspace\tools\launch-app.ps1`；再次核对 apps.json hash。
   - 执行 `python openclaw\p4_config_patch.py --step 1b --apply`，确认生成 `openclaw.json.bak-p4-1b`、写后校验通过。
4. **启动网关并核对 O1-B/P4**
   - 启动网关，确认插件服务启动、`approval-ipc.json` 出现且 descriptor 只含 port/capability。
   - 做一次不执行真实副作用的审批演练；确认 IPC 生命周期与审计，再继续。
5. **重启语音守护进程**
   - 结束 `GuanjiaVoice` 后等待至少 12 秒，再启动；以新 `tmp\voice.log` 的 `device rate`/`voice.ready` 为准。
   - 语音启动后检查 panel 状态与 tray 正常，不要在 SSH 会话直接启动语音。
6. **按下列真机句子验收**
   - 所有验收通过后才清理维护备份；保留 P4 自动备份供回滚。

## 最小真机验收句子

| 功能 | 说/做什么 | 预期 |
|---|---|---|
| O1-B 待确认 | “管家，删除一个专门用于验收的空文件。” | 只问一次；托盘显示待确认；审计有 `pending.open`。 |
| O1-B 批准 | “允许。” | 不把“允许”交给模型闲聊；30 秒免喊；模型仅重试原调用一次；审计顺序含 `pending.approved`、`pending.executed`。 |
| O1-B 拒绝/重放 | 重新制造同类待确认后说“不要”；再说“允许”或重复上一确认 | 拒绝后绝不执行；已执行的闩锁不能第二次放行；改写参数仍被拦。 |
| O2 精确映射 | “管家，打开网易云音乐。” | 使用 apps.json 显式别名，进程/窗口核实后才回答成功。 |
| O2 回退/歧义 | “管家，打开网易云”以及一个故意含混名称 | 前者可唯一匹配；含混名称列候选并追问，不启动。 |
| O2 不存在 | “管家，打开一个不存在的软件。” | 如实说未登记/未找到，不调用任意 shell 启动。 |
| P4-1b | 在新会话询问记忆相关内容，再做一次普通工具请求 | `memory_search`/`memory_get` 可直接可见；`process` 仍可用；不会暴露白名单外工具。 |
| Q0/Q1 默认 | 不改面板默认设置，正常喊“管家，几点了？” | 行为与原来一致；panel 仅显示前台程序诊断且 quiet_active 为 false。 |
| Q2 静默 | 面板启用影音白名单，前台打开匹配的全屏影音至少超过进入滞后；制造一条自动 nudge 或待确认 | 主动 nudge 不念；托盘/事件可见；随后喊“管家，几点了？”仍正常回应和朗读。 |
| P0 面板 | 打开 `http://127.0.0.1:18795/panel` | 状态、程序诊断、设置保存、待确认卡片均可用；非本机/无 token API 请求仍拒绝。 |

## 回滚

1. **网关/Guard/O2/P4 失败**：停止网关；恢复部署前的 guard、`launch-app.ps1`、`apps.json` 备份；执行 `python openclaw\p4_config_patch.py --step 1b --rollback`（仅当之后无人手改配置）；启动网关并确认 IPC descriptor 生命周期回到旧行为。
2. **Q0–Q2/面板问题**：恢复对应 Python/面板文件；重启语音守护进程（结束后至少等 12 秒再启动）。紧急降级可先在 `voice-settings.json` 保持/改回 `program_quiet.enabled=false`，其效果是停止静默决策而不影响唤醒和音频链。
3. **仅 O2 问题**：停止网关，恢复旧 `workspace\tools\launch-app.ps1` 与 apps.json，启动网关；不需要改 guard 策略。
4. **审批异常**：停止网关使 loopback IPC 失效（fail-closed）；不要尝试通过模型复放参数。恢复旧 guard 后再启动，待确认项应重新发起。

## 本次证据与未覆盖项

已运行并通过：`test_p4_config_patch.py`、`test_app_index.py`、`test_approval_words.py`、`test_program_context.py`、`test_program_quiet.py`、`test_announcement.py`、`test_p21_panel_control.py`、guard 的 `approvals.test.ts` 与 `approval-ipc.test.ts`。P4-1b live `--check` 和 schema dry-run 通过，未写任何配置。

未运行：网关重启后的插件 runtime inspect、真实 IPC descriptor 生命周期、真实工具调用、真实 TTS/托盘/全屏影音验收；这些必须按上表在维护窗口执行，不能由离线测试替代。
