# 程序感知静默设计（2026-10-14，设计稿，未开发）

## 1. 目标与边界

当主人正在看电视、直播或其他沉浸式影音内容时，思绪不主动出声；任务完成、异常、待确认改为托盘状态/通知。**唤醒词始终可用**：主人喊“管家”后，识别、对话、确认和正常回复仍照常工作。

这不是现有 `GAMING` 的替代品。游戏态目前会停麦、停播、释放 ASR/TTS，因此不能用于影音静默；本期新增的是“禁止主动播报”的软静默，不能关闭听觉链路、不能改变唤醒词判定、不能取消正在进行的用户发起轮次。

不做：

- 不以任意后台进程（浏览器、播放器、Steam、Discord 等）作为静默依据。
- 不做整盘扫描、屏幕截图识别、云端媒体识别或前台内容分析。
- 不把任务管家直接接进 voice；当前 `task_manager.py` 仍是离线骨架。

## 2. 当前代码事实与可复用点

### 2.1 语音和免喊

- `VoiceDaemon._on_heard()` 通过 `wake.decide()` 判断去留；`_in_followup()` 在已有免喊窗口或正在正常对话时放行。
- `_accept_utterance()` 将已放行文本入 `text_q` 并播放提示音；`_wake_ack()` 只喊唤醒词时回“在呢”并开启免喊窗口。
- `_handle_item()` 负责一轮网关/TTS；`stop_speaking()` 只停止播放、不关闭网关流。任何程序感知静默都不得改这些被动链路。
- nudge 只在“一轮含承诺词而零工具调用”等条件下追发，现有实现已在 pending approval 存在时抑制 promise nudge；新增静默要另加“主动播报/追发是否可念”的门，而不是把语音输入拒掉。

### 2.2 现有运行态与 Windows 探测

- `runtime_state.py` 已有 `RuntimeMonitor`，每 2 秒轮询进程名、前台全屏和 GPU 利用率，并有可注入的 `procs/fullscreen/gpu/games`，适合沿用测试模式。
- 已存在 `foreground_fullscreen()`：用 `GetForegroundWindow`、`GetWindowRect`、`MonitorFromWindow`、`GetMonitorInfoW` 判断前台窗口是否铺满显示器；它只返回布尔值，**目前没有前台 PID、exe 路径、窗口标题或最小化/遮挡详情**。
- `process_names()` 用 Toolhelp32 快照取得全系统 exe 名，`gpu_util()` 通过 `nvidia-smi` 取最大 GPU 利用率。它们可用于游戏判断，但不能证明某个前台视频正在播放。
- `RuntimeMonitor.is_quiet()` 当前语义是 `state != ACTIVE`，包括开机/游戏后的 5 分钟 IDLE 和 GAMING；`VoiceDaemon.quiet()` 同样面向“主动侧”。不能把新影音静默塞进这两个状态，否则会误导现有游戏逻辑。

### 2.3 托盘、面板、日志、确认

- `status.tray_view()` 已有优先级：依赖错误 > pending > 静音 > 游戏中 > 启动 > 说话 > 在听。`Tray.notify()` 可发本机气泡；当前 pending 在非游戏时通知、游戏时只变紫。
- `VoiceDaemon.status()` 已经向面板/SSE 输出 `state/muted/gaming/runtime/followup_left/...`；`PanelBackend.snapshot()` 会转发 status、设置、事件和日志。
- `EventHub`、`elog` 与 `logq` 支持结构化事件；`audittail.PendingConfirm` 已把守卫 pending 映射成托盘和状态刷新。
- 当前面板设置通过 `SettingsStore` 热更新；`panel.py` 已有通用 GET/POST 注册点，但本期设计不要求新增路由。
- O1-B pending 仍绑定审批闩锁。程序静默时应保留其状态和超时；只改变提示渠道，不能自动批准、拒绝或让模型重试。

### 2.4 任务完成的现状

`task_manager.py` 能持久化 queued/running/waiting_approval/interrupted/terminal 状态和摘要，但尚未被 voice、guard、panel 调用。因此“任务完成改托盘”的实现必须等任务管家接线批次；本设计先定义事件契约，不能假定现有语音进程已收到完成事件。

## 3. 推荐判断：前台节目优先，音频仅作辅助

### 3.1 信号取舍

| 信号 | 用途 | 是否单独触发影音静默 | 原因 |
|---|---|---:|---|
| 前台窗口 exe/AUMID + 用户分类 | 主信号 | 是 | 可解释、可配置，避免后台应用误伤 |
| 前台全屏 | 强化信号 | 否 | 浏览器全屏可能是文档/游戏；窗口化播放器也可能在看 |
| 影音程序白名单 | 主分类 | 是，但有进入/退出滞后 | 用户意图明确，适合电视/直播客户端 |
| 浏览器前台 + 全屏 + 已配置域名/标题关键词 | 次分类 | 是 | 允许视频站；默认不按所有浏览器静音 |
| 系统音频会话活动 | 仅佐证/诊断 | 否 | 音乐、游戏、通话、后台网页都可能有声音，误判率高 |
| GPU 利用率 | 仅与全屏合用来识别游戏 | 否 | 视频硬解/浏览器/GPU 任务不可靠 |
| 后台进程存在 | 仅诊断 | 否 | 明确禁止“任意后台程序静音” |

推荐规则：

1. `manual_quiet`（面板/语音日后可加）优先级最高，直到明确关闭或超时。
2. 前台 exe 命中 `media_apps`，连续 5 秒后进入 `PROGRAM_QUIET`。
3. 前台 exe 为浏览器时，只有“前台全屏”且命中用户配置的标题关键词或域名线索时进入；V1 不获取浏览器 URL，默认只允许进程名 + 标题关键词。
4. 失去命中连续 15 秒才退出，防切菜单、广告、全屏切换抖动。
5. 音频会话活动只写诊断字段 `audio_active`，不改变状态；后续若接入 Core Audio，须先采集一周误判日志再讨论是否作为条件。

### 3.2 最小 Win32 能力

现有全屏布尔值不足以分类前台程序。无需新第三方 Python 依赖，建议在 `runtime_state.py` 或独立 `program_context.py` 以 `ctypes` 补：

```text
GetForegroundWindow
GetWindowThreadProcessId
OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)
QueryFullProcessImageNameW
GetWindowTextW
GetWindowRect + MonitorFromWindow + GetMonitorInfoW
IsIconic / IsWindowVisible
```

返回不可变 `ForegroundApp(pid, exe_name, exe_path?, title, fullscreen, visible)`。`OpenProcess`/路径读取失败必须降级为仅 PID/标题/全屏，不影响语音。此接口和现有 `process_names()` 一样应可注入 fixture。

`Get-StartApps`、注册表 App Paths、AppIndex 是“启动时选什么”的数据源，不是“当前前台是什么”的可靠来源；不可拿它们替代前台窗口 API。

## 4. 配置和分类

在 `data/voice-settings.json` 增加一个受 `SettingsStore` 验证的 `program_quiet` 节（默认关闭，避免升级即静音）：

```json
{
  "enabled": false,
  "enter_hold_sec": 5,
  "exit_hold_sec": 15,
  "media_apps": ["potplayermini64.exe", "tvbox.exe"],
  "browser_apps": ["msedge.exe", "chrome.exe"],
  "browser_title_keywords": ["哔哩哔哩", "Bilibili", "直播", "爱奇艺", "腾讯视频"],
  "manual_timeout_sec": 10800
}
```

- 进程名全部 casefold，标题关键词大小写不敏感；不支持正则，避免配置误写造成性能/安全问题。
- 只显示可编辑数组与开关；路径、标题和 PID 是运行态诊断信息，不写入设置。
- 可选 `always_quiet_apps` 仅用于明确影音客户端。浏览器没有关键词时不静音。
- 以“前台命中”为前提；后台 `media_apps`、后台浏览器、有声音的任何后台程序都不触发。

面板新增“程序感知静默”卡片：开关、应用/浏览器/关键词列表、当前前台名称（脱敏/只显示 basename）、全屏标记、命中规则、进入/退出倒计时、最近一次降级原因。列表编辑沿用现有 `SettingsStore` 热更新，不让面板执行或枚举应用。

## 5. 行为矩阵

| 事件 | ACTIVE/IDLE | PROGRAM_QUIET | GAMING |
|---|---|---|---|
| 带唤醒词的用户指令 | 正常处理和回复 | **正常处理和回复** | 当前语义：不听不说；不在本期改变 |
| 免喊窗口后续指令 | 正常处理 | 正常处理（既有窗口照常） | 当前不处理 |
| 主动任务完成播报 | 可念 | 不念，托盘通知/状态 | 不念，托盘状态 |
| 主动任务失败/异常 | 可念（短句） | 不念，托盘错误/通知 | 不念，托盘错误 |
| 待确认 | 紫色托盘 + 气泡 + 现有口头提问 | 紫色托盘 + 气泡；不额外主动念 | 紫色托盘，只变色、不弹气泡 |
| 模型普通回复 | 正常念 | 仅由用户唤醒/免喊触发时念 | 当前不念 |
| nudge | 现有规则 | 只在用户发起轮内允许；不作为独立主动播报 | 当前禁止 |

说明：

- “电视/直播时除非喊管家，否则不主动说话”不等于拒绝免喊窗口；主人刚喊过“管家”后应继续自然交互。
- 待确认的首个“需要确认吗”若来自**当前用户发起轮**，可照常念；若是任务后台/异步产生，PROGRAM_QUIET 只能托盘提醒。
- 任务完成/异常目前无接线。本表是未来 `task.notify` 事件的消费策略。

## 6. 状态机与日志

新增独立程序感知状态，避免污染 `RuntimeMonitor` 的 `ACTIVE/IDLE/GAMING`：

```text
NORMAL ──前台影音连续 enter_hold──> CANDIDATE_ENTER
  ▲                                  │
  │                         条件持续 ▼
  └──非影音连续 exit_hold── QUIET_PROGRAM
                                      │
                     手动/唤醒词不改变状态，仅放行当前被动轮
```

- `PROGRAM_QUIET` 是“主动播报抑制标志”，不是音频/语音守护状态。
- `GAMING` 覆盖 `PROGRAM_QUIET` 的播报决策；从游戏退出后重新采样前台程序。
- 前台 API 异常、配置为空、探测超过预算（建议每 2 秒检测耗时 >200ms）时进入 `NORMAL`，记录一次降级，不阻塞音频线程。

建议新增结构化事件（均不记完整窗口标题，最多 80 字并经脱敏）：

| ev | 必填数据 |
|---|---|
| `program.foreground` | exe、fullscreen、title_hint、pid（可选） |
| `program.quiet_enter` / `program.quiet_exit` | rule、hold_ms、exe |
| `program.probe_failed` | api、异常类别 |
| `program.quiet_suppressed` | kind=task_done/task_failed/pending/nudge、reason |
| `program.quiet_bypassed` | kind=wake/followup/user_turn |

面板状态和托盘追加 `program_quiet` / `foreground_app` / `quiet_reason`；托盘优先级建议保持错误 > pending > muted > gaming > program_quiet > busy > listening。program quiet 的颜色用低饱和蓝灰，标签“影音中（主动静默，喊管家可说话）”。

## 7. 误判与降级

1. **浏览器全屏非视频**：浏览器必须同时命中用户关键词；否则不静音。
2. **播放器只是后台播歌**：要求前台窗口；不因后台音频静音。
3. **全屏游戏被识别为影音**：游戏态原逻辑优先；在日志中记录两个命中，游戏覆盖。
4. **标题拿不到/受保护进程**：若 exe 命中明确影音白名单仍可静默；浏览器规则缺标题则不静默。
5. **前台探测失败或高延迟**：fail-open（保持可说话），只托盘/日志提示诊断，不能停止麦克风或 TTS。
6. **主人需要立刻听结果**：唤醒词和免喊窗口始终绕过“主动静默”；可在后续单独加入一次性“这件事完成时提醒我”。

## 8. 分批实现与验收

### Q0：只读探测与取证

- 新增 `ForegroundApp` Win32 采集器及 fixture 单测：桌面、普通窗口、全屏、访问拒绝、无前台窗口。
- 不改变语音/托盘行为；每 2 秒只写限频 `program.foreground` 变化事件。
- 验收：打开/切换 Edge、播放器、游戏时，日志准确显示前台 exe/fullscreen；不因后台播放器变化。

### Q1：分类和面板配置

- `ProgramQuietMonitor` 独立于 `RuntimeMonitor`，实现进入/退出滞后、配置验证、status 字段。
- 面板显示和编辑配置，默认关闭。
- 验收：明确影音 exe 前台 5 秒进入，切走 15 秒退出；浏览器未命中关键词不进；状态变化不打断正在说的话。

### Q2：主动播报门

- 新建单点 `should_announce(event_kind, user_initiated)`，任务/审批/nudge 都从这里决策，不在各处复制 `if quiet`。
- 先接待确认与 nudge；任务事件在 O3 真正接线后再接。
- 验收：影音时后台 pending 只托盘；喊“管家”后指令和回复仍可听；无 pending 时 nudge 不主动出声。

### Q3：任务完成/失败与回归

- O3 接线层把 `TaskManager` 状态变化送到播报门和托盘；不从 JSONL 轮询猜完成。
- 验收：完成、失败、interrupted、waiting approval 分别按矩阵呈现；重启恢复 interrupted 绝不念“完成”。

## 9. 上线前检查

1. 先在面板保持默认关闭，完成 Q0 一天日志取证。
2. 只添加一两个确定影音客户端和一个浏览器关键词，验证误判后再扩表。
3. 用“喊管家 + 不喊管家 + 免喊后续 + pending + 游戏”完整走查；特别确认 PROGRAM_QUIET 没有调用 `_suspend_audio()`、`stop_speaking()` 或清 `text_q`。
4. 出问题时关闭 `program_quiet.enabled` 即刻回退，不需要改 games.json、权限表或网关配置。
