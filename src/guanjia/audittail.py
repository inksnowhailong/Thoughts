"""跟随分级插件的审计文件（workspace\\guanjia\\audit.jsonl），把主线会话的工具调用实时交给面板。

网关的流式回复里没有内部工具的进度，所以从审计里拿：每次工具调用分级插件都写一行
{"ts", "tool", "level", "decision", "sessionKey", "summary", ...}。启动时文件已存在就从末尾开始，只看新写入的行；
启动时还没有这个文件、后来被换成新文件、或被截短，都从新内容的开头读。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("guanjia.audittail")

AUDIT_PATH = Path.home() / ".openclaw" / "workspace" / "guanjia" / "audit.jsonl"
MAIN_SESSION_KEY = "agent:main:main"

DECISION_STATE = {  # 审计 decision → 面板「工具/审批」方框的状态
    "allow": "done", "allow-confirmed": "done", "allow-inner-dedup": "done", "approval-allow-once": "done",
    "approval-requested": "active", "block-need-confirm": "blocked", "block-no-confirmer": "blocked",
    "approval-deny": "blocked", "approval-timeout": "blocked", "approval-cancelled": "blocked",
    "pending.open": "active", "pending.approved": "active", "pending.rejected": "blocked",
    "pending.expired": "blocked", "pending.executed": "done", "allow-approved-latch": "done",
}


# ---- 「待确认」状态：来自分级插件审计里的拦截 / 确认记录 ----
# 打开：工具被拦下、等主人口头同意（v2 的 block-need-confirm；approval-requested 是 v1 遗留，v2 插件不再产生）。
PENDING_OPEN = frozenset({"block-need-confirm", "approval-requested", "pending.open"})
# 了结：主人说了允许 / 别的话、同意后重试放行、或旧审批有了结果。l2-* 这几行没有 tool 字段，tool_entry 看不到，靠 gate_event。
PENDING_CLOSE = frozenset({
    "l2-confirmed", "l2-dropped", "l2-remembered", "l2-remember-refused", "l2-remember-failed", "allow-confirmed",
    "approval-allow-once", "approval-deny", "approval-timeout", "approval-cancelled",
    "pending.approved", "pending.rejected", "pending.expired", "pending.executed", "allow-approved-latch",
})
# 拦截 / 确认类记录的全集：回复里声称「被拦、要审批」时，本轮见过其中任何一条才算有据（block-no-confirmer / block-refused 是真拦，但没人能确认，不进待确认）
GATE_DECISIONS = PENDING_OPEN | PENDING_CLOSE | {"block-no-confirmer", "block-refused"}
PENDING_TTL_SEC = 180.0  # 与插件 guard.ts 的 PENDING_TTL_MS（3 分钟）一致：过了这么久插件那边的待确认也作废了


def gate_event(raw: object) -> dict | None:
    """一行审计 → 拦截 / 确认事件 {ts, decision, summary}；不是主线会话的、或不是 GATE_DECISIONS 里的返回 None。"""
    if not isinstance(raw, dict) or raw.get("kind") not in {"tool", "pending"} or raw.get("sessionKey") != MAIN_SESSION_KEY:
        return None
    decision = str(raw.get("decision", ""))
    if decision not in GATE_DECISIONS:
        return None
    return {"ts": raw.get("ts", ""), "decision": decision, "summary": str(raw.get("summary", ""))[:160]}


class PendingConfirm:
    """主人有没有一件「管家在等你确认」的事：block-need-confirm 打开，确认 / 否认 / 重试放行 / 超时了结。线程安全。"""

    def __init__(self, ttl: float = PENDING_TTL_SEC, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl = ttl
        self.clock = clock
        self._lock = threading.Lock()
        self._summary = ""
        self._since = 0.0

    def feed(self, event: dict) -> str:
        """喂一条 gate_event，返回 "opened"（新出现一件待确认）/ "closed"（刚了结）/ ""（没变化）。"""
        decision = event["decision"]
        with self._lock:
            if decision in PENDING_OPEN:
                self._summary, self._since = event.get("summary") or "有一件事", self.clock()
                return "opened"
            if decision in PENDING_CLOSE and self._active():
                self._summary = ""
                return "closed"
        return ""

    def _active(self) -> bool:
        return bool(self._summary) and self.clock() - self._since < self.ttl

    def current(self) -> str:
        """待确认的那件事的摘要；没有、或已超时返回空串。"""
        with self._lock:
            return self._summary if self._active() else ""


def tool_entry(raw: object) -> dict | None:
    """一行审计 → 面板用的工具记录；不是工具调用、或不是主线会话的返回 None。内层去重那行也跳过（外层已经记过）。"""
    if not isinstance(raw, dict) or raw.get("kind") != "tool" or raw.get("sessionKey") != MAIN_SESSION_KEY \
            or not raw.get("tool"):
        return None
    decision = str(raw.get("decision", ""))
    if decision == "allow-inner-dedup":
        return None
    return {"ts": raw.get("ts", ""), "tool": raw["tool"], "level": raw.get("level", ""), "decision": decision,
            "state": DECISION_STATE.get(decision, "info"), "summary": str(raw.get("summary", ""))[:160]}


class AuditTail:
    """后台线程每 0.5 秒读一次新增行，主线会话的工具调用回调 on_tool(entry)；拦截 / 确认类记录另回调 on_gate(event)。"""

    def __init__(self, on_tool: Callable[[dict], None], path: Path = AUDIT_PATH, interval: float = 0.5,
                 on_gate: Callable[[dict], None] | None = None) -> None:
        self.on_tool = on_tool
        self.on_gate = on_gate
        self.path = path
        self.interval = interval
        self._pos: int | None = None  # None = 还没定位过
        self._ino: int | None = None  # 文件身份：被换成新文件时从头读
        self._buf = b""
        self._stop = threading.Event()

    def poll(self) -> int:
        """读一次新增内容，返回交给回调的条数。第一次调用时文件已存在就只定位到末尾。"""
        try:
            st = self.path.stat()
        except OSError:
            if self._pos is None:
                self._pos = 0  # 启动时还没有文件：以后出现的内容都是新的
            return 0
        if self._pos is None:
            self._pos, self._ino = st.st_size, st.st_ino
            return 0
        if st.st_ino != self._ino or st.st_size < self._pos:  # 换了文件 / 被截短
            self._pos, self._ino, self._buf = 0, st.st_ino, b""
        if st.st_size == self._pos:
            return 0
        try:
            with self.path.open("rb") as f:
                f.seek(self._pos)
                chunk = f.read(st.st_size - self._pos)
        except OSError:
            return 0  # 被占着：下次再读
        self._pos += len(chunk)
        lines = (self._buf + chunk).split(b"\n")
        self._buf = lines.pop()  # 最后一段可能是写了一半的行
        n = 0
        for line in lines:
            try:
                raw = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            gate = gate_event(raw) if self.on_gate else None
            if gate:  # 先于 on_tool：这一行同时是工具记录时，回调里的状态（待确认、本轮拦截）已经是最新的
                try:
                    self.on_gate(gate)
                except Exception:  # noqa: BLE001
                    log.exception("audit tail gate callback error")
            entry = tool_entry(raw)
            if entry:
                n += 1
                try:
                    self.on_tool(entry)
                except Exception:  # noqa: BLE001
                    log.exception("audit tail callback error")
        return n

    def start(self) -> None:
        self.poll()
        threading.Thread(target=self._loop, name="audit-tail", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception:  # noqa: BLE001  跟随出错不能让面板从此看不到工具调用
                log.exception("audit tail poll error")

    def stop(self) -> None:
        self._stop.set()
