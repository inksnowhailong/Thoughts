# 家庭语音管家（二代「思绪」）统一事件日志与排查能力 · 实现计划

> 日期：2026-10-11 ｜ 作者：架构师 ｜ 执行：Sonnet 照做
> 项目：家里电脑 `F:\coding\guanjia`（Python，`src/guanjia/`）
> 前提：另一位执行者正在 `voice.py` / `turns.py` 里加「只说不做自动追问」（`_should_nudge` / `_send_nudge` / `turns.needs_nudge`），**本计划在其合入之后实施**；凡改到 `voice.py` 的地方都写函数名，不写行号。

---

## 0. 一句话结论

新增一条**统一事件流**（`tmp\logs\events-<进程>-<日期>.jsonl`，一事件一行，字段 `ts/comp/level/uid/ev/msg/data`），由一个极轻的异步写入器 `guanjia.elog` 产生；现有 `EventHub` 的 `stage/new_utterance/finish` 自动派生事件，因此**大部分环节零改动就有日志**。网关侧不自己解析流里没有的东西，而是**复用 OpenClaw 已经按 run_id 记好的 trajectory**（模型名、token、stopReason、每次工具调用及成败、发给模型的消息全都在里面），在一轮结束后摘要成一条 `gateway.run` 事件、并把大字段另存为 blob。查询统一走 `python -m guanjia.logq`：默认紧凑一行一事件、带行数/字数上限、可直接写 UTF-8 文件，`--turn <uid>` 一条命令看完一轮从听到念完 + 网关侧 + 自动诊断提示。

---

## 1. 现状核实（2026-10-11 13:40 实测）

| 位置 | 内容 | 问题 |
|---|---|---|
| `tmp\voice.log`（262KB，5MB×3 轮转） | logging 自由文本；每 10 秒一行 TTS `/health` | 噪声大；无 uid；只能 tail/grep；`enroll start/saved` 之间每段录音**完全没记** |
| `tmp\utterances.jsonl`（145KB/124 行，单行平均 1.2KB） | 每句一条：stages + timeline 两份重复 | 无模型、stopReason、token、工具成败；被拒的「嗯」也占 1KB |
| `tmp\voice-supervisor.log` | 看门狗纯文本 | 10:36→10:39 等了 184 秒只记了一行结论；12:16 `another supervisor is running` 不知道对方是谁、是否活着 |
| `tmp\voice-stderr.txt` | 每次拉起被覆盖 | 崩溃信息下次启动就没了 |
| `tmp\tts_wsl.log` | WSL IndexTTS stdout | 只有推理耗时，无启动时间点 |
| `~\.openclaw\workspace\guanjia\audit.jsonl` | 分级插件每次工具调用一行 | **没有 runId**，只能靠时间窗口对到一轮 |
| `openclaw-agent.sqlite` → `trajectory_runtime_events` | 每个 run_id 一组事件：`session.started`（provider/modelId/toolCount）、`context.compiled`（prompt、messages、tools）、`prompt.submitted`、`tool.call`（name/args/toolCallId）、`tool.result`（success、exitCode、durationMs、输出）、`model.completed`、`trace.artifacts`（finalStatus、**stopReason**、**usage**、assistantTexts、toolMetas、aborted/timedOut）、`session.ended` | 信息最全，但**滚动窗口 10MiB**，旧的会被挤掉；`runtrace.py` 只用了时间点 |
| `state\openclaw.sqlite` → `audit_events` | agent.run.started/finished、tool.action.* 带 run_id | 可做旁证，不必用 |
| `%TEMP%\openclaw\openclaw-YYYY-MM-DD.log`（JSON 行） | 网关日志：`[model-fetch] response … status=200 elapsedMs=…`、`run … ended with stopReason=stop`、警告/重试/降级 | 现成可读，`openclaw logs --json` 也能拿 |

**关键事实**：`brain.Brain.stream()` 已把流里第一个 chunk 的 `id`（`chatcmpl_…`）存到 `last_run_id`，它**就是** trajectory 的 `run_id`。语音 uid ↔ 网关 run 的关联现成可用，不需要动网关插件。

**用今天的「打开音响驱动」验证过**：run `chatcmpl_37052118…`（13:48）在 trajectory 里是 `prompt「让你干的活你干成怎么样了呀」→ 回复「…我先用只读方式找官方控制软件…让我再试一次」→ 零 tool.call → stopReason=stop`。也就是**模型嘴上答应、实际没发工具调用就自己结束了**，不是工具被拦。新设计里 `logq --turn` 会直接打出这条诊断。

OpenClaw 自带能力（已读 `docs/tools/trajectory.md`、`docs/cli/logs.md`、`docs/gateway/diagnostics.md`）：
- `openclaw sessions export-trajectory --session-key agent:main:main --json`：导出整个会话的脱敏 bundle（`events.jsonl` + `manifest.json`），**整会话级、体积大**，只做最后手段；
- `openclaw logs --json --limit N`：读网关日志；
- `openclaw gateway diagnostics export`：整机诊断 zip。
结论：**按轮查询自己读 trajectory 表（只读）最省**；这些 CLI 写进 AI 指南作为兜底。

---

## 2. 总体架构

```
 ┌─────────── 语音进程 guanjia.voice ───────────────────────────────────────────┐
 │  各模块 ──elog.emit()──┐                                                    │
 │  EventHub.stage/new_utterance/finish ──(自动派生)──┤                          │
 │  logging WARNING+ ──LogBridge──────────────────────┤                          │
 │                                                    ▼                          │
 │                                   EventLog（queue.put_nowait，微秒级）         │
 │                                     │ 后台线程 0.5s 批量写                     │
 │                                     ├──> tmp\logs\events-voice-2026-10-11.jsonl│
 │                                     ├──> tmp\logs\blobs\2026-10-11\<uid>-*.txt │
 │                                     └──> tap → 面板 SSE（B6）                   │
 │  轮次结束 → runtrace.run_summary(run_id)（只读网关 sqlite）→ gateway.run 事件    │
 └──────────────────────────────────────────────────────────────────────────────┘
 看门狗 voice-supervisor.ps1 ──Write-Event──> tmp\logs\events-supervisor-<日期>.jsonl
 （每个进程写自己的文件，避免跨进程并发追加同一文件）

 python -m guanjia.logq ──只读──> events-*.jsonl（合并按 ts 排序）
                         ├──> tmp\utterances.jsonl（旧数据兜底）
                         ├──> 网关 trajectory（--runs / --payload 兜底）
                         └──> %TEMP%\openclaw\openclaw-<日期>.log（--turn 附 WARN+ 与本轮 run 行）
```

兼容：`voice.log`、`utterances.jsonl`、面板现有记录**原样保留**（不删、不改格式）；新事件流是新增的。等新流稳定一周后再考虑给 `utterances.jsonl` 瘦身（不在本计划内）。

---

## 3. 统一事件格式

一行一个 JSON，UTF-8（无 BOM），`ensure_ascii=False`：

```json
{"ts": 1791697687.489, "comp": "brain", "level": "info", "ev": "brain.stream_end", "uid": "u134807-012", "msg": "回复完毕 87 字", "data": {"run_id": "chatcmpl_3705…", "ok": true, "chunks": 31, "finish_reason": "stop", "ms": 2032}}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `ts` | 是 | epoch 秒，3 位小数 |
| `comp` | 是 | `voice` `asr` `vad` `speaker` `enroll` `wake` `brain` `gateway` `tool` `nudge` `tts` `play` `panel` `health` `supervisor` `guard` `log` |
| `level` | 是 | `debug` `info` `warn` `error`；默认只落 `info` 及以上（环境变量 `GUANJIA_ELOG_LEVEL=debug` 打开 debug） |
| `ev` | 是 | `<领域>.<动作>`，如 `turn.end`、`enroll.seg`、`gateway.run` |
| `uid` | 否 | 一句话/一轮的编号，沿用 `EventHub.new_utterance` 生成的 `uHHMMSS-NNN` |
| `msg` | 否 | ≤200 字的人话 |
| `data` | 否 | 结构化字段；单个字符串 ≤400 字、列表 ≤30 项、嵌套 ≤4 层，超出截断并标 `…(+N)` |

**大字段**：发给模型的消息、模型原文、长工具输出 → `elog.blob(uid, name, obj)` 存成 `tmp\logs\blobs\<日期>\<uid>-<name>.txt`（≤256KB，超出截断），事件里只放 `data.blob = "2026-10-11/u134807-012-gwctx.txt"`，`logq --payload` 按需取。

**轮转/保留**：事件文件按天一个（每进程），单文件超 50MB 开 `.1/.2` 分片；写入器在每天第一次写时清理：事件文件 **14 天**、blob **7 天**、blob 总量超 **300MB** 删最旧。`voice.log` 维持 5MB×3 不变。

**隐私**：所有字符串过 `events.redact()`（Bearer、`sk-…`、`token/key/secret/password=…`）；请求头、网关令牌、面板令牌**永不**作为参数传进 emit；blob 写入前对整段文本再 redact 一次。blob 含对话正文和工具输出，仅存本机，7 天自动删。

### 3.1 事件清单

**必须记（info 及以上）**

| comp | ev | 时机 / 关键 data | 覆盖盲区 |
|---|---|---|---|
| voice | `voice.start` | `VoiceDaemon.run()` 开头：pid、argv、in/out 设备、采样率、gate_mode、aec、关键设置 | 启动 |
| voice | `voice.ready` | 音频流打开、进入监听：`boot_ms` | 启动耗时 |
| voice | `voice.exit` | `request_exit(code)`：code、reason | 重启/退出 |
| health | `dep.change` | `_on_health` 里 gateway/tts 状态**变化时**（不记每次轮询） | TTS 没起来 |
| voice | `utt.new` | `EventHub.new_utterance` 派生：source、text、score | — |
| vad/asr/speaker/wake/brain/tool/tts/play | `<stage>.<state>` | `EventHub.stage(uid≠None)` 派生；`blocked` 且属于 gateway/tools/tts/play 时 level=warn | 全链路时间线 |
| voice | `turn.end` | `EventHub.finish` 派生的**一轮摘要**：source、verdict、reason、text≤60、reply≤160、spoken/unspoken 数、tools（名+决定）、timings、error、run_id | `--recent` 的数据源 |
| brain | `brain.run` | 收到首个 chunk：run_id、首字 ms | uid↔run 关联 |
| brain | `brain.stream_end` | ok、chunks、reply 字数、finish_reason、usage（流里有才记）、model（流里有才记）、error | stop reason |
| gateway | `gateway.run` | 轮次结束后读 trajectory：provider、model、stop、final_status、usage(input/output/cacheRead)、tools[{name, ok, ms, exit, args≤160, err≤200}]、aborted/timed_out/prompt_error、texts 数、phases（prep/model 首字 ms）、`blob`（发给模型的上下文摘要） | 模型/工具尝试/token/发给模型的消息 |
| tool | `tool.audit` | `_on_tool`（分级插件审计）：tool、level、decision、summary；有进行中的轮就带 uid | 审批/拦截 |
| nudge | `nudge.check` / `nudge.sent` | `_should_nudge` 判定结果（has_promise、tools 数、决定、原因）；`_send_nudge` 的 parent/child uid | 只说不做 |
| enroll | `enroll.start/seg/state/save/abort` | **每一段**：ok、reason、group、sec、effective_sec、zero_ratio、peak、self_score、playing/overlap | 声纹录不上 |
| panel | `panel.settings` / `panel.control` / `user_flag` | 改了哪些键；mute/restart/quit；主人标记 | 主人端 |
| supervisor | `sup.start` `deps.wait` `deps.ready` `deps.timeout` `child.start` `child.exit` `mutex_busy` `parent_gone` | 见 B5 | 启动/看门狗竞态 |
| log | `log` | `LogBridge`：任何 WARNING/ERROR 日志行，含异常堆栈尾 400 字 | 未预料的异常 |

**只放 debug（默认不落盘）**：`uid=None` 的实时环节（人声检测中、识别中）、barge-mon 采样、健康轮询成功、每个流增量。

---

## 4. 批次总览

| 批 | 内容 | 工作量 | 动线上 | 动网关插件 |
|---|---|---|---|---|
| B1 | 事件写入器 `elog.py` + EventHub 自动派生 + voice 启停/工具/追问事件 | 0.5 天 | 需重启语音；只新增 `tmp\logs\` | 否 |
| B2 | `logq` 查询工具 + `scripts\logq.cmd` + AI 使用指南写进 HANDOFF | 0.5–1 天 | 否（只读） | 否 |
| B3 | 网关可见性：`runtrace.run_summary`、`gateway.run` 事件 + blob、brain 流元信息、`logq --turn` 合并网关段与诊断、`--runs`、`--payload` | 1 天 | 需重启语音；**只读**网关 sqlite | 否 |
| B4 | 声纹注册逐段日志 + 依赖状态变化 | 0.5 天 | 需重启语音 | 否 |
| B5 | 看门狗 JSONL 事件、stderr 尾巴、互斥量持有者信息、依赖等待进度 | 0.5 天 | 需重启计划任务 | 否 |
| B6 | 面板「这句有问题」+ `logq --flags`；（可选）面板实时日志切新事件源并按模块/级别筛 | 0.5–1 天 | 需重启语音 | 否 |
| B7（可选） | 分级插件审计行补 `runId`/`toolCallId` | 0.2 天 | 需重启网关 | **是** |

依赖：B2 依赖 B1 的格式（可并行开发，用夹具测）；B3 依赖 B1/B2；B4/B5/B6 只依赖 B1。

---

## 5. B1 · 事件写入器与自动派生

### 5.1 改动文件
- 新增 `src/guanjia/elog.py`（完整代码见下）
- `src/guanjia/events.py`：`EventHub.__init__` 加 `elog=None` 参数；`new_utterance` / `stage` / `finish` 里派生事件（**events.py 不 import elog**，避免循环导入）
- `src/guanjia/voice.py`：`main()`、`VoiceDaemon.__post_init__`、`run()`、`request_exit()`、`_on_tool()`、`_should_nudge()`、`_send_nudge()`
- 新增 `tests/test_elog.py`

### 5.2 `src/guanjia/elog.py`（完整代码）

```python
"""统一事件日志：各模块一句 emit()，后台线程批量追加到 tmp\\logs\\events-<进程>-<日期>.jsonl。

    任意线程 ──emit()──> 内存队列（put_nowait，满了丢弃计数，绝不阻塞语音）
                              │ 后台线程每 0.5 秒批量写
                              ├──> events-voice-2026-10-11.jsonl（按天一个，超 50MB 分片 .1 .2）
                              └──> tap 回调（面板 SSE 用）
    大字段（发给模型的消息、模型原文）用 blob() 单独存，事件里只放相对路径。

一行一个事件：{"ts", "comp", "level", "ev", "uid"?, "msg"?, "data"?}。查询见 guanjia.logq。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import shutil
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parents[2] / "tmp" / "logs"
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
MAX_MSG = 200  # msg 上限
MAX_STR = 400  # data 里单个字符串上限
MAX_ITEMS = 30  # data 里列表最多几项
PART_MAX_BYTES = 50_000_000  # 单个事件文件超过就开新分片
KEEP_DAYS = 14  # 事件文件保留天数
BLOB_KEEP_DAYS = 7  # blob 保留天数（含对话正文，短一些）
BLOB_MAX_TOTAL = 300_000_000  # blob 目录总量上限
BLOB_MAX_BYTES = 256_000  # 单个 blob 上限


def _redact(s: str) -> str:
    from guanjia.events import redact  # 延迟导入：events 不反向依赖本模块
    return redact(s)


def clip(v, n: int = MAX_STR, depth: int = 0):
    """把任意值收成可 JSON 化、长度有界、已脱敏的样子。"""
    if isinstance(v, str):
        v = _redact(v)
        return v if len(v) <= n else f"{v[:n]}…(+{len(v) - n})"
    if v is None or isinstance(v, (bool, int)):
        return v
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, dict):
        if depth >= 4:
            return "{…}"
        return {str(k): clip(x, n, depth + 1) for k, x in list(v.items())[:40] if x is not None}
    if isinstance(v, (list, tuple, set)):
        if depth >= 4:
            return "[…]"
        items = list(v)
        out = [clip(x, n, depth + 1) for x in items[:MAX_ITEMS]]
        if len(items) > MAX_ITEMS:
            out.append(f"…(+{len(items) - MAX_ITEMS})")
        return out
    return clip(str(v), n, depth)


class EventLog:
    """事件写入器。emit 线程安全、不抛异常、不阻塞；sync=True 时直接落盘（单测用）。"""

    def __init__(self, proc: str = "voice", log_dir: Path = LOG_DIR, *, min_level: str | None = None,
                 clock: Callable[[], float] = time.time, sync: bool = False, maxsize: int = 10000) -> None:
        self.proc = proc
        self.dir = Path(log_dir)
        self.min_level = LEVELS.get(min_level or os.environ.get("GUANJIA_ELOG_LEVEL", "info"), 20)
        self.clock = clock
        self.sync = sync
        self.dropped = 0  # 队列满 / 写失败丢掉的条数
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._taps: list[Callable[[dict], None]] = []
        self._lock = threading.Lock()
        self._fh = None
        self._day: str | None = None
        self._part = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if not sync:
            self._thread = threading.Thread(target=self._run, name="elog", daemon=True)
            self._thread.start()
            atexit.register(self.close)

    # ---------- 写入 ----------
    def emit(self, comp: str, ev: str, msg: str = "", *, level: str = "info", uid: str | None = None,
             **data) -> dict | None:
        """记一个事件；低于最低级别返回 None。data 里值为 None 的键不记。"""
        try:
            if LEVELS.get(level, 20) < self.min_level:
                return None
            rec: dict = {"ts": round(self.clock(), 3), "comp": comp, "level": level, "ev": ev}
            if uid:
                rec["uid"] = uid
            if msg:
                rec["msg"] = clip(msg, MAX_MSG)
            data = {k: v for k, v in data.items() if v is not None}
            if data:
                rec["data"] = clip(data)
        except Exception:  # noqa: BLE001  日志自己出错不能影响调用方
            self.dropped += 1
            return None
        for tap in list(self._taps):
            try:
                tap(rec)
            except Exception:  # noqa: BLE001
                pass
        if self.sync:
            self._write([rec])
        else:
            try:
                self._q.put_nowait(rec)
            except queue.Full:
                self.dropped += 1
        return rec

    def blob(self, uid: str | None, name: str, obj) -> str | None:
        """大字段单独存一份，返回相对 blobs 目录的路径（写进事件 data.blob）；失败返回 None。只在非音频线程调用。"""
        day = datetime.fromtimestamp(self.clock()).strftime("%Y-%m-%d")
        rel = f"{day}/{uid or 'nouid'}-{name}.txt"
        try:
            text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1, default=str)
            text = _redact(text)
            if len(text) > BLOB_MAX_BYTES // 3:  # 中文按 3 字节估
                text = text[:BLOB_MAX_BYTES // 3] + f"\n…(截断，原长 {len(text)} 字)"
            p = self.dir / "blobs" / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            return rel
        except Exception:  # noqa: BLE001
            return None

    def add_tap(self, fn: Callable[[dict], None]) -> None:
        self._taps.append(fn)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    # ---------- 后台 ----------
    def _run(self) -> None:
        while not (self._stop.is_set() and self._q.empty()):
            try:
                first = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            batch = [first]
            while len(batch) < 500:
                try:
                    batch.append(self._q.get_nowait())
                except queue.Empty:
                    break
            self._write(batch)

    def _write(self, batch: list[dict]) -> None:
        with self._lock:
            try:
                for rec in batch:
                    self._file_for(rec["ts"]).write(json.dumps(rec, ensure_ascii=False) + "\n")
                if self._fh is not None:
                    self._fh.flush()
            except Exception:  # noqa: BLE001  磁盘满/被占用：丢掉这批，下次再试
                self.dropped += len(batch)
                self._fh = None

    def _file_for(self, ts: float):
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        if self._fh is not None and day == self._day and self._fh.tell() <= PART_MAX_BYTES:
            return self._fh
        if self._fh is not None:
            self._fh.close()
        self.dir.mkdir(parents=True, exist_ok=True)
        if day != self._day:
            self._day, self._part = day, 0
            self._prune(ts)
        else:
            self._part += 1
        suffix = f".{self._part}" if self._part else ""
        self._fh = open(self.dir / f"events-{self.proc}-{day}{suffix}.jsonl", "a", encoding="utf-8")  # noqa: SIM115
        return self._fh

    def _prune(self, ts: float) -> None:
        """每天第一次写时清理过期文件；任何错误都忽略。"""
        try:
            cut = (datetime.fromtimestamp(ts) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
            for f in self.dir.glob("events-*.jsonl"):
                day = f.name.split(".")[0][-10:]
                if day < cut:
                    f.unlink(missing_ok=True)
            bdir = self.dir / "blobs"
            if not bdir.is_dir():
                return
            bcut = (datetime.fromtimestamp(ts) - timedelta(days=BLOB_KEEP_DAYS)).strftime("%Y-%m-%d")
            for d in bdir.iterdir():
                if d.is_dir() and d.name < bcut:
                    shutil.rmtree(d, ignore_errors=True)
            files = sorted((f for f in bdir.rglob("*.txt")), key=lambda f: f.stat().st_mtime)
            total = sum(f.stat().st_size for f in files)
            for f in files:
                if total <= BLOB_MAX_TOTAL:
                    break
                total -= f.stat().st_size
                f.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass


class NullLog:
    """没初始化时的空实现（单测里半构造的守护进程、其他脚本）。"""

    dropped = 0

    def emit(self, *a, **k) -> None:
        return None

    def blob(self, *a, **k) -> None:
        return None

    def add_tap(self, fn) -> None:
        pass

    def close(self) -> None:
        pass


class LogBridge(logging.Handler):
    """把 WARNING 及以上的普通日志行（含异常堆栈尾）也写进事件流，comp 统一为 log、data.module 用面板归类。"""

    def __init__(self, log: EventLog | NullLog) -> None:
        super().__init__(level=logging.WARNING)
        self.log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            from guanjia.events import classify_log
            msg = record.getMessage()
            exc = logging.Formatter().formatException(record.exc_info)[-390:] if record.exc_info else None
            self.log.emit("log", "log", msg, level="error" if record.levelno >= logging.ERROR else "warn",
                          module=classify_log(record.name, msg), logger=record.name, exc=exc)
        except Exception:  # noqa: BLE001
            self.handleError(record)


_default: EventLog | NullLog = NullLog()


def init(proc: str, **kw) -> EventLog:
    """进程启动时调一次；之后各模块用 elog.emit(...)。"""
    global _default
    _default = EventLog(proc, **kw)
    return _default


def get() -> EventLog | NullLog:
    return _default


def emit(comp: str, ev: str, msg: str = "", **kw) -> dict | None:
    return _default.emit(comp, ev, msg, **kw)


def blob(uid: str | None, name: str, obj) -> str | None:
    return _default.blob(uid, name, obj)
```

### 5.3 `events.py`：EventHub 自动派生

在 `EventHub` 里加（示意，按现有风格写注释）：

```python
STAGE_COMP = {"mic": "voice", "vad": "vad", "asr": "asr", "speaker": "speaker", "wake": "wake",
              "gateway": "brain", "tools": "tool", "tts": "tts", "play": "play", "barge": "play"}
WARN_BLOCKED = ("gateway", "tools", "tts", "play")  # 这几环 blocked 是异常；声纹/唤醒拦下是正常的

# __init__ 新增参数 elog=None：self.elog = elog

def _e(self, comp: str, ev: str, msg: str = "", **kw) -> None:
    if self.elog is not None:
        self.elog.emit(comp, ev, msg, **kw)
```

- `new_utterance()` 末尾：`self._e("voice", "utt.new", (fields.get("text") or "")[:60], uid=uid, source=source, score=fields.get("score"), in_window=fields.get("in_window"))`
- `stage()` 末尾：`uid is None` → level `debug`；否则 `warn if state == "blocked" and stage in WARN_BLOCKED else info`；`self._e(STAGE_COMP.get(stage, stage), f"{stage}.{state}", item["note"], level=…, uid=uid, **data)`
- `finish()` 末尾（`_persist` 之后）：用 `rec` 组摘要，**不带 stages/timeline**：

```python
def _turn_summary(rec: dict) -> dict:
    return {"source": rec.get("source"), "verdict": rec.get("verdict"), "reason": rec.get("reason"),
            "text": (rec.get("text") or "")[:60], "reply": (rec.get("reply") or "")[:160],
            "spoken": len(rec.get("spoken") or []), "unspoken": len(rec.get("unspoken") or []),
            "tools": [f"{t.get('tool')}:{t.get('decision')}" for t in rec.get("tools") or []],
            "timings": rec.get("timings") or None, "error": rec.get("error") or None, "run_id": rec.get("run_id")}
```
level：有 error → `error`；有 unspoken → `warn`；否则 `info`。msg：`f"{verdict} 「{text[:24]}」→「{reply[:30]}」"`。

> 注意：`run_id` 由 `_fill_gateway_phases` 异步 `hub.update` 进记录，通常晚于 `finish`，所以 turn.end 里多半没有 run_id——这没关系，关联靠 B3 的 `brain.run` / `gateway.run` 事件（都带 uid）。

### 5.4 `voice.py` 接线
- `main()`：`basicConfig` 之前 `log_ev = elog.init("voice")`，`handlers.append(elog.LogBridge(log_ev))`。
- `VoiceDaemon.__post_init__`：`self.hub = EventHub(records_file=RECORDS_FILE, elog=elog.get())`；`self._last_health: dict = {}`。
- `run()` 开头：`elog.emit("voice", "voice.start", f"pid {os.getpid()}", pid=…, argv=sys.argv[1:], in_dev=…, out_dev=…, gate_mode=…, aec=…, settings={只挑 spk_threshold/wake_required/min_chars/follow_up_sec/earcon})`；音频流打开、进入主循环前：`voice.ready`，`boot_ms`（从进程启动 `time.monotonic()` 起算，`__post_init__` 记一个 `self._boot_t`）。
- `request_exit(code)`：`elog.emit("voice", "voice.exit", level="warn" if code not in (0, EXIT_RESTART) else "info", code=code)`。
- `_on_tool(entry)`：最前面 `elog.emit("tool", "tool.audit", f"{entry['tool']} {entry['level']} {entry['decision']}", uid=self._turn_uid, level="warn" if entry["state"] == "blocked" else "info", **entry)`。
- `_should_nudge(res)`：每个 `return` 分支前记 `nudge.check`（`decision` true/false、`why`：aborted/error/muted/queued/no_promise/has_tools/nudge、`tools=self._turn_tools`、`uid=self._turn_uid`）。只在 `has_promise(res.reply)` 为真或最终 True 时记，避免每轮都记。
- `_send_nudge()`：签名改为 `_send_nudge(self, parent_uid)`（`_handle_item` 里传 `uid`），新 uid 生成后记 `nudge.sent`，`data={"parent": parent_uid, "child": uid}`。**若执行者的追问改动尚未合入，这两处跳过，其余照做。**

### 5.5 先写的失败测试 `tests/test_elog.py`（check() 风格，要点）

```python
"""统一事件日志：格式、级别、脱敏、截断、按天分文件、清理、blob、异步不丢、EventHub 派生。
运行：cd /d F:\\coding\\guanjia && .venv\\Scripts\\python.exe tests\\test_elog.py
"""
import json, tempfile, time
from datetime import datetime, timedelta
from pathlib import Path
from guanjia import elog
from guanjia.events import EventHub

def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond: raise SystemExit(1)

def lines(d):
    return [json.loads(l) for f in sorted(Path(d).glob("events-*.jsonl")) for l in f.read_text(encoding="utf-8").splitlines()]

with tempfile.TemporaryDirectory() as d:
    now = [datetime(2026, 10, 11, 13, 0).timestamp()]
    log = elog.EventLog("t", Path(d), sync=True, clock=lambda: now[0])
    log.emit("brain", "brain.run", "首字", uid="u1", run_id="chatcmpl_x", none_field=None)
    log.emit("asr", "asr.partial", level="debug")
    log.emit("tool", "tool.audit", "Authorization: Bearer abcdefghijklmn sk-ABCDEFGH12345", cmd="x" * 1000)
    got = lines(d)
    check(len(got) == 2, "debug 默认不落盘")
    check(set(got[0]) == {"ts", "comp", "level", "ev", "uid", "msg", "data"} and "none_field" not in got[0]["data"], "字段齐全、None 不记")
    check("abcdefghijklmn" not in json.dumps(got[1], ensure_ascii=False) and "sk-***" in got[1]["msg"], "脱敏")
    check(len(got[1]["data"]["cmd"]) < 420 and "…(+" in got[1]["data"]["cmd"], "长字符串截断")
    now[0] += 86400
    log.emit("voice", "voice.start")
    check(len(list(Path(d).glob("events-t-*.jsonl"))) == 2, "跨天换文件")
    old = Path(d) / "events-t-2026-09-01.jsonl"; old.write_text("{}\n", encoding="utf-8")
    now[0] += 86400; log.emit("voice", "x")
    check(not old.exists(), "超过 14 天的文件被清理")
    rel = log.blob("u1", "gwctx", {"messages": ["你好" * 10]})
    check(rel and (Path(d) / "blobs" / rel).read_text(encoding="utf-8").count("你好") == 10, "blob 落盘")
    log.close()

with tempfile.TemporaryDirectory() as d:
    log = elog.EventLog("a", Path(d))
    for i in range(2000): log.emit("play", "play.tick", n=i)
    log.close()
    check(len(lines(d)) == 2000 and log.dropped == 0, "异步写 2000 条不丢")

bad = elog.EventLog("b", Path(__file__), sync=True)   # 目录其实是个文件：写不进
check(bad.emit("voice", "x") is not None and bad.dropped == 1, "写失败不抛异常，只计数")

t0 = time.perf_counter()
fast = elog.EventLog("f", Path(tempfile.mkdtemp()))
for _ in range(10000): fast.emit("tts", "tts.done", "句子", uid="u1", ms=123)
check((time.perf_counter() - t0) / 10000 < 50e-6, "emit 平均 < 50 微秒")
fast.close()

with tempfile.TemporaryDirectory() as d:
    log = elog.EventLog("h", Path(d), sync=True)
    hub = EventHub(elog=log)
    uid = hub.new_utterance("mic", text="打开音响")
    hub.stage(uid, "tts", "blocked", "TTS：超时")
    hub.stage(None, "vad", "active")
    hub.finish(uid, verdict="accept", reply="我先查一下", spoken=["我先查一下"], unspoken=["x"])
    evs = [e["ev"] for e in lines(d)]
    check(evs == ["utt.new", "tts.blocked", "turn.end"], f"派生事件顺序（uid=None 的实时状态不落盘）：{evs}")
    end = lines(d)[-1]
    check(end["level"] == "warn" and end["data"]["unspoken"] == 1 and "timeline" not in end["data"], "turn.end 是摘要不是全量")
```

### 5.6 上线 / 回滚 / 风险
- 上线：scp `elog.py events.py voice.py tests/test_elog.py` → 远程跑 `test_elog.py` 和现有全部 `tests\test_p2*.py test_p21*.py test_p22*.py test_voice_nudge.py` → 托盘「重启语音」（或控制口 `/api/control restart`，退出码 3 由看门狗立即拉起）→ `dir F:\coding\guanjia\tmp\logs` 看到 `events-voice-<今天>.jsonl`，说一句话后有 `utt.new … turn.end`。
- 回滚：`git checkout -- src/guanjia/events.py src/guanjia/voice.py` + 删 `elog.py`，重启语音；`tmp\logs\` 可整个删。
- 风险：只新增 `tmp\logs\` 下文件，不写线上数据、不动网关。唯一性能点是 emit 在音频/前端线程被调用——队列 put_nowait + 一次浅 clip，测试里卡了 50 微秒上限。`stage()` 每轮约 10 次，可忽略。

---

## 6. B2 · `logq` 查询工具（重点）

### 6.1 改动文件
- 新增 `src/guanjia/logq.py`（核心完整代码见下；B3 再加网关段）
- 新增 `scripts/logq.cmd`
- 新增 `tests/test_logq.py`
- `docs/plans/HANDOFF.md` 加「排查指南」一节（§6.6 原文）

### 6.2 命令行

```
python -m guanjia.logq                         # 等价 --recent 10
  --recent N [--all]          最近 N 轮一行摘要（默认只列 accept/wake_only/say/nudge；--all 含被拒/忽略）
  --turn UID                  这一轮完整时间线（+相对毫秒）+ 网关段 + 诊断提示（B3）
  --flags                     主人标记过的句子（B6）
  --runs N                    网关最近 N 个 run（含微信/TUI，不限语音）（B3）
  --payload UID|RUN_ID        发给模型的上下文摘要（blob 优先，没有就现查 trajectory）（B3）
  --boot                      启动/依赖/看门狗/退出相关事件（默认 --since 1d）
  --tail voice|stderr|tts|sup 旧文本日志尾部（配合 -k 过滤，绕开 tail/grep/管道）
  通用筛选：--since 10m|2h|1d|"13:40"|"2026-10-11 13:40"  --until …  --uid U  --comp a,b
            --level info|warn|error（最低级别）  --ev 'enroll.*,tool.*'  -k 关键字  --kb64 <base64关键字>
  输出：--detail（展开 data）  --json（原始 JSON 行）  --max-lines 80  --max-chars 8000  --out 文件（UTF-8）
  测试/迁移：--log-dir  --legacy-file  --agent-db  --gw-log-dir
```

默认：一事件一行、`HH:MM:SS.s comp L ev [uid] msg | k=v …`，单行 ≤160 字；超出行数时**保留最新的**，顶部提示省略了几条；任何模式总输出 ≤ `--max-chars`（默认 8000 字 ≈ 3k token）。

### 6.3 `src/guanjia/logq.py`（核心完整代码；B3 的网关函数在 §7.4 补进同一文件）

```python
"""日志查询：python -m guanjia.logq [选项]。默认紧凑（一事件一行、截断长字段、总量有上限），--detail 才展开。

数据源（全部只读）：
    tmp\\logs\\events-*.jsonl        新事件流（各进程一份，合并后按 ts 排序）
    tmp\\utterances.jsonl            旧记录（--turn 找不到新事件时兜底）
    网关 trajectory / 网关日志       见 B3（--turn 的网关段、--runs、--payload）
远程用法：scripts\\logq.cmd 会加 --out tmp\\logq-out.txt，再 scp 回来读，绕开 ssh 的 GBK 乱码。
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOG_DIR = ROOT / "tmp" / "logs"
LEGACY_RECORDS = ROOT / "tmp" / "utterances.jsonl"
TEXT_LOGS = {"voice": ROOT / "tmp" / "voice.log", "stderr": ROOT / "tmp" / "voice-stderr.txt",
             "tts": ROOT / "tmp" / "tts_wsl.log", "sup": ROOT / "tmp" / "voice-supervisor.log"}
LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
LV1 = {"debug": "D", "info": "I", "warn": "W", "error": "E"}
TURN_VERDICTS = ("accept", "wake_only", "say")
BOOT_EVS = ("voice.*", "sup.*", "deps.*", "child.*", "mutex_busy", "parent_gone", "dep.change")
LINE_WIDTH = 160


# ---------- 读 ----------
def parse_time(s: str, now: float | None = None) -> float:
    """10m / 2h / 1d / 13:40 / 2026-10-11 13:40 → epoch 秒。"""
    now = time.time() if now is None else now
    s = s.strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", s)
    if m:
        return now - float(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt.startswith("%H"):
            today = datetime.fromtimestamp(now)
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return dt.timestamp()
    raise argparse.ArgumentTypeError(f"看不懂的时间：{s}")


def event_files(t0: float, t1: float, log_dir: Path) -> list[Path]:
    out: list[Path] = []
    d, end = datetime.fromtimestamp(t0).date(), datetime.fromtimestamp(t1).date()
    while d <= end:
        out += sorted(log_dir.glob(f"events-*-{d:%Y-%m-%d}*.jsonl"))
        d += timedelta(days=1)
    return out


def load_events(t0: float, t1: float, log_dir: Path, needle: str | None = None) -> list[dict]:
    """读时间段内的事件；needle 是行级预筛（如 uid），先字符串匹配再解析，省时间。"""
    evs: list[dict] = []
    for f in event_files(t0, t1, log_dir):
        try:
            with f.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if needle and needle not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict) and t0 <= e.get("ts", 0) <= t1:
                        evs.append(e)
        except OSError:
            continue
    evs.sort(key=lambda e: e.get("ts", 0))
    return evs


# ---------- 格式 ----------
def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S.") + str(int(ts * 10) % 10)


def short(v, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    s = s.replace("\r", "").replace("\n", "⏎")
    return s if len(s) <= n else s[: n - 1] + "…"


def fmt_event(e: dict, *, detail: bool = False, rel: float | None = None) -> str:
    when = f"+{round((e['ts'] - rel) * 1000):>6}ms" if rel is not None else fmt_ts(e.get("ts", 0))
    head = f"{when} {e.get('comp', '?'):<10} {LV1.get(e.get('level'), '?')} {e.get('ev', '')}"
    if e.get("uid") and rel is None:
        head += f" [{e['uid']}]"
    if e.get("msg"):
        head += " " + short(e["msg"], 80)
    data = e.get("data")
    if not data:
        return head if detail else short(head, LINE_WIDTH)
    if detail:
        return head + "\n    " + json.dumps(data, ensure_ascii=False, indent=1).replace("\n", "\n    ")
    return short(head + " | " + " ".join(f"{k}={short(v, 40)}" for k, v in data.items()), LINE_WIDTH)


class Out:
    """收集输出行，守住行数/字数上限；写文件一律 UTF-8。"""

    def __init__(self, max_lines: int, max_chars: int) -> None:
        self.max_lines, self.max_chars = max_lines, max_chars
        self.lines: list[str] = []
        self.chars = 0
        self.cut = 0

    def add(self, s: str) -> None:
        if len(self.lines) >= self.max_lines or self.chars + len(s) + 1 > self.max_chars:
            self.cut += 1
            return
        self.lines.append(s)
        self.chars += len(s) + 1

    def text(self) -> str:
        tail = [f"…还有 {self.cut} 行没显示（加筛选、或调大 --max-lines/--max-chars）"] if self.cut else []
        return "\n".join(self.lines + tail) + "\n"

    def emit(self, path: str | None) -> None:
        text = self.text()
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text, encoding="utf-8")
            print(f"written {path} lines={len(self.lines)} cut={self.cut}")  # 纯 ASCII，ssh 下不乱码
        else:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stdout.write(text)


# ---------- 筛选 ----------
def matches(e: dict, a: argparse.Namespace) -> bool:
    if a.uid and e.get("uid") != a.uid:
        return False
    if a.comp and e.get("comp") not in a.comp:
        return False
    if LEVELS.get(e.get("level"), 20) < LEVELS[a.level]:
        return False
    if a.ev and not any(fnmatch.fnmatch(e.get("ev", ""), p) for p in a.ev):
        return False
    if a.keyword and a.keyword.lower() not in json.dumps(e, ensure_ascii=False).lower():
        return False
    return True


def list_events(a: argparse.Namespace, out: Out) -> None:
    evs = [e for e in load_events(a.since, a.until, a.log_dir, a.uid) if matches(e, a)]
    if len(evs) > a.max_lines:
        out.add(f"…前面省略 {len(evs) - a.max_lines + 1} 条（只显示最新的）")
        evs = evs[-(a.max_lines - 1):]
    for e in evs:
        out.add(json.dumps(e, ensure_ascii=False) if a.json else fmt_event(e, detail=a.detail))


# ---------- 最近几轮 ----------
def turn_line(end: dict, gw: dict | None, flag: dict | None) -> str:
    d = end.get("data") or {}
    t = d.get("timings") or {}
    g = (gw or {}).get("data") or {}
    bits = [fmt_ts(end["ts"]), f"[{end.get('uid')}]", f"{d.get('source', '')}/{d.get('verdict', '')}",
            f"「{short(d.get('text', ''), 24)}」→「{short(d.get('reply', ''), 34)}」"]
    if gw:
        tools = g.get("tools") or []
        bad = sum(1 for x in tools if x.get("ok") is False)
        bits.append(f"model={short(str(g.get('model', '')).split('/')[-1], 18)} stop={g.get('stop')} tools={len(tools)}"
                    + (f"(失败{bad})" if bad else ""))
    elif d.get("tools"):
        bits.append(f"audit={len(d['tools'])}")
    if t.get("first_sound_ms") is not None:
        bits.append(f"首音={t['first_sound_ms']}ms")
    if d.get("unspoken"):
        bits.append(f"漏念={d['unspoken']}")
    if d.get("error"):
        bits.append("ERR=" + short(d["error"], 30))
    if flag:
        bits.append("⚑" + short(flag.get("msg", ""), 20))
    return " ".join(bits)


def recent(a: argparse.Namespace, out: Out) -> None:
    evs = load_events(a.since, a.until, a.log_dir)
    ends = [e for e in evs if e.get("ev") == "turn.end"
            and (a.all or (e.get("data") or {}).get("verdict") in TURN_VERDICTS or (e.get("data") or {}).get("source") == "nudge")]
    by_uid = lambda ev: {e.get("uid"): e for e in evs if e.get("ev") == ev and e.get("uid")}  # noqa: E731
    gws, flags = by_uid("gateway.run"), by_uid("user_flag")
    for e in ends[-a.recent:]:
        out.add(turn_line(e, gws.get(e.get("uid")), flags.get(e.get("uid"))))
    if not ends:
        out.add("（这段时间没有轮次；试 --since 1d 或 --all）")


def flags(a: argparse.Namespace, out: Out) -> None:
    for e in load_events(a.since, a.until, a.log_dir, "user_flag"):
        if e.get("ev") == "user_flag":
            d = e.get("data") or {}
            out.add(f"{fmt_ts(e['ts'])} [{e.get('uid')}] ⚑{short(e.get('msg', ''), 40)} 「{short(d.get('text', ''), 24)}」→「{short(d.get('reply', ''), 30)}」")


# ---------- 一轮 ----------
def find_uid(uid: str, a: argparse.Namespace) -> list[dict]:
    """uid 只有时分秒：从今天往前找 a.days 天，取最近有命中的那一天。"""
    now = time.time()
    for back in range(a.days):
        day0 = datetime.fromtimestamp(now - back * 86400).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        evs = [e for e in load_events(day0, day0 + 86399.999, a.log_dir, uid) if e.get("uid") == uid
               or (e.get("data") or {}).get("parent") == uid]
        if evs:
            return evs
    return []


def legacy_turn(uid: str, a: argparse.Namespace, out: Out) -> bool:
    """旧 utterances.jsonl 里的记录：只出 timeline 和关键字段，不重复 stages。"""
    try:
        lines = a.legacy_file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        if f'"{uid}"' not in line:
            continue
        r = json.loads(line)
        out.add(f"[旧记录] {uid} {r.get('source')}/{r.get('verdict')} 「{short(r.get('text', ''), 40)}」 原因={short(r.get('reason', ''), 30)}")
        for it in r.get("timeline", []):
            out.add(f"+{round((it['t'] - r['t0']) * 1000):>6}ms {it.get('stage'):<10} {it.get('state')} {short(it.get('note', ''), 60)}")
        out.add(f"回复：{short(r.get('reply', ''), 300)}")
        if r.get("tools"):
            out.add("工具：" + "; ".join(f"{t.get('tool')} {t.get('decision')} {short(t.get('summary', ''), 50)}" for t in r["tools"]))
        if r.get("unspoken") or r.get("error"):
            out.add(f"没念出：{short(r.get('unspoken'), 120)} 错误：{r.get('error')}")
        out.add(f"耗时：{short(r.get('timings'), 200)} run_id={r.get('run_id')}")
        return True
    return False


def show_turn(uid: str, a: argparse.Namespace, out: Out) -> None:
    evs = find_uid(uid, a)
    if not evs:
        if not legacy_turn(uid, a, out):
            out.add(f"没找到 {uid}（新事件流和 utterances.jsonl 都没有）；先用 --recent 20 --all 看看编号")
        return
    t0 = evs[0]["ts"]
    end = next((e for e in reversed(evs) if e.get("ev") == "turn.end" and e.get("uid") == uid), None)
    gw = next((e for e in evs if e.get("ev") == "gateway.run"), None)
    d = (end or {}).get("data") or {}
    out.add(f"== {uid} {datetime.fromtimestamp(t0):%Y-%m-%d %H:%M:%S} {d.get('source', '')}/{d.get('verdict', '')} "
            f"「{short(d.get('text', ''), 60)}」")
    for e in evs:
        if e.get("ev") in ("turn.end", "gateway.run"):
            continue
        out.add(fmt_event(e, detail=a.detail, rel=t0))
    if end:
        out.add(f"-- 回复：{short(d.get('reply', ''), 400 if a.detail else 160)}")
        out.add(f"-- 念出 {d.get('spoken')} 句，漏念 {d.get('unspoken')} 句；耗时 {short(d.get('timings'), 200)}"
                + (f"；错误 {d['error']}" if d.get("error") else ""))
    render_gateway(gw, evs, t0, a, out)  # B3 实现；B2 阶段先写成空函数
    for h in diagnose(d, (gw or {}).get("data"), evs):  # B3 补全；B2 阶段只做本地规则
        out.add("!! " + h)


def render_gateway(gw, evs, t0, a, out) -> None:  # B3 替换
    pass


def diagnose(end_d: dict, gw_d: dict | None, evs: list[dict]) -> list[str]:  # B3 扩充
    hints = []
    if end_d.get("unspoken"):
        hints.append(f"有 {end_d['unspoken']} 句没念出来（被打断或 TTS 失败），看上面 tts.* / play.* 事件")
    if end_d.get("error"):
        hints.append("本轮报错：" + short(end_d["error"], 120))
    return hints


# ---------- 旧文本日志 ----------
def tail_text(a: argparse.Namespace, out: Out) -> None:
    p = TEXT_LOGS[a.tail]
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        out.add(f"读不了 {p}：{e}")
        return
    if a.tail == "voice":
        lines = [ln for ln in lines if "/health" not in ln]  # 每 10 秒一行的健康检查太吵
    if a.keyword:
        lines = [ln for ln in lines if a.keyword.lower() in ln.lower()]
    for ln in lines[-a.max_lines:]:
        out.add(short(ln, LINE_WIDTH * 2 if a.detail else LINE_WIDTH))


# ---------- 入口 ----------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m guanjia.logq", description="管家日志查询（默认紧凑输出）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--recent", type=int, metavar="N")
    g.add_argument("--turn", metavar="UID")
    g.add_argument("--flags", action="store_true")
    g.add_argument("--runs", type=int, metavar="N")
    g.add_argument("--payload", metavar="UID|RUN_ID")
    g.add_argument("--boot", action="store_true")
    g.add_argument("--tail", choices=sorted(TEXT_LOGS))
    ap.add_argument("--all", action="store_true", help="--recent 也列被拒/忽略的句子")
    ap.add_argument("--since", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--days", type=int, default=3, help="--turn 往前找几天")
    ap.add_argument("--uid")
    ap.add_argument("--comp", type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument("--level", choices=list(LEVELS), default="info")
    ap.add_argument("--ev", type=lambda s: [x for x in s.split(",") if x])
    ap.add_argument("-k", "--keyword")
    ap.add_argument("--kb64", help="base64(UTF-8) 的关键字：ssh 命令行传中文会乱码时用")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--max-lines", type=int, default=80)
    ap.add_argument("--max-chars", type=int, default=8000)
    ap.add_argument("--out")
    ap.add_argument("--log-dir", type=Path, default=LOG_DIR)
    ap.add_argument("--legacy-file", type=Path, default=LEGACY_RECORDS)
    ap.add_argument("--agent-db", type=Path, default=None)
    ap.add_argument("--gw-log-dir", type=Path, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    now = time.time()
    if a.kb64:
        a.keyword = base64.b64decode(a.kb64).decode("utf-8")
    default_since = "1d" if (a.boot or a.flags or a.recent or a.tail) else "2h"
    a.since = parse_time(a.since or default_since, now)
    a.until = parse_time(a.until, now) if a.until else now + 60
    out = Out(a.max_lines + 5, a.max_chars)
    filtered = any([a.uid, a.comp, a.ev, a.keyword, a.level != "info"])
    if a.turn:
        show_turn(a.turn, a, out)
    elif a.flags:
        flags(a, out)
    elif a.runs:
        show_runs(a, out)  # B3
    elif a.payload:
        show_payload(a, out)  # B3
    elif a.tail:
        tail_text(a, out)
    elif a.boot:
        a.ev = a.ev or list(BOOT_EVS)
        list_events(a, out)
        warns = argparse.Namespace(**{**vars(a), "ev": None, "level": "warn"})
        out.add("-- 同期 WARN 以上 --")
        list_events(warns, out)
    elif a.recent or not filtered:
        a.recent = a.recent or 10
        recent(a, out)
    else:
        list_events(a, out)
    out.emit(a.out)
    return 0


def show_runs(a, out) -> None:  # B3 替换
    out.add("（--runs 在 B3 实现）")


def show_payload(a, out) -> None:  # B3 替换
    out.add("（--payload 在 B3 实现）")


if __name__ == "__main__":
    raise SystemExit(main())
```

### 6.4 `scripts/logq.cmd`

```bat
@echo off
rem 远程查日志：结果写成 UTF-8 文件，屏幕只回一行 ASCII，避免 ssh/cmd 的 GBK 乱码
cd /d F:\coding\guanjia
.venv\Scripts\python.exe -m guanjia.logq %* --out tmp\logq-out.txt
```

### 6.5 先写的失败测试 `tests/test_logq.py`（要点）
用 `tempfile` 造 `events-voice-<今天>.jsonl` 和 `events-supervisor-<今天>.jsonl` 夹具（含两轮 accept + 一句 ignored + 一个 `gateway.run` + 一个 `user_flag` + 一条 2000 字的 data 字段 + 一条 `mutex_busy`），全部通过 `logq.main([... "--log-dir", d, "--out", f])` 调用后读 `f`：
1. `--recent 5` 只出两行 accept、按时间顺序、含 `stop=stop tools=0`、被标记的那行带 `⚑`；`--all` 出三行。
2. `--turn u130001-001` 首行 `== u130001-001`，事件行以 `+     0ms` 起，不含另一轮的 uid。
3. `--comp tts --level warn` 只出 tts 的 warn；`--ev 'enroll.*'` 只出 enroll；`-k 音响` 与 `--kb64 <音响的 base64>` 结果相同。
4. 长字段：默认输出该行 ≤160 字且含 `…`；`--detail` 出现完整 2000 字。
5. `--max-lines 3` 输出含「还有」或「省略」提示；`--max-chars 200` 总字数 ≤ 260。
6. 两个进程文件的事件合并后按 ts 交错排序（`--boot` 里 supervisor 与 voice 事件交错）。
7. `--out` 文件能按 UTF-8 读回中文；stdout 那行是纯 ASCII。
8. 新事件里没有某 uid、`--legacy-file` 夹具里有 → 输出 `[旧记录]` 且不含 `"stages"`。
9. `parse_time("10m", now) == now - 600`；`parse_time("13:40", now)` 是当天 13:40。

### 6.6 给 AI 的排查指南（原文写进 `docs/plans/HANDOFF.md` 新一节）

> **排查管家问题：先查日志，不要先猜代码。** 远程统一这样跑（结果是 UTF-8 文件，不会乱码）：
> `ssh -o BatchMode=yes home "F:\coding\guanjia\scripts\logq.cmd <参数>"` → `scp home:F:/coding/guanjia/tmp/logq-out.txt $env:TEMP\logq.txt` → 用 Read 读。
> 1. **先定位**：`--recent 15`（主人说「刚才」）或 `--recent 15 --since "13:30" --until "13:50"`（主人说了时间）；主人在面板点过「这句有问题」就先跑 `--flags`。找到那一轮的 uid。
> 2. **看一轮**：`--turn <uid>`。看末尾 `!!` 诊断行和「网关」段（模型、stop、工具每次的成败、token）。追问轮会提示 child uid，接着 `--turn <child>`。
> 3. **按模块深挖**：声纹录不上 `--ev 'enroll.*' --since 30m`；没声音 `--comp tts,play --level warn --since 1h`；启动/重启/依赖 `--boot`；有异常 `--level error --since 1d`。
> 4. **要看模型到底收到了什么**：`--payload <uid>`（大，最后才看）。
> 5. 新事件里没有的老问题：`--tail voice -k <英文关键字>`、`--tail stderr`、`--tail sup`。
> 6. 微信/TUI 那边的轮次（不经语音）：`--runs 10`。
> 7. 兜底才用 OpenClaw 自带：`openclaw logs --json --limit 200`（网关日志）、`openclaw sessions export-trajectory --session-key agent:main:main --json`（整个会话导出，很大）。
> 规矩：默认输出已经截断；只有确实需要才加 `--detail`、调大 `--max-lines`。关键字带中文用 `--kb64`（PowerShell：`[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('音响'))`）。

### 6.7 上线 / 回滚 / 风险
上线：scp 两个新文件 + 测试，远程跑 `test_logq.py`；不用重启任何东西。回滚：删文件。风险：无（只读）。

---

## 7. B3 · 网关可见性

### 7.1 取数优先级（结论）
1. **语音进程解析流里已有字段**：`id`（run_id，已有）、`model`、`choices[0].finish_reason`、`usage`（OpenAI 兼容流通常只在最后一个 chunk 或带 `stream_options.include_usage` 时才有——**不加该参数**，避免改变网关行为，有就记、没有就算了）。
2. **只读 trajectory 表**（主力，信息完整）：轮次结束后按 run_id 读，摘要成 `gateway.run`。原因：trajectory 是 10MiB 滚动窗口，**必须在轮次结束时就把摘要和上下文另存**，不能事后再查。
3. 分级插件写事件：不需要（trajectory 里已有工具调用与成败）；只在想把「分级决定」精确挂到 toolCallId 时做 B7。
4. OpenClaw CLI：只进 AI 指南兜底。

### 7.2 改动文件
- `src/guanjia/brain.py`：`Brain.stream()` 记 `self.last_meta`
- `src/guanjia/runtrace.py`：新增 `run_summary()`、`recent_runs()`（保留 `run_phases()` 不改签名）
- `src/guanjia/voice.py`：`_on_turn_event()`（first_token → `brain.run`；stream_end → `brain.stream_end`）、`_fill_gateway_phases()`（改为同时产出 `gateway.run` + blob）
- `src/guanjia/logq.py`：替换 `render_gateway / diagnose / show_runs / show_payload`，新增 `gw_log_lines`
- 新增 `tests/test_runtrace_summary.py`；扩充 `tests/test_logq.py`

### 7.3 `brain.py`：`Brain.stream()`
在 `self.last_run_id = None` 旁加 `self.last_meta: dict = {}`；循环里每个解析成功的 chunk：
```python
meta = self.last_meta
meta["chunks"] = meta.get("chunks", 0) + 1
if chunk.get("model"):
    meta["model"] = chunk["model"]
choice = (chunk.get("choices") or [{}])[0]
if choice.get("finish_reason"):
    meta["finish_reason"] = choice["finish_reason"]
if isinstance(chunk.get("usage"), dict):
    meta["usage"] = chunk["usage"]
```
`__init__` 也初始化 `self.last_meta = {}`。纯内存字典赋值，不影响首字。

### 7.4 `runtrace.py`：新增（完整代码）

```python
TRACE_SCHEMA = "openclaw-trajectory"  # OpenClaw 2026.9.6 trajectory schemaVersion 1；对不上就返回 None


def _rows(run_id: str, db: Path) -> list[tuple[str, int]]:
    if not run_id or not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            return con.execute("select event_json, created_at from trajectory_runtime_events where run_id = ? order by seq",
                               (run_id,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _brief(v, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


def run_summary(run_id: str, db: Path = AGENT_DB) -> dict | None:
    """一个 run 的摘要：模型、stop、usage、每次工具调用（成败/耗时/参数/错误）、阶段时间点、发给模型的上下文摘要。
    model.completed 还没写出来（轮次刚结束）时返回 None，调用方稍后重试。"""
    out: dict = {"run_id": run_id, "phases": {}, "tools": []}
    calls: dict[str, dict] = {}
    done = False
    for event_json, created_at in _rows(run_id, db):
        try:
            e = json.loads(event_json)
        except (ValueError, TypeError):
            continue
        if e.get("traceSchema") not in (None, TRACE_SCHEMA):
            return None
        kind, d, t = e.get("type"), e.get("data") or {}, float(created_at) / 1000.0
        key = _PHASES.get(kind) or ("first_tool" if kind == "tool.call" else None)
        if key and key not in out["phases"]:
            out["phases"][key] = t
        if kind == "session.started":
            out.update(provider=e.get("provider"), model=e.get("modelId"), trigger=d.get("trigger"),
                       channel=d.get("messageProvider"), tool_count=d.get("toolCount"), session_key=e.get("sessionKey"))
        elif kind == "context.compiled":
            msgs = d.get("messages") or []
            sp = d.get("systemPrompt")
            out["ctx"] = {"prompt": d.get("prompt"), "messages_total": len(msgs), "messages_tail": msgs[-6:],
                          "system_chars": sp.get("originalChars") if isinstance(sp, dict) else len(sp or ""),
                          "tools": [x.get("name") if isinstance(x, dict) else str(x) for x in (d.get("tools") or [])][:80]}
        elif kind == "tool.call":
            calls[d.get("toolCallId") or f"#{len(calls)}"] = {"name": d.get("name"), "t": t, "args": _brief(d.get("args"))}
        elif kind == "tool.result":
            c = calls.setdefault(d.get("toolCallId") or f"#{len(calls)}", {"name": d.get("name"), "t": t})
            res = d.get("result") or {}
            det = res.get("details") or {} if isinstance(res, dict) else {}
            c.update(ok=bool(d.get("success")), ms=det.get("durationMs") or round((t - c["t"]) * 1000),
                     exit=det.get("exitCode"))
            if not d.get("success"):
                content = res.get("content") if isinstance(res, dict) else None
                text = content[0].get("text") if isinstance(content, list) and content and isinstance(content[0], dict) else res
                c["err"] = _brief(text, 200)
        elif kind == "model.completed":
            done = True
            out.setdefault("stop", d.get("stopReason"))
            out.setdefault("usage", d.get("usage"))
        elif kind == "trace.artifacts":
            u = d.get("usage") or {}
            out.update(stop=d.get("stopReason"), final_status=d.get("finalStatus"), aborted=d.get("aborted"),
                       timed_out=d.get("timedOut"), prompt_error=d.get("promptErrorSource"),
                       usage={k: u.get(k) for k in ("input", "output", "cacheRead", "cacheWrite", "total")},
                       texts=[_brief(x, 400) for x in (d.get("assistantTexts") or [])])
    if not done:
        return None
    out["tools"] = list(calls.values())
    return out


def recent_runs(n: int, db: Path = AGENT_DB) -> list[str]:
    """最近 n 个 run_id（新到旧）。"""
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute("select run_id, max(rowid) m from trajectory_runtime_events group by run_id order by m desc limit ?",
                               (n,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []
    return [r[0] for r in rows]
```

### 7.5 `voice.py` 接线
- `_on_turn_event(name, data)`：
  - `first_token`：`elog.emit("brain", "brain.run", uid=uid, run_id=getattr(self.brain, "last_run_id", None), first_ms=…)`
  - `stream_end`：`elog.emit("brain", "brain.stream_end", "回复完毕" if ok else "断流", uid=uid, level="info" if ok else "error", ok=…, error=…, reply_chars=len(当前回复)?, **getattr(self.brain, "last_meta", {}))`（回复长度可不记，turn.end 里有 reply）
- `_fill_gateway_phases(uid, run_id, marks)`：重试循环改为调 `run_summary(run_id)`（拿不到继续 sleep 1 秒，**最多 5 次**，因为 trajectory 在 `session.ended` 前后才 flush）；拿到后：
  1. 照旧算 `gateway_prep_ms / model_first_ms`、`hub.update(uid, run_id=…, timings=…)`；
  2. `rel = elog.blob(uid, "gwctx", s.pop("ctx", None))`（ctx 为 None 就不存）；
  3. `elog.emit("gateway", "gateway.run", f"{model短名} stop={stop} 工具 {n} 次", uid=uid, level=…, blob=rel, **{k: s[k] for k in ("run_id","provider","model","channel","stop","final_status","usage","tools","aborted","timed_out","prompt_error","texts","phases")})`，level：`final_status` 非 success / aborted / timed_out / 有失败工具 → `warn`；
  4. 5 次都拿不到：`elog.emit("gateway", "gateway.run_missing", uid=uid, run_id=run_id, level="warn")`。
- `phases` 里的时间是 epoch 秒，logq 渲染时换成相对本轮 t0 的毫秒。

### 7.6 `logq.py` 网关部分（替换 B2 的占位函数，完整代码）

```python
import os
import tempfile

from guanjia import runtrace

try:
    from guanjia.turns import has_promise
except Exception:  # noqa: BLE001  logq 在没有 numpy 的环境也要能跑
    def has_promise(reply: str) -> bool:
        return any(w in reply for w in ("我先", "我看看", "我查一下", "稍等", "马上", "这就", "让我", "我去", "我试试"))

TOOLISH = re.compile(r"<\|?tool|tool_call|function_call|\"name\"\s*:\s*\"\w+\"\s*,\s*\"arguments\"")


def gw_log_lines(t0: float, t1: float, run_id: str | None, a: argparse.Namespace, limit: int = 12) -> list[str]:
    """网关日志（%TEMP%\\openclaw\\openclaw-日期.log，JSON 行）里本轮时间窗内：含本 run_id 的行 + WARN 以上 + model-fetch 行。"""
    gdir = a.gw_log_dir or Path(os.environ.get("TEMP", tempfile.gettempdir())) / "openclaw"
    p = gdir / f"openclaw-{datetime.fromtimestamp(t0):%Y-%m-%d}.log"
    try:
        with p.open("rb") as fh:
            fh.seek(max(0, p.stat().st_size - 20_000_000))  # 最多读尾部 20MB
            raw = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out = []
    for ln in raw:
        try:
            r = json.loads(ln)
            ts = datetime.fromisoformat(r["time"]).timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        if not (t0 - 1 <= ts <= t1 + 3):
            continue
        meta = r.get("_meta") or {}
        lv = meta.get("logLevelName", "")
        msg = r.get("message") or " ".join(str(r[k]) for k in sorted(r) if k.isdigit())
        if (run_id and run_id in ln) or lv in ("WARN", "ERROR", "FATAL") or "[model-fetch]" in msg:
            out.append(f"+{round((ts - t0) * 1000):>6}ms gwlog {lv[:1]} {short(msg, 130)}")
    return out[-limit:]


def render_gateway(gw: dict | None, evs: list[dict], t0: float, a: argparse.Namespace, out: Out) -> None:
    if not gw:
        return
    g = gw.get("data") or {}
    u = g.get("usage") or {}
    out.add(f"-- 网关 run={short(g.get('run_id', ''), 24)} {g.get('provider')}/{g.get('model')} stop={g.get('stop')} "
            f"状态={g.get('final_status')} token 入{u.get('input')}/出{u.get('output')}/缓存{u.get('cacheRead')}"
            + (f" blob={g['blob']}" if g.get("blob") else ""))
    ph = g.get("phases") or {}
    if ph:
        out.add("   阶段：" + " ".join(f"{k}=+{round((v - t0) * 1000)}ms" for k, v in sorted(ph.items(), key=lambda kv: kv[1])
                                       if isinstance(v, (int, float))))
    tools = g.get("tools") or []
    if not tools:
        out.add("   工具：本轮模型没有发起任何工具调用")
    for x in tools:
        mark = "✓" if x.get("ok") else ("✗" if x.get("ok") is False else "?")
        out.add(f"   {mark} {x.get('name')} {x.get('ms')}ms exit={x.get('exit')} {short(x.get('args', ''), 70)}"
                + (f" 错误：{short(x.get('err', ''), 80)}" if x.get("err") else ""))
    for i, txt in enumerate(g.get("texts") or []):
        out.add(f"   模型原文[{i}]：{short(txt, 400 if a.detail else 120)}")
    t1 = max(e.get("ts", t0) for e in evs)
    for ln in gw_log_lines(t0, t1, g.get("run_id"), a):
        out.add(ln)


def diagnose(end_d: dict, gw_d: dict | None, evs: list[dict]) -> list[str]:
    """看一轮的常见毛病，给一句人话。只是线索，不是结论。"""
    hints: list[str] = []
    reply = end_d.get("reply") or " ".join((gw_d or {}).get("texts") or [])
    tools = (gw_d or {}).get("tools") or []
    if gw_d is not None:
        if not tools and has_promise(reply) and gw_d.get("stop") == "stop":
            hints.append("模型说了要做（承诺词），但这一轮一次工具调用都没发就正常结束了（stop）→ 模型行为问题，不是工具被拦；"
                         "看有没有 nudge.sent 追问、追问轮是否做了")
        bad = [x for x in tools if x.get("ok") is False]
        if bad:
            hints.append(f"工具失败 {len(bad)} 次：" + "；".join(f"{x.get('name')}: {short(x.get('err', ''), 60)}" for x in bad))
        if gw_d.get("stop") not in (None, "stop", "toolUse", "tool_calls"):
            hints.append(f"非正常结束 stopReason={gw_d.get('stop')}（length=输出被截断，error=模型侧报错）")
        if gw_d.get("aborted") or gw_d.get("timed_out"):
            hints.append(f"网关侧 aborted={gw_d.get('aborted')} timed_out={gw_d.get('timed_out')}：客户端断开或超时会取消本轮")
        if gw_d.get("prompt_error"):
            hints.append(f"提示词构建出错：{gw_d.get('prompt_error')}")
    elif end_d.get("verdict") == "accept":
        miss = any(e.get("ev") == "gateway.run_missing" for e in evs)
        hints.append("没有网关侧记录" + ("（trajectory 5 次都没查到）" if miss else "") + "：用 --runs 10 看网关最近的 run")
    if TOOLISH.search(reply):
        hints.append("回复里像混进了工具调用文本：模型想调工具但格式没被网关识别")
    blocked = [e for e in evs if e.get("ev") == "tool.audit" and str((e.get("data") or {}).get("decision", "")).startswith(
        ("block", "approval-deny", "approval-timeout", "approval-cancelled"))]
    if blocked:
        hints.append("分级插件拦下/审批未通过：" + "；".join(f"{(e.get('data') or {}).get('tool')} {(e.get('data') or {}).get('decision')}" for e in blocked))
    if end_d.get("unspoken"):
        hints.append(f"有 {end_d['unspoken']} 句没念出来（被打断或 TTS 失败），看 tts.* / play.* 事件")
    if end_d.get("error"):
        hints.append("本轮报错：" + short(end_d["error"], 120))
    child = next(((e.get("data") or {}).get("child") for e in evs if e.get("ev") == "nudge.sent"), None)
    if child:
        hints.append(f"本轮触发了自动追问，追问轮：--turn {child}")
    return hints


def show_runs(a: argparse.Namespace, out: Out) -> None:
    """网关最近 N 个 run（不限语音），一行一个；能对上语音 uid 的标出来。"""
    db = a.agent_db or runtrace.AGENT_DB
    uid_of = {(e.get("data") or {}).get("run_id"): e.get("uid")
              for e in load_events(time.time() - 3 * 86400, time.time() + 60, a.log_dir, "run_id")
              if e.get("ev") in ("brain.run", "gateway.run")}
    for rid in runtrace.recent_runs(a.runs, db):
        s = runtrace.run_summary(rid, db)
        if not s:
            out.add(f"{short(rid, 30)} （还没结束或读不到）")
            continue
        t = (s.get("phases") or {}).get("session_started")
        tools = s.get("tools") or []
        bad = sum(1 for x in tools if x.get("ok") is False)
        prompt = ((s.get("ctx") or {}).get("prompt")) or ""
        out.add(f"{fmt_ts(t) if t else '?'} {s.get('channel') or s.get('trigger')} uid={uid_of.get(rid, '-')} "
                f"stop={s.get('stop')} tools={len(tools)}" + (f"(失败{bad})" if bad else "")
                + f" 「{short(prompt, 30)}」→「{short(' '.join(s.get('texts') or []), 40)}」 run={short(rid, 22)}")


def show_payload(a: argparse.Namespace, out: Out) -> None:
    """发给模型的上下文摘要：先找 gateway.run 的 blob，没有就现查 trajectory（可能已滚出窗口）。"""
    key = a.payload
    gw = None
    if not key.startswith("chatcmpl_") and "-" in key and key.startswith("u"):
        gw = next((e for e in find_uid(key, a) if e.get("ev") == "gateway.run"), None)
    else:
        gw = next((e for e in load_events(time.time() - 3 * 86400, time.time() + 60, a.log_dir, key)
                   if e.get("ev") == "gateway.run"), None)
    rel = ((gw or {}).get("data") or {}).get("blob")
    if rel:
        text = (a.log_dir / "blobs" / rel).read_text(encoding="utf-8", errors="replace")
    else:
        rid = ((gw or {}).get("data") or {}).get("run_id") or key
        s = runtrace.run_summary(rid, a.agent_db or runtrace.AGENT_DB)
        if not s or not s.get("ctx"):
            out.add(f"没有 {key} 的上下文（blob 不在、trajectory 也查不到）")
            return
        text = json.dumps(s["ctx"], ensure_ascii=False, indent=1)
    for ln in text.splitlines():
        out.add(ln if a.detail else short(ln, LINE_WIDTH * 2))
```

### 7.7 先写的失败测试
`tests/test_runtrace_summary.py`：临时 sqlite 建 `trajectory_runtime_events(session_id, seq, run_id, event_json, created_at)`，照 §1 实测结构插一个 run：session.started（provider/modelId）、context.compiled（messages 10 条、tools 3 个、systemPrompt 为截断 dict）、prompt.submitted、tool.call×2、tool.result（一成一败，失败的 content[0].text="blocked by guard"）、model.completed、trace.artifacts（stopReason=stop、usage、assistantTexts）。断言：
- `model/provider/stop/final_status/usage.input` 正确；`tools` 两项，`ok` 分别 True/False，失败项 `err` 含 "blocked"；`ctx.messages_tail` 长 6、`ctx.system_chars` 取 originalChars；
- 去掉 model.completed 行 → `None`；`traceSchema` 改成别的 → `None`；表不存在 → `None` 不抛；
- `recent_runs(1)` 返回最后插入的 run_id。

`tests/test_logq.py` 追加：夹具 `gateway.run`（tools=[]、stop=stop）+ `turn.end`（reply「我先用只读方式找一下」）→ `--turn` 输出含「一次工具调用都没发」；tools 含失败项 → 输出含「✗」和「工具失败 1 次」；`--gw-log-dir` 夹具里一行 WARN、一行含 run_id、一行无关 INFO → 只出前两行；`--payload <uid>` 读 blob；`--runs 1 --agent-db <上面的临时库>` 出一行含 `stop=stop`。

`tests/test_voice_nudge.py` 风格补一个接线用例（放 `tests/test_p3_gateway_events.py`）：半构造 VoiceDaemon，`brain` 换成带 `last_run_id/last_meta` 的假对象，`runtrace.run_summary` monkeypatch 成第 1 次返回 None、第 2 次返回摘要 → `_fill_gateway_phases` 产生一条 `gateway.run`（用 `EventLog(sync=True)` + `add_tap` 收集），且 `hub.record(uid)["run_id"]` 已写入。

### 7.8 上线 / 回滚 / 风险
- 上线：scp → 远程跑三个测试 + 全量回归 → 重启语音 → 说一句「现在几点」→ `logq.cmd --recent 3` 能看到 `model=DeepSeek-V4-Flash stop=stop`；**验收用今天的真事**：`logq.cmd --runs 10` 能列出 13:48 那条「让你干的活你干成怎么样了呀」且 tools=0、stop=stop。
- 回滚：`git checkout -- src/guanjia/brain.py src/guanjia/runtrace.py src/guanjia/voice.py src/guanjia/logq.py`，重启语音。
- 风险：读网关 sqlite（`mode=ro`，与现有 `run_phases` 同样方式，已在线上跑过；WAL 下只读连接不阻塞网关写）。trajectory 是 OpenClaw 内部表，**升级 OpenClaw 后可能变**：`traceSchema` 不符就返回 None，只丢网关段，不影响语音。blob 含对话正文，7 天清理。不动网关、不动插件。

---

## 8. B4 · 声纹注册逐段 + 依赖状态

### 8.1 改动
- `src/guanjia/enroll_session.py`：`EnrollSession.feed()` 里 `playing` 分支也返回 `info`（先算 `quality()` 的 info 再判 playing，**判据顺序不变**：playing 仍然直接拒）；所有返回里带 `group`（当前要录的组键，testing 时为 `"test"`）和 `total=len(self.samples)`。
- `src/guanjia/voice.py`：
  - `_enroll_take(seg)`：`r = sess.feed(...)` 之后：
    ```python
    info = r.get("info") or {}
    elog.emit("enroll", "enroll.seg", "收下" if r.get("ok") else str(r.get("reason")),
              level="info" if r.get("ok") else "warn", ok=bool(r.get("ok")), group=r.get("group"),
              total=r.get("total"), self_score=r.get("self_score"), playing=self._playing(), overlap=overlap,
              seg_start_lag=round(time.monotonic() - seg.start, 2), **info)
    ```
    （info 已含 sec / effective_sec / zero_ratio / peak）；状态从 recording 变 testing/ready 时再记 `enroll.state`。
  - `_speaker_action_locked(body)`：现有 `log.info("enroll start…")` / `"enroll saved…"` 旁边各补 `enroll.start`（name、阈值常量 MIN_EFFECTIVE_SEC/MAX_ZERO_RATIO/MIN_PEAK/MIN_SELF_SIM，方便事后对照）、`enroll.save`（n、test_score、test_old_score、backup）；放弃/超时分支补 `enroll.abort`（reason、已收段数、最后一次 last）。
  - `_on_health(h)`：和 `self._last_health` 比较 gateway/tts 布尔值，**变化时**记 `dep.change`（`level=warn` 表示变坏）。
  - `_frontend_step` 里已有的「丢弃」判定（如有 `log.info("丢弃…")`）旁补 `asr.drop`（reason、seg_sec）——若该分支只走 logging 则由 B1 的派生已覆盖，可跳过。
- 测试 `tests/test_p4_enroll_events.py`：
  1. `quality()` 拒收（全零、过短、峰值太小）各一段喂给 `EnrollSession.feed` → 返回都含 `info.zero_ratio`；`playing=True` 时也含 info 且 reason 不变；
  2. 半构造 VoiceDaemon + 假 sess（feed 返回预设 dict）调 `_enroll_take` → 收到一条 `enroll.seg`，`level=warn`、data 有 `effective_sec/zero_ratio/peak/overlap`；
  3. `_on_health` 连续两次相同状态只出一条 `dep.change`，变化后再出一条。
- 上线：重启语音；让主人开一次注册（或面板「重新注册」录 3 段后放弃）→ `logq.cmd --ev "enroll.*" --since 10m` 每段一行。回滚：git checkout 两个文件。风险：`feed()` 判据顺序不能变（现有 `tests/test_p22_speaker.py`、`test_p22_voice_enroll.py` 必须全过）。

---

## 9. B5 · 看门狗事件

### 9.1 改动 `scripts/voice-supervisor.ps1`
1. 新增（放在 `Write-Log` 下面）：
```powershell
$EventsDir = Join-Path $LogDir 'logs'
New-Item -ItemType Directory -Force -Path $EventsDir | Out-Null

# 结构化事件（与 guanjia.elog 同格式），每个看门狗进程追加到自己当天的文件；写不进不影响看门狗
function Write-Event([string]$Ev, [string]$Level = 'info', [string]$Msg = '', [hashtable]$Data = @{}) {
    try {
        $Data['pid'] = $PID
        $ts = [Math]::Round(([DateTimeOffset]::Now).ToUnixTimeMilliseconds() / 1000.0, 3)
        $rec = [ordered]@{ ts = $ts; comp = 'supervisor'; level = $Level; ev = $Ev; msg = $Msg; data = $Data }
        $f = Join-Path $EventsDir ('events-supervisor-{0:yyyy-MM-dd}.jsonl' -f (Get-Date))
        [IO.File]::AppendAllText($f, (($rec | ConvertTo-Json -Compress -Depth 5) + "`n"), [Text.UTF8Encoding]::new($false))
    } catch {}
}

function Get-TailText([string]$Path, [int]$Lines = 20, [int]$MaxChars = 1500) {
    try {
        if (-not (Test-Path $Path)) { return '' }
        $t = (Get-Content -Path $Path -Tail $Lines -Encoding UTF8) -join "`n"
        if ($t.Length -gt $MaxChars) { $t = $t.Substring($t.Length - $MaxChars) }
        return $t
    } catch { return '' }
}
```
2. 打点（每处保留原 `Write-Log`，**旁边加** `Write-Event`）：
   - 抢互斥量失败：读 `voice-supervisor.pid`，`Get-CimInstance Win32_Process -Filter "ProcessId=$holder"` 拿 `CreationDate/ParentProcessId/CommandLine`，记 `mutex_busy`（level warn，data：holder_pid、holder_alive、holder_started、holder_parent、my_parent=$ParentPid 需先算 ParentPid → 把 `$ParentPid` 计算挪到互斥量之前），用于看清「新旧看门狗互相认为对方在跑」到底是谁占着、它的父进程还在不在。
   - `start`：`sup.start`（job_bound、exe、args、parent_pid、parent_name）。
   - `Wait-Deps`：每 15 秒一条 `deps.wait`（waited_s、gateway、tts）；就绪 `deps.ready`（waited_s）；超时 `deps.timeout`（level warn，waited_s、gateway、tts、`tts_tail = Get-TailText (Join-Path $LogDir 'tts_wsl.log') 8 800`）。
   - `Import-VoiceEnv`：`env` 只记键名不记值？——值是调参用的阈值，不是密钥，可以记；但统一过一个 `-notmatch 'TOKEN|KEY|SECRET'` 判断，命中则值记 `***`。
   - 拉起：`child.start`（child_pid）；启动失败：`child.start_failed`（level error，message）。
   - 退出：`child.exit`（code、ran_s、level：0/3 info，其他 error；非 0/3 时带 `stderr_tail = Get-TailText (Join-Path $LogDir 'voice-stderr.txt') 30 1500`）。
   - `parent_gone`（where：waiting_deps / running）、`quit`、`restart_requested`、`backoff`（sec）。
3. **另一个小修正（顺手、低风险）**：`voice-stderr.txt` 每次被覆盖——拉起前若存在且非空，先 `Move-Item -Force` 成 `voice-stderr.prev.txt`，崩溃后下一次拉起还能看到上一份。`logq --tail stderr` 先读当前、空的话提示看 `.prev`（logq 的 TEXT_LOGS 加 `"stderr-prev"`）。

> 看门狗竞态本身（重启时新旧看门狗互相认为对方在跑）**本计划只负责让它可见**，不改互斥逻辑；有了 `mutex_busy` 的持有者信息后另立修复。

### 9.2 测试 `tests/test_p5_supervisor_events.ps1`（照 `tests/test_p2_supervisor.ps1` 的写法）
- 用临时目录作 `-LogPath`，`-SkipDeps`，`-Exe` 指向 `.venv\Scripts\python.exe`、`-ArgLine` 跑 `tests\fake_voice_child.py`（让它以 0 退出；如不支持传退出码，就在测试里写一个临时 py `import sys; sys.exit(0)`）、`-MutexName` 用随机名；
- 断言 `logs\events-supervisor-*.jsonl` 每行能被 `ConvertFrom-Json` 解析，依次含 `sup.start`、`child.start`、`child.exit`（code=0）；
- 子进程以 7 退出、stderr 写一行中文 → `child.exit` level=error 且 `stderr_tail` 含该中文（UTF-8 不乱码）；
- 先在测试进程里占住同名互斥量并写 pid 文件 → 再起看门狗 → 立即退出且有 `mutex_busy`，`holder_pid` = 测试进程 PID、`holder_alive=true`。

### 9.3 上线 / 回滚 / 风险
- 上线：scp → 跑 `test_p5_supervisor_events.ps1` 和原 `test_p2_supervisor.ps1` → **主人在场时**重启计划任务（`schtasks /End /TN GuanjiaVoice` 再 `/Run`；这正是今天出过竞态的操作，重启后立刻 `logq.cmd --boot --since 10m` 确认只有一个 `sup.start` 且 `child.start`）。
- 回滚：`git checkout -- scripts/voice-supervisor.ps1`，再重启计划任务。
- 风险：**动开机自启链路**——脚本语法错会导致语音不再自启。所有新代码包在 `try {} catch {}` 里；上线前必须在远程用 `powershell -NoProfile -File scripts\voice-supervisor.ps1 -SkipDeps -LogPath <临时> -MutexName <随机> -Exe <假子进程>` 实跑一遍。不写线上数据，不动网关。

---

## 10. B6 · 面板「这句有问题」+ 事件源

### 10.1 主人标记（必做）
- `src/guanjia/panel.py`：新增
```python
def attach_flags(self, log) -> None:
    """面板每句话旁的「这句有问题」：写一条 user_flag 事件（logq --flags 列出），并在记录上标记。"""
    def flag(body: dict) -> tuple[int, dict]:
        uid = str(body.get("uid") or "").strip()
        note = str(body.get("note") or "").strip()[:200]
        if not uid:
            raise ValueError("缺少 uid")
        rec = self.hub.record(uid) or {}  # 面板只留最近 50 句，老的查不到也照样记
        log.emit("panel", "user_flag", note or "（没写备注）", level="warn", uid=uid,
                 text=(rec.get("text") or "")[:80], reply=(rec.get("reply") or "")[:160], verdict=rec.get("verdict"))
        self.hub.update(uid, flag=note or "有问题")
        return 200, {"ok": True}
    self.register_post("/api/flag", flag)
```
- `voice.py` `VoiceDaemon.__post_init__`：`self.panel.attach_flags(elog.get())`（紧跟 `attach_prompts`）。
- `panel.html`：在渲染每条记录卡片的函数里加一个按钮 `<button data-alt="flag-button">这句有问题</button>`，点了 `prompt('哪里不对？（可空）')`，用页面已有的带 `X-Guanjia-Token` 的 POST 封装发 `/api/flag {uid, note}`；记录有 `flag` 字段时卡片显示一个「⚑ 备注」角标。
- 同时补 `panel.settings` / `panel.control` 事件：在 `PanelBackend.update_settings` 成功后记 `changed=list(patch)`；`PanelBackend.control` 记 action（这两处传入的 log 同上，可在 `attach_flags` 里顺手保存 `self._elog = log`）。
- 测试 `tests/test_p6_flag.py`：`PanelBackend(...)` + `EventLog(sync=True)`，`route_post("/api/flag", {"uid": u, "note": "没打开"})` → 200、产生 `user_flag`（data.text 来自 hub 记录）、`hub.record(u)["flag"] == "没打开"`；缺 uid → 400；uid 不在 hub → 仍 200 且 text 为空。另在 `test_p22_panel_routes.py` 的风格下验证 `/api/flag` 需要令牌。

### 10.2 面板实时日志切新事件源（可选，改动不大才做）
- `events.py`：`EventHub` 加 `push_elog(rec)` → `self._emit({"type": "elog", "e": rec})`，并在 `_emit` 里把 `"elog"` 也排除出 `_events` 回放缓冲（同 `"log"`），另设 `self._elogs = deque(maxlen=1000)`；`PanelBackend.snapshot()` 增加 `"elogs": hub.elogs(400)`。
- `voice.py`：`elog.get().add_tap(self.hub.push_elog)`（tap 在 emit 调用线程执行，`_emit` 是非阻塞 put，可接受）。
- `panel.html`：日志区加「事件」页签：模块多选（comp）、最低级别下拉（默认 info）、uid 输入框；每行用与 logq 相同的紧凑格式。旧「日志」页签保留。
- 如果 panel.html 的日志区结构改起来超过 ~80 行，**这一项推迟**，只做 10.1。

### 10.3 上线 / 回滚 / 风险
重启语音（panel.html 每次请求现读，HTML 改动刷新即见；panel.py/events.py 需重启）。回滚 git checkout。风险：面板 POST 路由已有令牌/同源保护，`/api/flag` 复用；不动线上数据与网关。

---

## 11. B7（可选）· 分级插件审计补 runId

- `F:\coding\guanjia\openclaw\plugins\guanjia-guard\index.ts`：`before_tool_call` 的 `ctx` 里有 `runId`（`before_prompt_build` 已在用 `ctx?.runId`），审计行加 `"runId"`、`"toolCallId"`（取 `event.toolCallId`，没有就不写）。`audittail.tool_entry()` 透传这两个字段，`tool.audit` 事件就能精确挂到 run 与 trajectory 的某次 tool.call。
- **动网关插件，需要重启网关**（会打断进行中的会话与审批），由主人决定是否做、何时做。不做的影响：`tool.audit` 只能按「当前进行中的语音轮」归属（现状），微信轮的工具决定挂不到 run 上。

---

## 12. 需要主人决定的事

1. **保留期**：事件 14 天、blob（含对话正文和工具输出）7 天、blob 总量 300MB——可以吗？
2. **B7 要不要做**：给分级插件审计加 runId，需要重启一次网关。trajectory 已经覆盖了「工具有没有调、成没成」，所以建议**先不做**，用一周再看。
3. **B5 上线时机**：重启计划任务要主人在场（今天的看门狗竞态就是这个操作触发的），约个时间。

---

## 13. 风险汇总

| 风险 | 批 | 应对 |
|---|---|---|
| 日志拖慢首字 | B1/B3 | emit 只入队；网关摘要在轮次结束后的后台线程做；测试卡 50µs/次 |
| trajectory 结构随 OpenClaw 升级变化 | B3 | 校验 `traceSchema`，不符返回 None；`run_phases` 原逻辑保留 |
| trajectory 10MiB 滚动窗口丢旧 run | B3 | 轮次结束即摘要 + 存 blob |
| 看门狗脚本出错导致不自启 | B5 | 全部 try/catch；上线前远程实跑；主人在场重启 |
| 隐私 | 全部 | redact 兜底；令牌不进参数；blob 7 天清理；仅本机 |
| 与「只说不做追问」改动冲突 | B1 | 在其合入后实施；只在 `_should_nudge/_send_nudge` 加事件，不改判定 |
| 中文经 ssh 乱码 | B2 | `--out` + `logq.cmd` + `--kb64` |

## 主人已定（2026-10-11 14:16）

1. 保留期按计划：事件 14 天；大字段 7 天；总量 300MB。
2. B7 先不做，用一周再看。
3. B5 等主人说方便时再上线（要重启开机自启计划任务）。其余批次（B1–B4、B6）照做，排在「常用工具直给 + 审批托盘」那批提交之后（同改 voice.py / tray.py）。

