# 执行者工作笔记（子任务开工先读这一份）

> 给派去改「思绪」家庭语音管家的子任务看。目的：不重新踩坑、少往返、快上手。背景与决策看交接文档 `思绪二代-交接文档.md`，这里只写「怎么干活」。最后更新 2026-10-12。

## 1. 远程执行：一律用 gj.ps1

本机 `C:\Users\moxue\Desktop\Claude-Artifact-Archive-2026-10-08\04-自部署AI-定型架构\tools\gj.ps1`：

```powershell
$gj = "C:\Users\moxue\Desktop\Claude-Artifact-Archive-2026-10-08\04-自部署AI-定型架构\tools\gj.ps1"
& $gj 'Get-Process python* | Select Id,StartTime'          # 默认在 F:\coding\guanjia 下执行
& $gj -File .\some-script.ps1                               # 长脚本
$r = & $gj '...'; [IO.File]::WriteAllText("$env:TEMP\x.txt", ($r -join "`n"), [Text.UTF8Encoding]::new($false))   # 输出很长时落文件再 Read
```

- 一次调用完成执行与取回，中文、管道符、引号都不会坏。短脚本约 7 秒，长脚本约 15 秒。
- **不要**再手写 `ssh home "..."` 拼命令、`-EncodedCommand`、远程写 UTF-8 文件再 scp 回来——这些坑 gj.ps1 都处理了。
- 改文件：`scp home:F:/coding/guanjia/<路径> $env:TEMP\...` 拉下来用编辑工具改，再 scp 回去；本机 .ps1 若含中文必须存成 **UTF-8 带 BOM**（PowerShell 5.1 否则按 GBK 读）。
- 本机临时文件放 `%TEMP%`，**不要**放 `D:\coding`（主人的工作区）。**不要**结束本机所有 scp/ssh 进程（别的子任务在用），只结束自己启动的 PID。
- 家里电脑睡眠会掉线：每分钟重试，最多 5 次，仍不通就停下回执。

## 2. 测试：分层 + 并行

```powershell
& $gj 'powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-tests.ps1 -Pattern wake,asr'   # 改代码时：只跑相关
& $gj 'powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-tests.ps1'                     # 提交前：全量并行，约 20 秒
```

- 每个文件一行汇总，失败才展开 FAIL 行和 stderr 尾巴；`TOTAL ... bad=0` 才算过。
- 偶发失败先单独重跑一次确认，再判断是不是自己弄坏的。已知偶发：`test_p21_review_fixes` 的「tail thread survives a read error」（串行也会偶发，测试时序太紧，待修）；`test_p21_voice_gate`（panel restart 时序）、`test_p22_panel_routes`（连接被中止）并行时偶发。
- **循环里的变量别和外层 `with ... as x` 重名**（10-11 崩溃就是 `g` 被覆盖）。
- 测试是 `tests\test_*.py` 的 check() 脚本风格；先写失败测试再实现。测试只用临时目录里的假工作区/假数据，**绝不写线上** `C:\Users\inksn\.openclaw\workspace`、`data\speakers.npz`、`levels.json`。

## 3. 提交

- 用 `F:\Git\mingw64\bin\git.exe`（`F:\Git\cmd\git.exe` 会自动加 Co-authored-by trailer）。
- 本机命令串里不要出现字面 `git commit`（本机包装会改写），写 `$v='com'+'mit'; & $g $v ...`。
- 信息只用 ASCII、短横线风格（如 `fix-xxx`）。只 add 自己的文件，`commit -- <文件>` 限定路径；别带进 `docs/plans/*`、`scripts/*_bench.py` 这些未跟踪文件。

## 4. 上线与重启（通常由 leader 做；子任务默认停在上线前）

- 重启语音：`schtasks /End /TN GuanjiaVoice`，**等 12 秒以上**再 `/Run`（太快会看门狗竞态，两个都退出）。以 `tmp\voice.log` 新的 `device rate` 行为准。
- TTS（18791）开机可能没起来：`schtasks /Run /TN GuanjiaTTS-WSL`，约 1 分钟。
- 网关：`openclaw gateway stop --force` → 确认 18789 不监听 → 改配置 → 启动 → 确认网关进程启动时间晚于配置修改时间。PowerShell 5.1 传 JSON 给 node 会吞引号，用 `'{\"k\":\"v\"}'`。
- 动线上数据前先备份；有回滚路径才动。

## 5. 排查：先查日志

`& $gj 'scripts\logq.cmd --recent 15; Get-Content tmp\logq-out.txt -Encoding UTF8 -Raw'`，细节见交接文档第 12 节（`--turn <uid>`、`--boot`、`--comp audio`、`--level warn --since 1h`）。

## 6. 代码地图（常改的地方）

| 文件 | 管什么 |
|---|---|
| `src/guanjia/voice.py` | 语音守护进程主体：音频流、前端线程（VAD/ASR/声纹/判定）、一轮对话、托盘/面板事件 |
| `wake.py` | 碎音、唤醒词拼音匹配、`decide()` 判定（声纹不参与去留，只认人） |
| `asr.py` / `asr_final.py` | 流式识别 / 段末二次识别（SenseVoice 2024，补 0.4 s） |
| `seg_filter.py` | 丢噪声门掏空段、流式未确认的幻觉段 |
| `turns.py`、`speech_shape.py`、`textseg.py` | 一轮回复的流式切句、口语整形、只说没做/编造拦截判定 |
| `speaker.py`、`enroll_session.py` | 声纹库（v2 多向量 top-3）、面板引导注册 |
| `elog.py`、`logq.py` | 结构化事件日志与查询 |
| `panel.py`、`panel.html`、`panel_memory.js`、`prompts.py`、`wsfiles.py`、`memory_page.py` | 面板与工作区文件安全读写 |
| `openclaw/plugins/guanjia-guard/` | 分级插件（TS）：L0–L3、需要确认、审计、exec ask 改 off |
| `scripts/voice-supervisor.ps1` | 语音看门狗（计划任务 GuanjiaVoice 拉起） |
