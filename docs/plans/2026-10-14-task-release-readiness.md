# 2026-10-14 任务管家第二上线包就绪审查

审查范围：O3、O4、O5、O5b、P1 面板任务摘要和 P4-1b。本文是上线前只读审查结论；本次未修改线上配置、未重启网关或语音。

## 结论

**可请求重启上线。** 代码阻塞已清除，已通过任务、Guard、面板 DTO 与 P4 迁移的针对性检查；剩余项目均为重启后的真机验收，不是代码阻塞。

1. **任务审批闭环（已修复）。** 语音 approval IPC 可按高熵 pending id 读取并决定最新 pending，不把 worker session 或原始参数给 UI；Guard 决定后仅回传 task id + approve/reject 到 Task IPC。approve 以原 session 重启受闩锁约束的 retry；reject 协作取消 waiting task。
2. **取消到达 executor（已修复）。** Task IPC 的 `/cancel` 回调统一进入 `TaskWorkerExecutor.request_cancel()`，覆盖 queued/running/waiting_approval；已送入网关的请求仍不能硬中止，返回后也不会完成写入。
3. **worker 完成协议（已修复）。** executor 严格读取最后一个非空行的 `TASK_OUTCOME`；前置说明不会再误判失败。
4. **P4-1b allowlist（已修复）。** 迁移保留当前 allow 中的未知手工项，仅追加缺失的四个 task 工具；不再整体覆盖 allowlist。当前 live dry-run 的完整差异是追加 `task_start`、`task_status`、`task_cancel`、`task_report`；其余 allow 项保持不变。

## 已核对的实现事实

### O3 状态和恢复

- `TaskManager` 的 JSONL 每次状态变化 `flush + fsync`；任务 id 为 UUID4 hex，独立会话键固定为 `agent:main:task-<id>`。
- 上次进程留下的 `running` / `waiting_approval` 在 restore 时改为 `interrupted`，理由固定为“管家重启中断，未自动继续”；不会扫描后自动恢复。
- 可运行状态仅由 `TaskWorkerAdapter.reserve()` 领取，`TaskManager.max_concurrent` 默认是 2。`TaskDispatchScheduler` 也不扫描历史 `queued`。
- 取消只是状态改变，绝不 kill 外部进程；这是正确边界，但当前尚未把取消信号送到 executor（阻塞项 2）。

### O4 IPC、Guard 和提示词

- Task IPC：语音启动时在 `127.0.0.1:随机端口` 监听，描述符为
  `C:\Users\inksn\.openclaw\workspace\guanjia\task-ipc.json`。随机 capability 不进入 task 审计或工具返回；语音正常 stop 时删除描述符。
- Approval IPC：网关启动时在 `127.0.0.1:随机端口` 监听，描述符为
  `C:\Users\inksn\.openclaw\workspace\guanjia\approval-ipc.json`。随机 capability 每次网关启动重生，网关 stop 时删除描述符。
- 两个服务均 fail-closed：描述符、端口、capability 或 HTTP 调用失效时，工具不会创建或执行任务/审批。
- 描述符依赖 workspace 目录的当前用户 ACL；Node 的 `mode: 0o600` 在 Windows 上不能替代 NTFS DACL。上线前必须确认 `C:\Users\inksn\.openclaw\workspace\guanjia` 不向其他账户提供读取权限。
- `task_start/status/cancel/report` 是 direct-only 工具，Task IPC 只给工具层返回摘要，隐藏 goal 和 worker session。`AGENTS.md` 已要求长任务才建 task，短操作不建；但其中“task_start 不执行模型”的旧说明已被 O5b 改变，应在修复后更新为“创建后仅在本轮首句成功播放时才受控调度”。
- P4 dry-run 已验证通过，但当前 live `agents.entries.main.tools.allow` 与 P4-1b 目标不同：dry-run 只提出一个更新，即完整替换 allow 数组以加入四个 task 工具。`apply_1b()` 保留 deny，却**整体替换 allow**；因此上线前必须将 live allow 与目标数组做人工差异确认，防止丢失 P4-1b apply 后人工新增的工具。

### O5 / O5b

- O5 worker 使用新的 `Brain(session_key=agent:main:task-<id>)`，不复用主线 session；外部调用仍经过同一个 gateway Guard。
- Guard 对 task session 的 pending 只向 Task IPC 传 approval id、摘要和过期时间，不传原始 toolName/params。当前等待态绑定正确，但审批恢复未接通（阻塞项 1）。
- O5b 仅将当前语音 turn 的 `/start` 成功回调登记为候选；`task_status/report/cancel`、历史 queued、没有 turn 的 IPC start 都不会自动执行。
- 调度发生在主线 `TurnRunner` 已有 spoken 句、无网关/TTS error、没有 barge-in 且该 turn 播放结束之后；这比“已入 TTS 队列”更强。失败/打断会记 `task.dispatch_skipped`，任务保留 queued。
- 完成/失败通知经过 `AnnouncementPolicy`；影音静默时应仅托盘/结构化事件，不 TTS。主人主动查进度不经过此门。

### P1 面板

- 面板只从 `/api/status` 轮询专用 Task DTO，没有任务创建、领取、取消、审批或 worker 控制路由，符合只读控制边界。
- DTO 仅含短 id、summary、状态、进度、结果/错误、审批摘要和时间；`goal`、`session_key`、审批内部 id、worker/工具原始字段均不进入浏览器响应。

## 部署资产与顺序（在三项阻塞修复后执行）

### 源与目标

1. Python 运行源：`F:\coding\guanjia\src\guanjia\`
   - `task_manager.py`、`task_service.py`、`task_ipc.py`、`task_executor.py`、`task_dispatch.py`、`voice.py`。
2. Guard 插件源：`F:\coding\guanjia\openclaw\plugins\guanjia-guard\`
   - `index.ts`、`src\tasks.ts`、`src\guard.ts`、`src\approval-ipc.ts` 及其依赖/测试。
   - 上线前以 `C:\Users\inksn\.openclaw\openclaw.json` 的插件 path 为准同步到**实际加载路径**，不能仅复制仓库副本后假定网关会加载。
3. Workspace 提示词源：`F:\coding\guanjia\openclaw\workspace\AGENTS.md`。
   - 同步到 `C:\Users\inksn\.openclaw\workspace\AGENTS.md`，保留当前线上文件备份。
4. P4 配置：`F:\coding\guanjia\openclaw\p4_config_patch.py` 修改
   `C:\Users\inksn\.openclaw\openclaw.json` 的 `agents.entries.main.tools.allow`。

### 执行步骤

1. 先完成三个阻塞项的修复，并运行任务、IPC、guard、voice 的针对性测试。
2. 备份实际 Guard 加载目录、线上 workspace `AGENTS.md`、`openclaw.json` 和语音 Python 文件；记录当前网关/语音 PID。
3. 同步 Python 运行源、实际 Guard 加载目录、workspace `AGENTS.md`；确认没有把 repo 中的 `openclaw\workspace` 误当线上 workspace。
4. 在网关仍运行时只执行：
   `F:\coding\guanjia\.venv\Scripts\python.exe F:\coding\guanjia\openclaw\p4_config_patch.py --step 1b --check`
   并人工确认 allow 的完整新旧列表。
5. 停网关，确认 18789 不监听；执行 `--step 1b --apply`。脚本会创建
   `C:\Users\inksn\.openclaw\openclaw.json.bak-p4-1b`，并做 schema validate。
6. 启动网关，确认 Guard service 已创建 `approval-ipc.json`；再重启语音，确认它创建 `task-ipc.json`。顺序必须是**网关先、语音后**，否则 voice 的 task tool 调用会 fail-closed，且语音启动时拿不到稳定的审批服务。
7. 做下列最小真机验收，全部通过才移除备份窗口。

## 回滚

1. 先停止语音：它会删除 `task-ipc.json` 并停止 worker pool；正在运行的 task 之后恢复为 `interrupted`，不得自动续跑。
2. 停网关：它会删除 `approval-ipc.json`。
3. 恢复 Guard 实际加载目录、Python 源和 workspace `AGENTS.md` 的上线前备份。
4. 仍保持网关停止，运行：
   `F:\coding\guanjia\.venv\Scripts\python.exe F:\coding\guanjia\openclaw\p4_config_patch.py --step 1b --rollback`
   仅回滚 P4-1b，保留 P4-1。
5. 先启动网关，再启动语音；确认两个 descriptor 生命周期恢复为旧版本预期。

## 最小真机验收

1. 网关启动后，`approval-ipc.json` 存在，端口仅 loopback；网关停止后文件消失。
2. 语音启动后，`task-ipc.json` 存在，端口仅 loopback；语音停止后文件消失。
3. 语音请求一个超过 5 秒的多步任务：`task_start` 创建新 id，主线首句完整播完后任务才从 queued → running，session 是 `agent:main:task-<id>`。
4. 让首句 TTS 失败或在播放期间 barge-in：刚创建的任务必须仍为 queued；预先存在的 queued 永不被领取。
5. 连续创建三个可执行任务：最多两个 running，第三个 queued；`task_status/report/cancel` 不产生新的 worker。
6. worker 触发 L2 工具：任务变 `waiting_approval`；批准仅允许原 session、原工具、原 canonical params 的一次调用；拒绝/过期绝不放行。**本项在阻塞项 1 修复前预期失败，不能签收。**
7. 取消 running 任务：状态立刻 cancelled，worker 不再发起后续步骤，也不写 completed；对已送入网关的单次请求只能如实报告“协作式取消，不能硬中断”。
8. 开启影音静默并让 worker 完成/失败：无 TTS，保留托盘通知和 `task.notice` / `program.quiet_suppressed`；主动问 `task_status` 仍正常答复。
9. 面板只读显示任务状态和进度，不出现领取、取消或审批控制按钮。

## 预期中断

- 网关重启：约 15–45 秒，取决于 OpenClaw 加载插件和模型连接；期间语音主线无法请求网关。
- 语音重启：约 10–30 秒，取决于 ASR/TTS 健康检查；期间没有监听，task IPC 不可用。
- 先后重启而非并行重启时，总窗口按 25–75 秒预留；任何正在运行的 O5 worker 在语音重启后都应显示为 `interrupted`，不是 completed。
