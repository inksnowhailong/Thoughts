"""调试面板的数据源：每个环节开始/结束的结构化事件、每句话一条记录、实时日志。线程安全，与音频无关。

    前端线程 / 大脑线程 / 审计尾随 ──emit──> EventHub ──┬─> 最近 2000 条事件（SSE 断线重连补发）
                                                        ├─> 订阅者队列（每个面板页面一个，SSE 推送）
                                                        └─> 每句话的记录（内存留最近 50 条，结束时追加到 tmp\\utterances.jsonl）

环节（面板流程图的方框，按代码里的实际顺序）：麦克风 → 人声检测 → 识别 → 声纹 → 唤醒词 → 主程序 → 工具/审批 → 合成 → 播放，另有打断。
"""

from __future__ import annotations

import itertools
import json
import logging
import queue
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from guanjia.speech_shape import SHAPED_PREFIX

STAGES = ("mic", "vad", "asr", "speaker", "wake", "gateway", "tools", "tts", "play", "barge")
STAGE_CN = {"mic": "麦克风", "vad": "人声检测", "asr": "识别", "speaker": "声纹", "wake": "唤醒词",
            "gateway": "主程序", "tools": "工具/审批", "tts": "合成", "play": "播放", "barge": "打断"}
STATES = ("active", "done", "blocked", "skipped", "info")
LOG_MODULES = ("asr", "gate", "brain", "tools", "tts", "play", "barge", "barge-mon", "health", "settings", "panel",
               "tray", "runtime", "other")
RECORDS_FILE = Path(__file__).resolve().parents[2] / "tmp" / "utterances.jsonl"
RECORDS_FILE_MAX = 5_000_000  # 超过就改名成 .1 重新写

# 统一事件流（elog）：环节名 → 事件的 comp；这几环 blocked 是异常（升为 warn），声纹/唤醒拦下是正常过滤
STAGE_COMP = {"mic": "voice", "vad": "vad", "asr": "asr", "speaker": "speaker", "wake": "wake",
              "gateway": "brain", "tools": "tool", "tts": "tts", "play": "play", "barge": "play"}
WARN_BLOCKED = ("gateway", "tools", "tts", "play")

_REDACT = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.I), r"\1***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{8,}"), "sk-***"),
    (re.compile(r"((?:token|key|secret|password)[\"']?\s*[=:]\s*[\"']?)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1***"),
]


def redact(msg: str) -> str:
    """面板和落盘前去掉形似密钥的片段（守护进程本身不打印密钥，这是兜底）。"""
    for pat, rep in _REDACT:
        msg = pat.sub(rep, msg)
    return msg


def _redact_fields(fields: dict) -> dict:
    """每句话记录里会推给面板、写进 utterances.jsonl 的文字字段（回复、错误、工具命令行）也过一遍脱敏。"""
    for k, v in fields.items():
        if isinstance(v, str):
            fields[k] = redact(v)
        elif isinstance(v, list):
            fields[k] = [redact(x) if isinstance(x, str) else x for x in v]
    return fields


def classify_log(name: str, msg: str) -> str:
    """给一行日志归到面板的模块筛选里。"""
    if name == "httpx":
        if "/health" in msg:
            return "health"
        if ":18791/" in msg:
            return "tts"
        return "brain"
    if name.endswith(".turns"):
        return "tts"
    if name.endswith(".status"):
        return "health"
    if name.endswith((".tray", ".settings", ".panel", ".runtime")):
        return name.rsplit(".", 1)[1]
    m = msg.lstrip()
    for prefix, mod in (("barge-mon", "barge-mon"), ("barge-in", "barge"), ("stop speaking", "barge"),
                        ("play:", "play"), ("first audio", "play"), ("health:", "health"), ("tool ", "tools"),
                        ("reply:", "brain"), ("say", "brain"), ("unspoken", "tts"), ("tts", "tts"),
                        ("gate", "gate"), ("忽略", "gate"), ("拒绝", "gate"), ("唤醒", "gate"), ("丢弃", "asr"),
                        ("[", "asr"), ("runtime", "runtime"), ("settings", "settings"), ("muted", "panel"),
                        ("unmuted", "panel"), ("exit requested", "panel")):
        if m.startswith(prefix):
            return mod
    return "other"


def turn_summary(rec: dict) -> dict:
    """一轮的摘要（统一事件日志的 turn.end 用）：不含 stages / timeline 这两份重复的全量。
    run_id 由网关侧异步补进记录，通常晚于 finish，这里多半没有；uid ↔ run 的关联看 brain.run / gateway.run 事件。"""
    unspoken = rec.get("unspoken") or []
    shaped = sum(1 for x in unspoken if isinstance(x, str) and x.startswith(SHAPED_PREFIX))  # 口语整形主动省略的，不算「漏念」
    return {"source": rec.get("source"), "verdict": rec.get("verdict") or "", "reason": rec.get("reason") or None,
            "text": (rec.get("text") or "")[:60], "reply": (rec.get("reply") or "")[:160],
            "spoken": len(rec.get("spoken") or []), "unspoken": len(unspoken) - shaped, "shaped": shaped or None,
            "tools": [f"{t.get('tool')}:{t.get('decision')}" for t in rec.get("tools") or []] or None,
            "timings": rec.get("timings") or None, "error": rec.get("error") or None, "run_id": rec.get("run_id")}


class EventHub:
    """事件总线 + 每句话记录。所有方法线程安全。"""

    def __init__(self, keep_events: int = 2000, keep_records: int = 50, keep_logs: int = 2000,
                 records_file: Path | None = None, clock=time.time, elog=None) -> None:
        self.elog = elog  # 统一事件日志（guanjia.elog.EventLog）；None 不派生事件。events 不 import elog，避免循环导入
        self._lock = threading.RLock()
        self._seq = itertools.count(1)
        self._events: deque[dict] = deque(maxlen=keep_events)
        self._logs: deque[dict] = deque(maxlen=keep_logs)
        self._records: dict[str, dict] = {}
        self._order: deque[str] = deque()
        self._keep_records = keep_records
        self._subs: list[queue.Queue] = []
        self._ids = itertools.count(1)
        self.records_file = records_file
        self.clock = clock
        self.live: dict[str, dict] = {}  # 不属于某句话的实时环节状态（人声检测、识别中）

    def _e(self, comp: str, ev: str, msg: str = "", **kw) -> None:
        """派生一条统一事件日志；日志出任何问题都不能影响面板和对话。"""
        if self.elog is not None:
            try:
                self.elog.emit(comp, ev, msg, **kw)
            except Exception:  # noqa: BLE001
                pass

    # ---------- 事件 ----------
    def _emit(self, ev: dict) -> dict:
        with self._lock:
            ev = {"seq": next(self._seq), "t": self.clock(), **ev}
            if ev["type"] != "log":
                self._events.append(ev)
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(ev)
            except queue.Full:  # 页面卡住不读：丢事件不阻塞语音
                pass
        return ev

    def subscribe(self, maxsize: int = 2000) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def events_since(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._events[-1]["seq"] if self._events else 0

    def status(self, status: dict) -> None:
        self._emit({"type": "status", "status": status})

    def log(self, level: str, module: str, msg: str) -> None:
        item = {"type": "log", "level": level, "module": module, "msg": redact(msg)}
        ev = self._emit(item)
        with self._lock:
            self._logs.append(ev)

    def logs(self, n: int = 300) -> list[dict]:
        with self._lock:
            return list(self._logs)[-n:]

    # ---------- 每句话 ----------
    def new_utterance(self, source: str, **fields) -> str:
        now = self.clock()
        uid = f"u{datetime.fromtimestamp(now):%H%M%S}-{next(self._ids):03d}"
        rec = {"id": uid, "t0": now, "source": source, "text": "", "score": None, "speaker": "", "verdict": "", "reason": "",
               "sent": "", "stages": {}, "timeline": [], "tools": [], "reply": "", "spoken": [], "unspoken": [],
               "timings": {}, "done": False, **_redact_fields(fields)}
        with self._lock:
            self._records[uid] = rec
            self._order.append(uid)
            while len(self._order) > self._keep_records:
                self._records.pop(self._order.popleft(), None)
        self._emit({"type": "record", "record": self._copy(rec)})
        self._e("voice", "utt.new", (fields.get("text") or "")[:60], uid=uid, source=source, score=fields.get("score"),
                speaker=fields.get("speaker") or None, in_window=fields.get("in_window"))
        return uid

    def stage(self, uid: str | None, stage: str, state: str, note: str = "", **data) -> None:
        """一个环节的状态变化。uid=None 表示不属于某句话（人声检测、识别中这些实时状态）。"""
        now = self.clock()
        item = {"stage": stage, "state": state, "note": redact(note), **data}
        with self._lock:
            if uid is None:
                self.live[stage] = {"state": state, "note": item["note"], "t": now}
            else:
                rec = self._records.get(uid)
                if rec is not None:
                    st = rec["stages"].setdefault(stage, {"t0": now})
                    st.update({"state": state, "note": item["note"], "t1": now})
                    rec["timeline"].append({"t": now, **item})
        self._emit({"type": "stage", "uid": uid, **item})
        if uid is None:  # 实时环节（人声检测中、识别中）很吵，只在 debug 级别落盘
            level = "debug"
        else:
            level = "warn" if state == "blocked" and stage in WARN_BLOCKED else "info"
        self._e(STAGE_COMP.get(stage, stage), f"{stage}.{state}", item["note"], level=level, uid=uid, **data)

    def update(self, uid: str | None, **fields) -> None:
        if uid is None:
            return
        with self._lock:
            rec = self._records.get(uid)
            if rec is None:
                return
            timings = fields.pop("timings", None)
            if timings:
                rec["timings"].update(timings)
            rec.update(_redact_fields(fields))
            snap = self._copy(rec)
        self._emit({"type": "record", "record": snap})

    def add_tool(self, uid: str | None, tool: dict) -> None:
        if uid is None:
            return
        with self._lock:
            rec = self._records.get(uid)
            if rec is None:
                return
            rec["tools"].append(_redact_fields(dict(tool)))
            snap = self._copy(rec)
        self._emit({"type": "record", "record": snap})

    def finish(self, uid: str | None, **fields) -> None:
        """一句话处理完：记最终字段、标记结束、追加一行到 utterances.jsonl。"""
        if uid is None:
            return
        self.update(uid, done=True, **fields)
        rec = self.record(uid)
        if rec is not None and self.records_file is not None:
            self._persist(rec)
        if rec is not None:
            s = turn_summary(rec)
            level = "error" if s["error"] else ("warn" if s["unspoken"] else "info")
            self._e("voice", "turn.end", f"{s['verdict']} 「{s['text'][:24]}」→「{s['reply'][:30]}」", level=level, uid=uid, **s)

    def _persist(self, rec: dict) -> None:
        try:
            self.records_file.parent.mkdir(parents=True, exist_ok=True)
            if self.records_file.exists() and self.records_file.stat().st_size > RECORDS_FILE_MAX:
                self.records_file.replace(self.records_file.with_suffix(".jsonl.1"))
            with self.records_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 落盘失败不影响对话

    def record(self, uid: str) -> dict | None:
        with self._lock:
            rec = self._records.get(uid)
            return self._copy(rec) if rec else None

    def records(self) -> list[dict]:
        with self._lock:
            return [self._copy(self._records[u]) for u in self._order]

    @staticmethod
    def _copy(rec: dict) -> dict:
        return json.loads(json.dumps(rec, ensure_ascii=False))


class HubLogHandler(logging.Handler):
    """把日志行同步推给面板（按模块归类、去掉形似密钥的片段）。"""

    def __init__(self, hub: EventHub) -> None:
        super().__init__(level=logging.DEBUG)
        self.hub = hub
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            self.hub.log(record.levelname, classify_log(record.name, msg), self.format(record))
        except Exception:  # noqa: BLE001
            self.handleError(record)


class NullHub:
    """半构造的守护进程（单测里用 object.__new__ 拼的）没有真正的总线时用它，所有方法都什么也不做。"""

    def new_utterance(self, source: str, **fields) -> None:
        return None

    def __getattr__(self, name):
        return lambda *a, **k: None
