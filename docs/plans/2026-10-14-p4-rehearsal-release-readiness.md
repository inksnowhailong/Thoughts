# 2026-10-14 P4 休眠演练上线就绪审查

## 结论

代码可进入**水位验证阶段**，但还不能打开 `GUANJIA_SLEEP_REHEARSAL=1`。唯一部署前阻塞是：必须在实际加载 Guard 的插件目录核验 `main-watermark.ts` 已同步，并确认 `C:\Users\inksn\.openclaw\workspace\guanjia` 的 NTFS ACL 仅允许 `inksn` 与系统管理员读取。水位文件含时间、来源和序号，不含正文或工具参数，但仍不能向其他本机账户泄露活动时序。

## 核对结果

- Guard 在 `before_prompt_build` 中仅记录 `agent:main:main` 且 `trigger=user` 的消息；worker task session、cron/heartbeat、空消息和 `__guanjia_approved_retry__` 都不写水位。
- 水位位于 `C:\Users\inksn\.openclaw\workspace\guanjia\main-watermark.json`，通过 Guard Store 的临时文件 rename 原子写入；字段是 `version`、`last_activity_at`、`source`、`seq`、`session`。
- Python reader 对文件缺失/损坏、错误主线、非法来源/序号和未来时间均 fail-closed。
- scheduler 默认 `GUANJIA_SLEEP_REHEARSAL` 未设即 disabled。即使开关打开，水位不可用时仍为 blocked；不会调用 rehearsal。
- rehearsal 使用独立 `agent:main:sleep-rehearse-*` 键，只读 workspace snapshot，仅写 `F:\coding\guanjia\data\sleep\staging\<run>` 的 index staging、manifest、budget、dry-run 和 `rehearsal.json`。
- 代码未导入 committer、WorkspaceFiles 或 session reset；演练日志明确 `commit/sessions_reset/workspace_write = 禁止`。
- scheduler 对同一水位值去重；收到新活动会清除本空闲窗口的尝试标记。状态和事件会保留“实时主线消息水位复核尚未接入”的限制。
- voice status/panel 仅展示 `sleep_rehearsal` 状态、理由、门槛和最近结果，不提供开启、提交或 reset 控制。

## 部署顺序

1. 备份实际 Guard 插件加载目录、`F:\coding\guanjia\src\guanjia\sleep\` 和 `voice.py`；确认 OpenClaw 配置实际指向的插件目录，而不是只复制仓库副本。
2. 同步 Guard 的 `src\main-watermark.ts` 与 `index.ts` 到实际插件加载目录。
3. 同步 Python 的 `sleep\rehearsal.py`、`sleep\scheduler.py`、`sleep\watermark.py`、`sleep\__main__.py` 与 `voice.py`。
4. 检查 watermark 目录 ACL；不满足最小读取权限时停止。
5. 先重启 Guard/网关，再重启 voice。默认不开启 `GUANJIA_SLEEP_REHEARSAL`。

## 回滚

1. 停 voice，防止继续读取水位；它不会提交或 reset。
2. 停 Guard/网关。
3. 恢复上述 Guard 与 Python 文件备份。
4. 删除或保留 `main-watermark.json` 均安全：旧版 voice 不读取它；不要删除历史 staging，留作审计。
5. 先启动 Guard/网关，再启动 voice，确认环境变量仍未设置。

## 上线后验收

1. 保持 rehearsal 开关关闭，重启后发一条语音消息；确认 watermark 的 `seq` 递增、`source=voice`、不含正文。
2. 未来微信入口接入后，确认同一文件 `source=wechat` 且递增；task、cron/heartbeat 和审批内部 marker 均不得改变 seq。
3. 临时移走或损坏副本后，确认 voice status 显示 `blocked`，且没有 staging 目录新增。
4. 确认真实主线水位覆盖所有入口、文件 ACL 通过、以上三项连续观察至少一天后，设置 `GUANJIA_SLEEP_REHEARSAL=1` 并重启 voice。
5. 开关开启后的 3–5 天只审核 staging、budget、manifest 与 rehearsal 日志：不得出现 commit/reset/workspace 改动、重复空闲窗口、或水位 race。
6. P4-5 commit 和 P4-7 reset 仍须独立代码、独立开关和独立审查；本包不包含它们。

## 已运行的针对性检查

- `tests\test_sleep_rehearsal.py`
- `tests\test_sleep_scheduler.py`
- `tests\test_sleep_watermark.py`
- Guard `src\main-watermark.test.ts`

均通过；未运行 git、未重启或上线。
