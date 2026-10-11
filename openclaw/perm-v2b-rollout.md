# 权限 v2 补丁 b/c 上线与回滚清单

对应事故：2026-10-11 13:55~13:59 模型编造"要审批"。本次上线内容：

| 提交 | 内容 |
|---|---|
| `a2770df` | 四个专用工具直接可见（`catalogMode: direct-only`）、待确认托盘提醒、编造拦截核对、规则段/技能口径、`perm-v2b-apply.mjs`（去掉 `approvals.plugin`） |
| 本次（`feat-guard-strip-exec-ask`） | 守卫在 exec 钩子里把模型传的 `ask` 改成 `off`，审计记 `askStripped` |

不开 `--directory-mode`。全程在家里电脑 PowerShell 里执行；改配置、停启网关、重置会话都是线上动作，按顺序来。

## 上线

```powershell
# ---- 0. 变量与预检（网关还开着，只读） ----
$inst = 'C:\Users\inksn\AppData\Roaming\fnm\node-versions\v24.21.0\installation'
$node = "$inst\node.exe"
$oc   = "$inst\node_modules\openclaw\openclaw.mjs"          # 用 node 调 openclaw.mjs：PS 5.1 直接调原生程序会吞 JSON 里的双引号
$git  = 'F:\Git\mingw64\bin\git.exe'
cd F:\coding\guanjia
& $git log --oneline -3                                     # 应看到 feat-guard-strip-exec-ask 和 feat-core-tools-approval-tray
& $node $oc plugins inspect guanjia-guard --runtime --json  # 新代码能加载：plugin.status=loaded、diagnostics 为空、tools 4 个；记下 install.acceptedSurfaceHash
& $node openclaw\perm-v2b-apply.mjs --check --agents         # 只看会改什么，不写文件

# ---- 1. 先静音，避免网关重启时语音轮报错 ----
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18795/api/control -ContentType 'application/json' -Body '{"action":"mute"}'

# ---- 2. 停网关 ----
& $node $oc gateway stop --force
Start-Sleep 5
(Test-NetConnection 127.0.0.1 -Port 18789 -WarningAction SilentlyContinue).TcpTestSucceeded   # 必须 False
Get-CimInstance Win32_Process -Filter "Name='node.exe'" | ? { $_.CommandLine -match 'index\.js gateway' }   # 必须没有输出

# ---- 3. 补丁脚本：备份 → 去掉 approvals.plugin → 同步规则段和两份技能（不加 --directory-mode） ----
& $node openclaw\perm-v2b-apply.mjs --apply --agents
& $node $oc config get approvals.plugin                     # 应当没有值
(Get-Item C:\Users\inksn\.openclaw\openclaw.json).LastWriteTime   # 记下改配置的时间

# ---- 4. 启网关（计划任务 "OpenClaw Gateway"） ----
& $node $oc gateway start                                   # 起不来就：schtasks /run /tn "OpenClaw Gateway"
1..30 | % { if ((Test-NetConnection 127.0.0.1 -Port 18789 -WarningAction SilentlyContinue).TcpTestSucceeded) { 'gateway up'; break }; Start-Sleep 2 }

# ---- 5. 核对 ----
# 5a. 网关进程启动时间必须晚于改配置时间（早于的数量必须是 0）
$cfgTime = (Get-Item C:\Users\inksn\.openclaw\openclaw.json).LastWriteTime
(Get-CimInstance Win32_Process -Filter "Name='node.exe'" | ? { $_.CommandLine -match 'index\.js gateway' -and $_.CreationDate -le $cfgTime }).Count
# 5b. 插件诊断为 0、工具 4 个、声明面没变（acceptedSurfaceHash 与第 0 步记下的相同，所以不需要重新授权）
& $node $oc plugins inspect guanjia-guard --runtime --json
# 5c. exec 主机策略没被动到：仍是 security=full / ask=off
& $node $oc approvals get --json

# ---- 6. 重置主线会话 agent:main:main（网关必须开着；需要 operator.admin，本机 CLI 一般有） ----
& $node $oc sessions list --json                            # 重置前先记一下这条的 sessionId
$p = '{\"key\":\"agent:main:main\",\"reason\":\"reset\"}'
& $node $oc gateway call sessions.reset --params $p --json  # 返回 ok:true 和新 entry
& $node $oc sessions list --json                            # 重置后：同一个 key，会话历史已清空

# ---- 7. 重启语音和托盘（托盘就在语音进程里；看门狗 voice-supervisor.ps1 会拉起新进程，加载新代码；新进程默认不静音） ----
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18795/api/control -ContentType 'application/json' -Body '{"action":"restart"}'
Start-Sleep 25
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? { $_.CommandLine -match 'guanjia\.voice' } | select ProcessId, CreationDate   # 启动时间应是刚才
Get-Content F:\coding\guanjia\tmp\voice.log -Tail 15        # 看到 listening，托盘图标变绿

# ---- 8. 冒烟（对着音箱说，再看审计） ----
#  a) "打开音箱软件"   → audit 里 tool=launch_app、decision=allow，没有 via:tool_call
#  b) 让它删一个无用的测试文件 → 托盘变紫 + 气泡"管家在等你确认：…"，回答"允许"后恢复；audit 依次 block-need-confirm / l2-confirmed / allow-confirmed
#  c) 日志里 "tool-search: cataloged N tools" 的 N 应为 54（原来 58；第一轮对话后才会打这行）
Get-Content C:\Users\inksn\.openclaw\workspace\guanjia\audit.jsonl -Tail 12
Select-String C:\Users\inksn\AppData\Local\Temp\openclaw\openclaw-2026-10-11.log -Pattern 'cataloged' | select -Last 1
Select-String C:\Users\inksn\.openclaw\workspace\guanjia\audit.jsonl -Pattern 'askStripped' | select -Last 3   # 模型传过 ask 就会记原值
```

### 关于插件重新授权

不需要 `openclaw plugins enable guanjia-guard --accept-capabilities`。授权记录的是 **清单里声明的能力面**（工具名、声明的 hooks、MCP、技能等，见 `install.acceptedSurface`）的哈希，不是可执行文件：

- `catalogMode` 是工具对象上的运行时字段，不在清单里；
- exec 的 `ask` 改写走的是原来就在用的 `before_tool_call` 钩子（`acceptedSurface.hooks` 本来就是空）；
- `openclaw.plugin.json` 没有改，`contracts.tools` 仍是 speak / launch_app / get_location / get_weather。

所以第 5b 步 `acceptedSurfaceHash` 应与预检相同。**不要** `plugins disable` 再 `enable`（那会要求重新授权）。只有第 5b 步出现 consent / blocked 类诊断时才执行：
`& $node $oc plugins enable guanjia-guard --accept-capabilities`，再 `& $node $oc plugins reload guanjia-guard`，然后重新看 5b 直到 diagnostics 为空。

### 重置 `agent:main:main` 之后

- 保留：工作区文件（`SOUL.md` / `IDENTITY.md` / `AGENTS.md` / `MEMORY.md` / `memory/timeline.md` / 技能 / `apps.json`）、插件状态（`workspace/guanjia/` 下的分级表、记住的放行规则、审计）；会话条目上的设置（模型的用户级覆盖、思考档位、`sendPolicy`、队列模式、标签）；会话的 `delivery` 路由，所以微信那边的最后投递目标不变，语音仍用同一个 key。
- 丢失：这条会话已有的对话上下文（含 10-10 的"审批超时"历史）、压缩计数和 token 统计，旧历史按网关的 reset 归档策略保留但不再进入模型上下文；运行时自动产生的模型回退/限流覆盖会被清掉，回到配置里的默认模型（`siliconflow/deepseek-ai/DeepSeek-V4-Flash`）；插件进程内的待确认状态（3 分钟有效）本来就不跨重启。
- 方式：网关 RPC `sessions.reset`（`params: {key, reason: "reset"|"new"}`），官方聊天命令 `/new`、`/reset` 走的是同一套流程。`sessions.reset` 需要 `operator.admin`，万一提示权限不足，改在 Control UI 的该会话里发 `/reset`。

## 回滚

```powershell
# 1. 停网关
& $node $oc gateway stop --force
# 2. 还原 openclaw.json、AGENTS.md 和两份技能（用上线时 --apply 写下的备份清单 ~\.openclaw\perm-v2b-last.json）
& $node F:\coding\guanjia\openclaw\perm-v2b-apply.mjs --rollback
# 3. 代码回退（revert 不改历史；只回退已提交文件，别人的未提交文件不受影响）
cd F:\coding\guanjia
& $git revert --no-edit <本次提交哈希> a2770df
#    只想撤掉 exec 的 ask 改写、保留 a2770df：只 revert <本次提交哈希>
# 4. 启网关
& $node $oc gateway start
# 5. 重启语音和托盘（加载回退后的代码）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18795/api/control -ContentType 'application/json' -Body '{"action":"restart"}'
```

会话重置没有回滚：旧上下文已经不进模型了，也不需要回滚；回滚后 `approvals.plugin` 会回到微信路由。
