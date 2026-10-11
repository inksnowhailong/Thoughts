"""统一事件日志：各模块一句 emit()，后台线程批量追加到 tmp\\logs\\events-<进程>-<日期>.jsonl。

    任意线程 ──emit()──> 内存队列（put_nowait，满了丢弃计数，绝不阻塞语音）
                              │ 后台线程每 0.5 秒批量写
                              ├──> events-voice-2026-10-11.jsonl（按天一个，超 50MB 分片 .1 .2）
                              └──> tap 回调（面板 SSE 用）
    大字段（发给模型的消息、模型原文）用 blob() 单独存，事件里只放相对路径。

一行一个事件：{"ts", "comp", "level", "ev", "uid"?, "msg"?, "data"?}。查询见 guanjia.logq。
保留期（主人 2026-10-11 定）：事件文件 14 天、blob 7 天、blob 总量 300MB。
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
    if isinstance(v, (list, tuple, set, frozenset)):
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
            p.write_bytes(text.encode("utf-8"))  # 不用 write_text：Windows 会把 \n 改成 \r\n
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
                if self._fh is not None:
                    try:
                        self._fh.close()
                    except OSError:
                        pass
                self._fh = None

    def _file_for(self, ts: float):
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        new_day = day != self._day
        if self._fh is not None and not new_day:
            if self._fh.tell() <= PART_MAX_BYTES:
                return self._fh
            part = self._part + 1  # 当天文件超限：开下一个分片
        else:
            part = 0
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        self.dir.mkdir(parents=True, exist_ok=True)
        suffix = f".{part}" if part else ""
        # newline="\n"：Windows 下也写 LF，保持 JSONL 干净
        fh = open(self.dir / f"events-{self.proc}-{day}{suffix}.jsonl", "a", encoding="utf-8", newline="\n")  # noqa: SIM115
        self._day, self._part, self._fh = day, part, fh
        if new_day:
            self._prune(ts)
        return fh

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
